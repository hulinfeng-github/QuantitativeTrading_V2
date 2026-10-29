"""
A股智能量化系统 2.0 - 高胜率日内做T智能引擎 (T0SmartEngine 2.0)
专为 A 股 T+1 底仓量化设计：
1. 动态日内成交均价 (VWAP) 与自适应布林分时通道 (Dynamic Bollinger Band);
2. 冲高回落动态浮动对冲止盈 (Trailing Take-Profit Protection);
3. 严格单日单向唯一锁定 (多头只做正T，空头只做倒T，绝不多空互搏);
4. 单边跳水防飞刀熔断与止损后全天禁开正T;
5. 单日做T频次硬约束 (默认每日仅一次高确定性波段做T，规避摩擦);
6. 持仓状态机持久化与自动故障恢复。
"""
import os
import json
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional, List
import pandas as pd
import numpy as np
from config import T0_TRADES_FILE, load_config
from core.data_engine import data_engine

def round_price_dynamic(price: float, symbol: str = "") -> float:
    """
    根据标的类型自适应价格精度：
    ETF / 基金 / 5元以下标的，A股盘面最小变动单位为 0.001 (保留3位小数)；
    5元及以上主板个股，保留2位小数。
    """
    sym = str(symbol).lower()
    is_etf = any(sym.startswith(p) for p in ["sh5", "sz15", "sz16", "sh51", "sh56", "sh58", "sh50", "sz18"])
    if is_etf or price < 5.0:
        return round(float(price), 3)
    return round(float(price), 2)

