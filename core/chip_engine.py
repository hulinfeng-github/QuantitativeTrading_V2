"""
A股智能量化系统 2.0 - 高精度主力筹码分布与控盘引擎 (ChipEngine 2.0)
复现券商金工研报与 GitHub QuantsPlaybook CYQ 核心算法：
1. 换手率自适应衰减与三角密度迁移模型；
2. 70% 与 90% 筹码集中度 (SCR70, SCR90) 计算；
3. 全市场获利盘比例 (Profit Chip Ratio) 与主力加权建仓成本 (Smart Cost)；
4. 主力控盘五大生命周期研判：吸筹蓄势 / 突破起爆 / 锁仓主升 / 高位派发 / 破位杀跌；
5. 输出图表绘制所需的价格分箱密度曲线 (Price-Density Histogram)。
"""
import numpy as np
import pandas as pd
from typing import Dict, Any, Optional, List

class ChipEngine:
    def __init__(self, lookback_days: int = 120, price_bins: int = 100):
        self.lookback_days = lookback_days
        self.price_bins = price_bins

    def analyze_chips(self, df: pd.DataFrame, current_price: Optional[float] = None) -> Dict[str, Any]:
        """
        全量计算单只标的的筹码分布与微观控盘结构
        """
        if df is None or len(df) < 15:
            return self._empty_result(current_price or 0.0)

        data = df.tail(self.lookback_days).copy()
        if current_price is None or current_price <= 0:
            current_price = float(data.iloc[-1]['close'])

        min_p = float(data['low'].min()) * 0.98
        max_p = float(data['high'].max()) * 1.02
        if max_p <= min_p or np.isnan(min_p) or np.isnan(max_p):
            return self._empty_result(current_price)

        bin_edges = np.linspace(min_p, max_p, self.price_bins + 1)
        bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2.0
        chip_density = np.zeros(self.price_bins)

        # 换手率基准
        median_vol = data['volume'].median()
        if median_vol <= 0:
            median_vol = 1.0

        for _, row in data.iterrows():
            c_high = float(row['high'])
            c_low = float(row['low'])
            c_close = float(row['close'])
            c_open = float(row['open'])
            c_vol = float(row['volume'])

            if c_high <= c_low or c_vol <= 0:
                continue

            # 估算日换手率衰减 (限制在 0.5% ~ 15%)
            turnover = np.clip((c_vol / median_vol) * 0.025, 0.005, 0.15)

            # 历史筹码衰减
            chip_density *= (1.0 - turnover)

            # 当日新筹码生成 (三角分布)
            mid_val = (c_open + c_close + c_high + c_low) / 4.0
            mask = (bin_centers >= c_low) & (bin_centers <= c_high)
            indices = np.where(mask)[0]
            if len(indices) > 0:
                p_in = bin_centers[indices]
                half_w = (c_high - c_low) / 2.0
                if half_w > 0:
                    weights = 1.0 - (np.abs(p_in - mid_val) / half_w)
                    weights = np.maximum(weights, 0.05)
                else:
                    weights = np.ones(len(indices))
                w_sum = np.sum(weights)
                if w_sum > 0:
                    chip_density[indices] += (weights / w_sum) * turnover

        total_mass = np.sum(chip_density)
        if total_mass <= 0:
            return self._empty_result(current_price)

        chip_prob = chip_density / total_mass
        cum_prob = np.cumsum(chip_prob)

        # 1. 获利盘比例 (Profit Ratio)
        idx_cur = np.searchsorted(bin_centers, current_price)
        profit_ratio = float(cum_prob[min(idx_cur, len(cum_prob) - 1)]) * 100.0

        # 2. 70% 与 90% 筹码集中度 (SCR70, SCR90)
        p05 = float(bin_centers[np.searchsorted(cum_prob, 0.05)])
        p95 = float(bin_centers[min(np.searchsorted(cum_prob, 0.95), len(bin_centers) - 1)])
        concentration_90 = round((p95 - p05) / (p95 + p05), 4) if (p95 + p05) > 0 else 0.5

        p15 = float(bin_centers[np.searchsorted(cum_prob, 0.15)])
        p85 = float(bin_centers[min(np.searchsorted(cum_prob, 0.85), len(bin_centers) - 1)])
        concentration_70 = round((p85 - p15) / (p85 + p15), 4) if (p85 + p15) > 0 else 0.5

        # 3. 主力平均建仓成本 (Smart Money Cost, 筹码密集峰值加权)
        max_idx = np.argmax(chip_density)
        peak_price = float(bin_centers[max_idx])

        # 在密集峰附近 ±15% 区间求加权均价
        peak_mask = (bin_centers >= peak_price * 0.85) & (bin_centers <= peak_price * 1.15)
        if np.sum(peak_mask) > 0 and np.sum(chip_density[peak_mask]) > 0:
            main_cost = float(np.sum(bin_centers[peak_mask] * chip_density[peak_mask]) / np.sum(chip_density[peak_mask]))
        else:
            main_cost = float(np.sum(bin_centers * chip_prob))

        bias_to_main = ((current_price - main_cost) / main_cost) * 100.0

        # 4. 主力控盘五大生命周期研判
        if concentration_90 <= 0.12 and profit_ratio >= 70.0 and (1.5 <= bias_to_main <= 12.0):
            control_status = "主力突破起爆"
            status_desc = "筹码单峰高密，突破主力成本线，获利盘超70%，主力主升波段启动"
        elif concentration_90 <= 0.12 and abs(bias_to_main) <= 4.0:
            control_status = "主力吸筹蓄势"
            status_desc = "现价紧贴主力建仓底仓成本，筹码高度密集，安全垫极厚"
        elif bias_to_main > 12.0 and profit_ratio >= 85.0:
            if bias_to_main > 25.0:
                control_status = "主力高位派发风险"
                status_desc = "偏离主力底仓超25%，谨防高位诱多主力借利好出逃"
            else:
                control_status = "主力锁仓主升"
                status_desc = "主力控盘度极高，筹码高度锁定，顺势跟随主升浪"
        elif bias_to_main < -5.0 and profit_ratio < 30.0:
            control_status = "主力破位杀跌"
            status_desc = "已跌破主力防守线，上方全是套牢盘，严格观望回避"
        else:
            control_status = "区间震荡洗盘"
            status_desc = "主力在区间内反复洗盘震荡，等待方向明朗"

        return {
            "current_price": round(current_price, 2),
            "main_cost": round(main_cost, 2),
            "bias_to_main": round(bias_to_main, 2),
            "profit_chip_ratio": round(profit_ratio, 1),
            "concentration_90": round(concentration_90 * 100.0, 1),  # 转为百分比
            "concentration_70": round(concentration_70 * 100.0, 1),
            "control_status": control_status,
            "status_desc": status_desc,
            "bins": [round(x, 2) for x in bin_centers[::3]],
            "density": [round(float(x), 4) for x in chip_prob[::3]]
        }

    def _empty_result(self, price: float) -> Dict[str, Any]:
        return {
            "current_price": round(price, 2),
            "main_cost": round(price, 2),
            "bias_to_main": 0.0,
            "profit_chip_ratio": 50.0,
            "concentration_90": 20.0,
            "concentration_70": 15.0,
            "control_status": "数据不足",
            "status_desc": "历史K线不足，无法完成高精度筹码拟合",
            "bins": [],
            "density": []
        }

chip_engine = ChipEngine()
