"""
A股智能量化系统 2.0 - 高胜率日内做T智能引擎 (T0SmartEngine 2.0)
专为 A 股 T+1 底仓量化设计：
1. 动态日内成交均价 (VWAP) 与自适应布林分时通道 (Dynamic Bollinger Band);
2. 冲高回落动态浮动对冲止盈 (Trailing Take-Profit Protection);
3. 严格单日单向唯一锁定 (多头只做正T，空头只做倒T，绝不多空互搏);
4. 单边跳水防飞刀熔断与止损后全天禁开正T (破位超跌反弹例外);
5. 单日做T频次硬约束与冷却期机制 (支持多波段高确定性T+0);
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
            sym_lower = str(symbol).lower()
            is_etf = any(sym_lower.startswith(p) for p in ["sh5", "sz15", "sz16", "sh51", "sh56", "sh58", "sh50", "sz18"])
            
            if is_etf:
                # 场内ETF免征印花税，多数券商ETF佣金免除最低5元限制
                stamp_amt = 0.0
                comm_entry = entry_amt * comm_r
                comm_exit = exit_amt * comm_r
            else:
                comm_entry = max(min_comm, entry_amt * comm_r)
                comm_exit = max(min_comm, exit_amt * comm_r)
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
            self._append_history(pos.copy())

    def reset_today(self):
        with self._lock:
            self._trades = {}
            self._save()

    def get_statistics(self, window_days: Optional[int] = None) -> Dict[str, Any]:
        """按实际结算口径统计做T历史绩效"""
        history = self.load_history()
        cfg = load_config()
        cost_model = {
            "commission_rate": float(cfg.get("t0_commission_rate", 0.00025)),
            "min_commission": float(cfg.get("t0_min_commission", 5.0)),
            "stamp_tax_rate": float(cfg.get("t0_stamp_tax_rate", 0.0005)),
            "slippage_rate": float(cfg.get("t0_slippage_rate", 0.0005)),
        }
        if not history:
            return {
                "sample_size": 0, "win_rate": None, "profit_loss_ratio": None,
                "cost_model": cost_model, "note": "暂无做T历史平仓记录"
            }

        closed = [t for t in history if t.get("status") in ("FINISHED", "STOPPED")]
        if not closed:
            return {
                "sample_size": 0, "win_rate": None, "profit_loss_ratio": None,
                "cost_model": cost_model, "note": "暂无已平仓做T样本"
            }

        def _calc_stats(trades: List[Dict[str, Any]], field: str = "net_pnl_pct", amt_field: str = "net_pnl_amt"):
            if not trades:
                return {"sample_size": 0, "win_rate": None, "profit_loss_ratio": None, "avg_return_pct": None, "total_pnl_amt": 0.0}
            pnls = [float(t.get(field, t.get("pnl_pct", 0.0)) or 0.0) for t in trades]
            amts = [float(t.get(amt_field, t.get("gross_pnl_amt", 0.0)) or 0.0) for t in trades]
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p < 0]
            n = len(pnls)
            wr = round(len(wins) / n * 100.0, 1) if n > 0 else None
            avg_win = float(np.mean(wins)) if wins else 0.0
            avg_loss = float(np.mean(losses)) if losses else 0.0
            plr = round(abs(avg_win / avg_loss), 2) if avg_loss != 0 else None
            return {
                "sample_size": n,
                "win_count": len(wins),
                "loss_count": len(losses),
                "win_rate": wr,
                "profit_loss_ratio": plr,
                "avg_return_pct": round(float(np.mean(pnls)), 2) if pnls else None,
                "total_pnl_amt": round(float(sum(amts)), 2)
            }

        net_agg = _calc_stats(closed, field="net_pnl_pct", amt_field="net_pnl_amt")
        gross_agg = _calc_stats(closed, field="pnl_pct", amt_field="gross_pnl_amt")

        def _dir_stats(d):
            sub = [t for t in closed if t.get("direction") == d]
            return _calc_stats(sub, field="net_pnl_pct", amt_field="net_pnl_amt")

        result = {
            "total_samples": len(closed),
            "finished_count": sum(1 for t in closed if t.get("status") == "FINISHED"),
            "stopped_count": sum(1 for t in closed if t.get("status") == "STOPPED"),
            "cost_model": cost_model,
            "buy_t": _dir_stats("BUY_T"),
            "sell_t": _dir_stats("SELL_T"),
            "note": "净收益含佣金/印花税/滑点估算；非实盘盈亏",
        }
        result.update(net_agg)
        result["gross"] = gross_agg
        return result


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
    def _signal_cooldown_ok(pos: Dict[str, Any], cooldown_minutes: int = 20) -> bool:
        """
        信号观察模式的冷却判定：距上一次信号结束(或开始)是否已超过冷却分钟数。
        已结束的信号冷却后允许重新研判新波段机会，避免全天锁死。
        """
        raw = str(pos.get("exit_time") or pos.get("entry_time") or "")
        try:
            parts = [int(x) for x in raw.split(":")]
            hh, mm = parts[0], parts[1]
            ss = parts[2] if len(parts) > 2 else 0
            now = datetime.now()
            end_dt = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
            return (now - end_dt).total_seconds() / 60.0 >= max(1, int(cooldown_minutes))
        except (ValueError, TypeError, IndexError):
            return True

    @staticmethod
    def _close_action(action: str, direction: str) -> str:
        if action == "CLOSE_T":
            return "BUY_BACK" if direction == "SELL_T" else "SELL_HEDGE"
        if action == "STOP_T":
            return "BUY_BACK_STOP" if direction == "SELL_T" else "SELL_STOP"
        return ""

    @staticmethod
    def _t0_score(bias_pct: float, band_pct: float, trend_aligned: bool,
                  reversal: bool, minute_age: Optional[float], is_late: bool, is_oversold: bool = False) -> int:
        """做T信号量化置信强度得分 (0-99分)"""
        score = 52.0
        if band_pct and band_pct > 0:
            depth = max(0.0, abs(bias_pct) - band_pct)
            score += min(20.0, depth * 15.0)          # 偏离带宽越深，反弹/回踩势能越大
        if trend_aligned:
            score += 15.0
        if is_oversold:
            score += 10.0
        if reversal:
            score += 10.0
        if minute_age is not None and minute_age <= 2.0:
            score += 5.0
        if is_late:
            score -= 10.0
        return int(round(min(99.0, max(10.0, score))))

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
            "score": score,
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
        bias_buy_thresh = float(cfg.get("t0_bias_buy", -1.2))
        bias_sell_thresh = float(cfg.get("t0_bias_sell", 2.5))
        require_dual_resonance = bool(cfg.get("t0_require_dual_resonance", True))
        golden_window_only = bool(cfg.get("t0_golden_window_only", True))
        avoid_early_morning_short = bool(cfg.get("t0_avoid_early_morning_short", True))
        stop_pct = float(cfg.get("t0_stop_loss_pct", 1.5))
        take_profit_pct = float(cfg.get("t0_take_profit_pct", 1.5))
        trailing_pct = float(cfg.get("t0_trailing_profit_pct", 0.5))
        take_profit_vwap_pct = float(cfg.get("t0_take_profit_vwap_pct", 0.3))
        use_trend_filter = cfg.get("t0_trend_filter_enabled", True)
        use_dynamic_boll = cfg.get("t0_use_dynamic_boll", True)
        band_dev_k = float(cfg.get("t0_band_dev_k", 1.0))
        band_min_pct = float(cfg.get("t0_band_min_pct", 0.6))
        band_max_pct = float(cfg.get("t0_band_max_pct", 2.8))
        sell_asymmetry = float(cfg.get("t0_sell_asymmetry", 1.1))
        reversal_bars = max(1, int(cfg.get("t0_reversal_bars", 1)))
        vol_confirm = bool(cfg.get("t0_reversal_volume_confirm", False))
        vol_mult = float(cfg.get("t0_reversal_volume_mult", 1.0))
        vwap_slope_filter = bool(cfg.get("t0_vwap_slope_filter", False))
        vwap_slope_lookback = max(5, int(cfg.get("t0_vwap_slope_lookback", 15)))
        max_vwap_slope_pct = float(cfg.get("t0_max_vwap_slope_pct", 1.2))
        time_decay_enabled = bool(cfg.get("t0_time_decay_enabled", True))
        late_window_start = cfg.get("t0_late_window_start", "14:00")
        late_target_scale = float(cfg.get("t0_late_target_scale", 0.6))
        late_stop_scale = float(cfg.get("t0_late_stop_scale", 0.8))
        max_minute_age = max(1, int(cfg.get("t0_minute_max_age_minutes", 10)))
        signal_cooldown_minutes = max(1, int(cfg.get("t0_signal_cooldown_minutes", 20)))

        holding_shares = max(0, int(holding_shares or 0))
        if available_shares <= 0 and holding_shares > 0:
            available_shares = holding_shares
        available_shares = min(max(0, int(available_shares or 0)), holding_shares)

        # 针对 ETF / 宽基低波动品种自适应调整偏离阈值 (ETF天然波动率较小，日内罕见 -1.5% 偏离)
        sym_lower = str(symbol).lower()
        is_etf = any(sym_lower.startswith(p) for p in ["sh5", "sz15", "sz16", "sh51", "sh56", "sh58", "sh50", "sz18"])
        if is_etf:
            bias_buy_thresh = round(bias_buy_thresh * 0.6, 2)
            bias_sell_thresh = round(bias_sell_thresh * 0.6, 2)
            take_profit_pct = round(take_profit_pct * 0.6, 2)
            stop_pct = round(stop_pct * 0.6, 2)
            band_min_pct = round(band_min_pct * 0.6, 2)
            band_max_pct = round(band_max_pct * 0.65, 2)

        # ---- 交易时段守卫：仅在未传入 df_minute 时拦截非交易时段 ----
        if df_minute is None and not self.is_trading_session():
            return self._result(
                symbol, name, current_price, current_price, 0.0,
                current_price, current_price, "NEUTRAL", market_exposure,
                "HOLD_T", "非交易时段·暂停做T研判",
                ["当前非A股连续竞价交易时段，暂停生成或更新做T信号"],
                shares=0, score=0
            )

        now = datetime.now()
        current_hm = now.strftime("%H:%M")
        # 09:31 - 10:00 开盘脉冲期允许最少 3 根分时数据参与计算，捕捉黄金脉冲期
        required_bars = 3 if current_hm < "10:00" else 10

        # 1. 获取分时数据与计算 VWAP 偏离及自适应布林带
        vwap = current_price
        bias = 0.0
        dyn_lower = round_price_dynamic(current_price * (1.0 + bias_buy_thresh / 100.0), symbol)
        dyn_upper = round_price_dynamic(current_price * (1.0 + bias_sell_thresh / 100.0), symbol)
        minute_data_ok = False
        minute_bar_age_minutes = 0.0
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

            if len(minute_frame) > 0:
                last_row = minute_frame.iloc[-1]
                vwap = float(last_row["vwap"])
                bias = round((current_price - vwap) / vwap * 100.0, 2)
                minute_closes = minute_frame["price"].to_numpy(dtype=float)
                minute_data_ok = True

                try:
                    last_bar_time = datetime.strptime(str(last_row["time"])[:5], "%H:%M").time()
                    bar_datetime = datetime.combine(now.date(), last_bar_time)
                    minute_bar_age_minutes = (now - bar_datetime).total_seconds() / 60.0
                except (TypeError, ValueError):
                    minute_bar_age_minutes = 0.0

                if "volume" in minute_frame.columns:
                    vol_series = pd.to_numeric(minute_frame["volume"], errors="coerce").ffill()
                    minute_volumes = vol_series.diff().to_numpy(dtype=float)
                    minute_volumes = minute_volumes[np.isfinite(minute_volumes)]

                if len(minute_frame) >= 10:
                    vwap_series = pd.to_numeric(minute_frame["vwap"], errors="coerce").dropna().to_numpy(dtype=float)
                    if len(vwap_series) >= 6:
                        base_v = float(vwap_series[-6])
                        if base_v > 0:
                            vwap_slope_pct = round((float(vwap_series[-1]) - base_v) / base_v * 100.0, 3)

                if use_dynamic_boll and vwap > 0:
                    recent_prices = minute_closes[-min(len(minute_closes), 30):]
                    std_intraday = float(np.std(recent_prices) / vwap * 100.0) if len(recent_prices) > 1 else 1.0
                    band_offset = float(np.clip(std_intraday * band_dev_k, band_min_pct, band_max_pct))
                    dyn_lower = round_price_dynamic(vwap * (1.0 - band_offset / 100.0), symbol)
                    dyn_upper = round_price_dynamic(vwap * (1.0 + band_offset * sell_asymmetry / 100.0), symbol)
        elif df_minute is not None and len(df_minute) > 0:
            # 兼容精简 minute 数据
            last_p = float(df_minute["price"].iloc[-1])
            vwap = float(df_minute.get("vwap", pd.Series([last_p])).iloc[-1])
            bias = round((current_price - vwap) / vwap * 100.0, 2) if vwap > 0 else 0.0
            minute_data_ok = True
            minute_closes = pd.to_numeric(df_minute["price"], errors="coerce").dropna().to_numpy()

        # 2. 检查大趋势通道 (顺势定调)
        trend_direction = "NEUTRAL"
        if daily_kline is not None and "close" in daily_kline and len(daily_kline) >= 20:
            daily_source = daily_kline
            if "date" in daily_source.columns and len(daily_source) > 0:
                last_daily_date = str(daily_source["date"].iloc[-1])[:10]
                if last_daily_date == now.strftime("%Y-%m-%d"):
                    daily_source = daily_source.iloc[:-1]
            daily_close = pd.to_numeric(daily_source["close"], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
            if len(daily_close) >= 20:
                ma20 = float(daily_close.rolling(20, min_periods=20).mean().iloc[-1])
                prev_close = float(daily_close.iloc[-1])
                # 无论昨日收盘还是当前现价，只要在MA20之上均可视为偏多环境
                if prev_close >= ma20 or current_price >= ma20:
                    trend_direction = "BULL"
                else:
                    trend_direction = "BEAR"

        # 3. 检查现有持仓状态跟踪与冷却期解除
        # 检索当日同向止损风控熔断历史与单日做T频次统计
        use_circuit_breaker = bool(cfg.get("t0_daily_stop_circuit_breaker", True))
        max_daily_trades = max(1, int(cfg.get("t0_max_trades_per_day", 1)))
        today_str = now.strftime("%Y-%m-%d")
        hist = self.tracker.load_history()
        today_symbol_trades = [
            h for h in hist
            if h.get("symbol") == symbol and h.get("date") == today_str and h.get("status") in ["FINISHED", "STOPPED"]
        ]
        today_trade_count = len(today_symbol_trades)
        today_stopped_dirs = set()
        if use_circuit_breaker:
            today_stopped_dirs = {h.get("direction") for h in today_symbol_trades if h.get("status") == "STOPPED"}

        existing_pos = self.tracker.get_position(symbol)
        if (existing_pos is not None
                and existing_pos.get("status") in ["FINISHED", "STOPPED"]
                and self._signal_cooldown_ok(existing_pos, signal_cooldown_minutes)):
            existing_pos = None

        signal_action = "HOLD_T"
        signal_desc = "震荡观望"
        reasons = []

        active_dir = ""
        active_target = current_price
        active_stop = current_price
        active_entry = current_price
        available_lot = (available_shares // 100) * 100
        suggested_shares = available_lot if available_lot >= 100 else 100
        active_shares = suggested_shares
        entry_window_ok = ("09:31" <= current_hm <= "14:50") or (df_minute is not None and not self.is_trading_session())
        if golden_window_only and existing_pos is None and ("11:00" <= current_hm <= "11:30"):
            entry_window_ok = False
        is_late = time_decay_enabled and current_hm >= late_window_start

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

                    # 1. 触发防守硬止损
                    if round(current_price, 3) <= round(stop_p, 3) + 1e-4:
                        signal_action = "STOP_T"
                        signal_desc = "正T防守止损对冲平仓"
                        reasons.append(f"跌破正T防守底线 {stop_p}元(-{stop_pct}%)，建议立即卖出底仓止损保护！")
                        self.tracker.close_position(symbol, "STOPPED", current_price, reasons[-1])
                    # 2. 冲高回落动态浮动对冲止盈
                    elif peak_profit_pct >= take_profit_pct and (peak_profit_pct - curr_profit_pct) >= trailing_pct:
                        signal_action = "CLOSE_T"
                        signal_desc = "正T冲高回落对冲平仓"
                        reasons.append(f"日内冲高至 {peak_p}元(+{peak_profit_pct:.2f}%)后回落{trailing_pct}%，建议高抛卖出等量底仓，动态锁定做T利润！")
                        self.tracker.close_position(symbol, "FINISHED", current_price, reasons[-1])
                    # 3. 达到静态目标止盈
                    elif round(current_price, 3) >= round(target_p, 3) - 1e-4:
                        signal_action = "CLOSE_T"
                        signal_desc = "正T冲高卖出对冲平仓"
                        reasons.append(f"已达到日内正T预期对冲目标 {target_p}元(+{curr_profit_pct:.2f}%)，建议高抛卖出等量底仓（{pos_shares}股）锁定差价！")
                        self.tracker.close_position(symbol, "FINISHED", current_price, reasons[-1])
                    else:
                        signal_action = "HOLDING_T"
                        signal_desc = f"正T持仓中 (现浮动: {curr_profit_pct:+.2f}%)"

                elif direction == "SELL_T":
                    curr_profit_pct = (entry_p - current_price) / entry_p * 100.0 if entry_p > 0 else 0.0
                    peak_profit_pct = (entry_p - peak_p) / entry_p * 100.0 if entry_p > 0 else 0.0

                    # 1. 突破防守线止损买回
                    if round(current_price, 3) >= round(stop_p, 3) - 1e-4:
                        signal_action = "STOP_T"
                        signal_desc = "倒T反抽止损买回归位"
                        reasons.append(f"价格反抽冲破防守线 {stop_p}元，为防底仓抛飞，建议立即以市价买回等量底仓（{pos_shares}股）止损对冲！")
                        self.tracker.close_position(symbol, "STOPPED", current_price, reasons[-1])
                    # 2. 探底回升动态止盈
                    elif peak_profit_pct >= take_profit_pct and (peak_profit_pct - curr_profit_pct) >= trailing_pct:
                        signal_action = "CLOSE_T"
                        signal_desc = "倒T探底回升对冲平仓"
                        reasons.append(f"日内下探至 {peak_p}元(+{peak_profit_pct:.2f}%)后回升{trailing_pct}%，建议立即买回等量底仓，锁定做T利润！")
                        self.tracker.close_position(symbol, "FINISHED", current_price, reasons[-1])
                    # 3. 回踩目标价平仓买回
                    elif round(current_price, 3) <= round(target_p, 3) + 1e-4:
                        signal_action = "CLOSE_T"
                        signal_desc = "倒T回踩低吸买入平仓"
                        reasons.append(f"分时现价({current_price}元)回踩触达日内低吸目标价({target_p}元)！已兑现差价收益 +{curr_profit_pct:.2f}%，建议买回等量底仓（{pos_shares}股）收复筹码！")
                        self.tracker.close_position(symbol, "FINISHED", current_price, reasons[-1])
                    else:
                        signal_action = "HOLDING_T"
                        signal_desc = f"倒T持仓中 (现浮动: {curr_profit_pct:+.2f}%)"

            elif status in ["FINISHED", "STOPPED"]:
                signal_action = "FINISHED_WATCH"
                signal_desc = "今日做T战果已锁定·冷却观望"
                reasons.append("当前做T波段已完结平仓，系统进入冷却保护，防范频繁交易磨损")

        else:
            # 4. 未开仓状态：研判是否触发新做T买点/卖点
            def _vol_confirm(vols):
                if not vol_confirm or vols is None or len(vols) < 3:
                    return True
                mean_v = float(np.mean(vols[-3:]))
                return mean_v <= 0 or float(vols[-1]) >= mean_v * vol_mult

            def reversal_up(closes, cur):
                if len(closes) < 2:
                    return True
                seg = closes[-min(len(closes), reversal_bars + 1):]
                # 见底止跌回升：当前价高于这几根K线的局部最低点
                return (cur >= float(np.min(seg)) - 1e-4) and (cur > closes[-1] * 0.999 or seg[-1] >= seg[0])

            def reversal_down(closes, cur):
                if len(closes) < 2:
                    return True
                seg = closes[-min(len(closes), reversal_bars + 1):]
                # 冲高滞涨回落：当前价低于这几根K线的局部最高点
                return (cur <= float(np.max(seg)) + 1e-4) and (cur < closes[-1] * 1.001 or seg[-1] <= seg[0])

            buy_reversal_confirmed = reversal_up(minute_closes, current_price) and _vol_confirm(minute_volumes)
            sell_reversal_confirmed = reversal_down(minute_closes, current_price) and _vol_confirm(minute_volumes)

            if not minute_data_ok and df_minute is None:
                signal_desc = "分时数据不足或延迟·观望"
                reasons.append("需要有效分时数据以精确计算VWAP均价通道")
            elif not entry_window_ok:
                signal_desc = "垃圾时段防守·暂停新开仓" if golden_window_only and "11:00" <= current_hm <= "11:30" else "非开仓时段·暂停新信号"
                reasons.append("11:00-11:30属于午盘流动性骤降与假突破高发期，系统主动规避以确保做T高胜率" if golden_window_only and "11:00" <= current_hm <= "11:30" else "做T开仓仅在 09:31 至 14:50 执行")
            elif today_trade_count >= max_daily_trades:
                signal_desc = f"达单日做T上限({max_daily_trades}次)·观望"
                reasons.append(f"该标的今日已完成 {today_trade_count} 笔做T，已达单日频次上限({max_daily_trades}次)，防范过度交易磨损")
            elif (bias <= bias_buy_thresh and current_price <= dyn_lower) if require_dual_resonance else (bias <= bias_buy_thresh or current_price <= dyn_lower):
                # 正T买入判定：优先多头；若空头但偏离达超跌线，允许做超跌反抽
                is_oversold_bounce = (bias <= bias_buy_thresh * 1.35)
                trend_allows_buy = (trend_direction == "BULL") or is_oversold_bounce or (not use_trend_filter)

                if "BUY_T" in today_stopped_dirs:
                    signal_desc = "今日正T已止损·触发风控熔断"
                    reasons.append("该标的今日此前已触发正T防守止损，表明处于强单边下泄行情，日内暂停同向做T防范二次磨损")
                elif not trend_allows_buy:
                    signal_desc = "空头趋势压制·严禁接飞刀"
                    reasons.append(f"标的处于空头通道且负偏离({bias:.2f}%)未达恐慌超跌线({bias_buy_thresh*1.35:.2f}%)，防范单边阴跌")
                elif vwap_slope_filter and vwap_slope_pct is not None and vwap_slope_pct < -max_vwap_slope_pct and not is_oversold_bounce:
                    signal_desc = "分时VWAP单边下泄·跳过低吸"
                    reasons.append(f"分时VWAP斜率{vwap_slope_pct:+.2f}%单边急挫，逆势低吸风险高")
                elif not buy_reversal_confirmed:
                    signal_desc = "触及低吸下轨·等待分时企稳"
                    reasons.append("价格触及动态下轨，等待分时止跌转强反转确认")
                else:
                    signal_action = "BUY_T"
                    if is_oversold_bounce:
                        signal_desc = "🌟 破位超跌反弹·正T急跌低吸"
                        reasons.append(f"日内急跌出现极端负偏离({bias:.2f}%)，触发强力超跌反抽买点，轻仓快进快出做正T")
                    else:
                        signal_desc = "🌟 正T低吸机会触发"
                        reasons.append(f"现价偏离分时均价({vwap}元)达 {bias:.2f}%，触及动态布林下轨({dyn_lower}元)，具备日内反弹差价")

                    suggested_stop = round_price_dynamic(current_price * (1.0 - stop_pct / 100.0), symbol)
                    # 止盈目标：回归VWAP或至少保证足够的利润空间(cover交易摩擦)
                    suggested_target = round_price_dynamic(max(vwap * (1.0 + take_profit_vwap_pct / 100.0), current_price * (1.0 + take_profit_pct / 100.0)), symbol)
                    if is_late and suggested_target > current_price:
                        suggested_target = round_price_dynamic(current_price + (suggested_target - current_price) * late_target_scale, symbol)

                    active_dir = "BUY_T"
                    active_target = suggested_target
                    active_stop = suggested_stop
                    active_entry = current_price
                    active_shares = suggested_shares

                    t0_score = self._t0_score(bias, (vwap - dyn_lower)/vwap*100.0 if vwap > 0 else 0.0, trend_direction == "BULL", True, minute_bar_age_minutes, is_late, is_oversold_bounce)

                    self.tracker.open_position(
                        symbol=symbol, name=name, direction="BUY_T", price=current_price,
                        stop_price=suggested_stop, target_price=suggested_target, shares=active_shares
                    )

            elif (bias >= bias_sell_thresh and current_price >= dyn_upper) if require_dual_resonance else (bias >= bias_sell_thresh or current_price >= dyn_upper):
                # 倒T高抛判定：空头或震荡优先；多头通道中仅在极端脉冲时允许防抛飞
                is_extreme_pulse = (bias >= bias_sell_thresh * 1.25)
                trend_allows_sell = (trend_direction == "BEAR") or is_extreme_pulse or (not use_trend_filter)
                early_short_blocked = (avoid_early_morning_short and current_hm <= "09:45" and bias < (1.7 if is_etf else 2.8))

                if "SELL_T" in today_stopped_dirs:
                    signal_desc = "今日倒T已止损·触发风控熔断"
                    reasons.append("该标的今日此前已触发倒T防守止损，表明处于强单边主升行情，日内暂停同向做T防范再次卖飞")
                elif early_short_blocked:
                    signal_desc = "早盘抢筹脉冲·防倒T卖飞保护"
                    reasons.append(f"09:45前属于早盘竞价发现与主力抢筹脉冲期，现价涨幅偏离(+{bias:.2f}%)未达极值({1.7 if is_etf else 2.8}%)，暂不开倒T以防底仓卖飞")
                elif not trend_allows_sell:
                    signal_desc = "多头主升·严禁倒T防抛飞"
                    reasons.append("标的高处于多头主升通道，倒T高抛极易卖飞强势核心牛股，仅在极端偏离脉冲时高抛")
                elif vwap_slope_filter and vwap_slope_pct is not None and vwap_slope_pct > max_vwap_slope_pct:
                    signal_desc = "分时VWAP单边急涨·跳过倒T"
                    reasons.append(f"分时VWAP斜率{vwap_slope_pct:+.2f}%单边急涨，防范过早减仓卖飞")
                elif not sell_reversal_confirmed:
                    signal_desc = "触及高抛上轨·等待分时滞涨"
                    reasons.append("现价触及动态上轨，等待分时滞涨回落确认")
                else:
                    signal_action = "SELL_T"
                    signal_desc = "⚡ 倒T高抛机会触发"
                    reasons.append(f"现价冲高偏离分时均价({vwap}元)达 +{bias:.2f}%，触及动态布林上轨({dyn_upper}元)，适合高抛后低吸回补")

                    suggested_stop = round_price_dynamic(current_price * (1.0 + stop_pct / 100.0), symbol)
                    suggested_target = round_price_dynamic(min(vwap * (1.0 - take_profit_vwap_pct / 100.0), current_price * (1.0 - take_profit_pct / 100.0)), symbol)
                    if is_late and suggested_target < current_price:
                        suggested_target = round_price_dynamic(current_price - (current_price - suggested_target) * late_target_scale, symbol)

                    active_dir = "SELL_T"
                    active_target = suggested_target
                    active_stop = suggested_stop
                    active_entry = current_price
                    active_shares = suggested_shares

                    t0_score = self._t0_score(bias, (dyn_upper - vwap)/vwap*100.0 if vwap > 0 else 0.0, trend_direction == "BEAR", True, minute_bar_age_minutes, is_late, is_extreme_pulse)

                    self.tracker.open_position(
                        symbol=symbol, name=name, direction="SELL_T", price=current_price,
                        stop_price=suggested_stop, target_price=suggested_target, shares=active_shares
                    )

        calc_score = locals().get("t0_score", 0)
        return self._result(
            symbol, name, current_price, vwap, bias, dyn_lower, dyn_upper,
            trend_direction, market_exposure, signal_action, signal_desc, reasons,
            active_dir, active_target, active_stop, active_entry, active_shares,
            calc_score
        )

    def build_signal_payload(self, t0_res: Dict[str, Any]) -> Dict[str, Any]:
        """将日内做T研判结果转换为统一的通知卡片消息体"""
        cur_p = float(t0_res.get("current_price", 0.0))
        entry_p = t0_res.get("entry_price") or cur_p
        stop_p = t0_res.get("stop_price") or cur_p * 0.985
        target_p = t0_res.get("target_price") or cur_p * 1.015
        shares = int(t0_res.get("shares") or 100)
        sym = str(t0_res.get("symbol", ""))

        t0_score = int(t0_res.get("t0_score", 0) or t0_res.get("score", 0) or 85)

        trade_levels = {
            "entry_price": entry_p,
            "initial_stop": stop_p,
            "target_price": target_p
        }

        return {
            "symbol": sym,
            "name": t0_res.get("name", sym),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "price": cur_p,
            "signal_type": t0_res.get("signal_action", ""),
            "signal_desc": t0_res.get("signal_desc", ""),
            "score": t0_score,
            "t0_score": t0_score,
            "t0_strength": t0_score,
            "reasons": t0_res.get("reasons", []),
            "t0_direction": t0_res.get("t0_direction", ""),
            "close_action": t0_res.get("close_action", ""),
            "holding_shares": shares,
            "holding_cost": entry_p,
            "trade_levels": trade_levels
        }

    def check_eod_unclosed_positions(self) -> List[Dict[str, Any]]:
        """
        尾盘时段 (14:48 - 14:55) 集中安全审查
        对冲所有日内已开仓、但直到收盘前仍未闭环的做T头寸，严格防止做T被动演变为加仓
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
