"""
微信推送模块 (WeChat Notifier 2.0)
支持三种主流通道：
1. PushPlus (微信公众号弹窗，免安装APP，推荐)
2. Server酱 (方糖 Turbo 版)
3. 企业微信群机器人 (WebHook)
"""
import time
import requests
from typing import Dict, Any

def fmt_p(p) -> str:
    try:
        val = float(p)
        return f"{val:.3f}" if val < 10.0 else f"{val:.2f}"
    except (ValueError, TypeError):
        return str(p)

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
            title = f"{icon} 波段买入: {name} {desc}"
        elif sig_type == "SELL":
            icon = "⚠️"
            title = f"{icon} 减仓清仓预警: {name} {desc}"
        elif sig_type in ["SELL_T", "BUY_T"]:
            icon = "⚡"
            title = f"{icon} 日内做T机会: {name} {desc}"
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
            title = f"{icon} 2.30战法次日尾盘坚决清仓: {name}"
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

        if sig_type == "FORCE_EXIT":
            cost = signal.get("holding_cost", 0.0)
            shares = signal.get("holding_shares", 0)
            pnl_pct = signal.get("pnl_pct", 0.0)
            pnl_amt = signal.get("pnl_amt", 0.0)
            lines.extend([
                f"**🚨 操盘指令**: **建议立即以市价挂单【全部清仓卖出】**",
                f"**建议卖出执行价**: **{fmt_p(price)} 元**",
                f"**持仓买入成本**: {fmt_p(cost)} 元 (持仓: {shares} 股)",
                f"**当前累计亏损**: **{pnl_pct:+.2f}%** ({pnl_amt:+.2f} 元)",
                f"**风控纪律**: **已触及账户硬止损保护线，严禁侥幸扛单，务必果断平仓保留本金！**"
            ])
        elif sig_type == "EOD_FORCE_CLOSE_T":
            entry_p = signal.get("holding_cost", price)
            shares = signal.get("holding_shares", 100)
            pnl_pct = signal.get("pnl_pct", 0.0)
            pnl_amt = signal.get("pnl_amt", 0.0)
            lines.extend([
                f"**🚨 操盘指令**: **【必须在 14:55 前以市价卖出老底仓】**",
                f"**建议卖出执行价**: **{fmt_p(price)} 元**",
                f"**建议卖出股数**: **{shares} 股 (对冲今早买入股份)**",
                f"**今早低吸做T价**: {fmt_p(entry_p)} 元 (日内盈亏: {pnl_pct:+.2f}%, {pnl_amt:+.0f}元)",
                f"**风控纪律**: **严禁日内正T演变为被动加仓过夜！14:55 前必须卖出老底仓恢复原有仓位！**"
            ])
        elif sig_type == "EOD_HEDGE_ALERT_T":
            entry_p = signal.get("holding_cost", price)
            shares = signal.get("holding_shares", 100)
            diff_pct = signal.get("pnl_pct", 0.0)
            diff_amt = signal.get("pnl_amt", 0.0)
            lines.extend([
                f"**⚠️ 操盘建议**: **【尾盘自主抉择：买回底仓 vs 顺势保留现金】**",
                f"**当前参考市价**: **{fmt_p(price)} 元**",
                f"**早盘高抛卖出价**: {fmt_p(entry_p)} 元 (相对节省: {diff_pct:+.2f}%, {diff_amt:+.0f}元)",
                f"**方案 A (维持底仓)**: 如需维持原股数，请在 14:55 前以市价买回 {shares} 股",
                f"**方案 B (顺势防守)**: 如大盘偏弱或走熊，可顺势保留现金过夜，明日更低位再接回"
            ])
        elif sig_type == "BUY":
            lines.extend([
                f"**建议买入区间**: {price:.2f} ~ {price * 1.01:.2f} 元",
                f"**防守止损价**: **{levels.get('initial_stop')} 元** (-{levels.get('stop_pct')}%)",
                f"**目标止盈价**: **{levels.get('target_price')} 元** (+{levels.get('target_pct')}%)",
                f"**保本触发线**: {levels.get('breakeven_trigger')} 元"
            ])
        elif sig_type == "SELL":
            cost = signal.get("holding_cost", 0.0)
            lines.extend([
                f"**建议卖出执行价**: **{fmt_p(price)} 元** (逢盘中反抽果断挂单卖出)",
                f"**建议减仓比例**: **建议减仓 50% ~ 100% (波段破位清仓)**",
                f"**生命线破位参考**: MA20 ({signal.get('ma20', price):.2f}元)"
            ])
            if cost > 0:
                lines.append(f"**对应持仓成本**: {fmt_p(cost)} 元 (持仓: {signal.get('holding_shares', 0)} 股)")
        elif sig_type in ["SELL_T", "BUY_T", "CLOSE_T", "STOP_T"]:
            if sig_type == "SELL_T":
                lines.extend([
                    f"**做T指令**: 【倒T高抛】先卖出已有底仓",
                    f"**建议卖出高抛价**: **{fmt_p(price)} 元**",
                    f"**日内回补买回目标价**: {levels.get('target_price')} 元"
                ])
            elif sig_type == "BUY_T":
                lines.extend([
                    f"**做T指令**: 【正T低吸】逢低买入底仓做T",
                    f"**建议买入低吸价**: **{fmt_p(price)} 元**",
                    f"**日内冲高卖出目标价**: {levels.get('target_price')} 元"
                ])
            elif sig_type == "CLOSE_T":
                t0_dir = signal.get("t0_direction", "")
                close_act = signal.get("close_action", "")
                shares = signal.get("holding_shares") or signal.get("shares", 100)
                if t0_dir == "SELL_T" or close_act == "BUY_BACK":
                    lines.extend([
                        f"**做T指令**: **【🎯 倒T平仓买入回补】请立即买回等量底仓！**",
                        f"**建议回补买入价**: **{fmt_p(price)} 元**",
                        f"**建议买回股数**: **{shares} 股**",
                        f"**战果**: 价格已回踩达成预期差价，请立即买回等量底仓，收复筹码并锁定日内差价！"
                    ])
                else:
                    lines.extend([
                        f"**做T指令**: **【🎯 正T冲高卖出平仓】请立即高抛卖出等量底仓！**",
                        f"**建议平仓卖出价**: **{fmt_p(price)} 元**",
                        f"**建议卖出股数**: **{shares} 股**",
                        f"**战果**: 冲高达成预期对冲目标，请立即卖出等量底仓锁定做T利润，保持总持仓不变！"
                    ])
            elif sig_type == "STOP_T":
                t0_dir = signal.get("t0_direction", "")
                close_act = signal.get("close_action", "")
                shares = signal.get("holding_shares") or signal.get("shares", 100)
                if t0_dir == "SELL_T" or "BUY_BACK" in close_act:
                    lines.extend([
                        f"**做T指令**: **【🛑 倒T紧急买回止损】防止底仓抛飞**",
                        f"**紧急买回价**: **{fmt_p(price)} 元**",
                        f"**买回归位股数**: **{shares} 股**"
                    ])
                else:
                    lines.extend([
                        f"**做T指令**: **【🛑 正T防守卖出止损】平仓避险**",
                        f"**止损卖出价**: **{fmt_p(price)} 元**",
                        f"**止损卖出股数**: **{shares} 股**"
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
                action_text = f"**【🎯 市价挂单止盈卖出】** 达成预设 +2.5% 止盈目标价({fmt_p(target_p)}元)"
            elif sig_type == "TAIL_STOP_EXIT":
                action_text = f"**【🛑 市价挂单止损卖出】** 击穿 -1.8% 防守底线({fmt_p(stop_p)}元)"
            elif sig_type == "TAIL_TIMED_EXIT":
                action_text = f"**【⏳ 择机分批卖出离场】** 09:45后早盘冲高动能逐步衰竭，建议现价锁定利润"
            else:
                action_text = f"**【🚨 尾盘市价全额卖出】** 已达T+1持股极限，绝不二次留仓过夜"

            lines.extend([
                f"**操盘指令**: {action_text}",
                f"**建议卖出执行价**: **{fmt_p(price)} 元**",
                f"**昨日买入成本**: {fmt_p(buy_p)} 元 (买入日期: {buy_d})",
                f"**单笔结算盈亏**: **{pnl_pct:+.2f}%** (今早最高冲至 +{peak_pct:+.2f}%)",
                f"**纪律**: 2.30战法遵循一夜持股极速兑现机制，次日坚决落袋，严禁深套变长线！"
            ])

        if reasons:
            lines.append(f"**理由**: {'; '.join(reasons)}")

        return self.send_markdown(title=title, text="\n\n".join(lines))
