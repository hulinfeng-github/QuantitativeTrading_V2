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
import threading
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional, List, Tuple
import pandas as pd
import numpy as np
from config import T0_TRADES_FILE, T0_HISTORY_FILE, calc_round_trip_fees, get_trading_fee_params, load_config
from core.data_engine import data_engine
from core.persistence import atomic_write_json, safe_read_json

_T0_REGIME_SCALE = {
    "BEAR":    {"buy": 1.78, "sell": 0.77, "tp": 0.70},
    "BULL":    {"buy": 0.83, "sell": 1.80, "tp": 1.20},
    "REBOUND": {"buy": 0.83, "sell": 1.45, "tp": 1.00},
    "NEUTRAL": {"buy": 1.00, "sell": 1.00, "tp": 0.90},
}

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

def evaluate_market_regime(
    daily_kline: Optional[pd.DataFrame],
    current_price: float,
    symbol: str = "",
    as_of_date: Optional[str] = None,
    market_exposure: Optional[float] = None
) -> Tuple[str, float, str]:
    """
    严谨多因子趋势通道与牛熊市状态识别 (双均线+斜率+迟滞滤波防频繁切换)
    返回: (regime: 'BULL' | 'BEAR' | 'NEUTRAL', ma20: float, reason: str)
    """
    if daily_kline is None or len(daily_kline) < 20 or "close" not in daily_kline.columns:
        return "NEUTRAL", current_price, "日K线样本不足20天，中立平衡判定"
        
    df_source = daily_kline.copy()
    if "date" in df_source.columns and len(df_source) > 0:
        if as_of_date:
            df_source = df_source[df_source["date"].astype(str).str[:10] < as_of_date]
        else:
            today_s = datetime.now().strftime("%Y-%m-%d")
            last_d = str(df_source["date"].iloc[-1])[:10]
            if last_d == today_s:
                df_source = df_source.iloc[:-1]
                
    closes = pd.to_numeric(df_source["close"], errors="coerce").dropna().to_numpy()
    if len(closes) < 20:
        return "NEUTRAL", current_price, "有效收盘价样本不足20天"
        
    ma5 = float(np.mean(closes[-5:]))
    ma20 = float(np.mean(closes[-20:]))
    ma5_prev2 = float(np.mean(closes[-7:-2])) if len(closes) >= 7 else ma5
    ma20_prev3 = float(np.mean(closes[-23:-3])) if len(closes) >= 23 else ma20
    ma20_slope_pct = (ma20 - ma20_prev3) / ma20_prev3 * 100.0 if ma20_prev3 > 0 else 0.0
    ma5_slope_pct = (ma5 - ma5_prev2) / ma5_prev2 * 100.0 if ma5_prev2 > 0 else 0.0

    sym_lower = str(symbol).lower()
    is_etf = any(sym_lower.startswith(p) for p in ["sh5", "sz15", "sz16", "sh51", "sh56", "sh58", "sh50", "sz18"])

    # 迟滞缓冲区间：个股±1.2%，ETF±0.8%，防止在均线附近0.01元频繁假突破横跳
    buffer_pct = 0.8 if is_etf else 1.2
    upper_band = ma20 * (1.0 + buffer_pct / 100.0)
    lower_band = ma20 * (1.0 - buffer_pct / 100.0)

    p = float(current_price)

    # 1. 严谨牛市/多头主升通道 (BULL)：
    # 价格突破MA20缓冲上轨且MA20走平或走高；或MA5/MA20均线多头共振
    is_bull = False
    bull_reason = ""
    if p >= upper_band and ma20_slope_pct >= -0.15:
        is_bull = True
        bull_reason = f"现价({p:.2f}元)突破MA20缓冲上轨({upper_band:.2f}元)且均线走稳(3日斜率{ma20_slope_pct:+.2f}%)"
    elif p >= ma20 and ma5 >= ma20 and ma20_slope_pct > 0.05:
        is_bull = True
        bull_reason = f"均线多头共振(MA5>MA20且3日斜率{ma20_slope_pct:+.2f}%)"

    # 2. 迟滞平衡震荡区间 (NEUTRAL 判定前置，防MA20附近平盘被误判为反弹)：
    is_neutral_zone = (lower_band <= p <= upper_band) and (abs(ma5 - ma20) / ma20 <= buffer_pct / 100.0) and (-0.20 <= ma20_slope_pct <= 0.20)

    # 3. 短线超跌反弹通道 (REBOUND)：
    # 站上5日均线且5日均线走平或拐头向上，或从低位反弹收复至MA20下沿 (解决熊市反弹误判与0操作)
    is_rebound = False
    rebound_reason = ""
    if not is_bull and not is_neutral_zone and (p >= ma5 and (ma5_slope_pct >= -0.10 or p >= ma20 * 0.985)):
        is_rebound = True
        rebound_reason = f"短线站上MA5({ma5:.2f}元,斜率{ma5_slope_pct:+.2f}%)超跌反弹通道"

    # 4. 严谨熊市/空头弱势通道 (BEAR)：
    # 价格跌破MA20缓冲下轨且低于MA5，且MA5<MA20 (杜绝在MA5金叉反弹初段误判为熊市)
    is_bear = False
    bear_reason = ""
    if not is_bull and not is_rebound and not is_neutral_zone:
        if p <= lower_band and p < ma5 and ma5 < ma20 and ma20_slope_pct <= 0.15:
            is_bear = True
            bear_reason = f"现价({p:.2f}元)跌破MA20缓冲下轨({lower_band:.2f}元)且处于MA5/MA20下方空头弱势通道"
        elif p < ma20 and ma5 < ma20 and ma20_slope_pct < -0.05:
            is_bear = True
            bear_reason = f"均线空头死叉(MA5<MA20且3日斜率{ma20_slope_pct:+.2f}%)"

    # 宏观大盘极度冰点联动熔断：若大盘处于系统性杀跌(敞口<=20%)，禁止误判为中立/多头
    if market_exposure is not None and market_exposure <= 0.20:
        if is_bull or is_rebound:
            return "NEUTRAL", ma20, f"个股技术面强势但受大盘极度冰点(敞口{int(market_exposure*100)}%)系统性压制，降级为平衡震荡"
        else:
            return "BEAR", ma20, f"大盘极度冰点系统性杀跌(建议敞口{int(market_exposure*100)}%)，空头弱势通道主导"

    if is_bull:
        return "BULL", ma20, bull_reason
    elif is_rebound:
        return "REBOUND", ma20, rebound_reason
    elif is_bear:
        return "BEAR", ma20, bear_reason
    else:
        return "NEUTRAL", ma20, f"处于MA20±{buffer_pct}%迟滞平衡区间(MA20={ma20:.2f}元)"


