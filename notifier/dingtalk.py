"""
钉钉机器人 Markdown 卡片推送模块 (DingTalk Notifier 2.0)
支持加签密钥防伪、滑动时间窗口平滑限流(防20条/分超限限流10分钟)、智能熔断与多标的聚合推送
"""
import time
import hmac
import hashlib
import base64
import urllib.parse
import threading
import logging
import math
from collections import deque
from typing import Dict, Any, List, Tuple, Optional
import requests

logger = logging.getLogger("DingTalkNotifier")

def fmt_p(p) -> str:
    try:
        val = float(p)
        return f"{val:.3f}" if val < 10.0 else f"{val:.2f}"
    except (ValueError, TypeError):
        return str(p)


def fmt_metric(value, suffix: str = "", signed: bool = False, decimals: int = 1) -> str:
    try:
        number = float(value)
    except (ValueError, TypeError):
        return "未知"
    if not math.isfinite(number):
        return "未知"
    formatted = f"{number:+.{decimals}f}" if signed else f"{number:.{decimals}f}"
    return f"{formatted}{suffix}"


class DingTalkRateLimiter:
    """
    钉钉自定义机器人 Webhook 级别高精平滑限流与熔断保护器
    钉钉官方严格规则: 每个机器人每分钟最多发送 20 条消息到群里，超过 20 条会被官方强制限流禁言 10 分钟 (errcode 130101)

    核心机制:
    1. 滑动时间窗口: 严格将单 Webhook 在 60 秒内的发送总次数限制在安全阈值 (默认 19 次，保留 1 次防抖安全余量)
    2. 最小请求间隔: 相邻外发请求之间至少保持 1.0 秒间隔，杜绝毫秒级高并发突发 (burst) 触发钉钉反爬与风控
    3. 智能熔断保护: 一旦从响应中捕获 130101 / 88 / 429 频控报错，自动开启 10 分钟熔断冷却，防止盲目持续请求导致封禁期顺延
    4. 全透明运行状态: 实时监控 60 秒已发量、剩余可用额度、熔断倒计时与拦截统计
    """
    MAX_REQUESTS_PER_MINUTE = 19   # 钉钉官方上限为20条/分，设为19条留有安全缓冲
    WINDOW_SECONDS = 60.0          # 60 秒滑动窗口
    MIN_INTERVAL_SECONDS = 1.0     # 连续请求之间至少间隔 1.0 秒防突发
    COOLDOWN_ON_ERROR = 600.0      # 触发限流后的官方惩罚时长 (10分钟/600秒)

    def __init__(self):
        self._lock = threading.Lock()
        # key: normalized webhook token -> deque of timestamps
        self._send_timestamps: Dict[str, deque] = {}
        # key: normalized webhook token -> float (last send timestamp)
        self._last_send_times: Dict[str, float] = {}
        # key: normalized webhook token -> float (cooldown until timestamp)
        self._cooldown_until: Dict[str, float] = {}
        # 统计计数
        self._stats: Dict[str, Dict[str, int]] = {}

    def _normalize_key(self, webhook: str) -> str:
        if not webhook:
            return ""
        if "access_token=" in webhook:
            token = webhook.split("access_token=")[1].split("&")[0]
            return f"token_{token}"
        return webhook.split("?")[0]

    def is_in_cooldown(self, webhook: str) -> Tuple[bool, float]:
        """检查当前 Webhook 是否处于官方 10 分钟限流熔断期中"""
        key = self._normalize_key(webhook)
        with self._lock:
            cd = self._cooldown_until.get(key, 0.0)
            now = time.time()
            if cd > now:
                return True, round(cd - now, 1)
            elif cd > 0:
                # 冷却期已过，自动解除熔断
                del self._cooldown_until[key]
            return False, 0.0

    def trigger_cooldown(self, webhook: str, errmsg: str = "", errcode: int = 130101):
        """记录钉钉返回的限流错误，触发 10 分钟熔断保护"""
        key = self._normalize_key(webhook)
        now = time.time()
        cd_end = now + self.COOLDOWN_ON_ERROR
        with self._lock:
            self._cooldown_until[key] = cd_end
            if key not in self._stats:
                self._stats[key] = {"total_sent": 0, "success_count": 0, "throttled_count": 0, "circuit_breaks": 0}
            self._stats[key]["circuit_breaks"] += 1

        logger.error(
            f"🚨【钉钉频控超限熔断】检测到官方限流惩罚 (errcode={errcode}, errmsg={errmsg})！"
            f"机器人已进入 10 分钟冷却保护期 (至 {time.strftime('%H:%M:%S', time.localtime(cd_end))})，"
            f"期间将自动拦截外发请求以避免惩罚期顺延。"
        )

    def acquire(self, webhook: str, wait: bool = True, max_wait: float = 3.0) -> Tuple[bool, float, str]:
        """
        申请发送令牌 (Token Bucket / Sliding Window)
        返回: (allowed: bool, wait_time: float, reason: str)
        """
        key = self._normalize_key(webhook)
        if not key:
            return False, 0.0, "Webhook为空"

        while True:
            with self._lock:
                now = time.time()

                # 1. 检查熔断状态
                cd_end = self._cooldown_until.get(key, 0.0)
                if cd_end > now:
                    remain = cd_end - now
                    return False, remain, f"处于10分钟限流熔断期中 (剩余{remain:.1f}秒)"

                # 2. 清理滑动窗口中超过 60 秒的陈旧时间戳
                if key not in self._send_timestamps:
                    self._send_timestamps[key] = deque(maxlen=self.MAX_REQUESTS_PER_MINUTE * 2)
                if key not in self._stats:
                    self._stats[key] = {"total_sent": 0, "success_count": 0, "throttled_count": 0, "circuit_breaks": 0}

                q = self._send_timestamps[key]
                cutoff = now - self.WINDOW_SECONDS
                while q and q[0] <= cutoff:
                    q.popleft()

                # 3. 检查最小发送间隔 (防毫秒级突发 burst)
                last_time = self._last_send_times.get(key, 0.0)
                interval_needed = 0.0
                if now - last_time < self.MIN_INTERVAL_SECONDS:
                    interval_needed = self.MIN_INTERVAL_SECONDS - (now - last_time)

                # 4. 检查 60 秒窗口内是否超限 (19条)
                window_wait = 0.0
                if len(q) >= self.MAX_REQUESTS_PER_MINUTE:
                    oldest = q[0]
                    window_wait = (oldest + self.WINDOW_SECONDS) - now + 0.1

                needed_wait = max(interval_needed, window_wait)

                if needed_wait <= 0.001:
                    # 允许发送，记录时间戳
                    send_time = time.time()
                    q.append(send_time)
                    self._last_send_times[key] = send_time
                    self._stats[key]["total_sent"] += 1
                    return True, 0.0, "OK"

                # 需等待，判断是否允许等待
                if not wait:
                    self._stats[key]["throttled_count"] += 1
                    return False, needed_wait, f"频控保护拦截: 60秒内已发{len(q)}条(上限{self.MAX_REQUESTS_PER_MINUTE})，需等待{needed_wait:.1f}秒"

                if needed_wait > max_wait:
                    self._stats[key]["throttled_count"] += 1
                    return False, needed_wait, f"频控保护拦截: 需等待{needed_wait:.1f}秒超过上限({max_wait}秒)"

            # 在锁外进行平滑休眠
            time.sleep(needed_wait)

    def record_success(self, webhook: str):
        """记录成功推送"""
        key = self._normalize_key(webhook)
        with self._lock:
            if key in self._stats:
                self._stats[key]["success_count"] += 1

    def get_status(self, webhook: str) -> Dict[str, Any]:
        """获取指定 Webhook 当前的频控状态与配额诊断数据"""
        key = self._normalize_key(webhook)
        with self._lock:
            now = time.time()
            cd_end = self._cooldown_until.get(key, 0.0)
            in_cd = cd_end > now
            cd_remain = max(0.0, cd_end - now) if in_cd else 0.0

            q = self._send_timestamps.get(key, deque())
            cutoff = now - self.WINDOW_SECONDS
            active_count = sum(1 for t in q if t > cutoff)
            remaining_quota = max(0, self.MAX_REQUESTS_PER_MINUTE - active_count)
            stats = self._stats.get(key, {"total_sent": 0, "success_count": 0, "throttled_count": 0, "circuit_breaks": 0})

            return {
                "key": key,
                "in_cooldown": in_cd,
                "cooldown_remaining_seconds": round(cd_remain, 1),
                "current_window_count": active_count,
                "remaining_quota": remaining_quota,
                "max_per_minute": self.MAX_REQUESTS_PER_MINUTE,
                "min_interval_seconds": self.MIN_INTERVAL_SECONDS,
                "total_sent": stats.get("total_sent", 0),
                "success_count": stats.get("success_count", 0),
                "throttled_count": stats.get("throttled_count", 0),
                "circuit_breaks": stats.get("circuit_breaks", 0)
            }

    def reset(self):
        """重置所有限流与熔断状态（用于单元测试）"""
        with self._lock:
            self._send_timestamps.clear()
            self._last_send_times.clear()
            self._cooldown_until.clear()
            self._stats.clear()


