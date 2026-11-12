"""
A股智能量化系统 2.0 - 组合风险预算与动态仓位限制 (PortfolioRisk)
1. ATR止损距离约束单笔账户风险；
2. 半凯利公式仅作为基于已结算样本估计的仓位上限；
3. 大盘宏观自适应风险敞口 (Market Regime Exposure Control)；
4. 单只个股集中度、总敞口和持仓数量上限。
"""
import math
import json
import numpy as np
from typing import Dict, Any, List, Optional, Tuple
from config import WATCHLIST_FILE, load_config

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
        self.risk_per_trade_pct = cfg.get("risk_per_trade_pct", 0.5)
        self.atr_stop_multiplier = cfg.get("atr_stop_multiplier", 2.0)

    def get_watchlist_portfolio_state(self, total_equity: Optional[float] = None) -> Dict[str, Any]:
        """Estimate current exposure from manually maintained watchlist holdings.

        This is only as complete as the user's watchlist and live quotes; a
        missing quote with open holdings is treated as fully exposed.
        """
        equity = float(total_equity or self.initial_capital)
        try:
            if not WATCHLIST_FILE.exists():
                return {"holdings_count": 0, "exposure_pct": 0.0, "available": True}
            with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
                items = json.load(f)
            if not isinstance(items, list):
                return {"holdings_count": 0, "exposure_pct": 100.0, "available": False}
        except Exception:
            return {"holdings_count": 0, "exposure_pct": 100.0, "available": False}

        holdings = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                shares = int(item.get("holding_shares") or item.get("shares") or 0)
            except (TypeError, ValueError):
                shares = 0
            if shares > 0 and item.get("symbol"):
                holdings.append((str(item["symbol"]), shares))

        if not holdings:
            return {"holdings_count": 0, "exposure_pct": 0.0, "available": True}
        if equity <= 0:
            return {"holdings_count": len(holdings), "exposure_pct": 100.0, "available": False}

        try:
            from core.data_engine import data_engine as market_data_engine
            quotes = market_data_engine.get_realtime_quotes([item[0] for item in holdings])
        except Exception:
            market_data_engine = None
            quotes = {}

        market_value = 0.0
        for symbol, shares in holdings:
            quote_symbol = market_data_engine.format_symbol(symbol) if market_data_engine else symbol
            quote = quotes.get(quote_symbol, {}) if quotes else {}
            price = float(quote.get("price") or 0.0)
            if price <= 0:
                # Unknown current value is not treated as zero exposure.
                return {"holdings_count": len(holdings), "exposure_pct": 100.0, "available": False}
            market_value += price * shares

        return {
            "holdings_count": len(holdings),
            "exposure_pct": round(market_value / equity * 100.0, 2),
            "available": True,
            "scope": "watchlist holdings only"
        }

    @staticmethod
    def resolve_available_shares(item: Dict[str, Any]) -> int:
        """
        解析「当日可卖股数」：
        - 显式填写 available_shares 时采用其值（限制在 0~总持仓之间）；
        - 未填该字段时默认等于总持仓，视为前日持仓可全部卖出；
        - 当日新买入的标的请显式把 available_shares 设为可卖数量（T+1 锁定部分填 0）。
        """
        try:
            holding = max(0, int(item.get("holding_shares") or item.get("shares") or 0))
        except (TypeError, ValueError):
            holding = 0
        if "available_shares" in item and item.get("available_shares") is not None:
            try:
                avail = max(0, int(item.get("available_shares")))
            except (TypeError, ValueError):
                return holding
            return min(avail, holding)
        return holding

    def calculate_position_size(
        self,
        symbol: str,
        price: float,
        atr: float,
        total_equity: float,
        current_holdings_count: int,
        market_regime: str = "多头震荡市",
        win_rate: Optional[float] = None,
        profit_loss_ratio: Optional[float] = None,
        current_exposure_pct: float = 0.0,
        performance_sample_size: int = 0
    ) -> Dict[str, Any]:
        """
        按样本校验、ATR风险预算和组合上限计算参考股数；样本不足时拒绝给仓位建议
        """
        self.reload_config()

        # 1. 确定宏观大盘总敞口上限。未知市场状态按防守处理。
        if "强多头" in market_regime or "牛市" in market_regime:
            max_total_exposure = self.bull_exposure
        elif "空头" in market_regime or "杀跌" in market_regime:
            max_total_exposure = self.bear_exposure
        elif "震荡" in market_regime or "中性" in market_regime:
            max_total_exposure = self.osc_exposure
        else:
            max_total_exposure = 0.0

        # 检查是否已达持仓只数上限
        if current_holdings_count >= self.max_positions:
            return {
                "allowed": False,
                "reason": f"持仓已达上限({self.max_positions}只)，风控禁止继续开新仓",
                "suggested_shares": 0,
                "suggested_capital": 0.0,
                "position_pct": 0.0
            }

        if total_equity <= 0 or price <= 0 or atr <= 0:
            return {
                "allowed": False,
                "reason": "净值、价格或 ATR 数据无效，禁止计算仓位",
                "suggested_shares": 0,
                "suggested_capital": 0.0,
                "position_pct": 0.0
            }
        if max_total_exposure <= 0:
            return {
                "allowed": False,
                "reason": "市场状态未知或风险敞口为零，暂停新增仓位",
                "suggested_shares": 0,
                "suggested_capital": 0.0,
                "position_pct": 0.0,
                "max_total_exposure": 0.0
            }

        if win_rate is None or profit_loss_ratio is None or performance_sample_size < 30:
            return {
                "allowed": False,
                "reason": "缺少至少30笔已结算、非重复的策略样本；不使用假设胜率/盈亏比计算凯利仓位",
                "suggested_shares": 0,
                "suggested_capital": 0.0,
                "position_pct": 0.0,
                "performance_sample_size": performance_sample_size
            }

        # 2. 半凯利公式作为仓位上限；负期望或零期望不建仓。
        b = float(profit_loss_ratio)
        p = min(max(float(win_rate), 0.0), 1.0)
        if b <= 0:
            return {
                "allowed": False,
                "reason": "盈亏比无效，无法证明策略具有正期望",
                "suggested_shares": 0,
                "suggested_capital": 0.0,
                "position_pct": 0.0
            }
        q = 1.0 - p
        kelly_f = max(0.0, (p * b - q) / b)
        half_kelly_pct = min(kelly_f * 0.5, self.max_single_pct)
        if half_kelly_pct <= 0:
            return {
                "allowed": False,
                "reason": "按输入胜率与盈亏比估算的期望收益不为正，暂不开仓",
                "suggested_shares": 0,
                "suggested_capital": 0.0,
                "position_pct": 0.0
            }

        # 3. 用 ATR 止损距离控制单笔账户风险；再以等额槽位限制总敞口。
        norm_vol = atr / price * 100.0
        stop_distance_pct = max(norm_vol * self.atr_stop_multiplier, 0.1)
        risk_budget_pct = max(self.risk_per_trade_pct, 0.0)
        risk_limited_pct = risk_budget_pct / stop_distance_pct * 100.0
        remaining_exposure_pct = max(max_total_exposure * 100.0 - max(float(current_exposure_pct), 0.0), 0.0)
        remaining_slots = max(self.max_positions - current_holdings_count, 1)
        per_slot_exposure_pct = min(
            max_total_exposure / max(self.max_positions, 1) * 100.0,
            remaining_exposure_pct / remaining_slots
        )
        target_single_pct = min(
            half_kelly_pct * 100.0,
            self.max_single_pct * 100.0,
            per_slot_exposure_pct,
            remaining_exposure_pct
        )
        effective_pct = min(target_single_pct, risk_limited_pct)
        if effective_pct <= 0:
            return {
                "allowed": False,
                "reason": "风险预算计算结果为零，暂不开仓",
                "suggested_shares": 0,
                "suggested_capital": 0.0,
                "position_pct": 0.0,
                "risk_budget_pct": risk_budget_pct
            }

        suggested_capital = total_equity * effective_pct
        # A股买入整手 (100股整数倍)
        raw_shares = int(suggested_capital / (price * 1.001))
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
            "risk_budget_pct": risk_budget_pct,
            "risk_at_stop_pct": round(actual_pct * stop_distance_pct / 100.0, 3),
            "current_exposure_pct": round(max(float(current_exposure_pct), 0.0), 2),
            "reason": f"半凯利上限、ATR止损风险预算({risk_budget_pct}%)与组合敞口槽位共同约束"
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