def compute_intraday_divergence(
    minute_frame: Optional[pd.DataFrame],
    current_price: float,
    min_bars: int = 4
) -> Tuple[bool, bool, str]:
    """
    分时右侧动能拐点与量价共振识别 (MACD绿柱缩短/金叉/底背离 与 红柱缩短/死叉/顶背离)
    剔除容易在单边下泄时钝化欺骗的简单KDJ金叉，确保右侧反转真实性
    返回: (buy_divergence_confirmed, sell_divergence_confirmed, divergence_desc)
    """
    if minute_frame is None or len(minute_frame) < min_bars:
        return False, False, ""

    price_col = "price" if "price" in minute_frame.columns else ("close" if "close" in minute_frame.columns else None)
    if not price_col:
        return False, False, ""
    prices = pd.to_numeric(minute_frame[price_col], errors="coerce").dropna().to_numpy(dtype=float)
    if len(prices) < min_bars:
        return False, False, ""

    # 计算分时 MACD (EMA12, EMA26, DEA9)
    s = pd.Series(prices)
    ema12 = s.ewm(span=12, adjust=False).mean()
    ema26 = s.ewm(span=26, adjust=False).mean()
    dif = (ema12 - ema26).to_numpy()
    dea = pd.Series(dif).ewm(span=9, adjust=False).mean().to_numpy()
    macd_hist = (dif - dea) * 2.0

    lb = min(len(prices), 15)

    # 1. 正T买入动能拐点确认 (绿柱收窄 OR MACD低位金叉 OR MACD底背离)
    macd_green_shrink = False
    macd_red_shrink = False
    if len(macd_hist) >= 3:
        macd_green_shrink = (macd_hist[-1] < 0 and macd_hist[-1] > macd_hist[-2] and (len(macd_hist) < 4 or macd_hist[-2] > macd_hist[-3]))
        macd_red_shrink = (macd_hist[-1] > 0 and macd_hist[-1] < macd_hist[-2] and (len(macd_hist) < 4 or macd_hist[-2] < macd_hist[-3]))

    # 当分时样本较短(如测试数据或开盘初期的4~7根K线)，MACD尚未收敛，辅以极值拐点研判
    if len(prices) < 8 and len(prices) >= 3:
        p_last = prices[-1]
        p_prev1 = prices[-2]
        p_prev2 = prices[-3]
        if (p_last > p_prev1) and (p_prev1 <= p_prev2):
            macd_green_shrink = True
        if (p_last < p_prev1) and (p_prev1 >= p_prev2):
            macd_red_shrink = True

    macd_gold_cross = len(dif) >= 2 and (dif[-1] >= dea[-1] and dif[-2] < dea[-2])
    
    macd_bottom_div = False
    if lb >= 8:
        min_idx = int(np.argmin(prices[-lb:]))
        price_trough = prices[-lb + min_idx]
        macd_trough = dif[-lb + min_idx]
        if 2 < min_idx < (lb - 2):
            if prices[-1] <= price_trough * 1.003 and dif[-1] > macd_trough + 0.002 and dif[-1] > dif[-2]:
                macd_bottom_div = True

    # 2. 倒T卖出动能拐点确认 (红柱收窄 OR MACD高位死叉 OR MACD顶背离)
    macd_dead_cross = len(dif) >= 2 and (dif[-1] <= dea[-1] and dif[-2] > dea[-2])
    
    macd_top_div = False
    if lb >= 8:
        max_idx = int(np.argmax(prices[-lb:]))
        price_peak = prices[-lb + max_idx]
        macd_peak = dif[-lb + max_idx]
        if 2 < max_idx < (lb - 2):
            if prices[-1] >= price_peak * 0.997 and dif[-1] < macd_peak - 0.002 and dif[-1] < dif[-2]:
                macd_top_div = True

    buy_div = macd_green_shrink or macd_gold_cross or macd_bottom_div
    reasons_buy = []
    if macd_bottom_div:
        reasons_buy.append("MACD分时底背离")
    elif macd_gold_cross:
        reasons_buy.append("MACD低位金叉")
    elif macd_green_shrink:
        reasons_buy.append("MACD绿柱连续收窄" if len(prices) >= 8 else "分时极值拐点反弹")

    sell_div = macd_red_shrink or macd_dead_cross or macd_top_div
    reasons_sell = []
    if macd_top_div:
        reasons_sell.append("MACD分时顶背离")
    elif macd_dead_cross:
        reasons_sell.append("MACD高位死叉")
    elif macd_red_shrink:
        reasons_sell.append("MACD红柱连续收窄" if len(prices) >= 8 else "分时极值拐点滞涨")

    desc = "+".join(reasons_buy if buy_div else (reasons_sell if sell_div else []))
    return buy_div, sell_div, desc


