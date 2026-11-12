"""
A股智能量化系统 2.0 - 市场情绪周期与宏观大势雷达 (SentimentRadar 2.0)
结合国内头部游资与量化机构情绪周期理论：
1. 跟踪上证指数与沪深300多周期均线生命线 (MA5/20/60)；
2. 衡量全市场广度与赚钱效应；
3. 输出 0~100 市场情绪温度计与四维周期：
   - 极度冰点 (0~25)
   - 弱势磨底 (26~50)
   - 升温发酵 (51~75)
   - 亢奋高潮 (76~100)
4. 输出宏观风险敞口控制乘数 (Exposure Target)，指导组合总仓位动态防守。
"""
from typing import Dict, Any, Optional
import pandas as pd
import numpy as np
from core.data_engine import data_engine

class SentimentRadar:
    def __init__(self):
        self._last_sentiment: Optional[Dict[str, Any]] = None

    def analyze_market_regime(self) -> Dict[str, Any]:
        """
        全面评估 A 股大盘当前宏观牛熊环境与情绪周期
        """
        # 优先获取最新实时行情点位兜底
        real_q = data_engine.get_realtime_quote("sh000001")
        live_price = float(real_q.get("price", 0.0)) if real_q else 0.0
        live_pct = float(real_q.get("change_pct", 0.0)) if real_q else 0.0

        # 1. 获取上证指数日K线
        sh_df = data_engine.get_daily_kline("sh000001", count=100)
        hs300_df = data_engine.get_daily_kline("sh000300", count=100)

        if sh_df is None or len(sh_df) < 60:
            return self._default_regime(live_price=live_price, live_pct=live_pct)

        if not {"date", "close"}.issubset(sh_df.columns):
            return self._default_regime(live_price=live_price, live_pct=live_pct)

        # Stale index bars can produce plausible but unsafe moving averages.
        # Allow short weekends/holidays; reject data more than a week old.
        try:
            latest_bar_date = pd.to_datetime(sh_df.iloc[-1]["date"]).date()
            age_days = (pd.Timestamp.now().date() - latest_bar_date).days
            if age_days < 0 or age_days > 7:
                return self._default_regime(live_price=live_price, live_pct=live_pct)
        except (KeyError, TypeError, ValueError):
            return self._default_regime(live_price=live_price, live_pct=live_pct)

        close_series = pd.to_numeric(sh_df["close"], errors="coerce")
        if close_series.tail(60).isna().any() or not np.isfinite(close_series.tail(60)).all():
            return self._default_regime(live_price=live_price, live_pct=live_pct)

        # 计算上证指数关键技术指标
        close = live_price if live_price > 0 else float(close_series.iloc[-1])
        prev_close = float(close_series.iloc[-2])
        sh_pct = live_pct if live_price > 0 else round((close - prev_close) / prev_close * 100.0, 2)

        ma5 = float(close_series.rolling(5).mean().iloc[-1])
        ma20 = float(close_series.rolling(20).mean().iloc[-1])
        prev_ma20 = float(close_series.rolling(20).mean().iloc[-2])
        ma60 = float(close_series.rolling(60).mean().iloc[-1]) if len(sh_df) >= 60 else ma20

        ma20_slope = (ma20 - prev_ma20) / prev_ma20 * 100.0 if prev_ma20 > 0 else 0.0

        # 2. 情绪温度综合计分 (0 ~ 100)
        temp_score = 50.0  # 基准分

        # 价格位置对温度的影响 (-25 ~ +25)
        if close > ma20:
            temp_score += 15.0
            if ma20_slope > 0.05:
                temp_score += 10.0
        else:
            temp_score -= 15.0
            if ma20_slope < -0.05:
                temp_score -= 10.0

        # 牛熊线过滤 (-15 ~ +15)
        if close > ma60:
            temp_score += 15.0
        else:
            temp_score -= 15.0

        # 当日涨跌幅动量 (-10 ~ +10)
        temp_score += np.clip(sh_pct * 8.0, -10.0, 10.0)

        temp_score = float(np.clip(temp_score, 5.0, 95.0))

        # 3. 情绪周期定调与建议仓位敞口
        if temp_score >= 75.0:
            cycle_phase = "升温亢奋期"
            regime = "强多头牛市"
            target_exposure = 0.90
            action_guide = "多头共振，顺势进攻，积极参与主升浪与突破龙头"
        elif temp_score >= 50.0:
            cycle_phase = "温和复苏期"
            regime = "多头震荡市"
            target_exposure = 0.65
            action_guide = "结构性行情，精选个股，结合日内做T摊薄成本"
        elif temp_score >= 25.0:
            cycle_phase = "弱势分化期"
            regime = "空头震荡市"
            target_exposure = 0.35
            action_guide = "存量博弈，严格防守，轻仓观望，严禁盲目追高"
        else:
            cycle_phase = "极度冰点期"
            regime = "系统性杀跌"
            target_exposure = 0.15
            action_guide = "极度冰点风险释放，空仓或轻仓防守，等待右侧拐点"

        result = {
            "index_price": round(close, 2),
            "index_pct": sh_pct,
            "ma5": round(ma5, 2),
            "ma20": round(ma20, 2),
            "ma60": round(ma60, 2),
            "ma20_slope": round(ma20_slope, 3),
            "above_ma20": close > ma20,
            "above_ma60": close > ma60,
            "temp_score": round(temp_score, 1),
            "cycle_phase": cycle_phase,
            "regime": regime,
            "target_exposure": target_exposure,
            "action_guide": action_guide,
            "data_available": True,
            "can_open_new_positions": True
        }
        self._last_sentiment = result
        return result

    def _default_regime(self, live_price: float = 0.0, live_pct: float = 0.0) -> Dict[str, Any]:
        # Missing index history is a risk event: never invent an index level or
        # permit fresh exposure based on stale/default values.
        p = live_price if live_price > 0 else 0.0
        return {
            "index_price": round(p, 2),
            "index_pct": live_pct,
            "ma5": None,
            "ma20": None,
            "ma60": None,
            "ma20_slope": 0.0,
            "above_ma20": False,
            "above_ma60": False,
            "temp_score": 0.0,
            "cycle_phase": "行情数据不足",
            "regime": "数据不可用·防守",
            "target_exposure": 0.0,
            "action_guide": "大盘历史行情不足，暂停新增仓位，待数据恢复后重新评估",
            "data_available": False,
            "can_open_new_positions": False
        }

sentiment_radar = SentimentRadar()
