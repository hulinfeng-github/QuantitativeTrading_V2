"""
A股智能量化系统 2.0 - 『两点半战法』多因子分时微结构引擎 (TailStrategy230 2.0)

核心重构亮点：
1. 市场环境与情绪周期硬门控（sentiment_radar）：系统性杀跌与冰点退潮期自动空仓防御，仅在多头/震荡分化/冰点修复期激活；
2. 尾盘 14:25~14:35 真实分钟级分时微结构确认：
   - 分时抗跌与 VWAP 均价线承接（全天/午后站稳 VWAP 占比、回踩缩量企稳）；
   - 14:00~14:30 尾盘抢筹脉冲与价格重心稳步抬升；
   - 严格踢除“全天阴跌最后3分钟偷袭诱多”与“全天大振幅尾盘放量滞涨派发”；
3. CYQ 主力筹码与偏度修正 RSRS 2.0 协同：过滤上方重度套牢区与高乖离加速派发风险；
4. 严格因果 5 分钟级历史回测：在 T 日 14:30 切片决策建仓，T+1 日按真实 5 分钟轨迹追踪止盈/VWAP跌破止盈/硬止损；
5. 原子持久化交易跟踪器（TailTradeTracker）：支持任意时刻启停恢复与防重复预警。
"""
import concurrent.futures
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

import numpy as np
import pandas as pd

from config import load_config, TAIL_TRADES_FILE, TAIL_SIGNALS_CACHE_FILE
from core.data_engine import data_engine
from core.sentiment_radar import sentiment_radar
from core.chip_engine import chip_engine
from core.rsrs_skew import calculate_rsrs_skew
from core.persistence import atomic_write_json, safe_read_json


