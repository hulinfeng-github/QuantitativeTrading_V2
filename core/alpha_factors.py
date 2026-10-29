"""
A股智能量化系统 2.0 - 启发式 Qlib Alpha 因子与技术特征库 (AlphaFactors 2.0)
汇集微软 Qlib Alpha158 核心特征与国内顶级量化机构因子：
1. 动量与超额收益因子：ROC_5, ROC_20, 动量加速度；
2. 量价共振因子：量比(Vol_Ratio), 20日量价相关系数(VP_Corr);
3. 相对通道定位因子：20日高低极值定位(High_Low_Pos_20), 布林带 %B;
4. 波动率与真实波幅：ATR_14, 标准化波动率(Norm_ATR), 历史年化波动率;
5. 经典动能与震荡：MACD, RSI_14, KDJ;
6. Alpha 综合评分：全向量化计算，输出标准化多因子综合量化得分 (0~100).
"""
import numpy as np
import pandas as pd
from typing import Dict, Any, Tuple

def calculate_ema(series: pd.Series, span: int) -> pd.Series:
    """计算指数移动平均 (EMA)"""
    return series.ewm(span=span, adjust=False).mean()

def calculate_alpha_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    全量计算 Alpha 因子与技术特征
    :param df: 包含 date, open, close, high, low, volume 的 DataFrame
    :return: 扩充后的全因子 DataFrame
    """
    if df is None or len(df) < 20:
        return df

    data = df.copy()
    close = data['close']
    high = data['high']
    low = data['low']
    vol = data['volume']

    # 1. 均线与乖离率 (Moving Averages & Bias)
    data['ma5'] = close.rolling(window=5, min_periods=1).mean()
    data['ma10'] = close.rolling(window=10, min_periods=1).mean()
    data['ma20'] = close.rolling(window=20, min_periods=1).mean()
    data['ma60'] = close.rolling(window=60, min_periods=1).mean()

    data['bias_ma20'] = (close - data['ma20']) / data['ma20'] * 100.0
    data['bias_ma60'] = (close - data['ma60']) / data['ma60'] * 100.0
    data['ma_align_bull'] = (
        (data['ma5'] > data['ma20']) & (data['ma20'] > data['ma60'])
    ).astype(int)

    # 2. 动量因子 (Rate of Change)
    data['roc_5'] = (close / close.shift(5) - 1.0) * 100.0
    data['roc_20'] = (close / close.shift(20) - 1.0) * 100.0
    data['roc_60'] = (close / close.shift(60) - 1.0) * 100.0
    data['roc_accel'] = data['roc_5'] - data['roc_20']  # 动量加速度

    # 3. 量能因子 (Volume Metrics & Qlib VP Correlation)
    vol_ma5 = vol.rolling(window=5, min_periods=1).mean()
    vol_ma20 = vol.rolling(window=20, min_periods=1).mean()
    data['vol_ratio_5'] = np.where(vol_ma5 > 0, vol / vol_ma5, 1.0)
    data['vol_ratio_20'] = np.where(vol_ma20 > 0, vol / vol_ma20, 1.0)

    # 量价相关性: 计算 20 日收益率与成交量变化的相关系数
    ret_1d = close.pct_change()
    vol_1d = vol.pct_change()
    data['vp_corr_20'] = ret_1d.rolling(window=20, min_periods=5).corr(vol_1d).fillna(0.0)

    # 4. 真实波幅与波动率 (ATR & Normalized Volatility)
    prev_close = close.shift(1).fillna(close)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    data['atr'] = tr.rolling(window=14, min_periods=1).mean()
    data['norm_atr'] = (data['atr'] / close) * 100.0  # 真实波动率百分比

    # 20日历史年化波动率
    data['hist_vol_20'] = ret_1d.rolling(window=20, min_periods=5).std() * np.sqrt(250) * 100.0

    # 5. 高低区间相对定位 (Relative Positioning: Qlib Stochastics)
    low_20 = low.rolling(window=20, min_periods=1).min()
    high_20 = high.rolling(window=20, min_periods=1).max()
    denom_20 = high_20 - low_20
    data['high_low_pos_20'] = np.where(denom_20 > 0, (close - low_20) / denom_20, 0.5)

    # 6. 布林带 (Bollinger Bands & %B)
    std_20 = close.rolling(window=20, min_periods=1).std()
    data['boll_mid'] = data['ma20']
    data['boll_up'] = data['boll_mid'] + 2.0 * std_20
    data['boll_down'] = data['boll_mid'] - 2.0 * std_20
    boll_width = data['boll_up'] - data['boll_down']
    data['boll_pct_b'] = np.where(boll_width > 0, (close - data['boll_down']) / boll_width, 0.5)

    # 7. MACD (Moving Average Convergence Divergence)
    ema12 = calculate_ema(close, 12)
    ema26 = calculate_ema(close, 26)
    data['dif'] = ema12 - ema26
    data['dea'] = calculate_ema(data['dif'], 9)
    data['macd_bar'] = (data['dif'] - data['dea']) * 2.0

    # 8. RSI (Relative Strength Index, 14天)
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=14, min_periods=1).mean()
    avg_loss = loss.rolling(window=14, min_periods=1).mean()
    rs = np.where(avg_loss > 0, avg_gain / avg_loss, 100.0)
    data['rsi_14'] = 100.0 - (100.0 / (1.0 + rs))

    # 9. 综合 Alpha 得分向量计算 (0 ~ 100)
    score = np.zeros(len(data))

    # 趋势得分 (0~25)
    score += np.where(close > data['ma20'], 10, 0)
    score += np.where(close > data['ma60'], 10, 0)
    score += np.where(data['ma_align_bull'] == 1, 5, 0)

    # 动量得分 (0~25)
    score += np.clip(data['roc_20'].fillna(0) * 1.2, 0, 15)
    score += np.where(data['roc_accel'].fillna(0) > 0, 10, 0)

    # 量价配合 (0~20)
    score += np.where(data['vol_ratio_5'].fillna(1.0) >= 1.2, 10, 0)
    score += np.where(data['vp_corr_20'].fillna(0) > 0.2, 10, 0)

    # 区间位置与形态 (0~20)
    score += np.where(data['high_low_pos_20'].fillna(0.5) >= 0.7, 10, 0)
    score += np.where((data['dif'] > data['dea']) & (data['macd_bar'] > 0), 10, 0)

    # 波动率健康度 (0~10)
    score += np.where((data['norm_atr'].fillna(3) >= 1.5) & (data['norm_atr'].fillna(3) <= 6.0), 10, 5)

    data['alpha_score'] = np.clip(score, 0, 100).round(1)

    return data
