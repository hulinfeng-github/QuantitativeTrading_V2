"""
A股智能量化系统 2.0 - 偏度修正 RSRS 模型 (RSRS 2.0 with Skewness Penalty)
金工研报经典升级：光大证券 RSRS 择时体系 + 华泰证券残差偏度过滤
核心创新：
1. 滑动 N 日高低价 OLS 线性回归斜率 Beta 与判定系数 R²；
2. 滑动 M 日 Z-score 统计标准化；
3. 残差分布偏度(Skewness)惩罚因子：识别“虚高引线与诱多拉高”，
   大幅滤除假突破与日内急拉杀跌震荡，显著提高买点胜率与卡玛比率。
"""
import numpy as np
import pandas as pd
from scipy import stats
from typing import Tuple, Dict, Any

def calculate_rsrs_skew(
    df: pd.DataFrame,
    n: int = 18,
    m: int = 600,
    use_skew: bool = True
) -> pd.DataFrame:
    """
    计算偏度修正 RSRS 全序列
    :param df: 包含 high, low 的 DataFrame
    :param n: 回归计算窗口 (默认18天)
    :param m: Z-score 标准化窗口 (默认600天)
    :param use_skew: 是否开启偏度惩罚修正
    :return: 包含 rsrs_beta, rsrs_r2, rsrs_norm, rsrs_skew 的 DataFrame
    """
    if df is None or len(df) < n:
        return df

    data = df.copy()
    highs = data['high'].values
    lows = data['low'].values
    length = len(data)

    betas = np.full(length, np.nan)
    r2_scores = np.full(length, np.nan)
    skews = np.full(length, 0.0)

    # 1. 滑动窗口 OLS 线性回归: High = alpha + beta * Low
    for i in range(n - 1, length):
        h_win = highs[i - n + 1 : i + 1]
        l_win = lows[i - n + 1 : i + 1]

        l_mean = np.mean(l_win)
        h_mean = np.mean(h_win)

        denom = np.sum((l_win - l_mean) ** 2)
        if denom <= 1e-9:
            betas[i] = 1.0
            r2_scores[i] = 0.0
            continue

        cov = np.sum((l_win - l_mean) * (h_win - h_mean))
        beta = cov / denom
        alpha = h_mean - beta * l_mean
        betas[i] = beta

        # 拟合优度 R²
        h_pred = alpha + beta * l_win
        ss_res = np.sum((h_win - h_pred) ** 2)
        ss_tot = np.sum((h_win - h_mean) ** 2)
        r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 1e-9 else 0.0
        r2_scores[i] = max(min(r2, 1.0), 0.0)

        # 残差偏度计算 (Skewness)
        if use_skew and ss_res > 1e-9:
            residuals = h_win - h_pred
            res_std = np.std(residuals)
            if res_std > 1e-9:
                skew_val = float(np.mean(((residuals - np.mean(residuals)) / res_std) ** 3))
                skews[i] = skew_val

    data['rsrs_beta'] = betas
    data['rsrs_r2'] = r2_scores
    data['rsrs_skew_val'] = skews

    # 2. 滑动 M 天 Z-score 标准化
    beta_series = pd.Series(betas)
    rolling_mean = beta_series.rolling(window=m, min_periods=max(n * 2, 30)).mean()
    rolling_std = beta_series.rolling(window=m, min_periods=max(n * 2, 30)).std()

    z_beta = np.where(rolling_std > 1e-9, (beta_series - rolling_mean) / rolling_std, 0.0)
    data['rsrs_z'] = z_beta

    # 3. 修正 RSRS = Z_beta * R²
    rsrs_mod = z_beta * r2_scores
    data['rsrs_mod'] = rsrs_mod

    # 4. 偏度修正因子 (过滤诱多与脉冲假阳线)
    if use_skew:
        # 当偏度为负或过低时，说明存在大量向下的异常突兀残差，施加平滑惩罚
        skew_penalty = np.clip(1.0 + skews * 0.25, 0.5, 1.3)
        data['rsrs_norm'] = np.round(rsrs_mod * skew_penalty, 3)
    else:
        data['rsrs_norm'] = np.round(rsrs_mod, 3)

    return data
