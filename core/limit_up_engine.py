"""
A股智能量化系统 2.0 - 游资弱转强打板专精引擎 (LimitUpEngine 2.0 · Weak-to-Strong Only)

核心重构定位：彻底剔除泛泛的首板突破与无序连板跟风，专精【T-1 分歧 -> T 日转强】高胜率/高盈亏比战法：
1. T-1 日“弱/分歧”前置画像（缺一不可）：
   - 前置强势基因：T-1 触及大阳(>=+5%)或近 5 日存在涨停活性且股价处于中期生命线附近；
   - 分歧形态（满足其一）：爆量烂板、炸板回落/长上影洗盘、或高位强分歧断板承接（严禁无承接一字A杀核按钮）；
2. T 日“转强”量化硬核确认：
   - 竞价/开盘超预期（跳空高开或弱转强瞬间拉升，严禁无量一字死板无法买进）；
   - 早盘 09:30~10:30 主动抢筹量能占比（占 T-1 日成交量比例达标）；
   - 真实分时 VWAP 均价线承接与反假弱转强风控（严防高开低走大阴线诱多，测算真实封板时间与炸板次数）；
   - 盘口实时封单金额 / 封成比（基于真实 Level-1 买一档挂单计算，绝不虚构）；
3. 情绪周期与板块梯队共振：融合 `sentiment_radar` 与腾讯概念板块涨停广度，优选主线题材助攻核心；
4. 原子持久化账本与严格因果 5 分钟级历史回测。
"""
import csv
import io
import math
import shutil
import threading
import concurrent.futures
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple

import numpy as np
import pandas as pd

from config import DATA_DIR, LIMIT_UP_TRADES_FILE, LIMIT_UP_CSV_FILE, load_config
from core.data_engine import data_engine
from core.sentiment_radar import sentiment_radar
from core.persistence import atomic_write_json, atomic_write_text, safe_read_json


