"""
A股智能量化系统 2.0 - 现代组合风控与动态仓位优化引擎 (PortfolioRisk 2.0)
结合 Barra 风险模型与桥水全天候风险平价理论：
1. 波动率平价 (Volatility Parity)：个股仓位与自身真实波动率反比加权，平滑净值波动；
2. 半凯利公式 (Half-Kelly Criterion)：基于前瞻胜率与盈亏比动态配置头寸；
3. 大盘宏观自适应风险敞口 (Market Regime Exposure Control)；
4. 单只个股集中度上限与全天候自适应 ATR 硬止损、保本止损与最高价回撤止盈。
"""
import math
import numpy as np
from typing import Dict, Any, List, Optional, Tuple
from config import load_config

class PortfolioRiskManager:
    def __init__(self):
        self.reload_config()

    def reload_config(self):
        cfg = load_config()
        self.initial_capital = cfg.get("initial_capital", 100000.0)
        self.max_positions = cfg.get("max_portfolio_positions", 5)
        self.max_single_pct = cfg.get("max_single_position_pct", 0.25)
        self.bull_exposure = cfg.get("bull_market_max_exposure", 0.95)
        self.osc_exposure = cfg.get("oscillation_max_exposure", 0.55)
        self.bear_exposure = cfg.get("bear_market_max_exposure", 0.20)
        self.trailing_pct = cfg.get("trailing_stop_pct", 0.06)

    def calculate_position_size(
        self,
        symbol: str,
        price: float,
        atr: float,
        total_equity: float,
        current_holdings_count: int,
        market_regime: str = "多头震荡市",
        win_rate: float = 0.55,
        profit_loss_ratio: float = 2.0
    ) -> Dict[str, Any]:
        """
        结合半凯利公式与波动率平价计算单只标的的建议建仓股数与资金比例
        """
        self.reload_config()

        # 1. 确定宏观大盘总敞口上限
        if "强多头" in market_regime or "牛市" in market_regime:
            max_total_exposure = self.bull_exposure
        elif "空头" in market_regime or "杀跌" in market_regime:
            max_total_exposure = self.bear_exposure
        else:
            max_total_exposure = self.osc_exposure

        # 检查是否已达持仓只数上限
        if current_holdings_count >= self.max_positions:
            return {
                "allowed": False,
                "reason": f"持仓已达上限({self.max_positions}只)，风控禁止继续开新仓",
                "suggested_shares": 0,
                "suggested_capital": 0.0,
                "position_pct": 0.0
            }

        # 2. 半凯利公式基准比例测算: f = 0.5 * (p * b - q) / b
        b = max(profit_loss_ratio, 1.0)
        p = min(max(win_rate, 0.3), 0.8)
        q = 1.0 - p
        kelly_f = max(0.0, (p * b - q) / b)
        half_kelly_pct = min(kelly_f * 0.5, self.max_single_pct)

        # 3. 波动率平价调整 (以日波幅 2.5% 为基准)
        norm_vol = (atr / price * 100.0) if price > 0 else 3.0
        vol_factor = np.clip(2.5 / max(norm_vol, 0.5), 0.5, 1.5)

        # 综合单股目标配比
        target_single_pct = min(half_kelly_pct * vol_factor, self.max_single_pct)

        # 结合大盘总敞口缩放
        effective_pct = target_single_pct * (max_total_exposure / 0.8)
        effective_pct = min(max(effective_pct, 0.05), self.max_single_pct)

        suggested_capital = total_equity * effective_pct
        # A股买入整手 (100股整数倍)
        raw_shares = int(suggested_capital / price) if price > 0 else 0
        suggested_shares = (raw_shares // 100) * 100

        if suggested_shares < 100:
            return {
                "allowed": False,
                "reason": "计算资金不足买入 100 股(一手)，暂不开仓",
                "suggested_shares": 0,
                "suggested_capital": 0.0,
                "position_pct": 0.0
            }

        actual_capital = round(suggested_shares * price, 2)
        actual_pct = round(actual_capital / total_equity * 100.0, 1)

        return {
            "allowed": True,
            "suggested_shares": suggested_shares,
            "suggested_capital": actual_capital,
            "position_pct": actual_pct,
            "norm_volatility_pct": round(norm_vol, 2),
            "max_total_exposure": round(max_total_exposure * 100.0, 0),
            "reason": f"半凯利({actual_pct}%)结合波动率平价配比"
        }

    def check_exit_signals(
        self,
        current_price: float,
        entry_price: float,
        highest_price: float,
        initial_stop: float
    ) -> Tuple[bool, str]:
        """
        持仓全流程风控核查：硬止损、保本止损与最高价移动回撤止盈
        """
        # 1. 触发初始 ATR 硬止损
        if current_price <= initial_stop:
            return True, f"跌破初始ATR防守硬止损线({initial_stop}元)，果断斩仓避险"

        # 2. 触发保本止损 (利润曾超过 +6%，但现价跌回建仓价附近)
        if highest_price >= entry_price * 1.06 and current_price <= entry_price * 1.005:
            return True, "利润冲高超6%后破位回落，触发保本止损平仓离场"

        # 3. 触发最高价移动止盈 (自最高点回撤超过 trailing_stop_pct)
        if highest_price >= entry_price * 1.05:
            trailing_exit_price = highest_price * (1.0 - self.trailing_pct)
            if current_price <= trailing_exit_price:
                ret_pct = (current_price - entry_price) / entry_price * 100.0
                return True, f"自最高点({highest_price}元)回撤超{self.trailing_pct*100}%，锁定收益平仓(收益: {ret_pct:+.1f}%)"

        return False, "持仓健康，顺势持有"

portfolio_risk = PortfolioRiskManager()