# 全局共享单例限流器
dingtalk_rate_limiter = DingTalkRateLimiter()


def format_batch_signals_markdown(signals: List[Dict[str, Any]], group_name: str = "") -> Tuple[str, str]:
    """将多个交易信号格式化为单条聚合 Markdown 卡片内容"""
    gtag = f"【{group_name}】" if group_name else "【量化2.0】"
    title = f"⚡ {gtag}多标的规则信号摘要 ({len(signals)} 只)"
    blocks = [
        f"### ⚡ {gtag}量化规则信号摘要 ({len(signals)} 只标的)",
        f"> 以下标的触发规则筛选条件。信号不代表实际成交、交易指令或可执行委托：",
        "---"
    ]

    for idx, signal in enumerate(signals, 1):
        name = signal.get("name", "")
        symbol = signal.get("symbol", "")
        sig_type = signal.get("signal_type", "HOLD")
        desc = signal.get("signal_desc", "")
        price = float(signal.get("price", 0.0))
        levels = signal.get("trade_levels", {})
        cost = float(signal.get("holding_cost", 0.0))
        pnl_pct = signal.get("pnl_pct", 0.0)
        s_group = signal.get("group_name", "")
        sig_gtag = f"【{s_group}】" if s_group and s_group != group_name else ""

        if sig_type in ["FORCE_EXIT", "EOD_FORCE_CLOSE_T"]:
            icon = "🚨"
        elif sig_type == "EOD_HEDGE_ALERT_T":
            icon = "⚠️"
        elif sig_type == "BUY":
            icon = "🚀"
        elif sig_type == "SELL":
            icon = "⚠️"
        elif sig_type in ["BUY_T", "SELL_T"]:
            icon = "🌟" if sig_type == "BUY_T" else "⚡"
        elif sig_type in ["CLOSE_T", "STOP_T"]:
            icon = "🎯" if sig_type == "CLOSE_T" else "🛑"
        elif sig_type in ["TAIL_PROFIT_EXIT", "TAIL_STOP_EXIT", "TAIL_TIMED_EXIT", "TAIL_EOD_EXIT"]:
            icon = "🎯" if "PROFIT" in sig_type else ("🛑" if "STOP" in sig_type else "⏳")
        elif sig_type == "LIMIT_UP":
            icon = "⚡"
        else:
            icon = "🔔"

        item_lines = [
            f"#### {idx}. {icon} {sig_gtag}{name} (`{symbol}`) · {desc}",
            f"- **当前市价**: **{fmt_p(price)} 元**"
        ]

        if sig_type == "FORCE_EXIT":
            item_lines.append(f"- **风险提醒**: 触发模型风控条件；请核对实时行情、实际持仓和可卖数量。系统未提交委托。 (成本: {fmt_p(cost)}元, 价格变化: {pnl_pct:+.2f}%)")
        elif sig_type == "BUY":
            item_lines.append(f"- **多因子研究候选**: 当前价 {price:.2f}元；模型目标/止损参考 {levels.get('target_price', '-')}元 / {levels.get('initial_stop', '-')}元。不是买入指令或可成交价格；系统未提交委托。")
        elif sig_type == "SELL":
            item_lines.append(f"- **策略退出提醒**: 参考价 {fmt_p(price)}元。请结合实际持仓和可成交价格判断；不是委托指令。")
        elif sig_type in ["BUY_T", "SELL_T", "CLOSE_T", "STOP_T"]:
            item_lines.append(f"- **做T模型提醒**: 参考价 {fmt_p(price)}元，模型目标/风控线 {levels.get('target_price', '-')}元 / {levels.get('initial_stop', '-')}元。请核对可用底仓、T+1可卖数量与成交状态；系统未下单。")
        elif sig_type == "EOD_FORCE_CLOSE_T":
            item_lines.append("- **尾盘风险提醒**: 请核对当日买入数量、可卖底仓和实际委托；模型提醒不等于必须交易或已成交。")
        elif sig_type == "EOD_HEDGE_ALERT_T":
            item_lines.append("- **尾盘情景提醒**: 模型列出对冲情景供核对；需结合真实持仓和交易限制判断，不构成买卖指令。")
        elif sig_type in ["TAIL_PROFIT_EXIT", "TAIL_STOP_EXIT", "TAIL_TIMED_EXIT", "TAIL_EOD_EXIT"]:
            item_lines.append(f"- **尾盘策略行情提醒**: 当前价 {fmt_p(price)}元，价格变化 {pnl_pct:+.2f}%（未扣费用）。提醒不代表委托或成交，请核对实际持仓。")
        elif sig_type == "LIMIT_UP":
            item_lines.append(f"- **日线涨停候选**: 参考价 {fmt_p(price)}元 | 日线规则分 {signal.get('hardness_score', '不可用')} | 不是交易指令或盘口强度判断。")

        reasons = signal.get("reasons", [])
        if reasons:
            item_lines.append(f"- **量化归因**: {reasons[0]}")

        blocks.append("\n".join(item_lines))
        blocks.append("---")

    blocks.append(f"\n> QuantitativeTrading 2.0 · 聚合推送防限流引擎 · {time.strftime('%Y-%m-%d %H:%M:%S')}")
    return title, "\n".join(blocks)


