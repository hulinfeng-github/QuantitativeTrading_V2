"""
A股智能量化系统 2.0 - 多因子共振波段选股与诊断引擎 (MultiFactorStrategy 2.0)
创新融合：
1. 偏度修正机构 RSRS 2.0 算法 (阻力支撑动量)；
2. 启发式 Qlib Alpha 因子库 (动量、量价相关性、区间相对定位)；
3. 高精 CYQ 主力筹码集中度与控盘生命周期 (吸筹/起爆/锁仓/派发)；
4. 宏观大盘牛熊自适应滤网与一票否决风控；
5. ATR 自适应动态止损、移动止盈与盈亏比测算。
"""
import pandas as pd
import numpy as np
from typing import Dict, Any, Optional, List
from config import load_config
from core.alpha_factors import calculate_alpha_features
from core.rsrs_skew import calculate_rsrs_skew
from core.chip_engine import chip_engine

class MultiFactorStrategy:
    def __init__(self):
        self.reload_config()

    def reload_config(self):
        """动态加载全局配置"""
        cfg = load_config()
        self.rsrs_n = cfg.get("rsrs_n", 18)
        self.rsrs_m = cfg.get("rsrs_m", 600)
        self.buy_threshold = cfg.get("rsrs_buy_threshold", 0.7)
        self.sell_threshold = cfg.get("rsrs_sell_threshold", -0.7)
        self.use_skew = cfg.get("rsrs_use_skew", True)

        self.w_rsrs = cfg.get("weight_rsrs", 0.30)
        self.w_alpha = cfg.get("weight_alpha", 0.25)
        self.w_chip = cfg.get("weight_chip", 0.25)
        self.w_trend = cfg.get("weight_trend", 0.20)

        self.atr_mult = cfg.get("atr_stop_multiplier", 2.0)
        self.trailing_pct = cfg.get("trailing_stop_pct", 0.06)

    def analyze(self, symbol: str, name: str, df: pd.DataFrame) -> Optional[Dict[str, Any]]:
        """全量计算指标与策略因果研判（单股最新K线，集成技术面与基本面深度财报）"""
        if df is None or len(df) < 30:
            return None

        # 1. 扩充 Alpha 特征与 RSRS
        df_feat = calculate_alpha_features(df)
        df_all = calculate_rsrs_skew(df_feat, n=self.rsrs_n, m=self.rsrs_m, use_skew=self.use_skew)

        res = self.analyze_bar(symbol, name, df_all, idx=-1)
        if res:
            try:
                from core.financial_engine import financial_engine
                fin = financial_engine.analyze(symbol, name)
                res["financial"] = fin
                if fin and fin.get("red_flags", {}).get("has_major_risk"):
                    # 财务排雷一票否决/严惩：降低策略总分并标明警示
                    res["score"] = min(res["score"], 58.0)
                    if res["signal_type"] == "BUY":
                        res["signal_type"] = "WATCH"
                        res["signal_desc"] = "财务排雷预警·暂缓开仓"
                    res["reasons"].insert(0, f"🚨【基本面避雷】{fin['red_flags']['risk_summary']}")
                elif fin and fin.get("fundamental_score", 0) >= 80:
                    dp = fin.get("dupont", {})
                    gw = fin.get("growth", {})
                    res["reasons"].append(
                        f"📊 基本面优异({fin.get('fundamental_score')}分·{fin.get('persona')}): "
                        f"ROE {dp.get('roe', 0)}%, 扣非增速 {gw.get('deduct_profit_yoy', 0):+.1f}%"
                    )
            except Exception as e:
                pass
        return res

    def analyze_bar(
        self,
        symbol: str,
        name: str,
        df: pd.DataFrame,
        idx: int = -1,
        market_regime_bull: bool = True
    ) -> Optional[Dict[str, Any]]:
        """
        对已计算全部因子的DataFrame指定位置进行严格因果研判（杜绝未来函数）
        """
        length = len(df)
        if idx < 0:
            idx = length + idx
        if idx < 1 or idx >= length:
            return None

        curr = df.iloc[idx]
        prev = df.iloc[idx - 1]

        close = float(curr['close'])
        prev_close = float(prev['close'])
        pct_change = round((close - prev_close) / prev_close * 100.0, 2) if prev_close > 0 else 0.0

        ma5 = float(curr['ma5']) if not np.isnan(curr['ma5']) else close
        ma20 = float(curr['ma20']) if not np.isnan(curr['ma20']) else close
        ma60 = float(curr['ma60']) if not np.isnan(curr['ma60']) else close
        prev_ma20 = float(prev['ma20']) if not np.isnan(prev['ma20']) else ma20
        ma20_slope = (ma20 - prev_ma20) / prev_ma20 * 100.0 if prev_ma20 > 0 else 0.0

        rsrs_norm = float(curr['rsrs_norm']) if 'rsrs_norm' in curr and not np.isnan(curr['rsrs_norm']) else 0.0
        prev_rsrs_norm = float(prev['rsrs_norm']) if 'rsrs_norm' in prev and not np.isnan(prev['rsrs_norm']) else 0.0
        rsrs_beta = float(curr['rsrs_beta']) if 'rsrs_beta' in curr and not np.isnan(curr['rsrs_beta']) else 1.0
        rsrs_r2 = float(curr['rsrs_r2']) if 'rsrs_r2' in curr and not np.isnan(curr['rsrs_r2']) else 0.0

        alpha_score = float(curr['alpha_score']) if 'alpha_score' in curr and not np.isnan(curr['alpha_score']) else 50.0
        vol_ratio = float(curr['vol_ratio_5']) if 'vol_ratio_5' in curr and not np.isnan(curr['vol_ratio_5']) else 1.0
        atr = float(curr['atr']) if 'atr' in curr and not np.isnan(curr['atr']) else close * 0.03

        # ----------------- 1. RSRS 因子子分 (0 ~ 100) -----------------
        s_rsrs = 0
        rsrs_reasons = []
        if rsrs_norm >= self.buy_threshold:
            s_rsrs = 100
            rsrs_reasons.append(f"偏度修正RSRS强劲超标({rsrs_norm:.2f} >= {self.buy_threshold})，主力推升意愿强烈")
        elif rsrs_norm >= 0.4:
            s_rsrs = 75
            rsrs_reasons.append(f"RSRS指标偏强多头({rsrs_norm:.2f})")
        elif rsrs_norm >= 0:
            s_rsrs = 45
        else:
            s_rsrs = 15

        if prev_rsrs_norm < self.buy_threshold and rsrs_norm >= self.buy_threshold:
            s_rsrs = min(s_rsrs + 10, 100)
            rsrs_reasons.append("RSRS金叉放量上穿临界线，右侧拐点确立")

        # ----------------- 2. 均线与动能子分 (0 ~ 100) -----------------
        s_trend = 0
        trend_reasons = []
        if close > ma20:
            s_trend += 35
            if ma20_slope > 0.05:
                s_trend += 15
                trend_reasons.append("股价站上MA20且生命线向上翘头")
        if close > ma60:
            s_trend += 25
            trend_reasons.append("位于MA60之上，处在中期牛熊线上方")
        if ma5 > ma20 and ma20 > ma60:
            s_trend += 25
            trend_reasons.append("MA5/20/60多头排列顺畅")

        # ----------------- 3. CYQ 2.0 筹码因子子分 (0 ~ 100) -----------------
        chip_info = chip_engine.analyze_chips(df.iloc[:idx + 1], current_price=close)
        control_status = chip_info.get("control_status", "区间震荡洗盘")
        main_cost = chip_info.get("main_cost", close)
        bias_to_main = chip_info.get("bias_to_main", 0.0)
        profit_ratio = chip_info.get("profit_chip_ratio", 50.0)
        conc_90 = chip_info.get("concentration_90", 20.0)

        s_chip = 50
        chip_reasons = []
        if control_status == "主力突破起爆":
            s_chip = 100
            chip_reasons.append(f"🚀 主力筹码突破起爆：刚突破主力底仓成本({main_cost}元，溢价{bias_to_main:+.1f}%)，获利盘超{profit_ratio}%，90%集中度{conc_90}%")
        elif control_status == "主力吸筹蓄势":
            s_chip = 85
            chip_reasons.append(f"🛡️ 主力底仓蓄势：紧贴主力建仓成本({main_cost}元)，筹码高度锁定")
        elif control_status == "主力锁仓主升":
            s_chip = 75
            chip_reasons.append(f"📈 主力锁仓主升：主力控盘良好(主力成本{main_cost}元)，顺应趋势波段")
        elif control_status == "主力高位派发风险":
            s_chip = 10
            chip_reasons.append(f"⚠️ 严禁追高：偏离主力成本过大({bias_to_main:+.1f}%)，警惕主力借利好高位派发")
        elif control_status == "主力破位杀跌":
            s_chip = 0
            chip_reasons.append("跌破主力防守成本线，筹码套牢严重")

        # ----------------- 4. 多因子综合加权评分 (0 ~ 100) -----------------
        total_score = (
            self.w_rsrs * s_rsrs +
            self.w_alpha * alpha_score +
            self.w_chip * s_chip +
            self.w_trend * s_trend
        )
        total_score = float(np.clip(total_score, 0, 100))

        # 一票否决权：若主力处于高位派发，强制扣分降级
        if control_status == "主力高位派发风险":
            total_score = min(total_score, 50.0)

        # 汇总理由
        all_reasons = rsrs_reasons + trend_reasons + chip_reasons
        if 'vp_corr_20' in curr and curr['vp_corr_20'] > 0.25:
            all_reasons.append(f"量价配合良好(20日量价相关性: {curr['vp_corr_20']:.2f})")

        # ----------------- 5. 信号研判 -----------------
        signal_type = "HOLD"
        signal_desc = "震荡观望"

        if control_status == "主力高位派发风险":
            signal_type = "WATCH"
            signal_desc = "高位派发·观望防守"
        elif rsrs_norm < self.sell_threshold or (close < ma20 and prev_close < prev_ma20):
            signal_type = "SELL"
            signal_desc = "减仓/清仓信号"
            all_reasons.append(f"RSRS破位下行({rsrs_norm:.2f} < {self.sell_threshold})或跌破MA20生命线")
        elif total_score >= 75.0 and rsrs_norm >= self.buy_threshold and close > ma20:
            if not market_regime_bull:
                if total_score < 85.0 or vol_ratio < 1.3:
                    signal_type = "WATCH"
                    signal_desc = "弱市压制·观望"
                    all_reasons.append("大盘处于弱势压制通道，防范系统性假突破，暂缓开仓")
                else:
                    signal_type = "BUY"
                    signal_desc = "弱市主力共振突破买点"
            else:
                signal_type = "BUY"
                signal_desc = "强力波段买点(主力突破起爆)" if control_status == "主力突破起爆" else "强力波段买点(多因子共振)"
        elif total_score >= 60.0 and rsrs_norm >= 0.4 and close > ma20:
            signal_type = "WATCH"
            signal_desc = "主力蓄势关注标的"

        # ----------------- 6. 动态 ATR 风控点位测算 -----------------
        initial_stop = round(close - self.atr_mult * atr, 2)
        stop_pct = round((close - initial_stop) / close * 100.0, 2)
        target_price = round(close + self.atr_mult * 2.0 * atr, 2)
        target_pct = round((target_price - close) / close * 100.0, 2)
        rr_ratio = round(target_pct / stop_pct, 2) if stop_pct > 0 else 2.0

        trade_levels = {
            "entry_price": close,
            "initial_stop": initial_stop,
            "stop_pct": stop_pct,
            "target_price": target_price,
            "target_pct": target_pct,
            "breakeven_trigger": round(close * 1.06, 2),
            "trailing_stop_pct": round(self.trailing_pct * 100.0, 1),
            "risk_reward_ratio": rr_ratio
        }

        return {
            "symbol": symbol,
            "name": name,
            "date": str(curr['date'])[:10],
            "price": close,
            "change_pct": pct_change,
            "signal_type": signal_type,
            "signal_desc": signal_desc,
            "score": round(total_score, 1),
            "rsrs_norm": round(rsrs_norm, 3),
            "rsrs_beta": round(rsrs_beta, 3),
            "rsrs_r2": round(rsrs_r2, 3),
            "alpha_score": round(alpha_score, 1),
            "vol_ratio": round(vol_ratio, 2),
            "atr": round(atr, 2),
            "ma5": round(ma5, 2),
            "ma20": round(ma20, 2),
            "ma60": round(ma60, 2),
            "chip_info": chip_info,
            "main_cost": main_cost,
            "bias_to_main": bias_to_main,
            "profit_chip_ratio": profit_ratio,
            "concentration_90": conc_90,
            "control_status": control_status,
            "reasons": all_reasons,
            "trade_levels": trade_levels
        }

multi_factor_strategy = MultiFactorStrategy()