def check_reversal_structure(
    prices: np.ndarray,
    current_price: float,
    volumes: Optional[np.ndarray] = None,
    direction: str = "BUY",
    vol_confirm: bool = True,
    vol_mult: float = 0.85
) -> Tuple[bool, str]:
    """
    右侧微观K线结构与量价确认：
    BUY (正T低吸确认): 必须包含连续2根K线不创新低，或底分型反转，严禁在当根创急跌新低时入场；
    SELL (倒T高抛确认): 必须包含连续2根K线不创新高，或顶分型滞涨，严禁在当根冲刺新高时入场。
    """
    if len(prices) < 3:
        return True, "样本不足"

    p_cur = current_price
    p_last = prices[-1]
    p_prev1 = prices[-2]
    p_prev2 = prices[-3]

    if direction == "BUY":
        recent_min = float(np.min(prices[-5:]))
        if p_cur < recent_min - 1e-4:
            return False, "现价正在下探创分时新低，严禁左侧接飞刀"

        two_bars_stop = (p_last >= p_prev1 - 1e-4) and (p_prev1 >= p_prev2 - 1e-4 or p_cur > p_prev1)
        fractal_bottom = (p_prev1 < p_prev2) and (p_last > p_prev1) and (p_cur >= (p_prev1 + p_prev2) / 2.0)

        if not (two_bars_stop or fractal_bottom):
            return False, "未形成连续止跌或底分型形态"

        if vol_confirm and volumes is not None and len(volumes) >= 3:
            v_mean = float(np.mean(volumes[-3:]))
            if v_mean > 0 and float(volumes[-1]) < v_mean * vol_mult:
                return False, "止跌反弹量能匮乏"

        return True, "分时底分型止跌确认"

    else:
        recent_max = float(np.max(prices[-5:]))
        if p_cur > recent_max + 1e-4:
            return False, "现价正在冲刺分时新高，防范倒T卖飞"

        two_bars_top = (p_last <= p_prev1 + 1e-4) and (p_prev1 <= p_prev2 + 1e-4 or p_cur < p_prev1)
        fractal_top = (p_prev1 > p_prev2) and (p_last < p_prev1) and (p_cur <= (p_prev1 + p_prev2) / 2.0)

        if not (two_bars_top or fractal_top):
            return False, "未形成连续滞涨或顶分型形态"

        # 顶部滞涨：A股分时冲高乏力多表现为缩量动能衰竭，允许缩量顶分型，仅排除极端成交量断流
        if vol_confirm and volumes is not None and len(volumes) >= 3:
            v_mean = float(np.mean(volumes[-3:]))
            if v_mean > 0 and float(volumes[-1]) < v_mean * 0.20:
                return False, "冲高成交量极端缺失"

        return True, "分时顶分型滞涨确认"