class DingTalkNotifier:
    def __init__(self, webhook: str, secret: str = ""):
        self.webhook = webhook.strip() if webhook else ""
        self.secret = secret.strip() if secret else ""

    def _get_signed_url(self) -> str:
        if not self.secret:
            return self.webhook
        timestamp = str(round(time.time() * 1000))
        secret_enc = self.secret.encode('utf-8')
        string_to_sign = f'{timestamp}\n{self.secret}'
        string_to_sign_enc = string_to_sign.encode('utf-8')
        hmac_code = hmac.new(secret_enc, string_to_sign_enc, digestmod=hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        sep = "&" if "?" in self.webhook else "?"
        return f"{self.webhook}{sep}timestamp={timestamp}&sign={sign}"

    def send_markdown(self, title: str, text: str, wait: bool = True, max_wait: float = 3.0, rate_limit: bool = True) -> bool:
        if not self.webhook:
            return False

        # 1. 频控检查与滑动时间窗口令牌申请
        if rate_limit:
            allowed, wait_sec, reason = dingtalk_rate_limiter.acquire(
                self.webhook, wait=wait, max_wait=max_wait
            )
            if not allowed:
                logger.warning(f"⚠️ [DingTalk 频控保护拦截] 消息未外发以防封禁: {reason} | 标题: {title}")
                print(f"[DingTalk 频控保护拦截] 消息未外发以防封禁: {reason} | 标题: {title}")
                return False

        url = self._get_signed_url()
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": title,
                "text": text
            }
        }
        try:
            resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=5)
            if resp.status_code == 200:
                res = resp.json()
                errcode = res.get("errcode", -1)
                errmsg = res.get("errmsg", "")
                if errcode == 0:
                    dingtalk_rate_limiter.record_success(self.webhook)
                    return True

                # 捕获钉钉官方 20条/分 频控报错，触发 10 分钟熔断保护
                if errcode in [130101, 88] or "freq limit" in str(errmsg).lower() or "too many" in str(errmsg).lower():
                    dingtalk_rate_limiter.trigger_cooldown(self.webhook, errmsg=errmsg, errcode=errcode)
                    print(f"🚨 [DingTalk 频控超限] 触发官方每分钟20条上限 (errcode: {errcode}, errmsg: {errmsg})！已自动启动10分钟熔断。")
                    return False
                else:
                    print(f"钉钉推送失败: errcode={errcode}, errmsg={errmsg}")
                    return False
            elif resp.status_code == 429:
                dingtalk_rate_limiter.trigger_cooldown(self.webhook, errmsg="HTTP 429 Too Many Requests", errcode=429)
                print("🚨 [DingTalk 429] 触发HTTP 429限流，已启动10分钟熔断。")
                return False
            return False
        except Exception as e:
            print(f"钉钉推送失败: {e}")
            return False

    def send_batch_signals(self, signals: List[Dict[str, Any]], group_name: str = "", wait: bool = True, max_wait: float = 5.0) -> bool:
        """
        将多条交易信号智能聚合为一张 Markdown 卡片集中推送
        从根本上解决同一分钟内多个标的信号密集触发导致的群刷屏与钉钉 20条/分 频控超限问题
        """
        if not signals:
            return False
        if len(signals) == 1:
            sig = signals[0].copy()
            if group_name and not sig.get("group_name"):
                sig["group_name"] = group_name
            return self.send_trade_signal(sig, wait=wait, max_wait=max_wait)

        title, text = format_batch_signals_markdown(signals, group_name=group_name)
        return self.send_markdown(title=title, text=text, wait=wait, max_wait=max_wait)

    def send_trade_signal(self, signal: Dict[str, Any], wait: bool = True, max_wait: float = 3.0) -> bool:
        """格式化发送多因子量化交易信号卡片（包含明确的买入/卖出价格、强制平仓警报与做T指导）"""
        name = signal.get("name", "")
        symbol = signal.get("symbol", "")
        sig_type = signal.get("signal_type", "HOLD")
        desc = signal.get("signal_desc", "")
        price = float(signal.get("price", 0.0))
        score = signal.get("score", 0)
        reasons = signal.get("reasons", [])
        levels = signal.get("trade_levels", {})
        advice = signal.get("position_advice", {})
        group_name = signal.get("group_name", "")
        group_tag = f"【{group_name}】" if group_name else ""

        # 图标与标题定调
        if sig_type in ["FORCE_EXIT", "EOD_FORCE_CLOSE_T"]:
            icon = "🚨"
            title = f"{icon} {group_tag}尾盘强平警报: {name} ({symbol})" if sig_type == "EOD_FORCE_CLOSE_T" else f"{icon} {group_tag}强制平仓警报: {name} ({symbol})"
        elif sig_type == "EOD_HEDGE_ALERT_T":
            icon = "⚠️"
            title = f"{icon} {group_tag}倒T尾盘抉择: {name} ({symbol})"
        elif sig_type == "BUY":
            icon = "🚀"
            title = f"{icon} {group_tag}波段买入信号: {name} ({desc})"
        elif sig_type == "SELL":
            icon = "⚠️"
            title = f"{icon} {group_tag}减仓清仓预警: {name} ({desc})"
        elif sig_type in ["SELL_T", "BUY_T"]:
            icon = "⚡" if sig_type == "SELL_T" else "🌟"
            title = f"{icon} {group_tag}日内做T机会: {name} ({desc})"
        elif sig_type in ["CLOSE_T", "STOP_T"]:
            icon = "🎯" if sig_type == "CLOSE_T" else "🛑"
            title = f"{icon} {group_tag}做T对冲平仓: {name} ({desc})"
        elif sig_type == "TAIL_PROFIT_EXIT":
            icon = "🎯"
            title = f"{icon} 2.30战法次日冲高止盈: {name} (+{signal.get('pnl_pct', 0.0):.2f}%)"
        elif sig_type == "TAIL_STOP_EXIT":
            icon = "🛑"
            title = f"{icon} 2.30战法次日破位止损: {name} ({signal.get('pnl_pct', 0.0):.2f}%)"
        elif sig_type == "TAIL_TIMED_EXIT":
            icon = "⏳"
            title = f"{icon} 2.30战法早盘动能衰竭: {name} ({signal.get('pnl_pct', 0.0):+.2f}%)"
        elif sig_type == "TAIL_EOD_EXIT":
            icon = "🚨"
            title = f"{icon} 2.30战法次日尾盘行情提醒: {name}"
        elif sig_type == "LIMIT_UP":
            icon = "⚡"
            title = f"{icon} 游资龙虎打板信号: {name} ({signal.get('board_type', '主板涨停')})"
        else:
            icon = "🔔"
            title = f"{icon} {group_tag}量化信号: {name} ({desc})"

        lines = [
            f"### {icon} {f'【量化2.0 · {group_name}】' if group_name else '【量化2.0】'}{name} ({symbol})",
            f"- **信号类别**: **{desc}** ({sig_type})",
            *( [f"- **所属做T分组**: **{group_name}**"] if group_name else [] ),
            f"- **当前最新市价**: **{fmt_p(price)} 元**"
        ]

        if score > 0:
            lines.append(f"- **多因子评分**: **{score} 分** | RSRS 2.0: {signal.get('rsrs_norm', 0.0)}")
            lines.append(f"- **主力筹码**: {signal.get('control_status', '平稳')} (主力成本: {signal.get('main_cost', price):.2f}元)")

        fin = signal.get("financial")
        if fin:
            dp = fin.get("dupont", {})
            gw = fin.get("growth", {})
            rf = fin.get("red_flags", {})
            lines.append(
                f"- **📊 财报数据**: **{fmt_metric(fin.get('fundamental_score'), '分')}** | {fin.get('persona') or '画像未知'}\n"
                f"  - 杜邦拆解: ROE {fmt_metric(dp.get('roe'), '%')} (净利率 {fmt_metric(dp.get('net_margin'), '%')} × 周转 {fmt_metric(dp.get('asset_turnover'), decimals=2)}) · {dp.get('driver_type') or '未知'}\n"
                f"  - 核心景气: 扣非YoY **{fmt_metric(gw.get('deduct_profit_yoy'), '%', signed=True)}** | 营收YoY {fmt_metric(gw.get('revenue_yoy'), '%', signed=True)} | 毛利率 {fmt_metric(gw.get('gross_margin'), '%')}\n"
                f"  - 财务排雷: {rf.get('risk_summary') or '风险状态未知'}"
            )

        lines.append("---")

        # 1. 强制平仓/硬止损警报 (FORCE_EXIT)
        if sig_type == "FORCE_EXIT":
            cost = signal.get("holding_cost", 0.0)
            shares = signal.get("holding_shares", 0)
            pnl_pct = signal.get("pnl_pct", 0.0)
            pnl_amt = signal.get("pnl_amt", 0.0)
            stop_line = levels.get("initial_stop", cost * 0.92)

            lines.extend([
                f"- **风险提醒**: 模型风控条件触发；请核对实时行情、实际持仓和可卖数量。系统未提交委托。",
                f"- **当前行情参考价**: **{fmt_p(price)} 元**",
                f"- **持仓买入成本**: **{fmt_p(cost)} 元** (持仓: {shares} 股)",
                f"- **当前累计亏损**: <font color=\"#10b981\">**{pnl_pct:+.2f}%** ({pnl_amt:+.2f} 元)</font>",
                f"- **账户止损红线**: **{stop_line:.2f} 元** (已严重击穿)",
                f"- **说明**: 价格变化不等于账户盈亏；请核对实际成交和交易限制。"
            ])

        # 2. 波段买入信号 (BUY)
        elif sig_type == "BUY":
            lines.extend([
                f"- **多因子研究候选**: 当前价 {fmt_p(price)}元；目标/止损模型参考 {levels.get('target_price', '-')}元 / {levels.get('initial_stop', '-')}元。",
                "- 未提供经验证的仓位建议；上述参考线不是可成交价格或收益预测，系统未提交委托。"
            ])

        # 3. 波段减仓/清仓信号 (SELL)
        elif sig_type == "SELL":
            cost = signal.get("holding_cost", 0.0)
            shares = signal.get("holding_shares", 0)
            pnl_pct = signal.get("pnl_pct")

            lines.extend([
                f"- **策略退出提醒**: 当前行情参考价 {fmt_p(price)} 元；不是委托指令。",
                f"- **生命线破位参考**: MA20 ({signal.get('ma20', price):.2f}元)"
            ])
            if cost > 0:
                pnl_str = f"{pnl_pct:+.2f}%" if pnl_pct is not None else f"{(price - cost)/cost*100:+.2f}%"
                lines.append(f"- **对应持仓成本**: {fmt_p(cost)} 元 (持仓: {shares} 股，当前浮动: {pnl_str})")

        # 4. 日内做T信号 (BUY_T / SELL_T / CLOSE_T / STOP_T)
        elif sig_type in ["BUY_T", "SELL_T", "CLOSE_T", "STOP_T"]:
            t0_strength = signal.get("t0_score") or signal.get("t0_strength")
            if t0_strength:
                lines.append(f"- **做T信号强度**: **{int(t0_strength)} 分** (规则启发式，非校准概率)")
            if sig_type == "SELL_T":
                lines.extend([
                    f"- **倒T模型观察**: 参考价 {fmt_p(price)} 元；目标/风控线 {levels.get('target_price')} / {levels.get('initial_stop')} 元。请核对可卖底仓；系统未下单。"
                ])
            elif sig_type == "BUY_T":
                lines.extend([
                    f"- **正T模型观察**: 参考价 {fmt_p(price)} 元；目标/风控线 {levels.get('target_price')} / {levels.get('initial_stop')} 元。请核对可用资金与T+1限制；系统未下单。"
                ])
            elif sig_type == "CLOSE_T":
                t0_dir = signal.get("t0_direction", "")
                close_act = signal.get("close_action", "")
                shares = signal.get("holding_shares") or signal.get("shares", 100)
                if t0_dir == "SELL_T" or close_act == "BUY_BACK":
                    lines.extend([
                        f"- **倒T行情提醒**: 回补观察价 {fmt_p(price)} 元，模型数量参考 {shares} 股；请核对实际仓位与委托，系统未下单。"
                    ])
                else:
                    lines.extend([
                        f"- **正T行情提醒**: 卖出观察价 {fmt_p(price)} 元，模型数量参考 {shares} 股；请核对实际可卖数量与委托，系统未下单。"
                    ])
            elif sig_type == "STOP_T":
                t0_dir = signal.get("t0_direction", "")
                close_act = signal.get("close_action", "")
                shares = signal.get("holding_shares") or signal.get("shares", 100)
                if t0_dir == "SELL_T" or "BUY_BACK" in close_act:
                    lines.extend([
                        f"- **倒T风险提醒**: 行情触及模型防守线，参考价 {fmt_p(price)} 元，数量参考 {shares} 股；请核对实际持仓和委托。"
                    ])
                else:
                    lines.extend([
                        f"- **正T风险提醒**: 行情触及模型防守线，参考价 {fmt_p(price)} 元，数量参考 {shares} 股；请核对T+1可卖数量和委托。"
                    ])

        # 5. 尾盘未对冲做T强制平仓与对冲抉择提醒
        elif sig_type == "EOD_FORCE_CLOSE_T":
            entry_p = signal.get("holding_cost", price)
            shares = signal.get("holding_shares", 100)
            pnl_pct = signal.get("pnl_pct", 0.0)
            pnl_amt = signal.get("pnl_amt", 0.0)
            lines.extend([
                f"- **尾盘风险提醒**: 请核对日内买入数量、可卖底仓与实际委托；系统未提交委托。",
                f"- **当前行情参考价**: **{fmt_p(price)} 元**",
                f"- **数量参考**: **{shares} 股** (非成交验证)",
                f"- **今早低吸做T价**: {fmt_p(entry_p)} 元 (日内盈亏: {pnl_pct:+.2f}%, {pnl_amt:+.0f}元)",
                f"- **说明**: 规则提示不等于必须交易或已成交，请以实际账户持仓和委托为准。"
            ])
        elif sig_type == "EOD_HEDGE_ALERT_T":
            entry_p = signal.get("holding_cost", price)
            shares = signal.get("holding_shares", 100)
            diff_pct = signal.get("pnl_pct", 0.0)
            diff_amt = signal.get("pnl_amt", 0.0)
            lines.extend([
                f"- **⚠️ 尾盘情景提醒**: 根据行情估算的对冲参考，不构成买卖指令。",
                f"- **当前参考市价**: **{fmt_p(price)} 元**",
                f"- **早盘高抛卖出价**: {fmt_p(entry_p)} 元 (相对节省: {diff_pct:+.2f}%, {diff_amt:+.0f}元)",
                f"- **模型数量参考**: {shares} 股；请核对实际持仓、可用资金与委托状态。"
            ])

        # 6. 2.30 尾盘突袭战法次日卖出与平仓指令
        elif sig_type in ["TAIL_PROFIT_EXIT", "TAIL_STOP_EXIT", "TAIL_TIMED_EXIT", "TAIL_EOD_EXIT"]:
            buy_p = signal.get("buy_price", price)
            target_p = signal.get("target_sell_price", levels.get("target_price", price))
            stop_p = signal.get("stop_loss_price", levels.get("initial_stop", price))
            pnl_pct = signal.get("pnl_pct", 0.0)
            peak_pct = signal.get("peak_gain_pct", pnl_pct)
            buy_d = signal.get("buy_date", "")

            if sig_type == "TAIL_PROFIT_EXIT":
                action_text = f"行情触及模型止盈观察线 {fmt_p(target_p)} 元"
            elif sig_type == "TAIL_STOP_EXIT":
                action_text = f"行情触及模型止损观察线 {fmt_p(stop_p)} 元"
            elif sig_type == "TAIL_TIMED_EXIT":
                action_text = "时段规则提醒；不代表卖出指令"
            else:
                action_text = "模型观察期限提醒；不代表卖出指令"

            lines.extend([
                f"- **行情说明**: {action_text}",
                f"- **当前行情参考价**: **{fmt_p(price)} 元**",
                f"- **记录买入成交价**: {fmt_p(buy_p)} 元 (日期: {buy_d})",
                f"- **价格变化**: **{pnl_pct:+.2f}%** (盘中高点变化 {peak_pct:+.2f}%，未扣费用)",
                "- 请核对实际持仓、可卖数量与委托成交；本提醒不会下单或记录卖出成交。"
            ])

        # 7. Daily-bar limit-up candidate research summary (LIMIT_UP)
        elif sig_type == "LIMIT_UP":
            b_type = signal.get("board_type", "首板突破")
            ladder = signal.get("ladder_count", 1)
            lines.extend([
                f"- **🎯 板型结构**: **{b_type}** (当前连板数: **{ladder}板**)",
                f"- **日线技术规则分**: **{signal.get('hardness_score', '不可用')} 分** (非封板硬度或盘口数据)",
                f"- **候选参考价**: **{fmt_p(price)} 元** (不代表可成交价格)",
                "",
                "仅使用日线数据生成的候选信号。系统没有封单队列、真实封板时间、炸板次数、实际成交和涨跌停排队信息。",
                "该信号不是买卖指令；历史后续行情跟踪不代表本账户成交或策略净收益。请自行核对交易规则和风险。"
            ])

        if reasons:
            lines.append("---")
            lines.append(f"- **量化归因**: {'; '.join(reasons)}")

        lines.append(f"\n> QuantitativeTrading 2.0 · {time.strftime('%Y-%m-%d %H:%M:%S')}")

        return self.send_markdown(title=title, text="\n".join(lines), wait=wait, max_wait=max_wait)