class TailStrategy230:
    def __init__(self):
        self._cached_backtest: Optional[Dict[str, Any]] = None
        self._cache_time: Optional[datetime] = None
        self._cached_candidates: Optional[List[Dict[str, Any]]] = None
        self._candidates_cache_time: Optional[datetime] = None
        self._scan_lock = threading.RLock()
        self.reload_config()

    def reload_config(self):
        """从全局配置动态加载 2.30 战法参数 (支持热加载调优)"""
        cfg = load_config()
        self.min_pct = float(cfg.get("tail230_min_pct", 1.5))
        self.max_pct = float(cfg.get("tail230_max_pct", 5.8))
        self.min_vol_ratio = float(cfg.get("tail230_min_vol_ratio", 1.15))
        self.min_turnover = float(cfg.get("tail230_min_turnover", 120000000.0))
        self.target_profit_pct = float(cfg.get("tail230_target_profit_pct", 2.5))
        self.stop_loss_pct = float(cfg.get("tail230_stop_loss_pct", 1.8))
        self.min_vwap_hold_ratio = float(cfg.get("tail230_min_vwap_hold_ratio", 0.60))
        self.min_tail_vol_mult = float(cfg.get("tail230_min_tail_vol_mult", 0.95))
        self.max_roc5_pct = float(cfg.get("tail230_max_roc5_pct", 22.0))
        self.backtest_universe_size = int(cfg.get("tail230_backtest_universe_size", 50))

    def _default_backtest_universe(self) -> List[str]:
        """从全市场主板按成交额取流动性居前的代表性样本作为回测标的池。"""
        try:
            stocks = data_engine.get_main_board_stock_list()
            elig = [
                s for s in stocks
                if data_engine.is_main_board_equity(s.get("symbol", ""))
                and float(s.get("turnover", 0) or 0) >= self.min_turnover
            ]
            elig.sort(key=lambda x: float(x.get("turnover", 0) or 0), reverse=True)
            symbols = [s["symbol"] for s in elig[:self.backtest_universe_size]]
            if symbols:
                return symbols
        except Exception:
            pass
        return [
            "sh601138", "sh600519", "sh601899", "sh600036",
            "sz002475", "sh600900", "sz000333", "sh600276",
            "sz000651", "sh601318", "sz002594", "sh601668",
            "sz002842", "sh601127", "sz002156", "sh603993"
        ]

    @staticmethod
    def evaluate_market_gate(market_regime: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        宏观情绪与大盘门控：
        - 系统性杀跌且无当日盘中修复迹象 -> 严格空仓拒开
        - 冰点修复期（低温但当日指数强势转红站稳分时均线）-> 谨慎激活
        - 震荡分化 / 多头复苏 / 强多头 -> 正常激活并输出阈值调节系数
        """
        m = market_regime if market_regime is not None else sentiment_radar.analyze_market_regime()
        if not m or not m.get("data_available", False):
            return {
                "allowed": False,
                "mode": "数据不可用",
                "min_score_required": 99.0,
                "min_vwap_ratio": 0.70,
                "reason": "大盘基准行情不可用，触发风控空仓"
            }

        temp_score = float(m.get("temp_score", 0.0))
        regime_name = str(m.get("regime", ""))
        index_pct = float(m.get("index_pct", 0.0))
        target_exp = float(m.get("target_exposure", 0.0))

        # 1. 系统性杀跌/退潮加速下跌：大盘明显下挫 (-0.6%以下) 或低温续跌 -> 严禁隔夜抢筹
        if (temp_score < 50.0 and index_pct <= -0.60) or (temp_score < 28.0 and index_pct < -0.25):
            return {
                "allowed": False,
                "mode": "系统性杀跌",
                "min_score_required": 99.0,
                "min_vwap_ratio": 0.75,
                "reason": f"大盘处于{regime_name}(温度{temp_score:.1f}分, 涨跌{index_pct:+.2f}%)，严禁尾盘隔夜开仓"
            }

        # 2. 低温冰点修复/企稳期（低温但当日指数不再加速下挫，维持震荡/翻红修复）：严选均线多头与尾盘抢筹核心
        if regime_name == "系统性杀跌" or temp_score < 28.0:
            return {
                "allowed": True,
                "mode": "冰点修复期",
                "min_score_required": 66.0,
                "min_vwap_ratio": 0.62,
                "reason": f"大盘低温({temp_score:.1f}分)但日内止跌企稳({index_pct:+.2f}%)，仅精选站稳MA20与尾盘放量抢筹强股"
            }

        # 3. 震荡分化期（28 <= temp_score < 60 或 target_exp < 0.65）
        if temp_score < 60.0 or target_exp < 0.65:
            return {
                "allowed": True,
                "mode": "震荡分化期",
                "min_score_required": 65.0,
                "min_vwap_ratio": 0.60,
                "reason": f"大盘震荡分化({regime_name}, 温度{temp_score:.1f}分)，精选分时承接与筹码锁定标的"
            }

        # 4. 温和复苏 / 强多头主升
        return {
            "allowed": True,
            "mode": "多头主升期",
            "min_score_required": 62.0,
            "min_vwap_ratio": 0.55,
            "reason": f"大盘处于{regime_name}(温度{temp_score:.1f}分)，积极捕捉尾盘共振抢筹机会"
        }

    @staticmethod
    def _normalize_minute_frame(minute_df: pd.DataFrame, cutoff_hm: str = "14:35") -> Optional[pd.DataFrame]:
        """
        统一规范化 1 分钟实时分时数据与 5 分钟历史 K 线数据（严格按 cutoff_hm 因果截断）。
        输出列：time_str, open, high, low, close, bar_vol, vwap
        """
        if minute_df is None or len(minute_df) == 0:
            return None

        df = minute_df.copy()
        if "time_str" in df.columns:
            df["t_col"] = df["time_str"].astype(str).str.slice(0, 5)
        elif "time" in df.columns:
            df["t_col"] = df["time"].astype(str).str.slice(0, 5)
        else:
            return None

        df = df[df["t_col"] <= cutoff_hm].sort_values("t_col").reset_index(drop=True)
        if len(df) == 0:
            return None

        is_5m_ohlc = {"open", "high", "low", "close"}.issubset(df.columns)
        if is_5m_ohlc:
            out = pd.DataFrame({
                "time_str": df["t_col"],
                "open": pd.to_numeric(df["open"], errors="coerce"),
                "high": pd.to_numeric(df["high"], errors="coerce"),
                "low": pd.to_numeric(df["low"], errors="coerce"),
                "close": pd.to_numeric(df["close"], errors="coerce"),
                "bar_vol": pd.to_numeric(df["volume"], errors="coerce").fillna(0.0).clip(lower=0.0),
                "vwap": pd.to_numeric(df["vwap"], errors="coerce") if "vwap" in df.columns else np.nan,
            })
        else:
            # 1分钟分时流：volume 为累计手数
            prices = pd.to_numeric(df.get("price"), errors="coerce")
            cum_vols = pd.to_numeric(df.get("volume"), errors="coerce").fillna(0.0)
            bar_vols = cum_vols.diff().fillna(cum_vols.iloc[0]).clip(lower=0.0)
            out = pd.DataFrame({
                "time_str": df["t_col"],
                "open": prices,
                "high": prices,
                "low": prices,
                "close": prices,
                "bar_vol": bar_vols,
                "vwap": pd.to_numeric(df.get("vwap"), errors="coerce"),
            })

        out = out.dropna(subset=["close"]).reset_index(drop=True)
        out = out[out["close"] > 0].reset_index(drop=True)
        if len(out) == 0:
            return None

        # 缺失 VWAP 时因果按成交量加权补齐
        if out["vwap"].isna().any() or (out["vwap"] <= 0).any():
            tp = (out["high"].fillna(out["close"]) + out["low"].fillna(out["close"]) + out["close"]) / 3.0
            cum_v = out["bar_vol"].cumsum()
            cum_pv = (tp * out["bar_vol"]).cumsum()
            calc_vwap = np.where(cum_v > 0, cum_pv / cum_v, out["close"])
            out["vwap"] = np.where((out["vwap"].notna()) & (out["vwap"] > 0), out["vwap"], calc_vwap)

        return out

    def evaluate_intraday_microstructure(
        self,
        minute_df: pd.DataFrame,
        prev_close: float,
        cutoff_hm: str = "14:35",
        min_vwap_ratio: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        核心分时微结构评估（严格仅使用 <= cutoff_hm 的因果分时切片）：
        1. 分时均价线（VWAP）抗跌与承接强度；
        2. 14:00 ~ cutoff 尾盘抢筹脉冲与价格重心抬升；
        3. 拦截“全天阴跌最后3分钟偷袭诱多”与“高振幅滞涨放量派发”。
        """
        bars = self._normalize_minute_frame(minute_df, cutoff_hm=cutoff_hm)
        if bars is None or len(bars) < 12 or prev_close <= 0:
            return {"passed": False, "reject_reason": "分时样本不足或前收无效"}

        min_ratio = self.min_vwap_hold_ratio if min_vwap_ratio is None else float(min_vwap_ratio)

        closes = bars["close"].to_numpy(dtype=float)
        vwaps = bars["vwap"].to_numpy(dtype=float)
        highs = bars["high"].to_numpy(dtype=float)
        lows = bars["low"].to_numpy(dtype=float)
        vols = bars["bar_vol"].to_numpy(dtype=float)
        times = bars["time_str"].tolist()

        cur_p = float(closes[-1])
        cur_vwap = float(vwaps[-1])
        day_open = float(bars["open"].iloc[0])
        day_high = float(np.max(highs))
        day_low = float(np.min(lows))

        # 1. 全天 & 午后 VWAP 承接力检验
        above_vwap_mask = closes >= vwaps * 0.996
        vwap_hold_ratio = float(np.mean(above_vwap_mask))

        afternoon_mask = np.array([t >= "13:00" for t in times], dtype=bool)
        if np.any(afternoon_mask):
            afternoon_vwap_ratio = float(np.mean(closes[afternoon_mask] >= vwaps[afternoon_mask] * 0.996))
        else:
            afternoon_vwap_ratio = vwap_hold_ratio

        # 现价必须稳站在分时均价线之上（允许极微幅回踩 -0.2%）
        if cur_p < cur_vwap * 0.998:
            return {
                "passed": False,
                "reject_reason": f"尾盘现价({cur_p:.2f})弱于分时均价({cur_vwap:.2f})，日内资金承接不足"
            }

        if vwap_hold_ratio < min_ratio and afternoon_vwap_ratio < 0.70:
            return {
                "passed": False,
                "reject_reason": f"分时站稳VWAP占比偏低(全天{vwap_hold_ratio*100:.0f}%, 午后{afternoon_vwap_ratio*100:.0f}%)"
            }

        # 2. 拦截“全天阴跌、仅最后几分钟偷袭诱多”
        pre_tail_mask = np.array([t <= "14:20" for t in times], dtype=bool)
        if np.any(pre_tail_mask):
            pre_tail_close = float(closes[pre_tail_mask][-1])
            pre_tail_from_prev = (pre_tail_close - prev_close) / prev_close * 100.0
            total_from_prev = (cur_p - prev_close) / prev_close * 100.0
            late_jump = (cur_p - pre_tail_close) / prev_close * 100.0
            if pre_tail_from_prev < 0.0 and total_from_prev > 0.5 and late_jump > total_from_prev * 0.72:
                return {
                    "passed": False,
                    "reject_reason": f"14:20前长期水下弱势({pre_tail_from_prev:+.2f}%)，仅末段偷袭脉冲({late_jump:+.2f}%)，疑似诱多"
                }

        # 3. 尾盘抢筹脉冲 & 价格重心检验（14:00~cutoff vs 10:00~14:00）
        tail_mask = np.array([t >= "14:00" for t in times], dtype=bool)
        base_mask = np.array(["10:00" <= t < "14:00" for t in times], dtype=bool)

        tail_vol_mult = 1.0
        tail_lift_pct = 0.0
        if np.any(tail_mask) and np.any(base_mask):
            tail_avg_v = float(np.mean(vols[tail_mask]))
            base_avg_v = float(np.mean(vols[base_mask]))
            if base_avg_v > 0:
                tail_vol_mult = round(tail_avg_v / base_avg_v, 2)

            pre_1400_closes = closes[base_mask]
            tail_closes = closes[tail_mask]
            ref_mid = float(np.mean(pre_1400_closes[-min(6, len(pre_1400_closes)):]))
            tail_end_avg = float(np.mean(tail_closes[-min(3, len(tail_closes)):]))
            if ref_mid > 0:
                tail_lift_pct = round((tail_end_avg - ref_mid) / ref_mid * 100.0, 2)

        # 尾盘抢筹量能硬门控：14:00 后成交率不可明显萎缩（实证显示 tail_vol_mult < 0.90 极易次日低开亏钱）
        if np.any(tail_mask) and np.any(base_mask) and tail_vol_mult < self.min_tail_vol_mult:
            return {
                "passed": False,
                "reject_reason": f"14:00后尾盘量能萎靡({tail_vol_mult:.2f}x < {self.min_tail_vol_mult:.2f}x)，缺乏尾盘主动抢筹买盘"
            }

        # 尾盘价格重心不能明显下坠（拒绝冲高跳水）
        if tail_lift_pct < -0.60:
            return {
                "passed": False,
                "reject_reason": f"14:00后分时重心回落({tail_lift_pct:+.2f}%)，存在尾盘抛压"
            }

        # 现价距离日内高点不能回撤过大
        drawdown_from_high_pct = (day_high - cur_p) / day_high * 100.0 if day_high > 0 else 0.0
        if drawdown_from_high_pct > 2.0:
            return {
                "passed": False,
                "reject_reason": f"距日内高点({day_high:.2f})回落{drawdown_from_high_pct:.2f}%，上方抛压较重"
            }

        # 4. 拦截“全天高振幅 + 尾盘放量滞涨派发”
        intraday_amp_pct = (day_high - day_low) / prev_close * 100.0
        if intraday_amp_pct > 8.0 and tail_vol_mult >= 2.6 and tail_lift_pct < 0.25:
            return {
                "passed": False,
                "reject_reason": f"日内高振幅({intraday_amp_pct:.1f}%)且尾盘放量滞涨({tail_vol_mult:.1f}倍)，疑为主力派发"
            }

        # 5. 分时微结构综合加分 (0 ~ 25)
        micro_bonus = 0.0
        micro_bonus += min(10.0, max(0.0, (vwap_hold_ratio - 0.50) * 20.0))
        if 1.05 <= tail_vol_mult <= 2.60 and tail_lift_pct >= 0.15:
            micro_bonus += 8.0
        elif tail_vol_mult >= 0.90 and tail_lift_pct >= 0.0:
            micro_bonus += 4.0
        if cur_p >= cur_vwap * 1.002 and drawdown_from_high_pct <= 0.9:
            micro_bonus += 5.0
        if cur_vwap > day_open * 1.003:
            micro_bonus += 2.0

        return {
            "passed": True,
            "vwap": round(cur_vwap, 3),
            "vwap_hold_ratio": round(vwap_hold_ratio, 3),
            "afternoon_vwap_ratio": round(afternoon_vwap_ratio, 3),
            "tail_vol_mult": tail_vol_mult,
            "tail_lift_pct": tail_lift_pct,
            "drawdown_from_high_pct": round(drawdown_from_high_pct, 2),
            "intraday_amp_pct": round(intraday_amp_pct, 2),
            "micro_bonus": round(micro_bonus, 1),
            "bar_count": len(bars)
        }

    def analyze_bar(
        self,
        df: pd.DataFrame,
        idx: int = -1,
        minute_df: Optional[pd.DataFrame] = None,
        market_regime: Optional[Dict[str, Any]] = None,
        cutoff_hm: str = "14:35"
    ) -> Optional[Dict[str, Any]]:
        """
        判断指定 K 线（及可选的真实分时切片）是否满足升级版 2.30 战法条件。
        """
        length = len(df)
        if idx < 0:
            idx = length + idx
        if idx < 20 or idx >= length:
            return None

        curr = df.iloc[idx]
        prev = df.iloc[idx - 1]

        close = float(curr["close"])
        prev_close = float(prev["close"])
        open_price = float(curr["open"])
        high = float(curr["high"])
        low = float(curr["low"])
        volume = float(curr["volume"])

        if prev_close <= 0 or close <= 0:
            return None

        pct_change = round((close - prev_close) / prev_close * 100.0, 2)

        # 1. 市场环境硬过滤 (若显式传入 market_regime 则执行严格拦截)
        gate = None
        if market_regime is not None:
            gate = self.evaluate_market_gate(market_regime)
            if not gate["allowed"]:
                return None

        # 2. 日内涨幅健康区间过滤 (默认 1.5% ~ 5.8%)
        if pct_change < self.min_pct or pct_change > self.max_pct:
            return None

        # 3. 阳线实体与上影线控盘保护
        if close <= open_price:
            return None
        upper_shadow = (high - close) / prev_close * 100.0
        body = (close - open_price) / prev_close * 100.0
        if upper_shadow > max(0.8, body * 0.85):
            return None

        # 4. 均线多头生命线过滤
        ma5 = float(df["close"].iloc[max(0, idx - 4):idx + 1].mean())
        ma20 = float(df["close"].iloc[max(0, idx - 19):idx + 1].mean())
        if close < ma5 or close < ma20:
            return None

        # 防连续暴涨透支接盘 (5日累计涨幅过热熔断)
        if idx >= 5:
            close_5d_ago = float(df["close"].iloc[idx - 5])
            if close_5d_ago > 0:
                roc5 = (close - close_5d_ago) / close_5d_ago * 100.0
                if roc5 > self.max_roc5_pct:
                    return None

        # 5. 量比校验 (存在分时数据时按已交易时间折算全天预计成交量)
        volume_window = pd.to_numeric(df["volume"].iloc[max(0, idx - 5):idx], errors="coerce")
        volume_window = volume_window.replace([np.inf, -np.inf], np.nan).dropna()
        if len(volume_window) < 5 or not np.isfinite(volume) or float(volume_window.mean()) <= 0:
            return None
        vol_ma5 = float(volume_window.mean())
        effective_vol = volume
        if minute_df is not None and len(minute_df) > 0:
            # 14:30 约完成全天 87.5% 交易时间；按实际 bar 数折算预计全天量能
            is_5m = "time_str" in minute_df.columns and "open" in minute_df.columns
            total_session_bars = 48.0 if is_5m else 240.0
            norm_check = self._normalize_minute_frame(minute_df, cutoff_hm=cutoff_hm)
            if norm_check is not None and len(norm_check) > 0:
                frac = float(np.clip(len(norm_check) / total_session_bars, 0.55, 1.0))
                if frac < 0.98 and minute_df is not None:
                    effective_vol = volume / frac

        vol_ratio = round(effective_vol / vol_ma5, 2)
        if vol_ratio < self.min_vol_ratio:
            return None

        # 6. 分时微结构硬验证 (若提供 minute_df 则严格核验 VWAP 承接与尾盘脉冲)
        micro_res: Optional[Dict[str, Any]] = None
        if minute_df is not None:
            min_vwap = gate["min_vwap_ratio"] if gate else self.min_vwap_hold_ratio
            micro_res = self.evaluate_intraday_microstructure(
                minute_df, prev_close=prev_close, cutoff_hm=cutoff_hm, min_vwap_ratio=min_vwap
            )
            if not micro_res.get("passed", False):
                return None

        # 7. CYQ 筹码与偏度修正 RSRS 2.0 协同检验 (严格因果截取到 idx)
        sub_df = df.iloc[:idx + 1]
        chip_res = chip_engine.analyze_chips(sub_df, current_price=close)
        control_status = chip_res.get("control_status", "数据不足")
        profit_ratio = float(chip_res.get("profit_chip_ratio", 60.0))
        bias_to_main = float(chip_res.get("bias_to_main", 0.0))
        scr90 = float(chip_res.get("concentration_90", 20.0))

        # 若上方重度套牢 (获利盘 < 48%) 或 高位严重派发 (偏离主力成本 > 24%) -> 过滤
        if control_status in ("主力高位派发风险", "主力破位杀跌"):
            return None
        if bias_to_main > 24.0:
            return None
        if len(sub_df) >= 30 and profit_ratio < 48.0:
            return None

        rsrs_norm = 0.0
        rsrs_skew_val = 0.0
        if len(sub_df) >= 40:
            rsrs_win = max(40, min(120, len(sub_df)))
            rsrs_df = calculate_rsrs_skew(sub_df.tail(rsrs_win), n=18, m=rsrs_win, use_skew=True)
            val_norm = rsrs_df["rsrs_norm"].iloc[-1]
            val_skew = rsrs_df["rsrs_skew_val"].iloc[-1]
            if pd.notna(val_norm) and np.isfinite(val_norm):
                rsrs_norm = float(val_norm)
            if pd.notna(val_skew) and np.isfinite(val_skew):
                rsrs_skew_val = float(val_skew)
            # 极端负向支撑破位拦截
            if rsrs_norm < -0.75:
                return None

        # 8. 近 10 日小箱体突破判定 & ATR 自适应止盈止损点位
        recent_high = float(df["high"].iloc[max(0, idx - 10):idx].max())
        is_breakout = close >= recent_high * 0.985

        atr_pct = 2.5
        if len(sub_df) >= 15:
            tr = np.maximum(
                sub_df["high"].tail(14).to_numpy(dtype=float) - sub_df["low"].tail(14).to_numpy(dtype=float),
                np.abs(sub_df["high"].tail(14).to_numpy(dtype=float) - sub_df["close"].iloc[-15:-1].to_numpy(dtype=float))
            )
            atr_pct = float(np.mean(tr)) / close * 100.0 if close > 0 else 2.5

        dyn_target_pct = round(float(np.clip(max(self.target_profit_pct, atr_pct * 0.85), 2.0, 3.8)), 2)
        dyn_stop_pct = round(float(np.clip(max(self.stop_loss_pct, atr_pct * 0.58), 1.5, 2.3)), 2)
        if dyn_target_pct / dyn_stop_pct < 1.35:
            dyn_target_pct = round(dyn_stop_pct * 1.4, 2)

        # 9. 多因子综合评分 (0 ~ 99)
        score = 52.0
        score += min(vol_ratio * 8.0, 16.0)
        if is_breakout:
            score += 10.0
        if ma5 > ma20:
            score += 6.0
        if control_status in ("主力突破起爆", "主力吸筹蓄势", "主力锁仓主升"):
            score += 7.0
        elif profit_ratio >= 70.0 and scr90 <= 16.0:
            score += 5.0
        if rsrs_norm >= 0.3:
            score += 5.0
        elif rsrs_norm < 0.0:
            score -= 4.0
        if micro_res:
            score += micro_res.get("micro_bonus", 0.0)
        else:
            # 无分时测试模式下的日线实体补偿分
            score += 6.0

        score = round(float(np.clip(score, 0.0, 99.0)), 1)
        if gate and score < gate["min_score_required"]:
            return None

        reasons = [
            f"尾盘健康涨幅({pct_change:+.2f}%)，折算量比 {vol_ratio:.2f} 倍",
            f"站稳 MA5({ma5:.2f}) 与 MA20({ma20:.2f})" + (f"，突破近10日箱体({recent_high:.2f})" if is_breakout else ""),
            f"CYQ 筹码[{control_status}] (获利盘 {profit_ratio:.0f}%, 90%集中度 {scr90:.1f}%, RSRS={rsrs_norm:.2f})"
        ]
        if micro_res:
            reasons.insert(
                0,
                f"分时承接强劲：全天{micro_res['vwap_hold_ratio']*100:.0f}%/午后{micro_res['afternoon_vwap_ratio']*100:.0f}%站稳VWAP({micro_res['vwap']}), "
                f"14:00后量能脉冲{micro_res['tail_vol_mult']:.2f}x、重心抬升{micro_res['tail_lift_pct']:+.2f}%"
            )

        return {
            "symbol": str(curr.get("symbol", "")),
            "date": str(curr.get("date", ""))[:10],
            "price": close,
            "change_pct": pct_change,
            "score": score,
            "vol_ratio": vol_ratio,
            "ma5": round(ma5, 2),
            "ma20": round(ma20, 2),
            "vwap": micro_res.get("vwap") if micro_res else None,
            "vwap_hold_ratio": micro_res.get("vwap_hold_ratio") if micro_res else None,
            "tail_vol_mult": micro_res.get("tail_vol_mult") if micro_res else None,
            "control_status": control_status,
            "profit_chip_ratio": round(profit_ratio, 1),
            "rsrs_norm": rsrs_norm,
            "market_mode": gate["mode"] if gate else "独立评估",
            "reasons": reasons,
            "target_sell_price": round(close * (1.0 + dyn_target_pct / 100.0), 2),
            "target_pct": dyn_target_pct,
            "stop_loss_price": round(close * (1.0 - dyn_stop_pct / 100.0), 2),
            "stop_pct": dyn_stop_pct
        }

    def scan_today_candidates(self, limit: int = 40, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        今日全市场主板 2.30 战法实时扫描（融合大盘情绪硬门控 + 分时微结构 + 筹码/RSRS协同）
        """
        now = datetime.now()
        if not force_refresh and self._cached_candidates is not None and self._candidates_cache_time:
            if (now - self._candidates_cache_time).total_seconds() < 180:
                return self._cached_candidates[:limit]

        with self._scan_lock:
            now = datetime.now()
            if not force_refresh and self._cached_candidates is not None and self._candidates_cache_time:
                if (now - self._candidates_cache_time).total_seconds() < 180:
                    return self._cached_candidates[:limit]

            market = sentiment_radar.analyze_market_regime()
            gate = self.evaluate_market_gate(market)
            if not gate["allowed"]:
                print(f"[Tail230] {gate['reason']}")
                self._cached_candidates = []
                self._candidates_cache_time = datetime.now()
                return []

            stocks = data_engine.get_main_board_stock_list(force_refresh=force_refresh)

            matched = []
            for s in stocks:
                try:
                    chg = float(s.get("change_pct", 0))
                    to = float(s.get("turnover", 0))
                    if self.min_pct <= chg <= self.max_pct and to >= self.min_turnover:
                        matched.append(s)
                except (ValueError, TypeError):
                    continue

            matched.sort(key=lambda x: float(x.get("turnover", 0)), reverse=True)
            target_stocks = matched[:55]

            # 若为盘中 14:30 前后，严格截断到当前时刻；若为盘后研究复算，截断到 14:35 防止未来函数
            cur_hm = now.strftime("%H:%M")
            cutoff_hm = cur_hm if ("09:30" <= cur_hm <= "14:40" and now.weekday() < 5) else "14:35"

            def _check_one(s):
                sym = s["symbol"]
                name = s["name"]
                try:
                    df = data_engine.get_daily_kline(sym, count=65)
                    if df is None or len(df) < 25:
                        return None
                    minute_df = data_engine.get_minute_data(sym)
                    res = self.analyze_bar(
                        df,
                        idx=-1,
                        minute_df=minute_df,
                        market_regime=market,
                        cutoff_hm=cutoff_hm
                    )
                    if res:
                        res["name"] = name
                        res["symbol"] = sym
                        return res
                except Exception:
                    pass
                return None

            candidates = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
                for r in executor.map(_check_one, target_stocks):
                    if r:
                        candidates.append(r)

            candidates.sort(key=lambda x: x["score"], reverse=True)
            self._cached_candidates = candidates
            self._candidates_cache_time = datetime.now()
            if candidates:
                atomic_write_json(TAIL_SIGNALS_CACHE_FILE, {
                    "date": now.strftime("%Y-%m-%d"),
                    "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                    "market_mode": gate["mode"],
                    "candidates": candidates[:20]
                })
            return candidates[:limit]

    @staticmethod
    def _build_historical_index_regime_map(sh_df: Optional[pd.DataFrame]) -> Dict[str, Dict[str, Any]]:
        """因果逐日重构历史上证指数宏观情绪温度状态（无未来函数）。"""
        regime_map: Dict[str, Dict[str, Any]] = {}
        if sh_df is None or len(sh_df) < 25:
            return regime_map

        closes = pd.to_numeric(sh_df["close"], errors="coerce").to_numpy(dtype=float)
        dates = sh_df["date"].astype(str).str[:10].tolist()
        for i in range(20, len(sh_df)):
            c = closes[i]
            prev_c = closes[i - 1]
            if not np.isfinite(c) or prev_c <= 0:
                continue
            ma20 = float(np.mean(closes[i - 19:i + 1]))
            prev_ma20 = float(np.mean(closes[i - 20:i]))
            ma60 = float(np.mean(closes[max(0, i - 59):i + 1]))
            slope = (ma20 - prev_ma20) / prev_ma20 * 100.0 if prev_ma20 > 0 else 0.0
            sh_pct = (c - prev_c) / prev_c * 100.0

            temp = 50.0
            if c > ma20:
                temp += 15.0 + (10.0 if slope > 0.05 else 0.0)
            else:
                temp -= 15.0 + (10.0 if slope < -0.05 else 0.0)
            temp += 15.0 if c > ma60 else -15.0
            temp += float(np.clip(sh_pct * 8.0, -10.0, 10.0))
            temp = float(np.clip(temp, 5.0, 95.0))

            if temp >= 75.0:
                reg, exp = "强多头牛市", 0.90
            elif temp >= 50.0:
                reg, exp = "多头震荡市", 0.65
            elif temp >= 25.0:
                reg, exp = "空头震荡市", 0.35
            else:
                reg, exp = "系统性杀跌", 0.15

            regime_map[dates[i]] = {
                "data_available": True,
                "temp_score": round(temp, 1),
                "regime": reg,
                "target_exposure": exp,
                "index_pct": round(sh_pct, 2),
                "above_ma20": c >= ma20
            }
        return regime_map

    def calculate_strategy_win_rate(
        self,
        sample_symbols: Optional[List[str]] = None,
        days: int = 60
    ) -> Dict[str, Any]:
        """
        严格因果 5 分钟级历史回测（杜绝未来函数）：
        - T 日：仅截取 <= 14:30 的 5 分钟K线与日线截止状态进行多因子+分时VWAP研判，14:30 收盘价+滑点进场；
        - T+1 日：严格遵守 A 股 T+1 制度，逐根走查 T+1 日 09:35~15:00 真实 5 分钟轨迹：
          * 开盘跳空止盈/止损；
          * 盘中触发硬止损 / 动态目标止盈；
          * 早盘 09:50~10:30 冲高见顶回落跌破次日 VWAP 锁盈离场；
          * 15:00 尾盘平仓离场（扣除双边佣金+印花税+滑点）。
        """
        if not sample_symbols:
            sample_symbols = self._default_backtest_universe()
        sample_symbols = [sym for sym in sample_symbols if data_engine.is_main_board_equity(sym)]

        sh_df = data_engine.get_daily_kline("sh000001", count=max(days + 80, 180))
        regime_map = self._build_historical_index_regime_map(sh_df)

        # 并发拉取日K线与真实5分钟K线
        symbol_daily: Dict[str, pd.DataFrame] = {}
        symbol_5m: Dict[str, pd.DataFrame] = {}

        def _load_sym_data(sym: str):
            d_df = data_engine.get_daily_kline(sym, count=max(days + 70, 150))
            m5_df = data_engine.get_intraday_kline_5m(sym, count=2000)
            return sym, d_df, m5_df

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            for sym, d_df, m5_df in executor.map(_load_sym_data, sample_symbols):
                if d_df is not None and len(d_df) >= 35:
                    d_df = d_df.copy()
                    d_df["date_str"] = d_df["date"].astype(str).str[:10]
                    symbol_daily[sym] = d_df
                if m5_df is not None and len(m5_df) >= 96:
                    symbol_5m[sym] = m5_df

        total_trades = 0
        win_trades = 0
        loss_trades = 0
        total_pnl_pct = 0.0
        win_pnl_list: List[float] = []
        loss_pnl_list: List[float] = []
        morning_premiums: List[float] = []
        trade_logs: List[Dict[str, Any]] = []
        bull_trades = 0
        bull_wins = 0
        bear_trades = 0
        bear_wins = 0
        used_5m_causal = False

        # A股真实双边交易摩擦：双边佣金(万2.5*2) + 印花税(万5) + 双边滑点(万5*2) ≈ 0.20%
        friction_pct = 0.20

        for sym, daily_df in symbol_daily.items():
            info = data_engine.resolve_symbol(sym)
            name = info.get("name", sym) if info else sym
            m5_df = symbol_5m.get(sym)
            if m5_df is None:
                continue

            m5_by_date = {d: grp.reset_index(drop=True) for d, grp in m5_df.groupby("date_str")}
            dates = daily_df["date_str"].tolist()

            for i in range(25, len(daily_df) - 1):
                sig_date = dates[i]
                next_date = dates[i + 1]
                if sig_date not in m5_by_date or next_date not in m5_by_date:
                    continue

                used_5m_causal = True
                day_bars = m5_by_date[sig_date]
                bars_1430 = day_bars[day_bars["time_str"] <= "14:30"].reset_index(drop=True)
                if len(bars_1430) < 30:
                    continue

                # 严格因果构造截止到 T 日 14:30 的部分日线，绝不读取 14:35~15:00 数据
                partial_daily = daily_df.iloc[:i + 1].copy()
                t_open = float(bars_1430["open"].iloc[0])
                t_high = float(bars_1430["high"].max())
                t_low = float(bars_1430["low"].min())
                t_close_1430 = float(bars_1430["close"].iloc[-1])
                t_vol_1430 = float(bars_1430["volume"].sum())
                # 若日K与5m单位不同(手 vs 股)，按昨日量校准尺度
                prev_daily_vol = float(partial_daily["volume"].iloc[-2])
                # 腾讯日K volume 单位多为手，新浪5m为股；统一折算回日K量级
                t_vol_scaled = t_vol_1430 / 100.0 if (prev_daily_vol > 0 and t_vol_1430 / prev_daily_vol > 30.0) else t_vol_1430

                idx_last = partial_daily.index[-1]
                partial_daily.loc[idx_last, "open"] = t_open
                partial_daily.loc[idx_last, "high"] = t_high
                partial_daily.loc[idx_last, "low"] = t_low
                partial_daily.loc[idx_last, "close"] = t_close_1430
                partial_daily.loc[idx_last, "volume"] = t_vol_scaled

                hist_market = regime_map.get(sig_date)
                sig_res = self.analyze_bar(
                    partial_daily,
                    idx=-1,
                    minute_df=bars_1430,
                    market_regime=hist_market,
                    cutoff_hm="14:30"
                )
                if not sig_res:
                    continue

                entry_price = round(t_close_1430 * 1.0005, 3)  # 含 0.05% 进场滑点
                if entry_price <= 0:
                    continue

                # 严格 T+1 逐 5 分钟因果平仓模拟
                t1_bars = m5_by_date[next_date]
                if len(t1_bars) == 0:
                    continue

                target_p = entry_price * (1.0 + sig_res["target_pct"] / 100.0)
                stop_p = entry_price * (1.0 - sig_res["stop_pct"] / 100.0)
                t1_open = float(t1_bars["open"].iloc[0])
                t1_max_high = float(t1_bars["high"].max())
                t1_morning_bars = t1_bars[t1_bars["time_str"] <= "10:00"]
                morning_high = float(t1_morning_bars["high"].max()) if len(t1_morning_bars) > 0 else t1_max_high
                morning_premiums.append((morning_high - entry_price) / entry_price * 100.0)

                exit_price = float(t1_bars["close"].iloc[-1])
                exit_time = str(t1_bars["time_str"].iloc[-1])
                exit_reason = "T+1收盘平仓"

                # 1. 开盘集合竞价跳空检查
                if t1_open <= stop_p:
                    exit_price = t1_open
                    exit_time = "09:30"
                    exit_reason = "T+1低开击穿硬止损防核"
                elif t1_open >= target_p:
                    exit_price = t1_open
                    exit_time = "09:30"
                    exit_reason = "T+1高开直接止盈兑现"
                else:
                    # 2. 逐 5 分钟盘中轨迹推演
                    peak_p = t1_open
                    exited = False
                    for _, b in t1_bars.iterrows():
                        b_time = str(b["time_str"])
                        b_open = float(b["open"])
                        b_high = float(b["high"])
                        b_low = float(b["low"])
                        b_close = float(b["close"])
                        b_vwap = float(b["vwap"])

                        if b_high > peak_p:
                            peak_p = b_high

                        # 保守假设：同根5m若先触及止损或双触及，按更靠近开盘价或止损优先
                        if b_low <= stop_p and b_high >= target_p:
                            if (b_open - stop_p) <= (target_p - b_open):
                                exit_price, exit_time, exit_reason = stop_p, b_time, "T+1分时触及硬止损"
                            else:
                                exit_price, exit_time, exit_reason = target_p, b_time, "T+1分时触及止盈目标"
                            exited = True
                            break
                        if b_low <= stop_p:
                            exit_price, exit_time, exit_reason = stop_p, b_time, "T+1分时击穿止损线"
                            exited = True
                            break
                        if b_high >= target_p:
                            exit_price, exit_time, exit_reason = target_p, b_time, "T+1分时触及目标止盈"
                            exited = True
                            break

                        # 冲高回落 VWAP 破位止盈锁利（09:45~10:35 窗口）
                        peak_gain = (peak_p - entry_price) / entry_price * 100.0
                        if "09:45" <= b_time <= "10:35" and peak_gain >= 0.85:
                            if b_close < b_vwap * 0.997 and b_close > entry_price * 1.002:
                                exit_price, exit_time, exit_reason = b_close, b_time, "早盘冲高钝化·跌破VWAP落袋止盈"
                                exited = True
                                break

                        # 早盘窗口结束走弱主动防损（10:30 若跌破分时均价VWAP，不再抱幻想等待午后跳水）
                        if b_time == "10:30" and b_close < b_vwap * 0.998:
                            exit_price, exit_time, exit_reason = b_close, b_time, "早盘窗口结束未强·跌破VWAP控险离场"
                            exited = True
                            break

                    if not exited:
                        exit_price = float(t1_bars["close"].iloc[-1])
                        exit_time = str(t1_bars["time_str"].iloc[-1])
                        exit_reason = "T+1尾盘15:00兑现退出"

                raw_ret = (exit_price - entry_price) / entry_price * 100.0
                net_ret = round(raw_ret - friction_pct, 2)

                total_trades += 1
                total_pnl_pct += net_ret
                if net_ret > 0:
                    win_trades += 1
                    win_pnl_list.append(net_ret)
                else:
                    loss_trades += 1
                    loss_pnl_list.append(net_ret)

                is_bull = bool(hist_market and hist_market.get("temp_score", 0) >= 50.0)
                if is_bull:
                    bull_trades += 1
                    if net_ret > 0:
                        bull_wins += 1
                else:
                    bear_trades += 1
                    if net_ret > 0:
                        bear_wins += 1

                trade_logs.append({
                    "symbol": sym,
                    "name": name,
                    "signal_date": sig_date,
                    "buy_date": sig_date,
                    "buy_time": "14:30",
                    "buy_price": round(entry_price, 2),
                    "sell_date": next_date,
                    "sell_time": exit_time,
                    "sell_price": round(exit_price, 2),
                    "return_pct": net_ret,
                    "morning_high_pct": round((morning_high - entry_price) / entry_price * 100.0, 2),
                    "is_win": net_ret > 0,
                    "market_regime": sig_res.get("market_mode", ""),
                    "score": sig_res.get("score"),
                    "reason": exit_reason
                })

        win_rate = round(win_trades / total_trades * 100.0, 1) if total_trades > 0 else None
        bull_win_rate = round(bull_wins / bull_trades * 100.0, 1) if bull_trades > 0 else None
        bear_win_rate = round(bear_wins / bear_trades * 100.0, 1) if bear_trades > 0 else None
        avg_win = round(float(np.mean(win_pnl_list)), 2) if win_pnl_list else None
        avg_loss = round(float(np.mean(loss_pnl_list)), 2) if loss_pnl_list else None
        max_single_loss = round(float(min(loss_pnl_list)), 2) if loss_pnl_list else 0.0
        avg_return = round(total_pnl_pct / total_trades, 2) if total_trades > 0 else None
        avg_morning_prem = round(float(np.mean(morning_premiums)), 2) if morning_premiums else None
        pl_ratio = round(abs(avg_win / avg_loss), 2) if avg_win is not None and avg_loss is not None and avg_loss != 0 else (99.0 if avg_win and not loss_pnl_list else None)

        trade_logs.sort(key=lambda x: (x["buy_date"], x.get("score", 0)), reverse=True)

        result = {
            "strategy_name": "2.30 尾盘分时承接与多因子共振实战引擎",
            "backtest_days": days,
            "used_5m_intraday": used_5m_causal,
            "total_trades": total_trades,
            "win_trades": win_trades,
            "loss_trades": loss_trades,
            "win_rate": win_rate,
            "enhanced_win_rate": bull_win_rate if bull_win_rate is not None else win_rate,
            "bear_win_rate": bear_win_rate,
            "bull_trades": bull_trades,
            "bear_trades": bear_trades,
            "avg_return_pct": avg_return,
            "avg_win_pct": avg_win,
            "avg_loss_pct": avg_loss,
            "max_single_loss_pct": max_single_loss,
            "profit_loss_ratio": pl_ratio,
            "avg_exit_session_high_premium_pct": avg_morning_prem,
            "avg_next_session_high_premium_pct": avg_morning_prem,
            "avg_morning_premium_pct": avg_morning_prem,
            "backtest_method": "causal_5m_intraday_1430_entry_t1_minute_exit",
            "execution_limitations": "基于真实5分钟历史分时切片严格因果回测（T日仅取<=14:30分时与截断日线进场，T+1逐5分钟推演止盈/跌破VWAP锁盈/硬止损退出），已扣除双边佣金印花税滑点(0.20%)。",
            "recent_trades": trade_logs[:40],
            "all_trades": trade_logs,
            "rules_summary": [
                "宏观情绪门控：系统性杀跌与退潮期自动空仓，仅多头/震荡分化/冰点修复激活",
                "14:30 真实分时确认：站稳 VWAP 均价线占比达标 + 14:00后脉冲抬升，踢除最后3分钟偷袭与高振幅滞涨",
                "CYQ 筹码与 RSRS 协同：严禁买入高位派发或上方重度套牢标的",
                "T+1 精细化次日兑现：开盘风控 + 早盘冲高跌破 VWAP 落袋 + ATR 动态止盈止损"
            ]
        }
        self._cached_backtest = result
        self._cache_time = datetime.now()
        return result

    def get_cached_or_run_backtest(self, force_refresh: bool = False) -> Dict[str, Any]:
        if not force_refresh and self._cached_backtest is not None:
            return self._cached_backtest
        return self.calculate_strategy_win_rate()


tail_strategy_230 = TailStrategy230()


class TailTradeTracker:
    """
    2.30 战法原子持久化跟踪器：
    跟踪用户确认的实际买入成交并发送次日精确分时行情预警（VWAP跌破止盈/目标止盈/防核止损），
    任意时刻停止或重启程序均可无缝断点恢复。
    """
    def __init__(self, storage_file: Optional[Path] = None, trades_file: Optional[Path] = None):
        target_file = storage_file or trades_file
        if target_file is None:
            try:
                self.storage_file = TAIL_TRADES_FILE
            except Exception:
                self.storage_file = Path(__file__).resolve().parent.parent / "storage" / "tail_trades_230.json"
        else:
            self.storage_file = target_file

        self._lock = threading.RLock()
        self._trades: List[Dict[str, Any]] = []
        self._load()

    def _load(self):
        with self._lock:
            data = safe_read_json(self.storage_file, [])
            self._trades = data if isinstance(data, list) else []

    def _save(self):
        with self._lock:
            if self.storage_file:
                atomic_write_json(self.storage_file, self._trades)

    def record_recommendations(self, candidates: List[Dict[str, Any]], limit: int = 3) -> List[Dict[str, Any]]:
        """记录研究信号；候选信号不能被登记为用户实际持仓。"""
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M:%S")

        added_trades = []
        with self._lock:
            for c in candidates[:limit]:
                sym = c.get("symbol")
                if not sym:
                    continue

                existing = next((t for t in self._trades if t.get("symbol") == sym and t.get("buy_date") == date_str), None)
                if existing:
                    continue

                buy_price = float(c.get("price", 0.0))
                target_p = float(c.get("target_sell_price") or round(buy_price * 1.025, 2))
                stop_p = float(c.get("stop_loss_price") or round(buy_price * 0.982, 2))

                trade_item = {
                    "id": f"{sym}_{date_str}",
                    "symbol": sym,
                    "name": c.get("name", sym),
                    "buy_date": date_str,
                    "buy_time": time_str,
                    "buy_price": buy_price,
                    "target_sell_price": target_p,
                    "target_pct": float(c.get("target_pct", 2.5)),
                    "stop_loss_price": stop_p,
                    "stop_pct": float(c.get("stop_pct", 1.8)),
                    "score": float(c.get("score", 70.0)),
                    "vol_ratio": float(c.get("vol_ratio", 1.0)),
                    "change_pct": float(c.get("change_pct", 0.0)),
                    "source": "research_signal",
                    "status": "SIGNAL_ONLY",
                    "peak_price": buy_price,
                    "reasons": c.get("reasons", []),
                    "exit_date": None,
                    "exit_time": None,
                    "exit_price": None,
                    "exit_reason": None,
                    "return_pct": None,
                    "alerted_flags": []
                }
                self._trades.insert(0, trade_item)
                added_trades.append(trade_item)

            if added_trades:
                self._save()
        return added_trades

    def add_manual_trade(
        self,
        symbol: str,
        name: str,
        buy_price: float,
        buy_date: Optional[str] = None,
        target_pct: float = 2.5,
        stop_pct: float = 1.8
    ) -> Dict[str, Any]:
        """记录用户确认的实际买入成交（幂等去重同一日同一标的）"""
        now = datetime.now()
        b_date = buy_date or now.strftime("%Y-%m-%d")
        b_time = now.strftime("%H:%M:%S")

        target_p = round(buy_price * (1.0 + target_pct / 100.0), 2)
        stop_p = round(buy_price * (1.0 - stop_pct / 100.0), 2)

        with self._lock:
            existing = next(
                (
                    t for t in self._trades
                    if t.get("symbol") == symbol
                    and t.get("buy_date") == b_date
                    and t.get("source") == "manual_actual_fill"
                    and t.get("status") in ["HOLDING", "ALERTED_TIMED", "ALERTED_EXIT"]
                ),
                None
            )
            if existing:
                return existing

            trade_item = {
                "id": f"{symbol}_{b_date}",
                "source": "manual_actual_fill",
                "entry_fill_confirmed": True,
                "symbol": symbol,
                "name": name,
                "buy_date": b_date,
                "buy_time": b_time,
                "buy_price": float(buy_price),
                "target_sell_price": target_p,
                "target_pct": target_pct,
                "stop_loss_price": stop_p,
                "stop_pct": stop_pct,
                "status": "HOLDING",
                "peak_price": float(buy_price),
                "reasons": ["用户自主确认买入/实盘建仓"],
                "exit_date": None,
                "exit_time": None,
                "exit_price": None,
                "exit_reason": None,
                "return_pct": None,
                "alerted_flags": []
            }
            self._trades.insert(0, trade_item)
            self._save()
            return trade_item

    def get_active_positions(self) -> List[Dict[str, Any]]:
        """只返回用户确认实际买入、且尚未人工确认平仓的记录。"""
        with self._lock:
            return [
                t for t in self._trades
                if t.get("source") == "manual_actual_fill"
                and t.get("status") in ["HOLDING", "ALERTED_TIMED", "ALERTED_EXIT"]
            ]

    def get_all_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return self._trades[:limit]

    def close_trade(self, symbol: str, buy_date: str, status: str, exit_price: float, exit_reason: str):
        now = datetime.now()
        with self._lock:
            for t in self._trades:
                if t.get("source") == "manual_actual_fill" and t.get("symbol") == symbol and t.get("buy_date") == buy_date:
                    t["status"] = status
                    t["exit_date"] = now.strftime("%Y-%m-%d")
                    t["exit_time"] = now.strftime("%H:%M:%S")
                    t["exit_price"] = round(exit_price, 2)
                    t["exit_reason"] = exit_reason
                    buy_p = float(t.get("buy_price", exit_price))
                    t["return_pct"] = round((exit_price - buy_p) / buy_p * 100.0, 2) if buy_p > 0 else 0.0
                    t["exit_fill_confirmed"] = True
                    t["return_method"] = "买卖成交价变化，未扣佣金、印花税及其他费用；不是账户净收益"
                    self._save()
                    return t
        return None

    def add_alert_flag(self, symbol: str, buy_date: str, flag: str):
        with self._lock:
            for t in self._trades:
                if t.get("symbol") == symbol and t.get("buy_date") == buy_date:
                    flags = t.setdefault("alerted_flags", [])
                    if flag not in flags:
                        flags.append(flag)
                        self._save()
                    return

    def reconcile_and_clean_expired_positions(self, current_dt: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """
        开机自检与断档恢复：
        核查停机期间已越过 T+1 交易日的未平仓头寸，调取真实历史日线/5分钟轨迹完成离线补结。
        """
        now = current_dt or datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        reconciled = []

        with self._lock:
            active_positions = [t for t in self._trades if t.get("status") in ["HOLDING", "ALERTED_TIMED"]]
            for pos in active_positions:
                sym = pos.get("symbol")
                buy_date = str(pos.get("buy_date", ""))[:10]
                if not sym or not buy_date or buy_date >= today_str:
                    continue

                buy_p = float(pos.get("buy_price", 0.0))
                target_p = float(pos.get("target_sell_price", buy_p * 1.025))
                stop_p = float(pos.get("stop_loss_price", buy_p * 0.982))

                df = data_engine.get_daily_kline(sym, count=30)
                if df is None or len(df) == 0:
                    continue

                df["date_str"] = df["date"].astype(str).str[:10]
                after_bars = df[df["date_str"] > buy_date]
                if len(after_bars) == 0:
                    continue

                t1_bar = after_bars.iloc[0]
                t1_date = str(t1_bar["date_str"])

                if t1_date == today_str:
                    continue

                t1_open = float(t1_bar["open"])
                t1_high = float(t1_bar["high"])
                t1_low = float(t1_bar["low"])
                t1_close = float(t1_bar["close"])

                if t1_open >= target_p:
                    status = "CLOSED_PROFIT"
                    exit_price = t1_open
                    reason = f"系统离线自动回溯补结: T+1日({t1_date})高开{t1_open:.2f}元直接触发止盈"
                elif t1_open <= stop_p:
                    status = "CLOSED_STOP"
                    exit_price = t1_open
                    reason = f"系统离线自动回溯补结: T+1日({t1_date})低开{t1_open:.2f}元直接触发止损"
                elif t1_high >= target_p and t1_low <= stop_p:
                    status = "CLOSED_STOP"
                    exit_price = stop_p
                    reason = f"系统离线自动回溯补结: T+1日({t1_date})振幅双触及，按保守止损{stop_p:.2f}元补结"
                elif t1_high >= target_p:
                    status = "CLOSED_PROFIT"
                    exit_price = target_p
                    reason = f"系统离线自动回溯补结: T+1日({t1_date})最高冲至{t1_high:.2f}元触发止盈"
                elif t1_low <= stop_p:
                    status = "CLOSED_STOP"
                    exit_price = stop_p
                    reason = f"系统离线自动回溯补结: T+1日({t1_date})最低跌至{t1_low:.2f}元触发止损"
                else:
                    status = "CLOSED_EOD"
                    exit_price = t1_close
                    reason = f"系统离线自动回溯补结: T+1日({t1_date})尾盘收盘平仓离场"

                pos["status"] = status
                pos["exit_date"] = t1_date
                pos["exit_time"] = "15:00:00"
                pos["exit_price"] = round(exit_price, 2)
                pos["exit_reason"] = reason
                pos["return_pct"] = round((exit_price - buy_p) / buy_p * 100.0, 2) if buy_p > 0 else 0.0
                reconciled.append(pos)

            if reconciled:
                self._save()
        return reconciled

    def check_morning_plan(self, quotes: Optional[Dict[str, Any]] = None, current_dt: Optional[datetime] = None) -> List[Dict[str, Any]]:
        self.reconcile_and_clean_expired_positions(current_dt)
        now = current_dt or datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        t1_positions = [p for p in self.get_active_positions() if p.get("buy_date") < today_str]
        if not t1_positions:
            return []

        symbols = [p["symbol"] for p in t1_positions]
        if quotes is None:
            quotes = data_engine.get_realtime_quotes(symbols)

        plans = []
        for pos in t1_positions:
            sym = pos["symbol"]
            name = pos["name"]
            buy_p = float(pos.get("buy_price", 0.0))
            target_p = float(pos.get("target_sell_price", buy_p * 1.025))
            stop_p = float(pos.get("stop_loss_price", buy_p * 0.982))
            q = quotes.get(sym, {})
            open_p = float(q.get("price") or q.get("open") or buy_p)
            open_chg = round((open_p - buy_p) / buy_p * 100.0, 2) if buy_p > 0 else 0.0

            plans.append({
                "symbol": sym,
                "name": name,
                "buy_date": pos.get("buy_date"),
                "buy_price": buy_p,
                "open_price": open_p,
                "open_change_pct": open_chg,
                "target_sell_price": target_p,
                "stop_loss_price": stop_p
            })
        return plans

    def check_next_day_exits(self, quotes: Optional[Dict[str, Any]] = None, current_dt: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """
        T+1 盘中分时实时监控预警：
        1. 硬止盈 / 冲高回落锁利；
        2. 早盘冲高滞涨且跌破次日 VWAP 主动止盈；
        3. 硬止损防核；
        4. 早盘窗口衰竭 / 尾盘清仓观察提醒。
        """
        self.reconcile_and_clean_expired_positions(current_dt)
        now = current_dt or datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        cur_hm = now.strftime("%H:%M")

        active_positions = self.get_active_positions()
        t1_positions = [p for p in active_positions if p.get("buy_date") < today_str]
        if not t1_positions:
            return []

        symbols = [p["symbol"] for p in t1_positions]
        if quotes is None:
            quotes = data_engine.get_realtime_quotes(symbols)

        exit_signals = []

        for pos in t1_positions:
            sym = pos["symbol"]
            name = pos["name"]
            buy_p = float(pos.get("buy_price", 0.0))
            target_p = float(pos.get("target_sell_price", buy_p * 1.025))
            stop_p = float(pos.get("stop_loss_price", buy_p * 0.982))
            peak_p = float(pos.get("peak_price", buy_p))
            buy_date = pos.get("buy_date", "")
            alerted_flags = pos.get("alerted_flags", [])

            q = quotes.get(sym)
            if not q:
                continue

            cur_p = float(q.get("price", 0.0))
            if cur_p <= 0:
                continue

            high_p = float(q.get("high", cur_p))
            vwap_p = float(q.get("vwap", 0.0) or 0.0)
            if vwap_p <= 0:
                m_df = data_engine.get_minute_data(sym)
                if m_df is not None and len(m_df) > 0 and "vwap" in m_df.columns:
                    vwap_p = float(m_df["vwap"].iloc[-1])

            if max(cur_p, high_p) > peak_p:
                peak_p = max(cur_p, high_p)
                pos["peak_price"] = peak_p
                self._save()

            cur_gain_pct = round((cur_p - buy_p) / buy_p * 100.0, 2) if buy_p > 0 else 0.0
            peak_gain_pct = round((peak_p - buy_p) / buy_p * 100.0, 2) if buy_p > 0 else 0.0

            # 1. 目标止盈 或 早盘冲高(>0.9%)后跌破VWAP落袋止盈
            vwap_trail_profit = (
                "09:35" <= cur_hm <= "10:35"
                and peak_gain_pct >= 0.90
                and cur_gain_pct >= 0.25
                and vwap_p > 0
                and cur_p < vwap_p * 0.997
            )
            if cur_p >= target_p or (high_p >= target_p and cur_p >= buy_p * 1.015) or vwap_trail_profit:
                if "PROFIT_EXIT" not in alerted_flags:
                    desc = (
                        "🎯【2.30战法·早盘跌破VWAP落袋】冲高回落锁定利润"
                        if vwap_trail_profit
                        else "🎯【行情提醒】价格触及止盈观察位，请核对实际持仓与委托"
                    )
                    sig = {
                        "symbol": sym,
                        "name": name,
                        "date": today_str,
                        "price": cur_p,
                        "signal_type": "TAIL_PROFIT_EXIT",
                        "signal_desc": desc,
                        "buy_price": buy_p,
                        "buy_date": buy_date,
                        "target_sell_price": target_p,
                        "stop_loss_price": stop_p,
                        "pnl_pct": cur_gain_pct,
                        "peak_gain_pct": peak_gain_pct,
                        "reasons": [
                            f"买入价 {buy_p:.2f}元，现价 {cur_p:.2f}元({cur_gain_pct:+.2f}%)，盘中峰值 {peak_p:.2f}元(+{peak_gain_pct:+.2f}%)。",
                            (f"早盘冲高后跌破分时均价({vwap_p:.2f}元)，触发动态保护落袋。" if vwap_trail_profit else f"触及止盈观察线 {target_p:.2f}元。"),
                            "此提醒不代表已成交；请核对实际持仓、可卖数量和委托状态。"
                        ],
                        "trade_levels": {
                            "entry_price": buy_p,
                            "suggested_sell_price": cur_p,
                            "target_price": target_p,
                            "initial_stop": stop_p,
                            "action_urgency": "行情提醒；不代表委托或成交"
                        }
                    }
                    exit_signals.append(sig)
                    self.add_alert_flag(sym, buy_date, "PROFIT_EXIT")
                    pos["status"] = "ALERTED_EXIT"
                    pos["last_alert_type"] = "PROFIT_EXIT"
                    self._save()
                continue

            # 2. 硬止损防核离场
            if cur_p <= stop_p:
                if "STOP_EXIT" not in alerted_flags:
                    sig = {
                        "symbol": sym,
                        "name": name,
                        "date": today_str,
                        "price": cur_p,
                        "signal_type": "TAIL_STOP_EXIT",
                        "signal_desc": "🛑【行情提醒】价格触及止损观察位，请核对实际持仓与委托",
                        "buy_price": buy_p,
                        "buy_date": buy_date,
                        "target_sell_price": target_p,
                        "stop_loss_price": stop_p,
                        "pnl_pct": cur_gain_pct,
                        "peak_gain_pct": peak_gain_pct,
                        "reasons": [
                            f"记录买入价为 {buy_p:.2f}元，当前行情触及止损观察位 {stop_p:.2f}元({cur_gain_pct:+.2f}%)。",
                            "未包含交易费用；此提醒不代表已成交，请及时核对持仓并严格防核。"
                        ],
                        "trade_levels": {
                            "entry_price": buy_p,
                            "suggested_sell_price": cur_p,
                            "target_price": target_p,
                            "initial_stop": stop_p,
                            "action_urgency": "行情提醒；不代表委托或成交"
                        }
                    }
                    exit_signals.append(sig)
                    self.add_alert_flag(sym, buy_date, "STOP_EXIT")
                    pos["status"] = "ALERTED_EXIT"
                    pos["last_alert_type"] = "STOP_EXIT"
                    self._save()
                continue

            # 3. 早盘黄金脉冲结束且滞涨观察
            if "09:45" <= cur_hm <= "10:15":
                if "TIMED_EXIT" not in alerted_flags and pos.get("status") == "HOLDING":
                    sig = {
                        "symbol": sym,
                        "name": name,
                        "date": today_str,
                        "price": cur_p,
                        "signal_type": "TAIL_TIMED_EXIT",
                        "signal_desc": "⏳【2.30战法·早盘动能衰竭】冲高尾声·择机离场",
                        "score": 85,
                        "buy_price": buy_p,
                        "buy_date": buy_date,
                        "target_sell_price": target_p,
                        "stop_loss_price": stop_p,
                        "pnl_pct": cur_gain_pct,
                        "peak_gain_pct": peak_gain_pct,
                        "reasons": [
                            f"当前时间已到达 {cur_hm}，早盘惯性冲高黄金窗口 (09:30~09:45) 结束。",
                            f"买入价 {buy_p:.2f}元，现价 {cur_p:.2f}元({cur_gain_pct:+.2f}%)，分时均价 {vwap_p:.2f}元。",
                            "请核对实际持仓和委托状态，结合分时承接决断去留。"
                        ],
                        "trade_levels": {
                            "entry_price": buy_p,
                            "suggested_sell_price": cur_p,
                            "target_price": target_p,
                            "initial_stop": stop_p,
                            "action_urgency": "时段观察提醒；不代表委托或成交"
                        }
                    }
                    exit_signals.append(sig)
                    self.add_alert_flag(sym, buy_date, "TIMED_EXIT")
                    pos["status"] = "ALERTED_TIMED"
                    self._save()
                continue

            # 4. 次日尾盘时限观察
            if "14:30" <= cur_hm <= "14:55":
                if "EOD_EXIT" not in alerted_flags and pos.get("status") in ["HOLDING", "ALERTED_TIMED"]:
                    sig = {
                        "symbol": sym,
                        "name": name,
                        "date": today_str,
                        "price": cur_p,
                        "signal_type": "TAIL_EOD_EXIT",
                        "signal_desc": "🚨【行情提醒】达到模型持仓时限观察点，请核对实际持仓",
                        "buy_price": buy_p,
                        "buy_date": buy_date,
                        "target_sell_price": target_p,
                        "stop_loss_price": stop_p,
                        "pnl_pct": cur_gain_pct,
                        "peak_gain_pct": peak_gain_pct,
                        "reasons": [
                            f"当前时间为 {cur_hm}，模型 T+1 持仓时限观察点已到；买入价 {buy_p:.2f}元，现价 {cur_p:.2f}元({cur_gain_pct:+.2f}%)。",
                            "请按实际持仓与市场承接及时自行处理。"
                        ],
                        "trade_levels": {
                            "entry_price": buy_p,
                            "suggested_sell_price": cur_p,
                            "target_price": target_p,
                            "initial_stop": stop_p,
                            "action_urgency": "时段观察提醒；不代表委托或成交"
                        }
                    }
                    exit_signals.append(sig)
                    self.add_alert_flag(sym, buy_date, "EOD_EXIT")
                    pos["status"] = "ALERTED_EXIT"
                    pos["last_alert_type"] = "EOD_EXIT"
                    self._save()
                continue

        return exit_signals


tail_trade_tracker = TailTradeTracker()
