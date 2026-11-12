"""
微信推送模块 (WeChat Notifier 2.0)
支持三种主流通道：
1. PushPlus (微信公众号弹窗，免安装APP，推荐)
2. Server酱 (方糖 Turbo 版)
3. 企业微信群机器人 (WebHook)
"""
import time
import math
import requests
from typing import Dict, Any

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

class WeChatNotifier:
    def __init__(self, provider: str = "pushplus", token: str = ""):
        self.provider = (provider or "pushplus").lower()
        self.token = token.strip() if token else ""

    def send_markdown(self, title: str, text: str) -> bool:
        if not self.token:
            return False

        if self.provider == "pushplus":
            return self._send_pushplus(title, text)
        elif self.provider == "serverchan":
            return self._send_serverchan(title, text)
        elif self.provider == "wecom":
            return self._send_wecom(title, text)
        return False

    def _send_pushplus(self, title: str, text: str) -> bool:
        url = "http://www.pushplus.plus/send"
        payload = {
            "token": self.token,
            "title": title[:50],
            "content": text,
            "template": "markdown"
        }
        try:
            resp = requests.post(url, json=payload, timeout=6)
            if resp.status_code == 200:
                return resp.json().get("code") == 200
            return False
        except Exception:
            return False

    def _send_serverchan(self, title: str, text: str) -> bool:
        url = f"https://sctapi.ftqq.com/{self.token}.send"
        payload = {"title": title[:50], "desp": text}
        try:
            resp = requests.post(url, data=payload, timeout=6)
            if resp.status_code == 200:
                return resp.json().get("code") == 0
            return False
        except Exception:
            return False

    def _send_wecom(self, title: str, text: str) -> bool:
        url = self.token if self.token.startswith("http") else f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={self.token}"
        payload = {
            "msgtype": "markdown",
            "markdown": {"content": f"### {title}\n{text}"}
        }
        try:
            resp = requests.post(url, json=payload, timeout=6)
            if resp.status_code == 200:
                return resp.json().get("errcode") == 0
            return False
        except Exception:
            return False

    def send_trade_signal(self, signal: Dict[str, Any]) -> bool:
        name = signal.get("name", "")
        symbol = signal.get("symbol", "")
        sig_type = signal.get("signal_type", "HOLD")
        desc = signal.get("signal_desc", "")
        price = float(signal.get("price", 0.0))
        score = signal.get("score", 0)
        reasons = signal.get("reasons", [])
        levels = signal.get("trade_levels", {})

        if sig_type in ["FORCE_EXIT", "EOD_FORCE_CLOSE_T"]:
            icon = "🚨"
            title = f"{icon} 尾盘强平警报: {name} ({symbol})" if sig_type == "EOD_FORCE_CLOSE_T" else f"{icon} 强制平仓警报: {name} ({symbol})"
        elif sig_type == "EOD_HEDGE_ALERT_T":
            icon = "⚠️"
            title = f"{icon} 倒T尾盘抉择: {name} ({symbol})"
        elif sig_type == "BUY":
            icon = "🚀"
            title = f"{icon} 多因子研究候选: {name} {desc}"
        elif sig_type == "SELL":
            icon = "⚠️"
            title = f"{icon} 策略退出提醒: {name} {desc}"
        elif sig_type in ["SELL_T", "BUY_T"]:
            icon = "⚡"
            title = f"{icon} 日内做T模型提醒: {name} {desc}"
        elif sig_type == "TAIL_PROFIT_EXIT":
            icon = "🎯"
            title = f"{icon} 尾盘策略行情提醒: {name}"
        elif sig_type == "TAIL_STOP_EXIT":
            icon = "🛑"
            title = f"{icon} 尾盘策略行情提醒: {name}"
        elif sig_type == "TAIL_TIMED_EXIT":
            icon = "⏳"
            title = f"{icon} 尾盘策略时段提醒: {name}"
        elif sig_type == "TAIL_EOD_EXIT":
            icon = "🚨"
            title = f"{icon} 尾盘策略持仓时段提醒: {name}"
        elif sig_type == "LIMIT_UP":
            icon = "⚡"
            title = f"{icon} 游资打板信号: {name} ({signal.get('board_type', '主板涨停')})"
        else:
            icon = "🎯"
            title = f"{icon} 量化信号: {name} {desc}"

        lines = [
            f"**标的**: {name} ({symbol})",
            f"**信号**: {desc} ({sig_type})",
            f"**最新市价**: **{fmt_p(price)} 元**"
        ]

        if score > 0:
            lines.append(f"**多因子评分**: {score}分 | RSRS: {signal.get('rsrs_norm', 0.0)}")
            lines.append(f"**主力筹码**: {signal.get('control_status', '平稳')} (主力成本: {signal.get('main_cost', price):.2f}元)")

        fin = signal.get("financial")
        if fin:
            dp = fin.get("dupont", {})
            gw = fin.get("growth", {})
            rf = fin.get("red_flags", {})
            lines.append(
                f"**📊 财报数据**: **{fmt_metric(fin.get('fundamental_score'), '分')}** | {fin.get('persona') or '画像未知'}\n"
                f"- 杜邦拆解: ROE {fmt_metric(dp.get('roe'), '%')} (净利率 {fmt_metric(dp.get('net_margin'), '%')} × 周转 {fmt_metric(dp.get('asset_turnover'), decimals=2)})\n"
                f"- 核心景气: 扣非YoY **{fmt_metric(gw.get('deduct_profit_yoy'), '%', signed=True)}** | 营收YoY {fmt_metric(gw.get('revenue_yoy'), '%', signed=True)}\n"
                f"- 财务排雷: {rf.get('risk_summary') or '风险状态未知'}"
            )

        if sig_type == "FORCE_EXIT":
            cost = signal.get("holding_cost", 0.0)
            shares = signal.get("holding_shares", 0)
            pnl_pct = signal.get("pnl_pct", 0.0)
            pnl_amt = signal.get("pnl_amt", 0.0)
            lines.extend([
                f"**风险提醒**: 模型风控条件触发。请核对实时行情、实际持仓和可卖数量；系统未提交委托。",
                f"**当前行情参考价**: **{fmt_p(price)} 元**",
                f"**持仓买入成本**: {fmt_p(cost)} 元 (持仓: {shares} 股)",
                f"**当前累计亏损**: **{pnl_pct:+.2f}%** ({pnl_amt:+.2f} 元)",
                f"**说明**: 价格变化不等于账户盈亏；请核对实际成交和交易限制。"
            ])
        elif sig_type == "EOD_FORCE_CLOSE_T":
            entry_p = signal.get("holding_cost", price)
            shares = signal.get("holding_shares", 100)
            pnl_pct = signal.get("pnl_pct", 0.0)
            pnl_amt = signal.get("pnl_amt", 0.0)
            lines.extend([
                f"**尾盘风险提醒**: 请核对日内买入数量、可卖底仓与实际委托；系统未提交委托。",
                f"**当前行情参考价**: **{fmt_p(price)} 元**",
                f"**数量参考**: **{shares} 股** (非成交验证)",
                f"**今早低吸做T价**: {fmt_p(entry_p)} 元 (日内盈亏: {pnl_pct:+.2f}%, {pnl_amt:+.0f}元)",
                f"**说明**: 规则提醒不等于必须交易或已成交，请以实际账户持仓和委托为准。"
            ])
        elif sig_type == "EOD_HEDGE_ALERT_T":
            entry_p = signal.get("holding_cost", price)
            shares = signal.get("holding_shares", 100)
            diff_pct = signal.get("pnl_pct", 0.0)
            diff_amt = signal.get("pnl_amt", 0.0)
            lines.extend([
                f"**⚠️ 尾盘情景提醒**: 模型对冲参考，不构成买卖指令。",
                f"**当前参考市价**: **{fmt_p(price)} 元**",
                f"**早盘高抛卖出价**: {fmt_p(entry_p)} 元 (相对节省: {diff_pct:+.2f}%, {diff_amt:+.0f}元)",
                f"**模型数量参考**: {shares} 股；请核对实际持仓、可用资金与委托状态。"
            ])
        elif sig_type == "BUY":
            lines.extend([
                f"**多因子研究候选**: 当前价 {fmt_p(price)} 元；目标/止损模型参考 {levels.get('target_price', '-')} / {levels.get('initial_stop', '-')} 元",
                "不是买入指令或可成交价格；系统未提供经验证的仓位建议，也未提交委托。"
            ])
        elif sig_type == "SELL":
            cost = signal.get("holding_cost", 0.0)
            lines.extend([
                f"**策略退出提醒**: 当前行情参考价 {fmt_p(price)} 元；不是委托指令。",
                f"**生命线破位参考**: MA20 ({signal.get('ma20', price):.2f}元)"
            ])
            if cost > 0:
                lines.append(f"**对应持仓成本**: {fmt_p(cost)} 元 (持仓: {signal.get('holding_shares', 0)} 股)")
        elif sig_type in ["SELL_T", "BUY_T", "CLOSE_T", "STOP_T"]:
            lines.extend([
                f"**做T行情提醒**: 参考价 {fmt_p(price)} 元；目标/风控线 {levels.get('target_price', '-')} / {levels.get('initial_stop', '-')} 元。",
                "请核对可用底仓、资金、T+1可卖数量与实际委托状态；系统不会下单。"
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
                f"**行情说明**: {action_text}",
                f"**当前行情参考价**: **{fmt_p(price)} 元**",
                f"**记录买入成交价**: {fmt_p(buy_p)} 元 (日期: {buy_d})",
                f"**价格变化**: **{pnl_pct:+.2f}%** (盘中高点变化 {peak_pct:+.2f}%，未扣费用)",
                "请核对实际持仓、可卖数量与委托成交；本提醒不会下单或记录卖出成交。"
            ])

        # 7. Daily-bar limit-up candidate research summary (LIMIT_UP)
        elif sig_type == "LIMIT_UP":
            b_type = signal.get("board_type", "首板突破")
            ladder = signal.get("ladder_count", 1)
            lines.extend([
                f"**板型结构**: **{b_type}** ({ladder}板) | 日线技术规则分: **{signal.get('hardness_score', '不可用')}** (非封板硬度)",
                f"**候选参考价**: **{fmt_p(price)} 元** (不代表可成交价格)",
                "",
                "仅使用日线数据生成的候选信号。系统没有封单队列、真实封板时间、炸板次数、实际成交和涨跌停排队信息。",
                "该信号不是买卖指令；后续行情跟踪不代表本账户成交或策略净收益。"
            ])

        if reasons:
            lines.append(f"**理由**: {'; '.join(reasons)}")

        return self.send_markdown(title=title, text="\n\n".join(lines))