class T0PositionTracker:
    def __init__(self, storage_file: Optional[Path] = None, history_file: Optional[Path] = None):
        self.storage_file = storage_file or T0_TRADES_FILE
        self.history_file = history_file or T0_HISTORY_FILE
        self._trades: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._load()

    def _load(self):
        today_str = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            data = safe_read_json(self.storage_file, {})
            if isinstance(data, dict):
                self._trades = {k: v for k, v in data.items() if isinstance(v, dict) and v.get("date") == today_str}
            else:
                self._trades = {}

    def _save(self):
        # 原子写盘：先写临时文件再替换，避免进程中断时写坏 JSON
        with self._lock:
            atomic_write_json(self.storage_file, self._trades, indent=2)

    def _append_history(self, pos: Dict[str, Any]):
        with self._lock:
            data = safe_read_json(self.history_file, [])
            hist: List[Dict[str, Any]] = data if isinstance(data, list) else []
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
            atomic_write_json(self.history_file, hist, indent=2)

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
        data = safe_read_json(self.history_file, [])
        return data if isinstance(data, list) else []

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

            # 净收益口径：复用全局统一成本引擎 (双边佣金含最低5元门槛 + 卖出印花税 + 双边滑点 + 过户费)
            try:
                cfg = load_config()
            except Exception:
                cfg = None

            entry_amt = entry_p * shares
            sym_lower = str(symbol).lower()
            is_etf = any(sym_lower.startswith(p) for p in ["sh5", "sz15", "sz16", "sh51", "sh56", "sh58", "sh50", "sz18"])
            fee_info = calc_round_trip_fees(
                entry_price=entry_p,
                exit_price=exit_price,
                shares=shares,
                direction=direction,
                is_etf=is_etf,
                cfg=cfg,
            )
            fees = fee_info["fee_amt"]
            net_amt = gross_amt - fees
            net_pct = net_amt / entry_amt * 100.0 if entry_amt > 0 else 0.0
            pos["fees_amt"] = round(fees, 2)
            pos["net_pnl_amt"] = round(net_amt, 2)
            pos["net_pnl_pct"] = round(net_pct, 2)
            pos["net_pnl_type"] = "signal_proxy_net_incl_fees_slippage_estimate"

            # 计算解套成本动态摊薄 (按当前持仓股数冲减每股成本)
            if shares > 0:
                dilution_per_share = round(net_amt / shares, 3)
                pos["dilution_per_share"] = dilution_per_share
                pos["new_holding_cost"] = round(entry_p - dilution_per_share, 3)
            else:
                pos["dilution_per_share"] = 0.0
                pos["new_holding_cost"] = entry_p

            self._save()
            self._append_history(pos.copy())

    def reset_today(self):
        with self._lock:
            self._trades = {}
            self._save()

    def get_statistics(self, window_days: Optional[int] = None) -> Dict[str, Any]:
        """按实际结算口径统计做T历史绩效"""
        history = self.load_history()
        cost_model = get_trading_fee_params(load_config())
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
                target=None, stop=None, entry=None, shares=0, score=0,
                regime_reason="", regime_status="") -> Dict[str, Any]:
        pos = self.tracker.get_position(symbol)
        res = {
            "symbol": symbol,
            "name": name,
            "current_price": current_price,
            "vwap": vwap,
            "bias_pct": bias,
            "dyn_lower": dyn_lower,
            "dyn_upper": dyn_upper,
            "trend_direction": trend,
            "regime_status": regime_status or trend,
            "regime_reason": regime_reason,
            "market_exposure": market_exposure,
            "signal_action": action,
            "signal_desc": desc,
            "reasons": reasons,
            "position": pos,
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
        if pos:
            if "dilution_per_share" in pos:
                res["dilution_per_share"] = pos["dilution_per_share"]
            if "new_holding_cost" in pos:
                res["new_holding_cost"] = pos["new_holding_cost"]
            if "net_pnl_amt" in pos:
                res["net_pnl_amt"] = pos["net_pnl_amt"]
            if "fees_amt" in pos:
                res["fees_amt"] = pos["fees_amt"]
        return res

    def analyze_t0(
        self,
        symbol: str,
        name: str,
        current_price: float,
        df_minute: Optional[pd.DataFrame] = None,
        daily_kline: Optional[pd.DataFrame] = None,
        holding_cost: float = 0.0,
        holding_shares: int = 0,
        available_shares: Optional[int] = None,
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
        divergence_filter_enabled = bool(cfg.get("t0_divergence_filter_enabled", True))
        agile_trailing_enabled = bool(cfg.get("t0_agile_trailing_enabled", True))
        agile_trailing_tiered = bool(cfg.get("t0_agile_trailing_tiered", True))
        agile_active_pct = float(cfg.get("t0_agile_trailing_active", 0.6))
        agile_callback_pct = float(cfg.get("t0_agile_trailing_callback", 0.2))
        agile_callback_tier1 = float(cfg.get("t0_agile_callback_tier1", 0.35))
        agile_callback_tier2 = float(cfg.get("t0_agile_callback_tier2", 0.50))
        agile_callback_etf_tier1 = float(cfg.get("t0_agile_callback_etf_tier1", 0.25))
        agile_callback_etf_tier2 = float(cfg.get("t0_agile_callback_etf_tier2", 0.35))
        afternoon_buy_cutoff = str(cfg.get("t0_afternoon_buy_cutoff", "14:00"))
        afternoon_sell_cutoff = str(cfg.get("t0_afternoon_sell_cutoff", "14:15"))
        etf_share_ratio = float(cfg.get("t0_etf_share_ratio", 0.8))
        etf_agile_active = float(cfg.get("t0_etf_agile_active", 0.5))
        etf_take_profit = float(cfg.get("t0_etf_take_profit_pct", 1.0))
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
        if available_shares is None:
            avail_shares = holding_shares
        else:
            avail_shares = min(max(0, int(available_shares)), holding_shares)

        # 针对 ETF / 个股读取对应用户配置基准 (配置真实生效，避免后续被硬编码覆写)
        sym_lower = str(symbol).lower()
        is_etf = any(sym_lower.startswith(p) for p in ["sh5", "sz15", "sz16", "sh51", "sh56", "sh58", "sh50", "sz18"])
        if is_etf:
            bias_buy_thresh = -abs(float(cfg.get("t0_etf_bias_buy", -0.55)))
            bias_sell_thresh = abs(float(cfg.get("t0_etf_bias_sell", 0.65)))
            take_profit_pct = float(cfg.get("t0_etf_take_profit_pct", 0.8))
            stop_pct = float(cfg.get("t0_etf_stop_loss_pct", 0.85))
            agile_active_pct = float(cfg.get("t0_etf_agile_active", 0.45))
            band_min_pct = round(band_min_pct * 0.6, 2)
            band_max_pct = round(band_max_pct * 0.65, 2)
            agile_callback_pct = round(agile_callback_pct * 0.75, 2)

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

        # 2. 严谨多因子趋势通道与牛熊市状态识别 (双均线+斜率+迟滞滤波防频繁切换)
        trend_direction, ma20, regime_reason = evaluate_market_regime(daily_kline, current_price, symbol, as_of_date=None, market_exposure=market_exposure)

        # 自适应非对称倾斜通道与参数分层调整：围绕用户配置基准做四象限比例缩放
        scale = _T0_REGIME_SCALE.get(trend_direction, _T0_REGIME_SCALE["NEUTRAL"])
        bias_buy_thresh = round(-abs(bias_buy_thresh) * scale["buy"], 2)
        bias_sell_thresh = round(abs(bias_sell_thresh) * scale["sell"], 2)
        take_profit_pct = round(max(0.5 if is_etf else 0.7, take_profit_pct * scale["tp"]), 2)
        band_lower_base = max(band_min_pct, round(abs(bias_buy_thresh) * 0.92, 2))
        band_upper_base = max(band_min_pct, round(bias_sell_thresh * 0.90, 2))
        max_intraday_chg_sell = 2.80 if is_etf else 4.50

        # 根据行情状态动态刷新布林带通道 (同时尊重 t0_band_dev_k 与 t0_sell_asymmetry)
        if use_dynamic_boll and vwap > 0 and len(minute_closes) > 0:
            std_intraday = float(np.std(minute_closes) / vwap * 100.0) if len(minute_closes) > 1 else 1.0
            lower_span = max(band_lower_base, min(std_intraday * band_dev_k, band_max_pct))
            upper_span = max(band_upper_base, min(std_intraday * band_dev_k * sell_asymmetry, band_max_pct * sell_asymmetry))
            dyn_lower = round_price_dynamic(vwap * (1.0 - lower_span / 100.0), symbol)
            dyn_upper = round_price_dynamic(vwap * (1.0 + upper_span / 100.0), symbol)

        # 3. 检查现有持仓状态跟踪与冷却期解除
        # 检索当日同向止损风控熔断历史与单日做T频次统计
        use_circuit_breaker = bool(cfg.get("t0_daily_stop_circuit_breaker", True))
        # 每日做T频次控制: 1次最好，满足严苛高确定性方可做第2次，硬上限2次
        max_daily_trades = max(1, int(cfg.get("t0_max_trades_per_day", 2)))
        today_str = now.strftime("%Y-%m-%d")
        hist = self.tracker.load_history()
        today_symbol_trades = [
            h for h in hist
            if h.get("symbol") == symbol and h.get("date") == today_str and h.get("status") in ["FINISHED", "STOPPED"]
        ]
        today_trade_count = len(today_symbol_trades)
        bias_mult = 1.30 if today_trade_count == 1 else 1.00
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
        available_lot = (avail_shares // 100) * 100
        t1_sell_locked = (holding_shares > 0 and available_lot < 100)
        if available_lot >= 100:
            if is_etf:
                suggested_shares = max(100, int((available_lot * etf_share_ratio) // 100) * 100)
            else:
                suggested_shares = available_lot
        else:
            # 持股但今日可用不足 1 手（T+1 锁定）时严禁赋 100 股兜底；仅纯观察模式(holding_shares==0)给 100 股模拟
            suggested_shares = 100 if holding_shares == 0 else 0
        active_shares = suggested_shares

        in_session = self.is_trading_session()
        entry_window_ok = ("09:31" <= current_hm <= afternoon_sell_cutoff) or (df_minute is not None and not in_session)
        buy_time_window_ok = (current_hm <= "11:30") or (not in_session and df_minute is not None)
        if golden_window_only and ("11:00" <= current_hm <= "11:30") and bias > (-1.0 if is_etf else -1.50):
            buy_time_window_ok = False
        buy_afternoon_blocked = in_session and (current_hm >= afternoon_buy_cutoff)

        def _get_agile_callback(peak_pct: float) -> float:
            if not agile_trailing_tiered:
                return agile_callback_pct
            if is_etf:
                return agile_callback_etf_tier2 if peak_pct >= 0.80 else agile_callback_etf_tier1
            else:
                return agile_callback_tier2 if peak_pct >= 1.20 else agile_callback_tier1
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
                    rt_fee_pct = calc_round_trip_fees(
                        entry_price=entry_p, exit_price=current_price,
                        shares=pos_shares or 100, direction="BUY_T", is_etf=is_etf, cfg=cfg
                    )["fee_pct"]
                    eff_min_fee_pct = round(rt_fee_pct + (0.04 if is_etf else 0.05), 3)

                    # 1. 触发防守硬止损
                    if round(current_price, 3) <= round(stop_p, 3) + 1e-4:
                        signal_action = "STOP_T"
                        signal_desc = "正T防守止损对冲平仓"
                        reasons.append(f"跌破正T防守底线 {stop_p}元(-{stop_pct}%)，建议立即卖出底仓止损保护！")
                        self.tracker.close_position(symbol, "STOPPED", current_price, reasons[-1])
                    # 2. 敏捷动态移动止盈 (按实际股数与最低 5 元佣金算得净保本门槛，彻底杜绝毛盈净亏)
                    elif agile_trailing_enabled and peak_profit_pct >= agile_active_pct and (peak_profit_pct - curr_profit_pct) >= _get_agile_callback(peak_profit_pct):
                        eff_cb = _get_agile_callback(peak_profit_pct)
                        if curr_profit_pct > eff_min_fee_pct:
                            signal_action = "CLOSE_T"
                            signal_desc = "正T敏捷移动止盈对冲平仓"
                            tier_info = f"阶梯回落{eff_cb:.2f}%" if agile_trailing_tiered else f"回落{agile_callback_pct}%"
                            reasons.append(f"日内冲高至 {peak_p}元(+{peak_profit_pct:.2f}%)后{tier_info}且扣费(约{rt_fee_pct:.2f}%)净利润锁定，敏捷移动锁定做T利润，摊薄持仓成本！")
                            self.tracker.close_position(symbol, "FINISHED", current_price, reasons[-1])
                        else:
                            signal_action = "HOLDING_T"
                            signal_desc = f"正T盈利冲高中 (当前浮动: +{curr_profit_pct:.2f}%, 需越过含5元低佣成本 {eff_min_fee_pct:.2f}%)"
                    # 2.1 横盘钝化保本减负平仓 (持仓超120分钟且下午14:00后扣费保本锁定，规避尾盘强制平仓摩擦)
                    elif current_hm >= "14:00" and existing_pos.get("entry_time"):
                        dur_mins = 0
                        try:
                            e_ts = existing_pos.get("entry_time", "")
                            if len(e_ts) >= 5:
                                dur_mins = (int(current_hm[:2]) * 60 + int(current_hm[3:5])) - (int(e_ts[:2]) * 60 + int(e_ts[3:5]))
                        except Exception:
                            pass
                        if dur_mins >= 120 and abs(curr_profit_pct) <= max(0.50, rt_fee_pct + 0.15) and curr_profit_pct > rt_fee_pct:
                            signal_action = "CLOSE_T"
                            signal_desc = "正T横盘钝化·保本减负平仓"
                            reasons.append(f"做T持仓已达{dur_mins}分钟且持续横盘钝化，在扣费成本线(+{curr_profit_pct:.2f}% > {rt_fee_pct:.2f}%)微利平仓，平稳释放底仓并规避尾盘被动强平")
                            self.tracker.close_position(symbol, "FINISHED", current_price, reasons[-1])
                    # 3. 冲高回落动态浮动对冲止盈
                    elif peak_profit_pct >= take_profit_pct and (peak_profit_pct - curr_profit_pct) >= trailing_pct:
                        signal_action = "CLOSE_T"
                        signal_desc = "正T冲高回落对冲平仓"
                        reasons.append(f"日内冲高至 {peak_p}元(+{peak_profit_pct:.2f}%)后回落{trailing_pct}%，建议高抛卖出等量底仓，动态锁定做T利润！")
                        self.tracker.close_position(symbol, "FINISHED", current_price, reasons[-1])
                    # 4. 达到静态目标止盈
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
                    rt_fee_pct = calc_round_trip_fees(
                        entry_price=entry_p, exit_price=current_price,
                        shares=pos_shares or 100, direction="SELL_T", is_etf=is_etf, cfg=cfg
                    )["fee_pct"]
                    eff_min_fee_pct = round(rt_fee_pct + (0.04 if is_etf else 0.05), 3)

                    # 1. 突破防守线止损买回
                    if round(current_price, 3) >= round(stop_p, 3) - 1e-4:
                        signal_action = "STOP_T"
                        signal_desc = "倒T反抽止损买回归位"
                        reasons.append(f"价格反抽冲破防守线 {stop_p}元，为防底仓抛飞，建议立即以市价买回等量底仓（{pos_shares}股）止损对冲！")
                        self.tracker.close_position(symbol, "STOPPED", current_price, reasons[-1])
                    # 2. 敏捷动态移动止盈 (按实际股数与最低 5 元佣金算得净保本门槛，防止小仓位毛盈净亏)
                    elif agile_trailing_enabled and peak_profit_pct >= agile_active_pct and (peak_profit_pct - curr_profit_pct) >= _get_agile_callback(peak_profit_pct):
                        eff_cb = _get_agile_callback(peak_profit_pct)
                        if curr_profit_pct > eff_min_fee_pct:
                            signal_action = "CLOSE_T"
                            signal_desc = "倒T敏捷移动止盈买回平仓"
                            tier_info = f"阶梯回升{eff_cb:.2f}%" if agile_trailing_tiered else f"回升{agile_callback_pct}%"
                            reasons.append(f"日内下探至 {peak_p}元(+{peak_profit_pct:.2f}%)后{tier_info}且扣费(约{rt_fee_pct:.2f}%)净利润锁定，敏捷买回等量底仓锁定差价，摊薄持仓成本！")
                            self.tracker.close_position(symbol, "FINISHED", current_price, reasons[-1])
                        else:
                            signal_action = "HOLDING_T"
                            signal_desc = f"倒T浮盈持有中 (当前价差: +{curr_profit_pct:.2f}%, 需越过含5元低佣成本 {eff_min_fee_pct:.2f}%)"
                    # 3. 探底回升动态止盈
                    elif peak_profit_pct >= take_profit_pct and (peak_profit_pct - curr_profit_pct) >= trailing_pct:
                        signal_action = "CLOSE_T"
                        signal_desc = "倒T探底回升对冲平仓"
                        reasons.append(f"日内下探至 {peak_p}元(+{peak_profit_pct:.2f}%)后回升{trailing_pct}%，建议立即买回等量底仓，锁定做T利润！")
                        self.tracker.close_position(symbol, "FINISHED", current_price, reasons[-1])
                    # 4. 回踩目标价平仓买回
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

            # 右侧微结构形态确认
            buy_reversal_confirmed, rev_buy_msg = check_reversal_structure(
                minute_closes, current_price, minute_volumes, direction="BUY",
                vol_confirm=vol_confirm, vol_mult=vol_mult
            )
            sell_reversal_confirmed, rev_sell_msg = check_reversal_structure(
                minute_closes, current_price, minute_volumes, direction="SELL",
                vol_confirm=vol_confirm, vol_mult=vol_mult
            )

            # 分时动能拐点识别 (MACD绿柱缩短/金叉/底背离 与 红柱缩短/死叉/顶背离)
            buy_div, sell_div, div_desc = compute_intraday_divergence(minute_frame if minute_data_ok else None, current_price)

            eff_buy_bias = bias_buy_thresh * bias_mult
            eff_sell_bias = bias_sell_thresh * bias_mult
            buy_condition_met = ((bias <= eff_buy_bias and current_price <= dyn_lower) or (bias <= eff_buy_bias * 1.25)) if require_dual_resonance else (bias <= eff_buy_bias or current_price <= dyn_lower)
            sell_condition_met = ((bias >= eff_sell_bias and current_price >= dyn_upper) or (bias >= eff_sell_bias * 1.20)) if require_dual_resonance else (bias >= eff_sell_bias or current_price >= dyn_upper)

            if not minute_data_ok and df_minute is None:
                signal_desc = "分时数据暂不可用·观望"
                reasons.append("需要有效分时数据以精确计算VWAP均价通道")
            elif not entry_window_ok:
                signal_desc = "非开仓时段·暂停新信号"
                reasons.append(f"做T开仓时段为 09:31 至 {afternoon_sell_cutoff}，{afternoon_sell_cutoff}后停止新开仓以防尾盘对冲强平摩擦")
            elif today_trade_count >= max_daily_trades:
                signal_desc = f"达单日做T上限({max_daily_trades}次)·观望"
                reasons.append(f"该标的今日已完成 {today_trade_count} 笔做T，已达单日频次上限({max_daily_trades}次)，防范过度交易磨损")
            elif buy_condition_met:
                if buy_afternoon_blocked or not buy_time_window_ok:
                    signal_desc = "时段风控保护·禁止新开正T"
                    reasons.append(f"正T低吸限制在早盘黄金窗口(09:31-11:30)，{afternoon_buy_cutoff}后禁止新开正T，规避尾盘强平摩擦")
                    return {
                        "symbol": symbol, "name": name, "current_price": current_price,
                        "vwap": vwap, "bias_pct": bias, "dyn_lower": dyn_lower, "dyn_upper": dyn_upper,
                        "trend_direction": trend_direction, "signal_action": "HOLD_T",
                        "signal_desc": signal_desc, "reasons": reasons,
                        "position": self.tracker.get_position(symbol), "direction": "",
                        "t0_direction": "", "close_action": "", "target_price": active_target,
                        "stop_price": active_stop, "entry_price": active_entry, "shares": active_shares
                    }

                # 正T买入判定：
                # 1. 宏观系统性杀跌硬防护：若大盘宏观敞口<=20%或极度冰点，严禁做正T
                is_macro_dump = (market_exposure is not None and market_exposure <= 0.20)
                early_morning_buy_blocked = (current_hm <= "09:45" and bias > bias_buy_thresh * 1.5)

                # 2. 严格牛熊通道分层一票否决：空头通道严禁正T接飞刀(仅限极端恐慌洗盘超跌快反)！
                if trend_direction == "BEAR":
                    is_extreme_oversold = (bias <= bias_buy_thresh * 1.10) and (vwap_slope_pct is not None and vwap_slope_pct >= -0.25)
                    trend_allows_buy = is_extreme_oversold
                else:  # BULL, REBOUND, NEUTRAL
                    trend_allows_buy = True

                # 3. 分时VWAP斜率平走保护：陡峭下泄严禁接飞刀
                slope_thresh_buy = max_vwap_slope_pct * 0.5 if is_etf else max_vwap_slope_pct
                vwap_slope_blocked_buy = vwap_slope_filter and (vwap_slope_pct is not None) and (vwap_slope_pct < -slope_thresh_buy)

                if t1_sell_locked:
                    signal_desc = "T+1锁定无旧底仓·暂停正T"
                    reasons.append(f"当前持仓 {holding_shares}股 今日可用可卖为 {avail_shares}股（不足1手/T+1锁定），尾盘无法卖出旧底仓对冲闭环，禁止正T演变成被动加仓")
                elif "BUY_T" in today_stopped_dirs:
                    signal_desc = "今日正T已止损·触发风控熔断"
                    reasons.append("该标的今日此前已触发正T防守止损，表明处于强单边下泄行情，日内暂停同向做T防范二次磨损")
                elif early_morning_buy_blocked:
                    signal_desc = "早盘急跌抢跑·防下泄接飞刀保护"
                    reasons.append(f"09:45前属于早盘开盘集中下泄脉冲期，现价跌幅偏离({bias:.2f}%)未达极端恐慌超跌线，等待抛压衰竭分时企稳再行研判")
                elif is_macro_dump:
                    signal_desc = "宏观系统性杀跌·暂停正T接飞刀"
                    reasons.append(f"宏观大盘处于极度冰点系统性杀跌期(宏观敞口{int(market_exposure*100)}%)，系统性抛压沉重，禁止左侧逆势低吸正T")
                elif not trend_allows_buy:
                    signal_desc = "空头通道严禁正T·等待顺势倒T"
                    reasons.append(f"标的处于弱势空头熊市通道({regime_reason})，严禁逆势抄底接飞刀！仅在反抽上轨时允许做顺势倒T高抛摊薄成本")
                elif vwap_slope_blocked_buy:
                    signal_desc = "分时VWAP陡峭下泄·防接飞刀拦截"
                    reasons.append(f"分时VWAP均线斜率({vwap_slope_pct:+.2f}%)陡峭下倾，处于瀑布杀跌期，严禁逆势抄底！")
                elif not buy_reversal_confirmed:
                    signal_desc = "触及低吸下轨·等待分时止跌"
                    reasons.append(f"现价偏离达 {bias:.2f}% 触及动态下轨({dyn_lower}元)，但{rev_buy_msg}，等待分时企稳形态")
                elif divergence_filter_enabled and not buy_div:
                    signal_desc = "触及低吸下轨·等待动能拐点共振"
                    reasons.append(f"价格触及动态下轨({dyn_lower}元)，但分时MACD动能尚未出现衰竭拐点(等待绿柱缩短或底背离确认)")
                else:
                    signal_action = "BUY_T"
                    if buy_div:
                        signal_desc = f"🌟 背离预测正T·低吸机会触发 ({div_desc})"
                        reasons.append(f"分时出现动能拐点共振【{div_desc}】，现价偏离分时均价({vwap}元)达 {bias:.2f}%，触及动态下轨({dyn_lower}元)，具备高胜率超跌反弹差价")
                    else:
                        signal_desc = "🌟 正T低吸机会触发"
                        reasons.append(f"现价偏离分时均价({vwap}元)达 {bias:.2f}%，触及动态布林下轨({dyn_lower}元)且微结构止跌确认，具备日内反弹差价")

                    suggested_stop = round_price_dynamic(current_price * (1.0 - stop_pct / 100.0), symbol)
                    suggested_target = round_price_dynamic(max(vwap * (1.0 + take_profit_vwap_pct / 100.0), current_price * (1.0 + take_profit_pct / 100.0)), symbol)
                    if is_late and suggested_target > current_price:
                        suggested_target = round_price_dynamic(current_price + (suggested_target - current_price) * late_target_scale, symbol)

                    active_dir = "BUY_T"
                    active_target = suggested_target
                    active_stop = suggested_stop
                    active_entry = current_price
                    active_shares = suggested_shares

                    t0_score = self._t0_score(bias, (vwap - dyn_lower)/vwap*100.0 if vwap > 0 else 0.0, trend_direction == "BULL", True, minute_bar_age_minutes, is_late, buy_div)

                    self.tracker.open_position(
                        symbol=symbol, name=name, direction="BUY_T", price=current_price,
                        stop_price=suggested_stop, target_price=suggested_target, shares=active_shares
                    )

            elif sell_condition_met:
                # 倒T高抛判定：
                # 1. 严格牛熊通道分层一票否决：强势多头主升通道严禁倒T防卖飞！
                if trend_direction == "BULL":
                    trend_allows_sell = (bias >= bias_sell_thresh * 1.15)
                else:  # BEAR, REBOUND, NEUTRAL
                    trend_allows_sell = True

                early_short_blocked = (avoid_early_morning_short and current_hm <= "09:45" and bias < (1.2 if is_etf else 2.0))

                # 2. 分时VWAP斜率平走保护：单边急涨严禁倒T
                slope_thresh_sell = max_vwap_slope_pct * 0.5 if is_etf else max_vwap_slope_pct
                vwap_slope_blocked_sell = vwap_slope_filter and (vwap_slope_pct is not None) and (vwap_slope_pct > slope_thresh_sell)

                # 3. 防单边主升大阳线脉冲冲刺
                open_p = float(df_minute.iloc[0].get('price', df_minute.iloc[0].get('open', current_price))) if df_minute is not None and len(df_minute) > 0 else current_price
                intraday_chg_from_open = (current_price - open_p) / open_p * 100.0 if open_p > 0 else 0.0
                momentum_surge_blocked = (intraday_chg_from_open > max_intraday_chg_sell)

                if t1_sell_locked:
                    signal_desc = "T+1锁定不可卖·禁止倒T"
                    reasons.append(f"当前持仓 {holding_shares}股 中今日可用可卖数量为 {avail_shares}股（不足1手/T+1锁定），严禁发出倒T卖出信号")
                elif "SELL_T" in today_stopped_dirs:
                    signal_desc = "今日倒T已止损·触发风控熔断"
                    reasons.append("该标的今日此前已触发倒T防守止损，表明处于强单边主升行情，日内暂停同向做T防范再次卖飞")
                elif early_short_blocked:
                    signal_desc = "早盘抢筹脉冲·防倒T卖飞保护"
                    reasons.append(f"09:45前属于早盘竞价发现与主力抢筹脉冲期，现价涨幅偏离(+{bias:.2f}%)未达极值，暂不开倒T以防底仓卖飞")
                elif not trend_allows_sell:
                    signal_desc = "多头主升·严禁倒T防卖飞"
                    reasons.append(f"标的高处于强势多头主升通道({regime_reason})，倒T高抛极易卖飞强势核心牛股")
                elif momentum_surge_blocked:
                    signal_desc = "盘中单边主升冲刺·跳过倒T"
                    reasons.append(f"日内相对开盘大涨 +{intraday_chg_from_open:.2f}% 处于强势多头爆发期，严禁盲目倒T")
                elif vwap_slope_blocked_sell:
                    signal_desc = "分时VWAP陡峭主升·防卖飞拦截"
                    reasons.append(f"分时VWAP斜率({vwap_slope_pct:+.2f}%)单边急涨，防范过早减仓卖飞")
                elif not sell_reversal_confirmed:
                    signal_desc = "触及高抛上轨·等待分时滞涨"
                    reasons.append(f"现价冲高触及动态上轨({dyn_upper}元)，但{rev_sell_msg}，等待滞涨形态")
                elif divergence_filter_enabled and not sell_div and trend_direction != "BEAR":
                    signal_desc = "触及高抛上轨·等待动能拐点共振"
                    reasons.append(f"价格冲高触及动态上轨({dyn_upper}元)，但分时MACD红柱尚未收窄衰竭，等待滞涨拐点共振")
                else:
                    signal_action = "SELL_T"
                    if sell_div:
                        signal_desc = f"⚡ 背离预测倒T·高抛机会触发 ({div_desc})"
                        reasons.append(f"分时冲高出现动能衰竭【{div_desc}】，现价偏离VWAP均价({vwap}元)达 +{bias:.2f}%，触及动态上轨({dyn_upper}元)，适合先卖底仓高抛、尾盘低吸回补降成本")
                    elif trend_direction == "BEAR":
                        signal_desc = "⚡ 弱势通道顺势倒T·高抛摊薄成本"
                        reasons.append(f"标的处于弱势空头通道({regime_reason})，现价冲高偏离分时均价({vwap}元)达 +{bias:.2f}%，触及上轨({dyn_upper}元)，顺势卖出底仓，待回落低吸摊薄持仓成本")
                    else:
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

                    t0_score = self._t0_score(bias, (dyn_upper - vwap)/vwap*100.0 if vwap > 0 else 0.0, trend_direction == "BEAR", True, minute_bar_age_minutes, is_late, sell_div)

                    self.tracker.open_position(
                        symbol=symbol, name=name, direction="SELL_T", price=current_price,
                        stop_price=suggested_stop, target_price=suggested_target, shares=active_shares
                    )

        calc_score = locals().get("t0_score", 0)
        return self._result(
            symbol, name, current_price, vwap, bias, dyn_lower, dyn_upper,
            trend_direction, market_exposure, signal_action, signal_desc, reasons,
            active_dir, active_target, active_stop, active_entry, active_shares,
            calc_score,
            regime_reason=regime_reason, regime_status=trend_direction
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

        res_dict = {
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
            "trade_levels": trade_levels,
            "trend_direction": t0_res.get("trend_direction", ""),
            "regime_status": t0_res.get("regime_status", ""),
            "regime_reason": t0_res.get("regime_reason", "")
        }
        # 传递解套成本摊薄信息
        for k in ["dilution_per_share", "new_holding_cost", "net_pnl_amt", "fees_amt"]:
            if k in t0_res:
                res_dict[k] = t0_res[k]
        return res_dict

    def check_eod_unclosed_positions(self) -> List[Dict[str, Any]]:
        """
        尾盘时段 (14:48 - 14:55) 集中安全审查
        对冲所有日内已开仓、但直到收盘前仍未闭环的做T头寸，严格防止做T被动演变为加仓
        """
        from core.t0_group_manager import t0_group_manager
        open_positions = self.tracker.get_open_positions()
        signals = []
        for pos in open_positions:
            sym = pos["symbol"]
            name = pos["name"]
            target_grps = t0_group_manager.get_groups_for_stock(sym)
            grp_name = target_grps[0].get("name", "") if target_grps else ""
            grp_id = target_grps[0].get("id", "") if target_grps else ""
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
                    "group_name": grp_name,
                    "group_id": grp_id,
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
                    "group_name": grp_name,
                    "group_id": grp_id,
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