class LimitUpEngine:
    SYNTHETIC_SEED_IDS = {
        "sh600501_20261028",
        "sh603103_20261028",
        "sz002535_20261027",
        "sh600592_20261027",
        "sz002842_20261026",
    }

    def __init__(self):
        self._trades: List[Dict[str, Any]] = []
        self._lock = threading.RLock()
        self._cached_wzs_backtest: Optional[Dict[str, Any]] = None
        self._load_trades()

    def _load_trades(self):
        """载入本地弱转强打板信号跟踪记录；旧版清理并支持受损恢复。"""
        with self._lock:
            if LIMIT_UP_TRADES_FILE.exists():
                loaded_trades = safe_read_json(LIMIT_UP_TRADES_FILE, None)
                if not isinstance(loaded_trades, list):
                    self._trades = []
                    return

                filtered_trades = [
                    trade for trade in loaded_trades
                    if isinstance(trade, dict) and str(trade.get("id", "")) not in self.SYNTHETIC_SEED_IDS
                ]
                needs_legacy_migration = any(
                    trade.get("data_provenance_version") != 2
                    for trade in filtered_trades
                )
                if len(filtered_trades) != len(loaded_trades) or needs_legacy_migration:
                    backup_path = LIMIT_UP_TRADES_FILE.with_suffix(".json.pre_seed_cleanup.bak")
                    if backup_path.exists():
                        backup_path = LIMIT_UP_TRADES_FILE.with_name(
                            f"{LIMIT_UP_TRADES_FILE.stem}.pre_seed_cleanup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak"
                        )
                    try:
                        shutil.copy2(LIMIT_UP_TRADES_FILE, backup_path)
                    except OSError:
                        pass
                    for trade in filtered_trades:
                        if trade.get("data_provenance_version") != 2:
                            trade["seal_turnover_ratio"] = None
                            trade["open_count"] = None
                            trade["data_provenance_version"] = 2
                    self._trades = filtered_trades
                    self._save_trades()
                    self.export_csv()
                    return

                self._trades = filtered_trades
                return

            self._trades = []
            self._save_trades()
            self.export_csv()

    def _save_trades(self):
        """原子写盘保存打板台账 JSON"""
        with self._lock:
            atomic_write_json(LIMIT_UP_TRADES_FILE, self._trades)

    def export_csv(self) -> str:
        """原子导出/更新带 UTF-8 BOM 的弱转强打板实战流水账本 CSV"""
        try:
            headers = [
                "信号编号", "信号日期", "股票代码", "股票名称", "弱转强形态分类", "连板高度",
                "触发参考价(元)", "封板时间", "弱转强置信分", "封成比(%)", "炸板次数", "成交额(万元)",
                "T-1分歧特征", "所属主线板块",
                "次日开盘价(元)", "次日开盘变动(%)", "次日最高价(元)", "次日最高变动(%)",
                "次日收盘价(元)", "次日收盘变动(%)", "次日继续涨停", "行情跟踪状态", "量化规则依据"
            ]
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(headers)
            with self._lock:
                for t in self._trades:
                    writer.writerow([
                        t.get("id", ""),
                        t.get("date", ""),
                        t.get("symbol", ""),
                        t.get("name", ""),
                        t.get("board_type", ""),
                        t.get("ladder_count", 1),
                        t.get("seal_price", 0.0),
                        t.get("seal_time") or "",
                        t.get("hardness_score", 0.0),
                        t.get("seal_turnover_ratio") if t.get("seal_turnover_ratio") is not None else "",
                        t.get("open_count") if t.get("open_count") is not None else "",
                        round(float(t["turnover_amount"]) / 10000.0, 1) if t.get("turnover_amount") is not None else "",
                        t.get("t1_divergence_type") or "",
                        t.get("sector") or "",
                        t.get("next_open_price", "-"),
                        t.get("next_open_pnl", "-"),
                        t.get("next_high_price", "-"),
                        t.get("next_high_pnl", "-"),
                        t.get("next_close_price", "-"),
                        t.get("next_close_pnl", "-"),
                        "是" if t.get("is_consecutive") else "否",
                        t.get("status", "HOLDING"),
                        "; ".join(t.get("reasons", []))
                    ])
            atomic_write_text(LIMIT_UP_CSV_FILE, buf.getvalue(), encoding="utf-8-sig")
            return str(LIMIT_UP_CSV_FILE)
        except Exception as e:
            print(f"[LimitUpEngine] 导出CSV表格异常: {e}")
            return ""

    @staticmethod
    def assess_t1_divergence(df: pd.DataFrame, idx: int = -1) -> Optional[Dict[str, Any]]:
        """
        评估 T-1 日（即 idx-1）是否具备“强势股良性分歧/弱态”前置画像：
        仅接受以下 3 类高确定性弱转强前置母本，彻底排除无波动平盘首板与一字死板：
        1. 爆量烂板（T-1 涨停但伴随 >=1.65 倍剧烈放量或 >=4% 深水回踩洗盘换手）
        2. 炸板回落 / 冲高长上影洗盘（T-1 盘中冲高 >=5% 或触及涨停，上影线 >=3.2%，未触发崩溃核按钮）
        3. 强势断板分歧承接（近 4 日曾有涨停，T-1 分歧断板但非一字跌停 A 杀、尾盘有承接）
        """
        length = len(df)
        if idx < 0:
            idx = length + idx
        if idx < 7 or idx >= length:
            return None

        prev = df.iloc[idx - 1]   # T-1
        prev2 = df.iloc[idx - 2]  # T-2

        t1_open = float(prev["open"])
        t1_high = float(prev["high"])
        t1_low = float(prev["low"])
        t1_close = float(prev["close"])
        t1_vol = float(prev["volume"])
        t2_close = float(prev2["close"])

        if t2_close <= 0 or t1_close <= 0 or t1_low <= 0:
            return None

        t1_pct = (t1_close - t2_close) / t2_close * 100.0
        t1_high_pct = (t1_high - t2_close) / t2_close * 100.0
        t1_open_pct = (t1_open - t2_close) / t2_close * 100.0
        t1_limit_up = round(t2_close * 1.10, 2)
        t1_touched_limit = (t1_high >= t1_limit_up - 0.02) or (t1_high_pct >= 9.6)
        t1_sealed_limit = (t1_pct >= 9.6) and (abs(t1_close - t1_high) <= 0.03)

        # 前5日均量 (idx-6 .. idx-2)
        vol_win = pd.to_numeric(df["volume"].iloc[max(0, idx - 6):idx - 1], errors="coerce").dropna()
        vol_ma5_prev = float(vol_win.mean()) if len(vol_win) >= 3 and float(vol_win.mean()) > 0 else t1_vol
        t1_vol_ratio = round(t1_vol / vol_ma5_prev, 2) if vol_ma5_prev > 0 else 1.0

        # 近 5 日是否具有强势活性（至少出现过涨停或 T-1 盘中强势冲击 >= +5%）
        recent_zt_count = 0
        for k in range(max(1, idx - 5), idx):
            c_k = float(df["close"].iloc[k])
            c_k1 = float(df["close"].iloc[k - 1])
            if c_k1 > 0 and ((c_k - c_k1) / c_k1 * 100.0 >= 9.6):
                recent_zt_count += 1

        prior_ladder = 0
        for k in range(idx - 1, max(0, idx - 8), -1):
            c_k = float(df["close"].iloc[k])
            c_k1 = float(df["close"].iloc[k - 1])
            if c_k1 > 0 and ((c_k - c_k1) / c_k1 * 100.0 >= 9.6):
                prior_ladder += 1
            else:
                break

        ma20_t1 = float(df["close"].iloc[max(0, idx - 20):idx].mean())
        if t1_close < ma20_t1 * 0.94:
            # 远低于生命线的破位反抽，不属于强势核心弱转强
            return None

        upper_shadow_pct = (t1_high - max(t1_open, t1_close)) / t2_close * 100.0
        lower_shadow_pct = (min(t1_open, t1_close) - t1_low) / t2_close * 100.0
        drawdown_from_high_pct = (t1_high - t1_close) / t2_close * 100.0
        amp_t1_pct = (t1_high - t1_low) / t2_close * 100.0

        # 拒绝无承接一字跌停 A 杀核按钮，并拒绝天地/地天超极端剧烈妖股(T-1振幅>16%)隔日接力
        max_t1_amp = float(load_config().get("limit_up_wzs_max_t1_amp_pct", 16.0))
        if t1_pct <= -9.5 and abs(t1_close - t1_low) <= 0.02:
            return None
        if amp_t1_pct > max_t1_amp:
            return None

        div_type = None
        div_desc = ""
        div_score = 0.0

        # 形态 B: 炸板回落 / 冲高长上影洗盘 (核心弱转强最暴利母本)
        if t1_touched_limit and not t1_sealed_limit and t1_pct >= -6.5:
            div_type = "炸板反包弱转强"
            div_desc = f"T-1曾封/触涨停后炸板分歧回落(收{t1_pct:+.2f}%, 振幅{amp_t1_pct:.1f}%, 量比{t1_vol_ratio:.1f}x)"
            div_score = 18.0 if t1_pct >= -1.0 else 15.0
        elif t1_high_pct >= 5.0 and upper_shadow_pct >= 3.2 and t1_pct >= -4.5:
            div_type = "长上影洗盘弱转强"
            div_desc = f"T-1盘中大涨{t1_high_pct:+.1f}%后长上影({upper_shadow_pct:.1f}%)充分洗盘，收{t1_pct:+.2f}%"
            div_score = 15.0
        # 形态 A: 爆量烂板 / 深水换手板 (T-1封死涨停但盘中分歧极大)
        elif t1_sealed_limit and (t1_vol_ratio >= 1.60 or (t1_close - t1_low) / t2_close * 100.0 >= 4.0 or t1_open_pct <= 1.0):
            # 排除毫无换手分歧的一字缩量板
            is_t1_one_word = abs(t1_open - t1_high) <= 0.01 and abs(t1_open - t1_low) <= 0.01
            if not is_t1_one_word:
                div_type = "爆量烂板弱转强"
                div_desc = f"T-1放量分歧烂板(量比{t1_vol_ratio:.1f}x, 振幅{amp_t1_pct:.1f}%)，今日筹码再度统一"
                div_score = 16.0
        # 形态 C: 强势连板/活跃股分歧断板，尾盘仍有承接
        elif recent_zt_count >= 1 and not t1_sealed_limit and -6.8 <= t1_pct <= 7.0 and (
            t1_vol_ratio >= 1.15 or lower_shadow_pct >= 1.5 or (t1_close - t1_low) / t2_close * 100.0 >= 2.0
        ):
            div_type = "断板反包弱转强"
            div_desc = f"近{recent_zt_count}板强势核心T-1断板洗盘(收{t1_pct:+.2f}%, 下影{lower_shadow_pct:.1f}%)"
            div_score = 16.0 if prior_ladder == 0 else 17.0

        if not div_type:
            return None

        return {
            "div_type": div_type,
            "div_desc": div_desc,
            "div_score": div_score,
            "t1_pct": round(t1_pct, 2),
            "t1_high_pct": round(t1_high_pct, 2),
            "t1_vol_ratio": t1_vol_ratio,
            "t1_vol": t1_vol,
            "t1_sealed_limit": t1_sealed_limit,
            "prior_ladder": prior_ladder,
            "recent_zt_count": recent_zt_count,
            "t1_upper_shadow_pct": round(upper_shadow_pct, 2),
            "t1_amp_pct": round(amp_t1_pct, 2),
        }

    @staticmethod
    def _normalize_intraday_bars(minute_df: pd.DataFrame, cutoff_hm: str = "15:00") -> Optional[pd.DataFrame]:
        """规范化 1 分钟实时分时与 5 分钟历史数据用于转强微观确认。"""
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

        is_5m = {"open", "high", "low", "close"}.issubset(df.columns)
        if is_5m:
            out = pd.DataFrame({
                "time_str": df["t_col"],
                "open": pd.to_numeric(df["open"], errors="coerce"),
                "high": pd.to_numeric(df["high"], errors="coerce"),
                "low": pd.to_numeric(df["low"], errors="coerce"),
                "close": pd.to_numeric(df["close"], errors="coerce"),
                "bar_vol": pd.to_numeric(df["volume"], errors="coerce").fillna(0.0).clip(lower=0.0),
                "vwap": pd.to_numeric(df["vwap"], errors="coerce") if "vwap" in df.columns else np.nan,
                "is_5m": True,
            })
        else:
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
                "is_5m": False,
            })

        out = out.dropna(subset=["close"]).reset_index(drop=True)
        out = out[out["close"] > 0].reset_index(drop=True)
        if len(out) == 0:
            return None

        if out["vwap"].isna().any() or (out["vwap"] <= 0).any():
            tp = (out["high"].fillna(out["close"]) + out["low"].fillna(out["close"]) + out["close"]) / 3.0
            cv = out["bar_vol"].cumsum()
            cpv = (tp * out["bar_vol"]).cumsum()
            out["vwap"] = np.where(cv > 0, cpv / cv, out["close"])
        return out

    def _evaluate_intraday_strengthening(
        self,
        minute_df: pd.DataFrame,
        prev_close: float,
        t1_vol: float,
        limit_target: float,
        cutoff_hm: str = "15:00"
    ) -> Dict[str, Any]:
        """
        T 日分时微观“转强 + 反假弱转强诱多”量化评估：
        1. 早盘 09:30~10:00 主动放量抢筹比例；
        2. 分时稳站均价线(VWAP)占比与抗回撤韧性（防高开低走大阴线诱多）；
        3. 精准计算首次封板时间 `seal_time` 与炸板次数 `open_count`。
        """
        bars = self._normalize_intraday_bars(minute_df, cutoff_hm=cutoff_hm)
        if bars is None or len(bars) < 3 or prev_close <= 0:
            return {"passed": True, "has_intraday": False}

        closes = bars["close"].to_numpy(dtype=float)
        highs = bars["high"].to_numpy(dtype=float)
        lows = bars["low"].to_numpy(dtype=float)
        vwaps = bars["vwap"].to_numpy(dtype=float)
        vols = bars["bar_vol"].to_numpy(dtype=float)
        times = bars["time_str"].tolist()
        is_5m = bool(bars["is_5m"].iloc[0])

        t0_open = float(bars["open"].iloc[0])
        early_mask = np.array([t <= "09:50" for t in times], dtype=bool)
        if np.any(early_mask):
            early_15_low = float(np.min(lows[early_mask]))
            early_15_close = float(closes[early_mask][-1])
            # 反假弱转强大阴线保护：早盘前20分钟内高开骤砸击穿承接线（跌破昨收且回撤超3.8%，或收于开盘价-3.5%以下且水下）
            if (early_15_low < t0_open * 0.962 and early_15_low < prev_close * 0.990) or (
                early_15_low < t0_open * 0.962 and early_15_close < prev_close
            ):
                return {
                    "passed": False,
                    "has_intraday": True,
                    "reject_reason": "早盘高开后瞬间放量下砸击穿承接线，疑为假弱转强诱多出货"
                }

        # 上午及全时段（截止当前/封板前）站稳 VWAP 比例（若上午有数据则同步检上午段，防止尾盘偷拉掩盖早盘溃败）
        vwap_hold_ratio = float(np.mean(closes >= vwaps * 0.996))
        morning_mask = np.array([t <= "11:30" for t in times], dtype=bool)
        morning_vwap_ratio = float(np.mean(closes[morning_mask] >= vwaps[morning_mask] * 0.996)) if np.any(morning_mask) else vwap_hold_ratio
        if vwap_hold_ratio < 0.60 or (np.sum(morning_mask) >= 3 and morning_vwap_ratio < 0.50):
            return {
                "passed": False,
                "has_intraday": True,
                "reject_reason": f"分时站稳VWAP仅{vwap_hold_ratio*100:.0f}%(早盘{morning_vwap_ratio*100:.0f}%)，承接偏弱"
            }

        # 早盘 09:30~10:00 抢筹成交量占 T-1 日成交量比例
        morning_30_mask = np.array([t <= "10:00" for t in times], dtype=bool)
        raw_early_vol = float(np.sum(vols[morning_30_mask])) if np.any(morning_30_mask) else float(np.sum(vols))
        # 单位尺度自洽：1m 腾讯分时为手，新浪 5m 为股，日线腾讯多为手
        early_vol_ratio = None
        if t1_vol > 0 and raw_early_vol > 0:
            ratio_direct = raw_early_vol / t1_vol
            # 新浪 5m bar_vol 为股而腾讯日线为手时差 100 倍
            early_vol_ratio = round(ratio_direct / 100.0 if (is_5m and ratio_direct > 5.0) else ratio_direct, 3)

        cfg = load_config()
        min_early_ratio = float(cfg.get("limit_up_wzs_min_early_vol_ratio", 0.12))
        # 若数据覆盖到 10:00 之后才卡 09:30-10:00 占比；开盘几分钟内按时间流逝折算
        elapsed_mins = min(30, len(bars) * (5 if is_5m else 1))
        adj_min_ratio = min_early_ratio * (elapsed_mins / 30.0)
        if early_vol_ratio is not None and early_vol_ratio < adj_min_ratio:
            return {
                "passed": False,
                "has_intraday": True,
                "reject_reason": f"早盘抢筹量能不足(占T-1全天{early_vol_ratio*100:.1f}% < 要求{adj_min_ratio*100:.1f}%)，拒绝无量虚高"
            }

        # 精确捕捉首次触及涨停板时间 & 炸板次数
        seal_idx = None
        for idx_bar in range(len(bars)):
            if highs[idx_bar] >= limit_target - 0.02 or closes[idx_bar] >= limit_target - 0.02:
                seal_idx = idx_bar
                break

        seal_time = times[seal_idx] if seal_idx is not None else None
        open_count = 0
        if seal_idx is not None:
            is_currently_sealed = closes[seal_idx] >= limit_target - 0.02
            for j in range(seal_idx + 1, len(bars)):
                bar_sealed = closes[j] >= limit_target - 0.02 and lows[j] >= limit_target - 0.03
                if is_currently_sealed and not bar_sealed:
                    open_count += 1
                    is_currently_sealed = False
                elif not is_currently_sealed and closes[j] >= limit_target - 0.02:
                    is_currently_sealed = True

        return {
            "passed": True,
            "has_intraday": True,
            "vwap_hold_ratio": round(vwap_hold_ratio, 3),
            "early_vol_ratio": early_vol_ratio,
            "seal_time": seal_time,
            "open_count": open_count if seal_idx is not None else None,
        }

    def evaluate_limit_up_bar(
        self,
        symbol: str,
        name: str,
        df: pd.DataFrame,
        minute_df: Optional[pd.DataFrame] = None,
        market_state: Optional[Dict[str, Any]] = None,
        plate_info: Optional[Dict[str, Any]] = None,
        quote: Optional[Dict[str, Any]] = None,
        allow_early_surge: bool = False,
        cutoff_hm: str = "15:00"
    ) -> Optional[Dict[str, Any]]:
        """
        严格评估是否满足【弱转强（Weak-to-Strong）】打板战法：
        不满足 T-1 分歧母本或 T 日转强门槛的一律返回 None，绝不收录普通首板或盲目连板！
        """
        if df is None or len(df) < 20:
            return None

        cfg = load_config()
        min_open_pct = float(cfg.get("limit_up_wzs_min_open_pct", 1.5))
        max_open_pct = float(cfg.get("limit_up_wzs_max_open_pct", 8.0))
        max_seal_time = str(cfg.get("limit_up_wzs_max_seal_time", "11:00"))

        # 0. 大盘极端系统性杀跌且无修复保护
        if market_state is not None:
            if not market_state.get("data_available", False):
                return None
            if float(market_state.get("temp_score", 0.0)) < 25.0 and float(market_state.get("index_pct", 0.0)) < -0.5:
                return None

        curr = df.iloc[-1]
        prev = df.iloc[-2]
        close = float(curr["close"])
        prev_close = float(prev["close"])
        open_price = float(curr["open"])
        high = float(curr["high"])
        low = float(curr["low"])

        if prev_close <= 0 or close <= 0:
            return None

        # 1. 硬前置门控：检验 T-1 分歧弱态画像（缺之直接剔除！）
        div_info = self.assess_t1_divergence(df, idx=-1)
        if not div_info:
            return None

        pct_change = round((close - prev_close) / prev_close * 100.0, 2)
        open_pct = round((open_price - prev_close) / prev_close * 100.0, 2)
        limit_target = round(prev_close * 1.10, 2)
        is_limit_up = (pct_change >= 9.60) and (abs(close - limit_target) <= 0.03 or abs(close - high) <= 0.03)

        # 2. T 日转强形态确认：涨停封板/触板，或在早盘强攻扫描模式下的高速冲击(+6.8%且稳站分时高点)
        is_early_surge = (
            allow_early_surge
            and pct_change >= 6.80
            and close >= open_price
            and (high - close) / prev_close * 100.0 <= 1.2
        )
        if not (is_limit_up or is_early_surge):
            return None

        # 3. 踢除一字缩量死板（无买入成交机会）
        is_one_price_board = (
            abs(open_price - high) <= 0.01
            and abs(open_price - low) <= 0.01
            and abs(open_price - close) <= 0.01
        )
        if is_one_price_board:
            return None

        # 4. 竞价/开盘超预期区间校验
        # 若为纯粹日线无分钟校验（如离线静态研判），要求至少稳健高开或平开强势拉升 (+0.3% ~ +8.5%)
        eff_min_open = min_open_pct if minute_df is not None else max(0.2, min_open_pct)
        if open_pct < eff_min_open or open_pct > max_open_pct:
            return None

        # 5. 计算连板高度 & 强转强形态分类
        ladder_count = 1
        if is_limit_up:
            ladder_count = 1 + int(div_info.get("prior_ladder", 0))
            if ladder_count >= 2:
                board_type = f"高标分歧转强({ladder_count}板)"
            else:
                board_type = div_info["div_type"]
        else:
            board_type = f"早盘扫板预警·{div_info['div_type']}"

        # 6. 分时微观与反诱多确认（提供 minute_df 时执行硬校验 + 严格早盘/上午封板时效门控）
        intraday_info = {"passed": True, "has_intraday": False}
        if minute_df is not None:
            intraday_info = self._evaluate_intraday_strengthening(
                minute_df=minute_df,
                prev_close=prev_close,
                t1_vol=div_info["t1_vol"],
                limit_target=limit_target,
                cutoff_hm=cutoff_hm
            )
            if not intraday_info.get("passed", False):
                return None
            if intraday_info.get("seal_time") and str(intraday_info["seal_time"]) > max_seal_time:
                return None

        # 7. 成交额、真实盘口封单、换手率、板块共振计算
        turnover_raw = pd.to_numeric(pd.Series([curr.get("turnover")]), errors="coerce").iloc[0]
        turnover = float(turnover_raw) if pd.notna(turnover_raw) and np.isfinite(turnover_raw) and turnover_raw > 0 else None

        seal_turnover_ratio = None
        seal_amount = None
        if quote:
            if not turnover and quote.get("turnover"):
                turnover = float(quote["turnover"])
            bid1 = float(quote.get("bid1", 0.0) or 0.0)
            bid1_vol = float(quote.get("bid1_vol", 0.0) or 0.0)
            ask1_vol = float(quote.get("ask1_vol", 0.0) or 0.0)
            if bid1 >= limit_target - 0.02 and bid1_vol > 0 and ask1_vol == 0:
                seal_amount = round(bid1 * bid1_vol * 100.0, 1)
                if turnover and turnover > 0:
                    seal_turnover_ratio = round(seal_amount / turnover * 100.0, 2)

        # 8. 弱转强综合置信度评分 (0 ~ 99)
        score = 52.0 + float(div_info["div_score"])
        reasons = [
            f"前置分歧: {div_info['div_desc']}",
            f"竞价超预期: 今日高开 {open_pct:+.2f}% 打破昨日弱势预期"
        ]

        # 高开超预期强度加分 (+1.5% ~ +6.5% 为黄金弱转强高开区间)
        if 1.5 <= open_pct <= 6.5:
            score += 10.0
        elif 0.5 <= open_pct < 1.5:
            score += 5.0
        else:
            score += 2.0

        # 分时封板时间与质量加减分
        seal_time = intraday_info.get("seal_time")
        open_count = intraday_info.get("open_count")
        early_vol_r = intraday_info.get("early_vol_ratio")

        if intraday_info.get("has_intraday"):
            vwap_hr = intraday_info.get("vwap_hold_ratio", 0.7)
            if vwap_hr >= 0.85:
                score += 6.0
            if early_vol_r is not None:
                if 0.20 <= early_vol_r <= 0.75:
                    score += 6.0
                    reasons.append(f"早盘30m放量抢筹({early_vol_r*100:.1f}% T-1全天量)，资金转强主动性极高")
                else:
                    reasons.append(f"早盘30m抢筹占比 {early_vol_r*100:.1f}%，稳站分时均价({vwap_hr*100:.0f}%)")
            if seal_time:
                if seal_time <= "10:00":
                    score += 8.0
                    reasons.append(f"早盘极速转强封板({seal_time})，辨识度领涨全市场")
                elif seal_time <= "10:35":
                    score += 5.0
                    reasons.append(f"上午主动转强上板({seal_time})")
                elif seal_time > "13:30":
                    score -= 4.0
            if open_count is not None:
                if open_count == 0:
                    score += 4.0
                elif open_count >= 3:
                    score -= 7.0
                    reasons.append(f"盘中多次炸板({open_count}次)，注意封板稳定性风险")
        else:
            # 纯日线评估时（无分时流），用均线多头与量比微调
            vol_history = pd.to_numeric(df["volume"].iloc[-6:-1], errors="coerce").dropna()
            vol_5 = float(vol_history.mean()) if len(vol_history) >= 3 else 0.0
            cur_vol = float(curr.get("volume", 0.0) or 0.0)
            if vol_5 > 0 and cur_vol / vol_5 >= 1.2:
                score += 6.0

        # 真实封成比加分
        if seal_turnover_ratio is not None:
            if seal_turnover_ratio >= 12.0:
                score += 6.0
                reasons.append(f"盘口买一强封单 {seal_amount/10000.0:.0f}万元 (真实封成比 {seal_turnover_ratio:.1f}%)")
            elif seal_turnover_ratio >= 4.0:
                score += 3.0
                reasons.append(f"盘口封单 {seal_amount/10000.0:.0f}万元 (封成比 {seal_turnover_ratio:.1f}%)")

        # 板块梯队共振与题材助攻加成
        sector_name = "主板"
        max_sector_zt = 0
        if plate_info:
            sector_name = plate_info.get("primary_sector", "主板")
            max_sector_zt = int(plate_info.get("max_sector_zt", 0) or 0)
            hot_concepts = plate_info.get("hot_concepts", [])
            if max_sector_zt >= 2:
                bonus = min(10.0, 4.0 + max_sector_zt * 1.0)
                score += bonus
                reasons.append(f"板块强共振助攻: {'、'.join(hot_concepts[:2])}")
            elif max_sector_zt == 0 and market_state and float(market_state.get("temp_score", 50.0)) < 50.0:
                score -= 5.0

        # 均线保护
        ma5 = float(df["close"].iloc[-5:].mean())
        ma20 = float(df["close"].iloc[-20:].mean())
        if close > ma5 > ma20:
            score += 4.0

        hardness_score = round(float(np.clip(score, 45.0, 99.0)), 1)

        return {
            "symbol": symbol,
            "name": name,
            "date": str(curr["date"])[:10],
            "price": close,
            "seal_price": close,
            "open_pct": open_pct,
            "change_pct": pct_change,
            "board_type": board_type,
            "t1_divergence_type": div_info["div_type"],
            "ladder_count": ladder_count,
            "is_one_price_board": False,
            "seal_time": seal_time,
            "hardness_score": hardness_score,
            "score_type": "弱转强量化置信度分(T-1分歧×早盘抢筹×VWAP承接×板块共振)",
            "seal_turnover_ratio": seal_turnover_ratio,
            "seal_amount": seal_amount,
            "open_count": open_count,
            "early_vol_ratio": early_vol_r,
            "turnover_amount": turnover,
            "sector": sector_name,
            "sector_zt_count": max_sector_zt,
            "reasons": reasons,
            "beginner_guide": {
                "data_limitations": "严格遵循【弱转强】专精模型与分时微结构评估；非委托指令，实盘需核对题材核心地位与市场承接"
            },
            "trade_levels": {
                "entry_price": close,
                "target_price": round(close * 1.06, 2),
                "initial_stop": round(close * 0.955, 2)
            }
        }

    def send_limit_up_signal(self, candidate: Dict[str, Any], force: bool = False) -> Dict[str, Any]:
        """向钉钉与微信推送弱转强打板候选摘要。"""
        from notifier.manager import notification_manager
        sym = candidate.get("symbol")
        name = candidate.get("name")
        price = float(candidate.get("price") or candidate.get("seal_price") or 0.0)
        b_type = candidate.get("board_type", "弱转强打板")

        signal_payload = {
            "symbol": sym,
            "name": name,
            "date": candidate.get("date") or datetime.now().strftime("%Y-%m-%d"),
            "signal_type": "LIMIT_UP",
            "signal_desc": f"⚡【弱转强专精打板】{b_type}",
            "price": price,
            "seal_price": price,
            "board_type": b_type,
            "ladder_count": candidate.get("ladder_count", 1),
            "hardness_score": candidate.get("hardness_score"),
            "score_type": candidate.get("score_type", "弱转强量化置信度分"),
            "seal_time": candidate.get("seal_time"),
            "seal_turnover_ratio": candidate.get("seal_turnover_ratio"),
            "open_count": candidate.get("open_count"),
            "trade_levels": candidate.get("trade_levels", {}),
            "reasons": candidate.get("reasons", [])
        }
        res = notification_manager.send_signal(signal_payload, force=force)
        return {"success": True, "res": res, "signal": signal_payload}

    def scan_today_candidates(self, limit: int = 15, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        全市场主板扫描今日符合【弱转强（Weak-to-Strong）】的顶级打板标的：
        - 早盘 09:30~10:30 兼顾强攻扫描（涨幅 >= +6.5% 且高开分时强承接）与已封涨停股；
        - 其他时段筛选涨幅 >= +9.6% 主板涨停股，逐一穿透 T-1 分歧、分时 VWAP、早盘抢筹与板块共振。
        """
        now = datetime.now()
        cur_hm = now.strftime("%H:%M")
        in_prime_wzs_window = (now.weekday() < 5) and ("09:30" <= cur_hm <= "10:35")
        cutoff_hm = cur_hm if (now.weekday() < 5 and "09:30" <= cur_hm <= "15:00") else "15:00"

        market_state = sentiment_radar.analyze_market_regime()
        if not market_state.get("data_available", False):
            return []
        if float(market_state.get("temp_score", 0.0)) < 25.0 and float(market_state.get("index_pct", 0.0)) < -0.5:
            print("[LimitUp-WZS] 市场处于极度冰点系统性杀跌未见修复，暂停弱转强扫板。")
            return []

        stocks = data_engine.get_main_board_stock_list(force_refresh=force_refresh)
        min_chg = 6.8 if in_prime_wzs_window else 9.6
        active_stocks = [
            s for s in stocks
            if float(s.get("change_pct", 0) or 0) >= min_chg
            and float(s.get("turnover", 0) or 0) >= 50000000.0  # 排除僵尸小票，最低 5000 万成交
        ]
        # 按涨幅降序、成交额降序排序，优先检验封板及核心高成交活跃标的
        active_stocks.sort(
            key=lambda x: (float(x.get("change_pct", 0) or 0) >= 9.6, float(x.get("turnover", 0) or 0)),
            reverse=True
        )
        target_stocks = active_stocks[:90]
        if not target_stocks:
            return []

        quotes = data_engine.get_realtime_quotes([s["symbol"] for s in target_stocks if s.get("symbol")])

        def _eval_one(s):
            sym = s.get("symbol")
            name = s.get("name", sym)
            try:
                df = data_engine.get_daily_kline(sym, count=45)
                if df is None or len(df) < 20:
                    return None
                # 快速初筛 T-1 分歧，避免无效分时/板块接口开销
                if not self.assess_t1_divergence(df, idx=-1):
                    return None
                minute_df = data_engine.get_minute_data(sym)
                plate_info = data_engine.get_stock_plates(sym)
                return self.evaluate_limit_up_bar(
                    symbol=sym,
                    name=name,
                    df=df,
                    minute_df=minute_df,
                    market_state=market_state,
                    plate_info=plate_info,
                    quote=quotes.get(sym),
                    allow_early_surge=in_prime_wzs_window,
                    cutoff_hm=cutoff_hm
                )
            except Exception:
                return None

        candidates: List[Dict[str, Any]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            for res in executor.map(_eval_one, target_stocks):
                if res:
                    candidates.append(res)

        candidates.sort(key=lambda x: x.get("hardness_score", 0.0), reverse=True)
        return candidates[:limit]

    def calculate_wzs_backtest(
        self,
        sample_symbols: Optional[List[str]] = None,
        days: int = 45
    ) -> Dict[str, Any]:
        """
        严格因果 5 分钟级【弱转强打板】历史回测：
        - T-1 日：严格验证是否符合 爆量烂板 / 炸板回落 / 断板承接；
        - T 日：逐根 5 分钟回放早盘（<= 14:30），确认竞价高开超预期 + 前15分钟反假弱转强未破位
          + 早盘抢筹量能 + VWAP 承接，并在非一字板的首次上板/封板时刻点以涨停价+滑点进场；
        - T+1 日：逐根 5 分钟推演次日开盘溢价与实战卖出表现（防跌破 VWAP / 冲高兑现 / 硬止损）。
        """
        if not sample_symbols:
            try:
                stocks = data_engine.get_main_board_stock_list()
                elig = [s for s in stocks if data_engine.is_main_board_equity(s.get("symbol", "")) and float(s.get("turnover", 0) or 0) >= 150000000.0]
                elig.sort(key=lambda x: float(x.get("turnover", 0) or 0), reverse=True)
                sample_symbols = [s["symbol"] for s in elig[:50]]
            except Exception:
                sample_symbols = []
        if not sample_symbols:
            sample_symbols = ["sz002842", "sh601138", "sz002156", "sh601127", "sh603993", "sz002475", "sz002594"]

        sample_symbols = [s for s in sample_symbols if data_engine.is_main_board_equity(s)]
        cfg = load_config()
        min_open_pct = float(cfg.get("limit_up_wzs_min_open_pct", 1.5))
        max_open_pct = float(cfg.get("limit_up_wzs_max_open_pct", 8.0))
        max_seal_time = str(cfg.get("limit_up_wzs_max_seal_time", "11:00"))

        sh_df = data_engine.get_daily_kline("sh000001", count=max(days + 80, 140))
        from core.strategy_tail_230 import TailStrategy230
        regime_map = TailStrategy230._build_historical_index_regime_map(sh_df)

        symbol_daily: Dict[str, pd.DataFrame] = {}
        symbol_5m: Dict[str, pd.DataFrame] = {}

        def _load(sym: str):
            return sym, data_engine.get_daily_kline(sym, count=max(days + 60, 120)), data_engine.get_intraday_kline_5m(sym, count=2000)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            for sym, d_df, m5_df in executor.map(_load, sample_symbols):
                if d_df is not None and len(d_df) >= 25:
                    d_df = d_df.copy()
                    d_df["date_str"] = d_df["date"].astype(str).str[:10]
                    symbol_daily[sym] = d_df
                if m5_df is not None and len(m5_df) >= 96:
                    symbol_5m[sym] = m5_df

        trades: List[Dict[str, Any]] = []
        friction_pct = 0.20

        for sym, d_df in symbol_daily.items():
            info = data_engine.resolve_symbol(sym)
            name = info.get("name", sym) if info else sym
            m5_df = symbol_5m.get(sym)
            if m5_df is None:
                continue
            m5_by_date = {d: grp.reset_index(drop=True) for d, grp in m5_df.groupby("date_str")}
            dates = d_df["date_str"].tolist()

            for i in range(10, len(d_df) - 1):
                t_date = dates[i]
                t1_next_date = dates[i + 1]
                if t_date not in m5_by_date or t1_next_date not in m5_by_date:
                    continue

                # 1. T-1 分歧检验（严格只用 <= i-1 日数据）
                div_info = self.assess_t1_divergence(d_df, idx=i)
                if not div_info:
                    continue

                hist_m = regime_map.get(t_date)
                if hist_m and hist_m.get("temp_score", 50.0) < 25.0 and hist_m.get("index_pct", 0.0) < -0.5:
                    continue

                prev_close = float(d_df.iloc[i - 1]["close"])
                if prev_close <= 0:
                    continue
                limit_target = round(prev_close * 1.10, 2)

                t_bars = m5_by_date[t_date]
                if len(t_bars) < 6:
                    continue

                t0_open = float(t_bars["open"].iloc[0])
                open_pct = (t0_open - prev_close) / prev_close * 100.0
                if open_pct < min_open_pct or open_pct > max_open_pct:
                    continue

                # 排除全天一字板不可成交
                if t0_open >= limit_target - 0.02 and float(t_bars["low"].iloc[:6].min()) >= limit_target - 0.02:
                    continue

                # 2. 逐根回放 T 日 5 分钟寻找上午黄金窗口内的非一字打板确认时点
                trigger_idx = None
                for k in range(len(t_bars)):
                    bt = str(t_bars["time_str"].iloc[k])
                    if bt > max_seal_time:
                        break
                    bh = float(t_bars["high"].iloc[k])
                    bc = float(t_bars["close"].iloc[k])
                    if bh >= limit_target - 0.02 or bc >= limit_target - 0.02:
                        # 检查截至该时点的微观承接与反诱多风控
                        slice_bars = t_bars.iloc[:k + 1]
                        micro = self._evaluate_intraday_strengthening(
                            slice_bars, prev_close=prev_close, t1_vol=div_info["t1_vol"],
                            limit_target=limit_target, cutoff_hm=bt
                        )
                        if micro.get("passed", False):
                            trigger_idx = k
                        break

                if trigger_idx is None:
                    continue

                entry_price = round(limit_target * 1.0005, 2)
                entry_time = str(t_bars["time_str"].iloc[trigger_idx])

                # 3. 逐根回放 T+1 日 5 分钟计算真实溢价与策略出场收益
                next_bars = m5_by_date[t1_next_date]
                if len(next_bars) == 0:
                    continue

                n_open = float(next_bars["open"].iloc[0])
                n_high = float(next_bars["high"].max())
                n_close = float(next_bars["close"].iloc[-1])

                open_pnl = round((n_open - entry_price) / entry_price * 100.0, 2)
                high_pnl = round((n_high - entry_price) / entry_price * 100.0, 2)

                # 次日实战退出路径：
                # - 连续封死涨停不卖，若炸板跌破次日 VWAP 或高位回撤 2.5% 止盈，低开破 -4.5% 止损
                n_limit_up = round(n_close * 1.10, 2)  # approx check vs entry
                target_exit_p = entry_price * 1.06
                stop_exit_p = entry_price * 0.955

                exit_price = n_close
                exit_time = str(next_bars["time_str"].iloc[-1])
                exit_reason = "T+1尾盘平仓"

                if n_open <= stop_exit_p:
                    exit_price, exit_time, exit_reason = n_open, "09:30", "T+1低开快速止损防核"
                elif n_open >= target_exit_p:
                    # 高开超 6% 顺势观察，若跌破 VWAP 止盈落袋
                    peak = n_open
                    sold = False
                    for _, nb in next_bars.iterrows():
                        nbt = str(nb["time_str"])
                        nbc = float(nb["close"])
                        nbh = float(nb["high"])
                        nbv = float(nb["vwap"])
                        if nbh > peak:
                            peak = nbh
                        if nbc < nbv * 0.995 and nbt >= "09:40":
                            exit_price, exit_time, exit_reason = nbc, nbt, "大幅高开跌破VWAP兑现暴利"
                            sold = True
                            break
                    if not sold:
                        exit_price, exit_time, exit_reason = n_close, str(next_bars["time_str"].iloc[-1]), "强势封板/高位锁利"
                else:
                    peak = n_open
                    sold = False
                    for _, nb in next_bars.iterrows():
                        nbt = str(nb["time_str"])
                        nbo = float(nb["open"])
                        nbh = float(nb["high"])
                        nbl = float(nb["low"])
                        nbc = float(nb["close"])
                        nbv = float(nb["vwap"])
                        if nbh > peak:
                            peak = nbh
                        if nbl <= stop_exit_p:
                            exit_price, exit_time, exit_reason = min(nbo, stop_exit_p), nbt, "T+1分时触及-4.5%风控止损"
                            sold = True
                            break
                        peak_g = (peak - entry_price) / entry_price * 100.0
                        if "09:40" <= nbt <= "11:30" and peak_g >= 2.0 and nbc < nbv * 0.994:
                            exit_price, exit_time, exit_reason = nbc, nbt, "T+1冲高回落跌破VWAP锁盈"
                            sold = True
                            break
                    if not sold:
                        exit_price, exit_time, exit_reason = n_close, str(next_bars["time_str"].iloc[-1]), "T+1收盘平仓"

                net_ret = round((exit_price - entry_price) / entry_price * 100.0 - friction_pct, 2)
                trades.append({
                    "symbol": sym,
                    "name": name,
                    "signal_date": t_date,
                    "seal_time": entry_time,
                    "board_type": div_info["div_type"],
                    "open_pct": round(open_pct, 2),
                    "entry_price": entry_price,
                    "exit_date": t1_next_date,
                    "exit_time": exit_time,
                    "exit_price": round(exit_price, 2),
                    "open_pnl_pct": open_pnl,
                    "high_pnl_pct": high_pnl,
                    "return_pct": net_ret,
                    "is_win": net_ret > 0,
                    "reason": exit_reason
                })

        total = len(trades)
        wins = [t["return_pct"] for t in trades if t["return_pct"] > 0]
        losses = [t["return_pct"] for t in trades if t["return_pct"] < 0]
        win_rate = round(len(wins) / total * 100.0, 1) if total > 0 else None
        open_prem_rate = round(sum(1 for t in trades if t["open_pnl_pct"] > 0) / total * 100.0, 1) if total > 0 else None
        avg_win = round(float(np.mean(wins)), 2) if wins else None
        avg_loss = round(float(np.mean(losses)), 2) if losses else None
        pl_ratio = round(abs(avg_win / avg_loss), 2) if avg_win is not None and avg_loss is not None and avg_loss != 0 else (99.0 if wins and not losses else None)
        max_loss = round(float(min(losses)), 2) if losses else 0.0
        avg_ret = round(float(np.mean([t["return_pct"] for t in trades])), 2) if total > 0 else None

        trades.sort(key=lambda x: x["signal_date"], reverse=True)
        res = {
            "strategy_name": "弱转强(Weak-to-Strong)专精打板 5m 因果回测",
            "backtest_days": days,
            "total_trades": total,
            "win_trades": len(wins),
            "loss_trades": len(losses),
            "win_rate": win_rate,
            "open_premium_win_rate": open_prem_rate,
            "avg_return_pct": avg_ret,
            "avg_win_pct": avg_win,
            "avg_loss_pct": avg_loss,
            "max_single_loss_pct": max_loss,
            "profit_loss_ratio": pl_ratio,
            "backtest_method": "causal_5m_wzs_intraday_board_hit_t1_exit",
            "recent_trades": trades[:40],
            "all_trades": trades
        }
        self._cached_wzs_backtest = res
        return res

    def record_board_hitting_trades(self, candidates: List[Dict[str, Any]], top_n: int = 3) -> List[Dict[str, Any]]:
        """将今日弱转强打板精选候选幂等写入本地台账"""
        today_str = datetime.now().strftime("%Y-%m-%d")
        added = []

        with self._lock:
            existing_ids = {t["id"] for t in self._trades}
            for c in candidates[:top_n]:
                trade_id = f"{c['symbol']}_{today_str.replace('-', '')}"
                if trade_id in existing_ids:
                    continue

                item = {
                    "id": trade_id,
                    "date": today_str,
                    "symbol": c["symbol"],
                    "name": c["name"],
                    "board_type": c.get("board_type", "弱转强打板"),
                    "t1_divergence_type": c.get("t1_divergence_type", ""),
                    "sector": c.get("sector", "主板"),
                    "ladder_count": c.get("ladder_count", 1),
                    "seal_price": c["seal_price"],
                    "seal_time": c.get("seal_time"),
                    "hardness_score": c.get("hardness_score"),
                    "score_type": c.get("score_type", "弱转强量化置信度分"),
                    "seal_turnover_ratio": c.get("seal_turnover_ratio"),
                    "open_count": c.get("open_count"),
                    "early_vol_ratio": c.get("early_vol_ratio"),
                    "turnover_amount": c.get("turnover_amount"),
                    "reasons": c.get("reasons", []),
                    "status": "HOLDING",
                    "next_open_price": None,
                    "next_open_pnl": None,
                    "next_high_price": None,
                    "next_high_pnl": None,
                    "next_close_price": None,
                    "next_close_pnl": None,
                    "is_consecutive": False,
                    "trade_levels": c.get("trade_levels", {}),
                    "data_limitations": "弱转强候选参考价和后续行情变动不代表实际成交",
                    "data_provenance_version": 2
                }
                self._trades.insert(0, item)
                existing_ids.add(trade_id)
                added.append(item)

            if added:
                self._save_trades()
                self.export_csv()

        return added

    def review_and_update_trades(self, review_date: Optional[str] = None) -> Dict[str, Any]:
        """更新候选信号之后首个交易日的真实 T+1 行情结算（支持停机跨日断档自动对齐）。"""
        today_str = review_date or datetime.now().strftime("%Y-%m-%d")
        updated_count = 0

        with self._lock:
            for t in self._trades:
                if t.get("status") == "HOLDING" and str(t.get("date", "")) < today_str:
                    sym = t.get("symbol")
                    buy_price = float(t.get("seal_price", 0.0))
                    if buy_price <= 0:
                        continue

                    df = data_engine.get_daily_kline(sym, count=45)
                    if df is not None and len(df) >= 2:
                        df["date_str"] = df["date"].astype(str).str[:10]
                        trade_date = str(t.get("date", ""))[:10]
                        next_bars = df[df["date_str"] > trade_date]
                        if len(next_bars) == 0:
                            continue

                        t1_bar = next_bars.iloc[0]
                        t1_date = str(t1_bar["date_str"])

                        next_open = float(t1_bar["open"])
                        next_high = float(t1_bar["high"])
                        next_close = float(t1_bar["close"])

                        open_pnl = round((next_open - buy_price) / buy_price * 100.0, 2)
                        high_pnl = round((next_high - buy_price) / buy_price * 100.0, 2)
                        close_pnl = round((next_close - buy_price) / buy_price * 100.0, 2)

                        limit_target = round(buy_price * 1.10, 2)
                        is_consecutive = (next_close >= limit_target * 0.995) or (close_pnl >= 9.60)
                        status = "WIN" if close_pnl > 0 else ("LOSS" if close_pnl < 0 else "FLAT")

                        t["settled_date"] = t1_date
                        t["next_open_price"] = next_open
                        t["next_open_pnl"] = open_pnl
                        t["next_high_price"] = next_high
                        t["next_high_pnl"] = high_pnl
                        t["next_close_price"] = next_close
                        t["next_close_pnl"] = close_pnl
                        t["is_consecutive"] = is_consecutive
                        t["status"] = status
                        updated_count += 1

            if updated_count > 0:
                self._save_trades()
                self.export_csv()

        stats = self.calculate_model_stats()
        stats["updated_trades_count"] = updated_count
        stats["settled_count"] = updated_count
        return stats

    def calculate_model_stats(self) -> Dict[str, Any]:
        """汇总弱转强打板信号后续表现与板型细分统计"""
        with self._lock:
            closed_trades = [t for t in self._trades if t.get("status") in ["WIN", "LOSS", "FLAT"]]
            total_all = len(self._trades)
            pending_count = sum(1 for t in self._trades if t.get("status") == "HOLDING")

        total = len(closed_trades)
        if total == 0:
            return {
                "total_trades": total_all,
                "closed_trades": 0,
                "settled_trades": 0,
                "pending_trades": pending_count,
                "open_premium_win_rate": None,
                "peak_profit_win_rate": None,
                "consecutive_rate": None,
                "avg_open_pnl": None,
                "avg_peak_pnl": None,
                "avg_close_pnl": None,
                "profit_loss_ratio": None,
                "big_loss_rate": None,
                "board_types": {},
                "by_board_type": {},
                "data_status": "暂无已结算弱转强样本，胜率和盈亏比不可用"
            }

        open_wins = sum(1 for t in closed_trades if float(t.get("next_open_pnl") or 0) > 0)
        open_win_rate = round(open_wins / total * 100.0, 1)

        peak_wins = sum(1 for t in closed_trades if float(t.get("next_high_pnl") or 0) >= 2.0)
        peak_win_rate = round(peak_wins / total * 100.0, 1)

        consecutives = sum(1 for t in closed_trades if t.get("is_consecutive"))
        consecutive_rate = round(consecutives / total * 100.0, 1)

        avg_open = round(float(np.mean([float(t.get("next_open_pnl") or 0) for t in closed_trades])), 2)
        avg_peak = round(float(np.mean([float(t.get("next_high_pnl") or 0) for t in closed_trades])), 2)
        avg_close = round(float(np.mean([float(t.get("next_close_pnl") or 0) for t in closed_trades])), 2)

        profits = [float(t.get("next_close_pnl") or 0) for t in closed_trades if float(t.get("next_close_pnl") or 0) > 0]
        losses = [abs(float(t.get("next_close_pnl") or 0)) for t in closed_trades if float(t.get("next_close_pnl") or 0) < 0]
        mean_p = float(np.mean(profits)) if profits else None
        mean_l = float(np.mean(losses)) if losses else None
        pl_ratio = round(mean_p / mean_l, 2) if mean_p is not None and mean_l is not None and mean_l > 0 else None

        big_losses = sum(1 for t in closed_trades if float(t.get("next_close_pnl") or 0) <= -4.0)
        big_loss_rate = round(big_losses / total * 100.0, 1)

        board_stats = {}
        unique_types = sorted({str(t.get("board_type", "")) for t in closed_trades if t.get("board_type")})
        for b_type in unique_types:
            sub = [t for t in closed_trades if t.get("board_type") == b_type]
            if sub:
                sub_win = sum(1 for t in sub if float(t.get("next_high_pnl") or 0) >= 2.0)
                board_stats[b_type] = {
                    "count": len(sub),
                    "win_rate": round(sub_win / len(sub) * 100.0, 1),
                    "avg_return": round(float(np.mean([float(t.get("next_close_pnl") or 0) for t in sub])), 2)
                }

        return {
            "total_trades": total_all,
            "closed_trades": total,
            "settled_trades": total,
            "pending_trades": pending_count,
            "open_premium_win_rate": open_win_rate,
            "peak_profit_win_rate": peak_win_rate,
            "consecutive_rate": consecutive_rate,
            "avg_open_pnl": avg_open,
            "avg_peak_pnl": avg_peak,
            "avg_close_pnl": avg_close,
            "profit_loss_ratio": pl_ratio,
            "big_loss_rate": big_loss_rate,
            "board_types": board_stats,
            "by_board_type": board_stats,
            "data_status": "统计弱转强专精信号后续表现；不代表真实成交或未来收益"
        }

    def get_all_trades(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            return self._trades[:limit]


limit_up_engine = LimitUpEngine()
