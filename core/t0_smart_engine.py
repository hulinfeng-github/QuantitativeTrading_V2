"""
A股智能量化系统 2.0 - 研究性日内做T信号引擎 (T0SmartEngine)
专为 A 股 T+1 底仓量化设计：
1. 分时成交均价 (VWAP) 与「相对VWAP平均偏离」自适应带宽 (替代固定偏离阈值)；
2. 均值回归止盈：目标 = 回归到 VWAP 附近 (带小幅缓冲)；
3. 严格单日单向唯一锁定 (多头只做正T，空头只做倒T，绝不多空互搏)；
4. 分时反转确认 + 大趋势顺势过滤 + 尾盘时间衰减；
5. 单日做T频次硬约束 (模型每日最多跟踪一次信号，减少过度交易；并不代表高胜率)；
6. 持仓状态机持久化 (线程安全 + 原子写盘) 与已平仓样本历史沉淀 (供绩效统计)。
"""
import os
import json
import threading
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional, List
import pandas as pd
import numpy as np
from config import T0_TRADES_FILE, T0_HISTORY_FILE, load_config
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
    def __init__(self, storage_file: Optional[Path] = None, history_file: Optional[Path] = None):
        self.storage_file = storage_file or T0_TRADES_FILE
        self.history_file = history_file or T0_HISTORY_FILE
        self._trades: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()
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
        # 原子写盘：先写临时文件再替换，避免进程中断时写坏 JSON
        try:
            tmp = self.storage_file.with_name(self.storage_file.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._trades, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.storage_file)
        except Exception as e:
            print(f"保存做T持仓状态失败: {e}")

    def _append_history(self, pos: Dict[str, Any]):
        try:
            hist: List[Dict[str, Any]] = []
            if self.history_file.exists():
                with open(self.history_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        hist = data
            key = (pos.get("symbol"), pos.get("date"), pos.get("entry_time"), pos.get("direction"))
            replaced = False
            for i, h in enumerate(hist):
                hk = (h.get("symbol"), h.get("date"), h.get("entry_time"), h.get("direction"))
                if hk == key:
                    hist[i] = pos
                    replaced = True
                    break
            if not replaced:
                hist.append(pos)
            hist = hist[-5000:]
            tmp = self.history_file.with_name(self.history_file.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(hist, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.history_file)
        except Exception as e:
            print(f"保存做T历史样本失败: {e}")

    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        today_str = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            pos = self._trades.get(symbol)
            if pos and pos.get("date") == today_str:
                return pos.copy()
            return None

    def get_open_positions(self) -> List[Dict[str, Any]]:
        """获取今日处于 OPEN 状态未对冲平仓的做T头寸"""
        today_str = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            return [
                v.copy() for v in self._trades.values()
                if v.get("date") == today_str and v.get("status") == "OPEN"
            ]

    def load_history(self) -> List[Dict[str, Any]]:
        try:
            if self.history_file.exists():
                with open(self.history_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return data
        except Exception:
            pass
        return []

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
        with self._lock:
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
                "trade_count": 1,
                "source": "model_signal_proxy",
                "actual_fill_confirmed": False
            }
            self._save()

    def update_peak_price(self, symbol: str, current_price: float):
        with self._lock:
            pos = self._trades.get(symbol)
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
        with self._lock:
            pos = self._trades.get(symbol)
            if not pos:
                return
            if pos.get("status") != "OPEN":
                # 已平仓，避免重复记录历史
                return
            pos["status"] = status  # "FINISHED" 或 "STOPPED"
            pos["exit_price"] = exit_price
            pos["exit_time"] = datetime.now().strftime("%H:%M:%S")
            pos["close_reason"] = reason

            entry_p = pos.get("entry_price", exit_price)
            direction = pos.get("direction", "BUY_T")
            shares = int(pos.get("shares", 0)) or 100

            if direction == "BUY_T":
                gross_pct = (exit_price - entry_p) / entry_p * 100.0 if entry_p > 0 else 0.0
                gross_amt = (exit_price - entry_p) * shares
            else:
                gross_pct = (entry_p - exit_price) / entry_p * 100.0 if entry_p > 0 else 0.0
                gross_amt = (entry_p - exit_price) * shares
            pos["pnl_pct"] = round(gross_pct, 2)
            pos["gross_pnl_amt"] = round(gross_amt, 2)
            pos["pnl_type"] = "signal_proxy_gross_excluding_fees"

            # 净收益口径：双边佣金(含最低5元) + 印花税(仅卖出) + 滑点(双边)估算
            try:
                cfg = load_config()
                comm_r = float(cfg.get("t0_commission_rate", 0.00025))
                min_comm = float(cfg.get("t0_min_commission", 5.0))
                stamp_r = float(cfg.get("t0_stamp_tax_rate", 0.0005))
                slip_r = float(cfg.get("t0_slippage_rate", 0.0005))
            except Exception:
                comm_r, min_comm, stamp_r, slip_r = 0.00025, 5.0, 0.0005, 0.0005

            entry_amt = entry_p * shares
            exit_amt = exit_price * shares
            comm_entry = max(min_comm, entry_amt * comm_r)
            comm_exit = max(min_comm, exit_amt * comm_r)
            # 正T先买后卖：印花税在卖出(exit)；倒T先卖后买：印花税在卖出(entry)
            stamp_amt = (exit_amt if direction == "BUY_T" else entry_amt) * stamp_r
            slip_amt = (entry_amt + exit_amt) * slip_r
            fees = comm_entry + comm_exit + stamp_amt + slip_amt
            net_amt = gross_amt - fees
            net_pct = net_amt / entry_amt * 100.0 if entry_amt > 0 else 0.0
            pos["fees_amt"] = round(fees, 2)
            pos["net_pnl_amt"] = round(net_amt, 2)
            pos["net_pnl_pct"] = round(net_pct, 2)
            pos["net_pnl_type"] = "signal_proxy_net_incl_fees_slippage_estimate"

            self._save()
            # 已平仓样本沉淀到历史文件，供长期绩效统计 (去重按 symbol+date+entry_time+direction)
            self._append_history(pos.copy())

    def reset_today(self):
        with self._lock:
            self._trades = {}
            self._save()


class T0SmartEngine:
    def __init__(self):
        self.tracker = T0PositionTracker()

    @staticmethod
    def is_trading_session() -> bool:
        """A股连续竞价交易时段 (周一至周五 09:30-11:30, 13:00-15:00)。"""
        now = datetime.now()
        if now.weekday() >= 5:
            return False
        hm = now.strftime("%H:%M")
        return ("09:30" <= hm <= "11:30") or ("13:00" <= hm <= "15:00")

    @staticmethod
    def _close_action(action: str, direction: str) -> str:
        if action == "CLOSE_T":
            return "BUY_BACK" if direction == "SELL_T" else "SELL_HEDGE"
        if action == "STOP_T":
            return "BUY_BACK_STOP" if direction == "SELL_T" else "SELL_STOP"
        return ""

    @staticmethod
    def _t0_score(bias_pct: float, band_pct: float, trend_aligned: bool,
                  reversal: bool, minute_age: Optional[float], is_late: bool) -> int:
        """做T信号强度 (0-99，规则启发式，非校准概率)。"""
        score = 40.0
        if band_pct and band_pct > 0:
            depth = max(0.0, abs(bias_pct) - band_pct)
            score += min(20.0, depth * 10.0)          # 偏离带宽越深越强
        if trend_aligned:
            score += 12.0
        if reversal:
            score += 12.0
        if minute_age is not None and minute_age <= 1.0:
            score += 8.0                             # 数据越新鲜置信越高
        if is_late:
            score -= 10.0                            # 尾盘时间价值降低
        return int(round(min(99.0, max(0.0, score))))

    def _result(self, symbol, name, current_price, vwap, bias, dyn_lower, dyn_upper,
                trend, market_exposure, action, desc, reasons, active_dir="",
                target=None, stop=None, entry=None, shares=0, score=0) -> Dict[str, Any]:
        return {
            "symbol": symbol,
            "name": name,
            "current_price": current_price,
            "vwap": vwap,
            "bias_pct": bias,
            "dyn_lower": dyn_lower,
            "dyn_upper": dyn_upper,
            "trend_direction": trend,
            "market_exposure": market_exposure,
            "signal_action": action,
            "signal_desc": desc,
            "reasons": reasons,
            "position": self.tracker.get_position(symbol),
            "direction": active_dir,
            "t0_direction": active_dir,
            "close_action": self._close_action(action, active_dir),
            "target_price": target if target is not None else current_price,
            "stop_price": stop if stop is not None else current_price,
            "entry_price": entry if entry is not None else current_price,
            "shares": shares,
            "t0_score": score
        }

    def analyze_t0(
        self,
        symbol: str,
        name: str,
        current_price: float,
        df_minute: Optional[pd.DataFrame] = None,
        daily_kline: Optional[pd.DataFrame] = None,
        holding_cost: float = 0.0,
        holding_shares: int = 0,
        available_shares: int = 0,
        market_exposure: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        全量研判当前标的日内做T机会与持仓状态监控
        """
        cfg = load_config()
        bias_buy_thresh = cfg.get("t0_bias_buy", -1.5)
        bias_sell_thresh = cfg.get("t0_bias_sell", 2.0)
        stop_pct = cfg.get("t0_stop_loss_pct", 1.5)
        take_profit_vwap_pct = cfg.get("t0_take_profit_vwap_pct", 0.25)
        use_trend_filter = cfg.get("t0_trend_filter_enabled", True)
        use_dynamic_boll = cfg.get("t0_use_dynamic_boll", True)
        band_dev_k = float(cfg.get("t0_band_dev_k", 1.5))
        band_min_pct = float(cfg.get("t0_band_min_pct", 0.6))
        band_max_pct = float(cfg.get("t0_band_max_pct", 2.8))
        sell_asymmetry = float(cfg.get("t0_sell_asymmetry", 1.15))
        reversal_bars = max(2, int(cfg.get("t0_reversal_bars", 2)))
        vol_confirm = cfg.get("t0_reversal_volume_confirm", True)
        vol_mult = float(cfg.get("t0_reversal_volume_mult", 1.0))
        vwap_slope_filter = cfg.get("t0_vwap_slope_filter", True)
        vwap_slope_lookback = max(5, int(cfg.get("t0_vwap_slope_lookback", 15)))
        max_vwap_slope_pct = float(cfg.get("t0_max_vwap_slope_pct", 0.8))
        time_decay_enabled = cfg.get("t0_time_decay_enabled", True)
        late_window_start = cfg.get("t0_late_window_start", "14:00")
        late_target_scale = float(cfg.get("t0_late_target_scale", 0.6))
        late_stop_scale = float(cfg.get("t0_late_stop_scale", 0.8))
        min_minute_bars = max(30, int(cfg.get("t0_minute_min_bars", 30)))
        max_minute_age = max(1, int(cfg.get("t0_minute_max_age_minutes", 5)))
        min_market_exposure = float(cfg.get("t0_min_market_exposure", 0.35))
        min_market_exposure_sell = float(cfg.get("t0_min_market_exposure_sell", 0.0))
        holding_shares = max(0, int(holding_shares or 0))
        available_shares = min(max(0, int(available_shares or 0)), holding_shares)

        # ---- 交易时段守卫：不依赖调用方，防御非交易时段/非交易日产生信号 ----
        if not self.is_trading_session():
            return self._result(
                symbol, name, current_price, current_price, 0.0,
                current_price, current_price, "NEUTRAL", market_exposure,
                "HOLD_T", "非交易时段·暂停做T研判",
                ["当前非A股连续竞价交易时段，暂停生成或更新做T信号"],
                shares=0, score=0
            )

        # 1. 获取分时数据与计算 VWAP 偏离及自适应带宽
        vwap = current_price
        bias = 0.0
        dyn_lower = round_price_dynamic(current_price * (1.0 + bias_buy_thresh / 100.0), symbol)
        dyn_upper = round_price_dynamic(current_price * (1.0 + bias_sell_thresh / 100.0), symbol)
        minute_data_ok = False
        minute_bar_age_minutes = None
        minute_closes = np.array([], dtype=float)
        minute_volumes = None
        vwap_slope_pct = None

        if df_minute is None:
            df_minute = data_engine.get_minute_data(symbol)
        if df_minute is not None and {"time", "price", "vwap"}.issubset(df_minute.columns):
            cols = ["time", "price", "vwap"] + (["volume"] if "volume" in df_minute.columns else [])
            minute_frame = df_minute[cols].copy()
            minute_frame["price"] = pd.to_numeric(minute_frame["price"], errors="coerce")
            minute_frame["vwap"] = pd.to_numeric(minute_frame["vwap"], errors="coerce")
            minute_frame = minute_frame.replace([np.inf, -np.inf], np.nan).dropna(subset=["price", "vwap"])
            minute_frame = minute_frame[(minute_frame["price"] > 0) & (minute_frame["vwap"] > 0)]
            if len(minute_frame) >= min_minute_bars:
                last_row = minute_frame.iloc[-1]
                try:
                    last_bar_time = datetime.strptime(str(last_row["time"])[:5], "%H:%M").time()
                    now = datetime.now()
                    bar_datetime = datetime.combine(now.date(), last_bar_time)
                    minute_bar_age_minutes = (now - bar_datetime).total_seconds() / 60.0
                    minute_data_ok = -2.0 <= minute_bar_age_minutes <= max_minute_age
                except (TypeError, ValueError):
                    minute_data_ok = False

                vwap = float(last_row["vwap"])
                bias = round((current_price - vwap) / vwap * 100.0, 2)
                minute_closes = minute_frame["price"].to_numpy(dtype=float)
                # 每分钟成交量 (累计量差分)，用于反转的放量确认
                if "volume" in minute_frame.columns:
                    vol_series = pd.to_numeric(minute_frame["volume"], errors="coerce").ffill()
                    minute_volumes = vol_series.diff().to_numpy(dtype=float)
                    minute_volumes = minute_volumes[np.isfinite(minute_volumes)]

                # 分时均价 VWAP 斜率：识别单边趋势日，趋势日不做逆势T
                if len(minute_frame) >= vwap_slope_lookback + 1:
                    vwap_series = pd.to_numeric(minute_frame["vwap"], errors="coerce").dropna().to_numpy(dtype=float)
                    if len(vwap_series) >= vwap_slope_lookback + 1:
                        base_v = float(vwap_series[-vwap_slope_lookback - 1])
                        if base_v > 0:
                            vwap_slope_pct = round((float(vwap_series[-1]) - base_v) / base_v * 100.0, 3)

                if use_dynamic_boll and minute_data_ok:
                    # 自适应带宽：基于近期价格相对 VWAP 的平均绝对偏离 (均值回归的“正常波动范围”)
                    recent = minute_closes[-min_minute_bars:]
                    if vwap > 0:
                        mad_pct = float(np.mean(np.abs(recent - vwap)) / vwap * 100.0)
                    else:
                        mad_pct = 0.0
                    band_pct = float(np.clip(mad_pct * band_dev_k, band_min_pct, band_max_pct))
                    buy_band = band_pct
                    sell_band = band_pct * sell_asymmetry
                    dyn_lower = round_price_dynamic(vwap * (1.0 - buy_band / 100.0), symbol)
                    dyn_upper = round_price_dynamic(vwap * (1.0 + sell_band / 100.0), symbol)

        if vwap > 0 and not use_dynamic_boll:
            dyn_lower = round_price_dynamic(vwap * (1.0 + bias_buy_thresh / 100.0), symbol)
            dyn_upper = round_price_dynamic(vwap * (1.0 + bias_sell_thresh / 100.0), symbol)

        # 2. 检查大趋势通道 (顺势定调)
        trend_direction = "NEUTRAL"
        if daily_kline is not None and "close" in daily_kline and len(daily_kline) >= 60:
            daily_source = daily_kline
            if "date" in daily_source.columns and len(daily_source) > 0:
                last_daily_date = str(daily_source["date"].iloc[-1])[:10]
                if last_daily_date == datetime.now().strftime("%Y-%m-%d"):
                    daily_source = daily_source.iloc[:-1]
            daily_close = pd.to_numeric(daily_source["close"], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
            if len(daily_close) >= 60:
                ma20_series = daily_close.rolling(20, min_periods=20).mean()
                ma60_series = daily_close.rolling(60, min_periods=60).mean()
                ma20 = float(ma20_series.iloc[-1])
                ma20_prev = float(ma20_series.iloc[-6])
                ma60 = float(ma60_series.iloc[-1])
                if current_price > ma20 > ma60 and ma20 > ma20_prev:
                    trend_direction = "BULL"
                elif current_price < ma20 or ma20 <= ma60 or ma20 <= ma20_prev:
                    trend_direction = "BEAR"

        # 3. 检查现有持仓状态跟踪
        existing_pos = self.tracker.get_position(symbol)
        signal_action = "HOLD_T"
        signal_desc = "震荡观望"
        reasons = []

        active_dir = ""
        active_target = current_price
        active_stop = current_price
        active_entry = current_price
        available_lot = (available_shares // 100) * 100
        # 做T标的可能是多人持仓，数量不设上限：信号数量直接取当日可卖底仓
        suggested_shares = available_lot
        active_shares = suggested_shares
        current_hm = datetime.now().strftime("%H:%M")
        entry_window_ok = "09:45" <= current_hm <= "14:30"
        is_late = time_decay_enabled and current_hm >= late_window_start

        if existing_pos:
            status = existing_pos.get("status")
            direction = existing_pos.get("direction")
            entry_p = existing_pos.get("entry_price", current_price)
            stop_p = existing_pos.get("stop_price", current_price)
            target_p = existing_pos.get("target_price", current_price)
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
                    if round(current_price, 3) <= round(stop_p, 3) + 1e-4:
                        signal_action = "STOP_T"
                        signal_desc = "正T防守止损对冲平仓"
                        reasons.append(f"触及模型止损观察位 {stop_p}元(-{stop_pct}%)；实际持仓、可卖数量和委托状态未核验，系统未下单。")
                        self.tracker.close_position(symbol, "STOPPED", current_price, reasons[-1])
                    elif round(current_price, 3) >= round(target_p, 3) - 1e-4:
                        signal_action = "CLOSE_T"
                        signal_desc = "正T回归VWAP对冲平仓"
                        reasons.append(f"价格回到VWAP附近的模型观察位 {target_p}元(+{(target_p-entry_p)/entry_p*100:.2f}%)；是否调整持仓需核对实际成交、可卖数量和委托状态。")
                        self.tracker.close_position(symbol, "FINISHED", current_price, reasons[-1])
                    else:
                        signal_action = "HOLDING_T"
                        signal_desc = f"正T模型信号观察中 (相对信号价变动: {curr_profit_pct:+.2f}%，未含交易成本)"

                elif direction == "SELL_T":
                    curr_profit_pct = (entry_p - current_price) / entry_p * 100.0 if entry_p > 0 else 0.0
                    if round(current_price, 3) >= round(stop_p, 3) - 1e-4:
                        signal_action = "STOP_T"
                        signal_desc = "倒T反抽止损买回归位"
                        reasons.append(f"价格触及模型防守观察位 {stop_p}元；系统未核验实际底仓与可买数量，也未提交订单。")
                        self.tracker.close_position(symbol, "STOPPED", current_price, reasons[-1])
                    elif round(current_price, 3) <= round(target_p, 3) + 1e-4:
                        signal_action = "CLOSE_T"
                        signal_desc = "倒T回归VWAP买回平仓"
                        reasons.append(f"分时现价({current_price}元)回到VWAP附近模型观察位({target_p}元)，模型价格变动为 {curr_profit_pct:+.2f}%；这不代表实际成交或已兑现收益。")
                        self.tracker.close_position(symbol, "FINISHED", current_price, reasons[-1])
                    else:
                        signal_action = "HOLDING_T"
                        signal_desc = f"倒T模型信号观察中 (相对信号价变动: {curr_profit_pct:+.2f}%，未含交易成本)"

            elif status in ["FINISHED", "STOPPED"]:
                signal_action = "FINISHED_WATCH"
                signal_desc = "今日模型信号跟踪已结束"
                reasons.append("模型信号状态已结束；该记录不代表实际平仓或账户收益")

        else:
            # 4. 未开仓状态：研判是否触发新做T买点/卖点
            def _vol_confirm(vols):
                # 放量确认：最近一根分钟量 >= 近 reversal_bars 根均量 × 系数
                if not vol_confirm or vols is None or len(vols) < reversal_bars + 1:
                    return True
                recent = vols[-reversal_bars:]
                mean_v = float(np.mean(recent))
                if mean_v <= 0:
                    return True
                return float(vols[-1]) >= mean_v * vol_mult

            def reversal_up(closes, cur, vols):
                if len(closes) < reversal_bars + 1:
                    return False
                seg = closes[-(reversal_bars + 1):]
                if not (np.all(np.diff(seg) > 0) and cur >= seg[-1] - 1e-9):
                    return False
                return _vol_confirm(vols)

            def reversal_down(closes, cur, vols):
                if len(closes) < reversal_bars + 1:
                    return False
                seg = closes[-(reversal_bars + 1):]
                if not (np.all(np.diff(seg) < 0) and cur <= seg[-1] + 1e-9):
                    return False
                return _vol_confirm(vols)

            buy_reversal_confirmed = reversal_up(minute_closes, current_price, minute_volumes)
            sell_reversal_confirmed = reversal_down(minute_closes, current_price, minute_volumes)

            if not minute_data_ok:
                signal_desc = "分时数据不足或已过期·不生成新信号"
                reasons.append(f"需要至少{min_minute_bars}条有效分时数据，且最新数据延迟不超过{max_minute_age}分钟")
            elif not entry_window_ok:
                signal_desc = "避开开盘与尾盘时段·不生成新信号"
                reasons.append("为减少开盘和尾盘波动噪声，新T0信号只在09:45至14:30评估")
            elif suggested_shares < 100:
                signal_desc = "当日可卖底仓不足·不生成T0信号"
                reasons.append("需在自选持仓中填写已从券商核对的当日可卖股数，至少100股且不高于总持仓")
            elif current_price <= dyn_lower:
                trend_allows_buy = trend_direction == "BULL" or (not use_trend_filter and trend_direction != "NEUTRAL")
                if not trend_allows_buy:
                    signal_desc = "日线趋势未确认·跳过低吸信号"
                    reasons.append(f"低吸只在MA20、MA60多头且MA20上行时启用；当前趋势={trend_direction}")
                elif market_exposure is None or market_exposure < min_market_exposure:
                    # 正T(买入低吸)仅在正敞口市场启用，弱市不接下跌刀
                    signal_desc = "市场风险偏高·不生成正T低吸"
                    reasons.append(f"正T要求市场参考敞口至少{min_market_exposure:.0%}；当前={market_exposure if market_exposure is not None else '未知'}")
                elif vwap_slope_filter and vwap_slope_pct is not None and vwap_slope_pct < -max_vwap_slope_pct:
                    signal_desc = "分时均价单边下行·跳过正T低吸"
                    reasons.append(f"分时VWAP斜率{vwap_slope_pct:+.2f}%单边下行超过{max_vwap_slope_pct}%，逆势低吸风险高")
                elif not buy_reversal_confirmed:
                    signal_desc = "低吸观察位触发·等待分时反转确认"
                    reasons.append("价格触及VWAP下轨，但最近分时未确认向上反转；不在下跌加速时追买")
                else:
                    signal_action = "BUY_T"
                    signal_desc = "正T低吸研究信号·趋势与分时反转已确认"

                    stop_pct_used = stop_pct * late_stop_scale if is_late else stop_pct
                    suggested_stop = round_price_dynamic(current_price * (1.0 - stop_pct_used / 100.0), symbol)
                    suggested_target = round_price_dynamic(vwap * (1.0 + take_profit_vwap_pct / 100.0), symbol)
                    if is_late and suggested_target > current_price:
                        # 尾盘时间衰减：目标更贴近现价，便于在收盘前兑现
                        suggested_target = round_price_dynamic(
                            current_price + (suggested_target - current_price) * late_target_scale, symbol
                        )

                    active_dir = "BUY_T"
                    active_target = suggested_target
                    active_stop = suggested_stop
                    active_entry = current_price
                    active_shares = suggested_shares

                    reasons.append(
                        f"日线多头趋势；现价偏离VWAP {bias:.2f}%并触及自适应下轨 {dyn_lower}元，最近{reversal_bars}根分时K线转强。信号未验证成交。"
                    )
                    band_for_score = (vwap - dyn_lower) / vwap * 100.0 if vwap > 0 else 0.0
                    t0_score = self._t0_score(bias, band_for_score, trend_allows_buy, True, minute_bar_age_minutes, is_late)

                    self.tracker.open_position(
                        symbol=symbol, name=name, direction="BUY_T", price=current_price,
                        stop_price=suggested_stop, target_price=suggested_target, shares=active_shares
                    )

            elif current_price >= dyn_upper:
                is_extreme_pulse = bias >= bias_sell_thresh * 1.35
                trend_allows_sell = (
                    trend_direction == "BEAR"
                    or (trend_direction == "BULL" and is_extreme_pulse)
                    or (not use_trend_filter and trend_direction != "NEUTRAL")
                )
                if not trend_allows_sell:
                    signal_desc = "上行趋势未出现极端偏离·跳过倒T"
                    reasons.append("多头趋势中不做普通偏离高抛，避免过早卖出；仅观察极端偏离后的回落确认")
                elif market_exposure is None or market_exposure < min_market_exposure_sell:
                    # 倒T(高抛)属防御性操作，门槛远低于正T；仅在指数数据完全不可用时拦截
                    signal_desc = "指数数据不可用·不生成倒T信号"
                    reasons.append("无法确认市场状态时暂停生成新做T信号，待数据恢复后重新评估")
                elif vwap_slope_filter and vwap_slope_pct is not None and vwap_slope_pct > max_vwap_slope_pct:
                    signal_desc = "分时均价单边急涨·跳过倒T高抛"
                    reasons.append(f"分时VWAP斜率{vwap_slope_pct:+.2f}%单边急涨超过{max_vwap_slope_pct}%，逆势高抛易卖飞")
                elif not sell_reversal_confirmed:
                    signal_desc = "高抛观察位触发·等待分时回落确认"
                    reasons.append("价格触及VWAP上轨，但最近分时未确认向下反转；避免在强势加速时提前高抛")
                else:
                    signal_action = "SELL_T"
                    signal_desc = "倒T高抛研究信号·偏离与分时回落已确认"

                    stop_pct_used = stop_pct * late_stop_scale if is_late else stop_pct
                    suggested_stop = round_price_dynamic(current_price * (1.0 + stop_pct_used / 100.0), symbol)
                    suggested_target = round_price_dynamic(vwap * (1.0 - take_profit_vwap_pct / 100.0), symbol)
                    if is_late and suggested_target < current_price:
                        suggested_target = round_price_dynamic(
                            current_price - (current_price - suggested_target) * late_target_scale, symbol
                        )

                    active_dir = "SELL_T"
                    active_target = suggested_target
                    active_stop = suggested_stop
                    active_entry = current_price
                    active_shares = suggested_shares

                    reasons.append(
                        f"现价高于VWAP {bias:+.2f}%，触及自适应上轨 {dyn_upper}元且最近{reversal_bars}根分时K线转弱；信号未验证成交。"
                    )
                    band_for_score = (dyn_upper - vwap) / vwap * 100.0 if vwap > 0 else 0.0
                    t0_score = self._t0_score(bias, band_for_score, trend_allows_sell, True, minute_bar_age_minutes, is_late)

                    self.tracker.open_position(
                        symbol=symbol, name=name, direction="SELL_T", price=current_price,
                        stop_price=suggested_stop, target_price=suggested_target, shares=active_shares
                    )

        return self._result(
            symbol, name, current_price, vwap, bias, dyn_lower, dyn_upper,
            trend_direction, market_exposure, signal_action, signal_desc, reasons,
            active_dir, active_target, active_stop, active_entry, active_shares,
            locals().get("t0_score", 0)
        )

    def build_signal_payload(self, t0_res: Dict[str, Any]) -> Dict[str, Any]:
        """将日内做T研判结果转换为统一的通知卡片消息体 (字段语义清晰，不污染持仓字段)"""
        cur_p = float(t0_res.get("current_price", 0.0))
        entry_p = t0_res.get("entry_price")
        stop_p = t0_res.get("stop_price")
        target_p = t0_res.get("target_price")
        shares = t0_res.get("shares")
        sym = str(t0_res.get("symbol", ""))

        trade_levels = {}
        if entry_p is not None:
            trade_levels["entry_price"] = entry_p
        if stop_p is not None:
            trade_levels["initial_stop"] = stop_p
        if target_p is not None:
            trade_levels["target_price"] = target_p

        payload = {
            "symbol": sym,
            "name": t0_res.get("name", sym),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "price": cur_p,
            "signal_type": t0_res.get("signal_action", ""),
            "signal_desc": f"{t0_res.get('signal_desc', '')} · 研究信号，未验证成交，不构成委托",
            "score": 0,  # 保持 0：避免触发多因子评分卡片的错误渲染
            "t0_score": int(t0_res.get("t0_score", 0) or 0),
            "t0_strength": t0_res.get("t0_score", 0),
            "reasons": t0_res.get("reasons", []),
            "t0_direction": t0_res.get("t0_direction", ""),
            "close_action": t0_res.get("close_action", ""),
            "t0_entry_price": entry_p,
            "t0_shares": int(shares) if shares is not None else 0,
            "trade_levels": trade_levels
        }
        # 只有上游明确提供了真实持仓信息时才透传，避免把做T信号价误标为“持仓成本”
        if t0_res.get("holding_cost") is not None:
            payload["holding_cost"] = float(t0_res["holding_cost"])
        if t0_res.get("holding_shares") is not None:
            payload["holding_shares"] = int(t0_res["holding_shares"])
        # 兼容旧卡片读取 shares 做数量展示
        if shares is not None:
            payload["shares"] = int(shares)
        return payload

    def get_stats(self) -> Dict[str, Any]:
        """基于历史已平仓做T样本的绩效统计 (净收益含佣金/印花税/滑点估算，非实盘盈亏)。"""
        cfg = load_config()
        cost_model = {
            "commission_rate": float(cfg.get("t0_commission_rate", 0.00025)),
            "min_commission": float(cfg.get("t0_min_commission", 5.0)),
            "stamp_tax_rate": float(cfg.get("t0_stamp_tax_rate", 0.0005)),
            "slippage_rate": float(cfg.get("t0_slippage_rate", 0.0005)),
        }

        hist = self.tracker.load_history()
        closed = [
            t for t in hist
            if isinstance(t, dict) and t.get("status") in ("FINISHED", "STOPPED")
            and t.get("pnl_pct") is not None
        ]
        if not closed:
            return {"available": False, "total_trades": 0, "cost_model": cost_model, "note": "暂无已平仓做T样本"}

        def net_pnl(t):
            if t.get("net_pnl_pct") is not None:
                return float(t["net_pnl_pct"])
            return float(t.get("pnl_pct", 0.0) or 0.0)

        def gross_pnl(t):
            return float(t.get("pnl_pct", 0.0) or 0.0)

        def _agg(values):
            values = [float(v) for v in values]
            wins = [p for p in values if p > 0]
            losses = [p for p in values if p <= 0]
            total = len(values)
            pf = round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else None
            return {
                "win_rate": round(len(wins) / total * 100.0, 2) if total else 0.0,
                "avg_pnl_pct": round(float(np.mean(values)), 2),
                "avg_win_pct": round(float(np.mean(wins)), 2) if wins else 0.0,
                "avg_loss_pct": round(float(np.mean(losses)), 2) if losses else 0.0,
                "expectancy_pct": round(float(np.mean(values)), 2),
                "profit_factor": pf,
                "max_loss_pct": round(float(np.min(values)), 2),
                "max_win_pct": round(float(np.max(values)), 2),
            }

        net_agg = _agg([net_pnl(t) for t in closed])
        gross_agg = _agg([gross_pnl(t) for t in closed])
        total = len(closed)

        def _dir_stats(direction):
            subset = [t for t in closed if t.get("direction") == direction]
            if not subset:
                return None
            a = _agg([net_pnl(t) for t in subset])
            a["trades"] = len(subset)
            return a

        result = {
            "available": True,
            "total_trades": total,
            "finished_count": sum(1 for t in closed if t.get("status") == "FINISHED"),
            "stopped_count": sum(1 for t in closed if t.get("status") == "STOPPED"),
            "cost_model": cost_model,
            "buy_t": _dir_stats("BUY_T"),
            "sell_t": _dir_stats("SELL_T"),
            "note": "净收益含佣金/印花税/滑点估算；非实盘盈亏",
        }
        # 顶层字段 = 净口径 (供复盘报告与接口直接消费)
        result.update(net_agg)
        result["gross"] = gross_agg
        return result

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
                # 模型信号状态没有真实成交记录；尾盘只形成研究提醒，不代表已买入或应卖出。
                diff_pct = round((cur_p - entry_p) / entry_p * 100.0, 2) if entry_p > 0 else 0.0
                diff_amt = round((cur_p - entry_p) * shares, 2) if entry_p > 0 else 0.0

                sig = {
                    "symbol": sym,
                    "name": name,
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "price": cur_p,
                    "signal_type": "EOD_FORCE_CLOSE_T",
                    "signal_desc": "⚠️【正T尾盘风险提醒】模型信号未闭环·请核对实际账户",
                    "score": 0,
                    "holding_cost": entry_p,
                    "holding_shares": shares,
                    "pnl_pct": diff_pct,
                    "pnl_amt": diff_amt,
                    "reasons": [
                        f"模型于 {pos.get('entry_time', '盘中')} 记录了低吸信号参考价 {entry_p}元；未核验实际买入成交。",
                        f"参考现价 {cur_p}元，相对信号价变动 {diff_pct:+.2f}%（参考金额 {diff_amt:+.0f}元，非账户盈亏）。",
                        "A股可卖数量受 T+1 与账户实际成交约束；请核对持仓和委托状态，系统未提交订单。"
                    ],
                    "trade_levels": {
                        "entry_price": entry_p,
                        "suggested_sell_price": cur_p,
                        "shares": shares,
                        "action_urgency": "不适用：仅为研究提醒，非委托时限"
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
                    "score": 0,
                    "holding_cost": entry_p,
                    "holding_shares": shares,
                    "pnl_pct": diff_pct,
                    "pnl_amt": diff_amt,
                    "reasons": [
                        f"模型于 {pos.get('entry_time', '盘中')} 记录了高抛信号参考价 {entry_p}元；未核验实际卖出成交。",
                        f"现价 {cur_p}元，相对信号价变动 {diff_pct:+.2f}%（参考金额 {diff_amt:+.0f}元，非账户盈亏）。",
                        "是否恢复底仓需结合实际成交、可买数量和风险承受能力判断；系统未提交订单。"
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