class T0PositionTracker:
    def __init__(self, storage_file: Optional[Path] = None):
        self.storage_file = storage_file or T0_TRADES_FILE
        self._trades: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self):
        today_str = datetime.now().strftime("%Y-%m-%d")
        try:
            if self.storage_file.exists():
                with open(self.storage_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        self._trades = {k: v for k, v in data.items() if v.get("date") == today_str}
        except Exception:
            self._trades = {}

    def _save(self):
        try:
            with open(self.storage_file, "w", encoding="utf-8") as f:
                json.dump(self._trades, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存做T持仓状态失败: {e}")

    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        today_str = datetime.now().strftime("%Y-%m-%d")
        pos = self._trades.get(symbol)
        if pos and pos.get("date") == today_str:
            return pos
        return None

    def get_open_positions(self) -> List[Dict[str, Any]]:
        """获取今日处于 OPEN 状态未对冲平仓的做T头寸"""
        today_str = datetime.now().strftime("%Y-%m-%d")
        return [
            v for v in self._trades.values()
            if v.get("date") == today_str and v.get("status") == "OPEN"
        ]

    def open_position(
        self,
        symbol: str,
        name: str,
        direction: str,
        price: float,
        stop_price: float,
        target_price: float,
        shares: int = 100
    ):
        today_str = datetime.now().strftime("%Y-%m-%d")
        self._trades[symbol] = {
            "symbol": symbol,
            "name": name,
            "date": today_str,
            "direction": direction,  # "BUY_T" (正T低吸) | "SELL_T" (倒T高抛)
            "status": "OPEN",        # "OPEN" | "FINISHED" | "STOPPED"
            "entry_price": price,
            "stop_price": stop_price,
            "target_price": target_price,
            "peak_price": price,
            "entry_time": datetime.now().strftime("%H:%M:%S"),
            "shares": shares,
            "trade_count": 1
        }
        self._save()

    def update_peak_price(self, symbol: str, current_price: float):
        pos = self.get_position(symbol)
        if not pos or pos.get("status") != "OPEN":
            return
        direction = pos.get("direction")
        if direction == "BUY_T":
            if current_price > pos.get("peak_price", 0.0):
                pos["peak_price"] = current_price
                self._save()
        elif direction == "SELL_T":
            if current_price < pos.get("peak_price", 999999.0):
                pos["peak_price"] = current_price
                self._save()

    def close_position(self, symbol: str, status: str, exit_price: float, reason: str = ""):
        pos = self.get_position(symbol)
        if not pos:
            return
        pos["status"] = status  # "FINISHED" 或 "STOPPED"
        pos["exit_price"] = exit_price
        pos["exit_time"] = datetime.now().strftime("%H:%M:%S")
        pos["close_reason"] = reason

        entry_p = pos.get("entry_price", exit_price)
        direction = pos.get("direction", "BUY_T")
        if direction == "BUY_T":
            pnl_pct = round((exit_price - entry_p) / entry_p * 100.0, 2)
        else:
            pnl_pct = round((entry_p - exit_price) / entry_p * 100.0, 2)
        pos["pnl_pct"] = pnl_pct

        self._save()

    def reset_today(self):
        self._trades = {}
        self._save()


class T0SmartEngine:
    def __init__(self):
        self.tracker = T0PositionTracker()

    def analyze_t0(
        self,
        symbol: str,
        name: str,
        current_price: float,
        df_minute: Optional[pd.DataFrame] = None,
        daily_kline: Optional[pd.DataFrame] = None,
        holding_cost: float = 0.0,
        holding_shares: int = 100
    ) -> Dict[str, Any]:
        """
        全量研判当前标的日内做T机会与持仓状态监控
        """
        cfg = load_config()
        bias_buy_thresh = cfg.get("t0_bias_buy", -1.5)
        bias_sell_thresh = cfg.get("t0_bias_sell", 2.0)
        stop_pct = cfg.get("t0_stop_loss_pct", 1.5)
        take_profit_pct = cfg.get("t0_take_profit_pct", 1.5)
        trailing_pct = cfg.get("t0_trailing_profit_pct", 0.5)
        use_trend_filter = cfg.get("t0_trend_filter_enabled", True)

        # 针对 ETF / 宽基低波动品种自适应调整偏离阈值 (ETF天然波动率较小，日内罕见 -1.5% 偏离)
        sym_lower = str(symbol).lower()
        is_etf = any(sym_lower.startswith(p) for p in ["sh5", "sz15", "sz16", "sh51", "sh56", "sh58", "sh50", "sz18"])
        if is_etf:
            bias_buy_thresh = round(bias_buy_thresh * 0.6, 2)
            bias_sell_thresh = round(bias_sell_thresh * 0.6, 2)
            take_profit_pct = round(take_profit_pct * 0.6, 2)
            stop_pct = round(stop_pct * 0.6, 2)

        # 1. 获取分时数据与计算 VWAP 及动态布林带
        if df_minute is None or len(df_minute) < 5:
            df_minute = data_engine.get_minute_data(symbol)

        vwap = current_price
        bias = 0.0
        dyn_lower = round_price_dynamic(current_price * (1.0 + bias_buy_thresh / 100.0), symbol)
        dyn_upper = round_price_dynamic(current_price * (1.0 + bias_sell_thresh / 100.0), symbol)

        if df_minute is not None and len(df_minute) > 0:
            last_row = df_minute.iloc[-1]
            vwap = float(last_row['vwap'])
            if vwap > 0:
                bias = round((current_price - vwap) / vwap * 100.0, 2)

            # 计算分时波动率与动态通道
            if len(df_minute) >= 15:
                prices = df_minute['price'].values
                std_intraday = float(np.std(prices) / vwap * 100.0)
                band_offset = max(1.2, min(std_intraday * 1.5, 3.5))
                if is_etf:
                    band_offset = max(0.6, min(std_intraday * 1.2, 2.0))
                dyn_lower = round_price_dynamic(vwap * (1.0 - band_offset / 100.0), symbol)
                dyn_upper = round_price_dynamic(vwap * (1.0 + band_offset / 100.0), symbol)

        # 2. 检查大趋势通道 (顺势定调)
        trend_direction = "NEUTRAL"
        if use_trend_filter and daily_kline is not None and len(daily_kline) >= 20:
            ma20 = float(daily_kline['close'].rolling(20).mean().iloc[-1])
            if current_price > ma20:
                trend_direction = "BULL"  # 多头环境：优先正T低吸
            else:
                trend_direction = "BEAR"  # 空头破位：优先倒T高抛
        else:
            trend_direction = "NEUTRAL"   # 未开启趋势过滤：双向基于分时VWAP做T

        # 3. 检查现有持仓状态跟踪
        existing_pos = self.tracker.get_position(symbol)
        signal_action = "HOLD_T"
        signal_desc = "震荡观望"
        reasons = []

        active_dir = ""
        active_target = current_price
        active_stop = current_price
        active_entry = current_price
        active_shares = min(holding_shares, 1000)

        if existing_pos:
            status = existing_pos.get("status")
            direction = existing_pos.get("direction")
            entry_p = existing_pos.get("entry_price", current_price)
            stop_p = existing_pos.get("stop_price", current_price)
            target_p = existing_pos.get("target_price", current_price)
            peak_p = existing_pos.get("peak_price", entry_p)
            pos_shares = existing_pos.get("shares", active_shares)

            active_dir = direction
            active_target = target_p
            active_stop = stop_p
            active_entry = entry_p
            active_shares = pos_shares

            if status == "OPEN":
                self.tracker.update_peak_price(symbol, current_price)

                if direction == "BUY_T":
                    curr_profit_pct = (current_price - entry_p) / entry_p * 100.0 if entry_p > 0 else 0.0
                    peak_profit_pct = (peak_p - entry_p) / entry_p * 100.0 if entry_p > 0 else 0.0

                    # 触发硬止损 (跌破防守线)
                    if round(current_price, 3) <= round(stop_p, 3) + 1e-4:
                        signal_action = "STOP_T"
                        signal_desc = "正T防守止损对冲平仓"
                        reasons.append(f"跌破正T防守底线 {stop_p}元(-{stop_pct}%)，请立即卖出底仓止损保护！")
                        self.tracker.close_position(symbol, "STOPPED", current_price, reasons[-1])
                    # 触发冲高回落动态对冲止盈
                    elif peak_profit_pct >= take_profit_pct and (peak_profit_pct - curr_profit_pct) >= trailing_pct:
                        signal_action = "CLOSE_T"
                        signal_desc = "正T冲高回落对冲平仓"
                        reasons.append(f"日内冲高至 {peak_p}元(+{peak_profit_pct:.2f}%)后回落{trailing_pct}%，请高抛卖出等量底仓，动态锁定做T利润！")
                        self.tracker.close_position(symbol, "FINISHED", current_price, reasons[-1])
                    # 达到静态目标止盈
                    elif round(current_price, 3) >= round(target_p, 3) - 1e-4:
                        signal_action = "CLOSE_T"
                        signal_desc = "正T冲高卖出对冲平仓"
                        reasons.append(f"已达到日内正T预期对冲目标 {target_p}元(+{take_profit_pct}%)，请果断高抛卖出等量底仓（{pos_shares}股）锁定差价！")
                        self.tracker.close_position(symbol, "FINISHED", current_price, reasons[-1])
                    else:
                        signal_action = "HOLDING_T"
                        signal_desc = f"正T持仓中 (现浮动: {curr_profit_pct:+.2f}%)"

                elif direction == "SELL_T":
                    curr_profit_pct = (entry_p - current_price) / entry_p * 100.0 if entry_p > 0 else 0.0
                    # 倒T突破压力防守线 -> 触发买回止损
                    if round(current_price, 3) >= round(stop_p, 3) - 1e-4:
                        signal_action = "STOP_T"
                        signal_desc = "倒T反抽止损买回归位"
                        reasons.append(f"价格反抽冲破防守线 {stop_p}元，为防底仓抛飞，请立即以市价买回等量底仓（{pos_shares}股）止损对冲！")
                        self.tracker.close_position(symbol, "STOPPED", current_price, reasons[-1])
                    # 倒T回踩目标位 -> 触发买回止盈！
                    elif round(current_price, 3) <= round(target_p, 3) + 1e-4:
                        signal_action = "CLOSE_T"
                        signal_desc = "倒T回踩低吸买入平仓"
                        reasons.append(f"分时现价({current_price}元) 回踩触达日内低吸目标价({target_p}元)！已兑现价差收益 +{curr_profit_pct:.2f}%，请立即买回等量底仓（{pos_shares}股）收复筹码！")
                        self.tracker.close_position(symbol, "FINISHED", current_price, reasons[-1])
                    else:
                        signal_action = "HOLDING_T"
                        signal_desc = f"倒T持仓中 (现浮动: {curr_profit_pct:+.2f}%)"

            elif status in ["FINISHED", "STOPPED"]:
                signal_action = "FINISHED_WATCH"
                signal_desc = "今日做T战果已锁定·观望"
                reasons.append("今日做T已完结平仓，单日频次锁定，防范频繁交易损耗")

        else:
            # 4. 未开仓状态：研判是否触发新做T买点/卖点
            if bias <= bias_buy_thresh and (current_price <= dyn_lower):
                # 判定是否为空头强压制 (若负偏离达 1.4 倍买入阈值以上，属于日内急跌恐慌深蹲，允许超跌做正T反弹)
                is_oversold_bounce = (bias <= bias_buy_thresh * 1.4)
                if trend_direction == "BEAR" and not is_oversold_bounce:
                    signal_desc = "空头趋势压制·严禁接飞刀"
                    reasons.append(f"标的处于MA20空头通道且负偏离({bias:.2f}%)未达恐慌超跌线({bias_buy_thresh*1.4:.2f}%)，禁止开仓")
                else:
                    signal_action = "BUY_T"
                    if trend_direction == "BEAR" and is_oversold_bounce:
                        signal_desc = "🌟 破位超跌反弹·正T急跌低吸"
                        reasons.append(f"日内急跌出现极端负偏离({bias:.2f}%)，触发强力超跌反抽买点，轻仓快进快出做正T")
                    else:
                        signal_desc = "🌟 正T低吸机会触发"
                        reasons.append(f"现价偏离分时均价({vwap}元)达 {bias:.2f}%，触及动态布林下轨({dyn_lower}元)，具备日内反弹差价")

                    suggested_stop = round_price_dynamic(current_price * (1.0 - stop_pct / 100.0), symbol)
                    suggested_target = round_price_dynamic(max(vwap * 1.002, current_price * (1.0 + take_profit_pct / 100.0)), symbol)

                    active_dir = "BUY_T"
                    active_target = suggested_target
                    active_stop = suggested_stop
                    active_entry = current_price
                    active_shares = min(holding_shares, 1000)

                    # 开仓记录
                    self.tracker.open_position(
                        symbol=symbol,
                        name=name,
                        direction="BUY_T",
                        price=current_price,
                        stop_price=suggested_stop,
                        target_price=suggested_target,
                        shares=active_shares
                    )

            # 倒T高抛信号：当前价格冲高突破分时均线正偏离阈值
            elif bias >= bias_sell_thresh and (current_price >= dyn_upper):
                if trend_direction == "BULL":
                    signal_desc = "多头主升·严禁倒T防抛飞"
                    reasons.append("标的高处于MA20多头主升通道，倒T高抛极易卖飞强势核心牛股")
                else:
                    signal_action = "SELL_T"
                    signal_desc = "⚡ 倒T高抛机会触发"
                    suggested_stop = round_price_dynamic(current_price * (1.0 + stop_pct / 100.0), symbol)
                    suggested_target = round_price_dynamic(min(vwap * 0.995, current_price * (1.0 - take_profit_pct / 100.0)), symbol)
                    reasons.append(f"现价冲高偏离分时均价({vwap}元)达 +{bias:.2f}%，触及动态布林上轨({dyn_upper}元)，适合高抛后低吸回补")

                    active_dir = "SELL_T"
                    active_target = suggested_target
                    active_stop = suggested_stop
                    active_entry = current_price
                    active_shares = min(holding_shares, 1000)

                    self.tracker.open_position(
                        symbol=symbol,
                        name=name,
                        direction="SELL_T",
                        price=current_price,
                        stop_price=suggested_stop,
                        target_price=suggested_target,
                        shares=active_shares
                    )

        close_action = ""
        if signal_action == "CLOSE_T":
            close_action = "BUY_BACK" if active_dir == "SELL_T" else "SELL_HEDGE"
        elif signal_action == "STOP_T":
            close_action = "BUY_BACK_STOP" if active_dir == "SELL_T" else "SELL_STOP"

        return {
            "symbol": symbol,
            "name": name,
            "current_price": current_price,
            "vwap": vwap,
            "bias_pct": bias,
            "dyn_lower": dyn_lower,
            "dyn_upper": dyn_upper,
            "trend_direction": trend_direction,
            "signal_action": signal_action,
            "signal_desc": signal_desc,
            "reasons": reasons,
            "position": self.tracker.get_position(symbol),
            "direction": active_dir,
            "t0_direction": active_dir,
            "close_action": close_action,
            "target_price": active_target,
            "stop_price": active_stop,
            "entry_price": active_entry,
            "shares": active_shares
        }

    def check_eod_unclosed_positions(self) -> List[Dict[str, Any]]:
        """
        尾盘（14:45 - 14:55）核查未对冲做T持仓，发出强制平仓/对冲决策警报
        """
        open_positions = self.tracker.get_open_positions()
        signals = []
        for pos in open_positions:
            sym = pos["symbol"]
            name = pos["name"]
            direction = pos["direction"]
            entry_p = float(pos.get("entry_price", 0.0))
            shares = int(pos.get("shares", 100))

            q = data_engine.get_realtime_quote(sym)
            cur_p = float(q.get("price", entry_p)) if q else entry_p
            if cur_p <= 0:
                cur_p = entry_p

            if direction == "BUY_T":
                # 正T（低吸买入）：直到尾盘未触发冲高平仓，必须卖出等量老底仓！
                # 严禁日内做T被动演变为加仓过夜，承担次日下行系统性风险
                diff_pct = round((cur_p - entry_p) / entry_p * 100.0, 2) if entry_p > 0 else 0.0
                diff_amt = round((cur_p - entry_p) * shares, 2) if entry_p > 0 else 0.0

                sig = {
                    "symbol": sym,
                    "name": name,
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "price": cur_p,
                    "signal_type": "EOD_FORCE_CLOSE_T",
                    "signal_desc": "🚨【正T尾盘强平】未达目标·强制卖出底仓",
                    "score": 99,
                    "holding_cost": entry_p,
                    "holding_shares": shares,
                    "pnl_pct": diff_pct,
                    "pnl_amt": diff_amt,
                    "reasons": [
                        f"早盘于 {pos.get('entry_time', '盘中')} 低吸做T买入（均价 {entry_p}元），直到尾盘未触发冲高对冲线",
                        f"现价 {cur_p}元 (日内做T盈亏: {diff_pct:+.2f}%, 盈亏额: {diff_amt:+.0f}元)",
                        "【A股T+1核心风控】为防止日内做T演变成'被动加仓'，必须在 14:55 前以市价卖出原有等量老底仓，恢复原有仓位配比！"
                    ],
                    "trade_levels": {
                        "entry_price": entry_p,
                        "suggested_sell_price": cur_p,
                        "shares": shares,
                        "action_urgency": "14:55 集合竞价前必须完成挂单卖出"
                    }
                }
                signals.append(sig)
                self.tracker.close_position(
                    sym,
                    status="FINISHED",
                    exit_price=cur_p,
                    reason="尾盘强制卖出老底仓对冲平仓"
                )

            elif direction == "SELL_T":
                # 倒T（高抛卖出）：直到尾盘未跌至回补线，此时手握现金，提示底仓抉择
                diff_pct = round((entry_p - cur_p) / entry_p * 100.0, 2) if entry_p > 0 else 0.0
                diff_amt = round((entry_p - cur_p) * shares, 2) if entry_p > 0 else 0.0

                sig = {
                    "symbol": sym,
                    "name": name,
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "price": cur_p,
                    "signal_type": "EOD_HEDGE_ALERT_T",
                    "signal_desc": "⚠️【倒T尾盘对冲】高抛未回补·底仓抉择提醒",
                    "score": 85,
                    "holding_cost": entry_p,
                    "holding_shares": shares,
                    "pnl_pct": diff_pct,
                    "pnl_amt": diff_amt,
                    "reasons": [
                        f"早盘于 {pos.get('entry_time', '盘中')} 高抛卖出底仓（均价 {entry_p}元），收回现金 {entry_p * shares:.0f}元",
                        f"现价 {cur_p}元，日内相对高抛差价: {diff_pct:+.2f}% (节省现金: {diff_amt:+.0f}元)",
                        "【方案一·锁定底仓】：若需维持原有底仓股数不变，请在 14:55 前以市价买回等量股份",
                        "【方案二·顺势减仓】：若大盘走弱且该股处于空头通道，可顺势保留现金过夜，明日更低位再行低吸"
                    ],
                    "trade_levels": {
                        "entry_price": entry_p,
                        "suggested_buy_price": cur_p,
                        "shares": shares,
                        "action_urgency": "14:55 集合竞价前自主决策"
                    }
                }
                signals.append(sig)
                self.tracker.close_position(
                    sym,
                    status="FINISHED",
                    exit_price=cur_p,
                    reason="尾盘倒T未回补·自主抉择锁定"
                )

        return signals

t0_smart_engine = T0SmartEngine()
