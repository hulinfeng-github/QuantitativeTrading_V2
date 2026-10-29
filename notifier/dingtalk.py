"""
钉钉机器人 Markdown 卡片推送模块 (DingTalk Notifier 2.0)
支持加签密钥防伪与富文本 Markdown 交易卡片格式化
"""
import time
import hmac
import hashlib
import base64
import urllib.parse
import requests
from typing import Dict, Any

def fmt_p(p) -> str:
    try:
        val = float(p)
        return f"{val:.3f}" if val < 10.0 else f"{val:.2f}"
    except (ValueError, TypeError):
        return str(p)

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

    def send_markdown(self, title: str, text: str) -> bool:
        if not self.webhook:
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
                return res.get("errcode") == 0
            return False
        except Exception as e:
            print(f"钉钉推送失败: {e}")
            return False

    def send_trade_signal(self, signal: Dict[str, Any]) -> bool:
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

        # 图标与标题定调
        if sig_type in ["FORCE_EXIT", "EOD_FORCE_CLOSE_T"]:
            icon = "🚨"
            title = f"{icon} 尾盘强平警报: {name} ({symbol})" if sig_type == "EOD_FORCE_CLOSE_T" else f"{icon} 强制平仓警报: {name} ({symbol})"
        elif sig_type == "EOD_HEDGE_ALERT_T":
            icon = "⚠️"
            title = f"{icon} 倒T尾盘抉择: {name} ({symbol})"
        elif sig_type == "BUY":
            icon = "🚀"
            title = f"{icon} 波段买入信号: {name} ({desc})"
        elif sig_type == "SELL":
            icon = "⚠️"
            title = f"{icon} 减仓清仓预警: {name} ({desc})"
        elif sig_type in ["SELL_T", "BUY_T"]:
            icon = "⚡" if sig_type == "SELL_T" else "🌟"
            title = f"{icon} 日内做T机会: {name} ({desc})"
        elif sig_type in ["CLOSE_T", "STOP_T"]:
            icon = "🎯" if sig_type == "CLOSE_T" else "🛑"
            title = f"{icon} 做T对冲平仓: {name} ({desc})"
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
        elif sig_type == "LIMIT_UP":
            icon = "⚡"
            title = f"{icon} 游资龙虎打板信号: {name} ({signal.get('board_type', '主板涨停')})"
        else:
            icon = "🔔"
            title = f"{icon} 量化信号: {name} ({desc})"

        lines = [
            f"### {icon} 【量化2.0】{name} ({symbol})",
            f"- **信号类别**: **{desc}** ({sig_type})",
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
                f"- **📊 财报深度体检**: **{fin.get('fundamental_score')}分** | {fin.get('persona')}\n"
                f"  - 杜邦拆解: ROE {dp.get('roe')}% (净利率 {dp.get('net_margin')}% × 周转 {dp.get('asset_turnover')}) · {dp.get('driver_type')}\n"
                f"  - 核心景气: 扣非YoY **{gw.get('deduct_profit_yoy', 0):+.1f}%** | 营收YoY {gw.get('revenue_yoy', 0):+.1f}% | 毛利率 {gw.get('gross_margin')}%\n"
                f"  - 财务排雷: {rf.get('risk_summary')}"
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
                f"- **🚨 操盘指令**: **建议立即以市价挂单【全部清仓卖出】**",
                f"- **建议卖出执行价**: **{fmt_p(price)} 元**",
                f"- **持仓买入成本**: **{fmt_p(cost)} 元** (持仓: {shares} 股)",
                f"- **当前累计亏损**: <font color=\"#10b981\">**{pnl_pct:+.2f}%** ({pnl_amt:+.2f} 元)</font>",
                f"- **账户止损红线**: **{stop_line:.2f} 元** (已严重击穿)",
                f"- **风控纪律**: **已触及账户硬止损保护线，严禁侥幸扛单，务必果断平仓保留本金！**"
            ])

        # 2. 波段买入信号 (BUY)
        elif sig_type == "BUY":
            lines.extend([
                f"- **建议买入价格区间**: **{price:.2f} ~ {price * 1.01:.2f} 元**",
                f"- **防守止损价 (必须执行)**: **{levels.get('initial_stop')} 元** (-{levels.get('stop_pct')}%)",
                f"- **第一目标止盈价**: **{levels.get('target_price')} 元** (+{levels.get('target_pct')}%)",
                f"- **保本触发线**: {levels.get('breakeven_trigger')} 元 (盈利超6%后自动保本)",
                f"- **建议建仓仓位**: **{advice.get('position_pct', 15)}%** (约 {advice.get('suggested_shares', 100)} 股，半凯利+波动率平价)"
            ])

        # 3. 波段减仓/清仓信号 (SELL)
        elif sig_type == "SELL":
            cost = signal.get("holding_cost", 0.0)
            shares = signal.get("holding_shares", 0)
            pnl_pct = signal.get("pnl_pct")

            lines.extend([
                f"- **建议卖出执行价**: **{fmt_p(price)} 元** (逢盘中反抽果断挂单卖出)",
                f"- **建议减仓比例**: **建议减仓 50% ~ 100% (波段破位清仓)**",
                f"- **生命线破位参考**: MA20 ({signal.get('ma20', price):.2f}元)"
            ])
            if cost > 0:
                pnl_str = f"{pnl_pct:+.2f}%" if pnl_pct is not None else f"{(price - cost)/cost*100:+.2f}%"
                lines.append(f"- **对应持仓成本**: {fmt_p(cost)} 元 (持仓: {shares} 股，当前浮动: {pnl_str})")

        # 4. 日内做T信号 (BUY_T / SELL_T / CLOSE_T / STOP_T)
        elif sig_type in ["BUY_T", "SELL_T", "CLOSE_T", "STOP_T"]:
            if sig_type == "SELL_T":
                lines.extend([
                    f"- **做T指令**: **【倒T高抛】先卖出已有底仓**",
                    f"- **建议卖出高抛价**: **{fmt_p(price)} 元** (已达布林上轨正偏离)",
                    f"- **日内回补买回目标价**: **{levels.get('target_price')} 元**",
                    f"- **做T防守止损价**: **{levels.get('initial_stop')} 元**"
                ])
            elif sig_type == "BUY_T":
                lines.extend([
                    f"- **做T指令**: **【正T低吸】逢低买入底仓做T**",
                    f"- **建议买入低吸价**: **{fmt_p(price)} 元** (负偏离均线支撑)",
                    f"- **日内冲高卖出目标价**: **{levels.get('target_price')} 元**",
                    f"- **正T防守止损价**: **{levels.get('initial_stop')} 元**"
                ])
            elif sig_type == "CLOSE_T":
                t0_dir = signal.get("t0_direction", "")
                close_act = signal.get("close_action", "")
                shares = signal.get("holding_shares") or signal.get("shares", 100)
                if t0_dir == "SELL_T" or close_act == "BUY_BACK":
                    lines.extend([
                        f"- **做T指令**: **【🎯 倒T平仓买入回补】请立即买回等量底仓！**",
                        f"- **建议回补买入价**: **{fmt_p(price)} 元** (触达低吸目标线)",
                        f"- **建议买回数量**: **{shares} 股**",
                        f"- **操盘指引**: 价格已回踩达成预期差价，请立即在交易软件中买回等量股份，收复底仓并锁定日内差价利润！"
                    ])
                else:
                    lines.extend([
                        f"- **做T指令**: **【🎯 正T冲高卖出平仓】请立即高抛卖出等量底仓！**",
                        f"- **建议平仓卖出价**: **{fmt_p(price)} 元** (触达冲高目标线)",
                        f"- **建议卖出数量**: **{shares} 股**",
                        f"- **操盘指引**: 冲高达成预期对冲目标，请立即卖出等量底仓，锁定做T利润，保持总持仓不变！"
                    ])
            elif sig_type == "STOP_T":
                t0_dir = signal.get("t0_direction", "")
                close_act = signal.get("close_action", "")
                shares = signal.get("holding_shares") or signal.get("shares", 100)
                if t0_dir == "SELL_T" or "BUY_BACK" in close_act:
                    lines.extend([
                        f"- **做T指令**: **【🛑 倒T紧急买回止损】防止底仓抛飞**",
                        f"- **紧急买回参考价**: **{fmt_p(price)} 元** (突破压力防守线)",
                        f"- **买回归位股数**: **{shares} 股**",
                        f"- **风控说明**: 价格反抽突破倒T防守线，请立即以市价买回归位底仓，避免踏空后续主升"
                    ])
                else:
                    lines.extend([
                        f"- **做T指令**: **【🛑 正T防守卖出止损】平仓避险**",
                        f"- **止损卖出参考价**: **{fmt_p(price)} 元** (跌破下轨支撑防守线)",
                        f"- **止损平仓股数**: **{shares} 股**",
                        f"- **风控说明**: 跌破做T防守底线，请立即卖出加仓股份止损，今日停止该标的正T开仓"
                    ])

        # 5. 尾盘未对冲做T强制平仓与对冲抉择提醒
        elif sig_type == "EOD_FORCE_CLOSE_T":
            entry_p = signal.get("holding_cost", price)
            shares = signal.get("holding_shares", 100)
            pnl_pct = signal.get("pnl_pct", 0.0)
            pnl_amt = signal.get("pnl_amt", 0.0)
            lines.extend([
                f"- **🚨 操盘指令**: **【必须在 14:55 前以市价卖出原有老底仓】**",
                f"- **建议卖出执行价**: **{fmt_p(price)} 元**",
                f"- **建议卖出股数**: **{shares} 股 (对冲今早做T加仓股份)**",
                f"- **今早低吸做T价**: {fmt_p(entry_p)} 元 (日内盈亏: {pnl_pct:+.2f}%, {pnl_amt:+.0f}元)",
                f"- **紧急风控纪律**: **严禁日内正T演变为被动加仓过夜！14:55 集合竞价前必须卖出老底仓恢复原有仓位！**"
            ])
        elif sig_type == "EOD_HEDGE_ALERT_T":
            entry_p = signal.get("holding_cost", price)
            shares = signal.get("holding_shares", 100)
            diff_pct = signal.get("pnl_pct", 0.0)
            diff_amt = signal.get("pnl_amt", 0.0)
            lines.extend([
                f"- **⚠️ 操盘建议**: **【尾盘自主抉择：买回底仓 vs 顺势保留现金】**",
                f"- **当前参考市价**: **{fmt_p(price)} 元**",
                f"- **早盘高抛卖出价**: {fmt_p(entry_p)} 元 (相对节省: {diff_pct:+.2f}%, {diff_amt:+.0f}元)",
                f"- **方案 A (维持底仓)**: 如需维持原股数，请在 14:55 前以市价买回 {shares} 股",
                f"- **方案 B (顺势防守)**: 如大盘偏弱或个股走熊，可顺势保留现金过夜，明日更低位再接回"
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
                action_text = f"**【🎯 市价挂单止盈卖出】** 达成预设 +2.5% 止盈目标线({fmt_p(target_p)}元)"
            elif sig_type == "TAIL_STOP_EXIT":
                action_text = f"**【🛑 市价挂单止损卖出】** 击穿 -1.8% 防守底线({fmt_p(stop_p)}元)"
            elif sig_type == "TAIL_TIMED_EXIT":
                action_text = f"**【⏳ 择机分批卖出离场】** 09:45后早盘冲高动能衰竭，建议现价锁定利润"
            else:
                action_text = f"**【🚨 尾盘市价全额卖出】** 已达T+1持股极限，绝不二次留仓过夜"

            lines.extend([
                f"- **操盘指令**: {action_text}",
                f"- **建议卖出执行价**: **{fmt_p(price)} 元**",
                f"- **昨日买入成本**: {fmt_p(buy_p)} 元 (买入日期: {buy_d})",
                f"- **单笔结算盈亏**: **{pnl_pct:+.2f}%** (今早最高冲至 +{peak_pct:+.2f}%)",
                f"- **量化风控纪律**: **2.30战法遵循一夜持股极速兑现机制，次日坚决落袋，严禁深套变长线！**"
            ])

        # 7. 游资龙虎打板专属新手小白保姆级操盘指南 (LIMIT_UP)
        elif sig_type == "LIMIT_UP":
            seal_p = float(signal.get("seal_price") or price)
            target_p = float(levels.get("target_price") or round(seal_p * 1.045, 2))
            stop_p = float(levels.get("stop_loss") or round(seal_p * 0.97, 2))
            b_type = signal.get("board_type", "首板突破")
            ladder = signal.get("ladder_count", 1)
            h_score = signal.get("hardness_score", 80.0)
            st_ratio = signal.get("seal_turnover_ratio", 15.0)

            sweep_point = round(seal_p * 0.995, 2)
            lines.extend([
                f"- **🎯 板型结构**: **{b_type}** (当前连板数: **{ladder}板**)",
                f"- **封板硬度评估**: **{h_score} 分** | 封成比: **{st_ratio}%** | 炸板次数: **{signal.get('open_count', 0)}次**",
                f"- **今日建议封板买入价**: **{fmt_p(seal_p)} 元** (以今日涨停价申报)",
                f"- **新手仓位纪律**: **建议单笔总资金 10% ~ 15% (新手严禁满仓，轻仓试错)**",
                "",
                "#### 📖 【新手小白保姆级具体实操指南】",
                "**一、 应该在什么点买？具体怎么操作？**",
                f"1. **【扫板买点 (主选)】**：当个股快速脉冲至 **{sweep_point:.2f}元 (+9.5%~+9.8%)**、盘口卖一涨停价剩余挂单即将被大资金扫光的一瞬间，立即在交易软件中选择【限价买入】，价格直接输入今日涨停价 **{fmt_p(seal_p)}元**，点击买入；",
                f"2. **【排板买点 (次选)】**：若股票已封死涨停，直接输入 **{fmt_p(seal_p)}元** 挂单排队。**【防守关键撤单指令】**：如果盘中封单迅速减少或万手大单持续砸盘即将开板，**必须立即点击【撤单】**，严禁在开板下杀过程中无脑接飞刀！",
                "",
                "**二、 次日 (T+1) 应该在什么点卖？具体怎么操作？(三大剧本实战对策)**",
                f"1. **【剧本 A · 正常早盘冲高止盈 (最常见核心利润来源)】**：次日早盘 **09:30 ~ 09:45** 惯性脉冲上攻触及 **+{target_p:.2f}元 (+3.0%~+5.0%)** 目标区间或分时跌破黄线滞涨时，**不要贪心，立即以市价全部卖出止盈**，锁定隔日利润！",
                f"2. **【剧本 B · 超预期大高开/秒板连板 (享受主升加速)】**：若次日 09:25 集合竞价大幅高开 **+4.0% 以上** 或开盘 3 分钟内直接秒封涨停板，**持股不动**，享受二连板/三连板龙头溢价；若封板后开板且 1 分钟内未能迅速回封，立即市价全额卖出！",
                f"3. **【剧本 C · 不及预期/大幅低开破位 (绝不扛单严格止损)】**：若次日大幅低开低于 -2.0%，或盘中跌破防守止损线 **{fmt_p(stop_p)}元 (-3.0%)**，**绝不心存侥幸！绝不补仓！立即以市价挂单止损卖出**，防止深套吃大面！"
            ])

        if reasons:
            lines.append("---")
            lines.append(f"- **量化归因**: {'; '.join(reasons)}")

        lines.append(f"\n> QuantitativeTrading 2.0 · {time.strftime('%Y-%m-%d %H:%M:%S')}")

        return self.send_markdown(title=title, text="\n".join(lines))
