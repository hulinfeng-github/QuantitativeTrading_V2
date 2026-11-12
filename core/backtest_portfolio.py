"""
A股智能量化系统 2.0 - 全组合与单标的日线代理回测引擎
特色：
1. 使用日线近似执行模型：T-1收盘信号、T日开盘撮合，止损和收盘退出按保守顺序估算；
2. 交易成本按代码假设估算，滑点并非真实成交数据；
3. 整手(100股)撮合与一字涨停跌停流动性封死模拟；
4. 沪深300 / 上证指数基准同步对标 (Alpha / Beta / 信息比率 IR)；
5. 风险指标：夏普比率、索提诺比率、卡玛比率、最大回撤与时间切分末段收益；
6. 输出每日净值时序对比数据，便于前端绘制专业对比收益曲线。
"""
import math
import numpy as np
import pandas as pd
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime

from core.data_engine import data_engine
from core.alpha_factors import calculate_alpha_features
from core.rsrs_skew import calculate_rsrs_skew
from core.multi_factor_strategy import multi_factor_strategy, MultiFactorStrategy
from core.portfolio_risk import portfolio_risk

class BacktestEngineV2:
    def __init__(
        self,
        initial_capital: float = 100000.0,
        commission_rate: float = 0.00025,
        min_commission: float = 5.0,
        stamp_duty_rate: float = 0.0005,
        transfer_fee_rate: float = 0.00001,
        slippage_rate: float = 0.001,
        oos_ratio: float = 0.30
    ):
        self.initial_capital = initial_capital
        self.commission_rate = commission_rate
        self.min_commission = min_commission
        self.stamp_duty_rate = stamp_duty_rate
        self.transfer_fee_rate = transfer_fee_rate
        self.slippage_rate = slippage_rate
        self.oos_ratio = oos_ratio

    def run_single_stock_backtest(
        self,
        symbol: str,
        name: str = "",
        df: Optional[pd.DataFrame] = None,
        days: int = 600,
        use_market_regime: bool = True
    ) -> Dict[str, Any]:
        """
        对单只股票执行严格因果历史回测
        """
        sym = data_engine.format_symbol(symbol)
        if df is None:
            df = data_engine.get_daily_kline(sym, count=days)

        if df is None or len(df) < 60:
            return {"error": f"标的 {sym} 历史有效K线不足60根，无法执行回测"}

        df = df.sort_values("date").reset_index(drop=True)
        if not name:
            info = data_engine.resolve_symbol(sym)
            name = info.get("name", sym) if info else sym

        # 获取沪深300作为基准。缺失的基准行情保留为空，绝不拿个股收益冒充基准。
        benchmark_df = data_engine.get_daily_kline("sh000300", count=days)
        bench_map = {}
        if benchmark_df is not None:
            benchmark_df = benchmark_df.sort_values("date").reset_index(drop=True)
            benchmark_df["ma20"] = benchmark_df["close"].rolling(20, min_periods=20).mean()
            for _, r in benchmark_df.iterrows():
                bench_map[str(r['date'])[:10]] = {
                    "close": float(r["close"]),
                    "ma20": float(r["ma20"]) if pd.notna(r["ma20"]) else None
                }

        # 计算 Alpha 特征与 RSRS
        df_feat = calculate_alpha_features(df)
        df_calc = calculate_rsrs_skew(df_feat, n=18, m=600, use_skew=True)

        # 步进参数
        total_bars = len(df_calc)
        oos_start_idx = int(total_bars * (1.0 - self.oos_ratio))

        capital = self.initial_capital
        cash = capital
        shares = 0
        entry_price = 0.0
        entry_date = ""
        entry_fee = 0.0
        peak_price = 0.0
        initial_stop = 0.0
        pending_exit_reason: Optional[str] = None

        trades: List[Dict[str, Any]] = []
        equity_curve: List[Dict[str, Any]] = []

        bench_start_price = None
        benchmark_complete = True

        for i in range(25, total_bars):
            curr_bar = df_calc.iloc[i]
            date_str = str(curr_bar['date'])[:10]
            curr_open = float(curr_bar['open'])
            curr_close = float(curr_bar['close'])
            curr_high = float(curr_bar['high'])
            curr_low = float(curr_bar['low'])

            # T-1 收盘信号在 T 日开盘执行；当日收盘风险信号顺延至下一交易日开盘。
            prev_analysis = multi_factor_strategy.analyze_bar(
                symbol=sym,
                name=name,
                df=df_calc,
                idx=i - 1,
                market_regime_bull=(
                    not use_market_regime
                    or bool(
                        bench_map.get(str(df_calc.iloc[i - 1]["date"])[:10], {}).get("ma20")
                        and bench_map[str(df_calc.iloc[i - 1]["date"])[:10]]["close"]
                        >= bench_map[str(df_calc.iloc[i - 1]["date"])[:10]]["ma20"]
                    )
                )
            )

            # 基准净值计算
            benchmark_bar = bench_map.get(date_str)
            b_close = benchmark_bar["close"] if benchmark_bar else None
            if bench_start_price is None:
                if b_close is not None and b_close > 0:
                    bench_start_price = b_close
            bench_return_pct = (
                round((b_close - bench_start_price) / bench_start_price * 100.0, 2)
                if b_close is not None and bench_start_price and bench_start_price > 0
                else None
            )
            if bench_return_pct is None:
                benchmark_complete = False

            # ---------------- 1. 按 T-1 已知的风控线模拟当日执行 ----------------
            exited_today = False
            if shares > 0:
                if prev_analysis and prev_analysis.get("signal_type") == "SELL" and not pending_exit_reason:
                    pending_exit_reason = "T-1收盘多因子卖出信号"
                prev_close = float(df_calc.iloc[i - 1]["close"])
                # 近似按普通主板 10% 涨跌停识别一字跌停；ST、新股等历史状态并未逐日还原。
                is_locked_limit_down = (
                    np.isclose(curr_high, curr_low, atol=0.005)
                    and curr_open <= prev_close * 0.905
                )
                prior_stop = initial_stop
                if peak_price >= entry_price * 1.05:
                    prior_stop = max(prior_stop, peak_price * (1.0 - portfolio_risk.trailing_pct))
                if peak_price >= entry_price * 1.06:
                    prior_stop = max(prior_stop, entry_price * 1.005)

                exit_reference = None
                exit_reason = ""
                if not is_locked_limit_down:
                    if pending_exit_reason:
                        exit_reference = curr_open
                        exit_reason = pending_exit_reason + " (次日开盘执行)"
                    elif curr_open <= prior_stop:
                        exit_reference = curr_open
                        exit_reason = "开盘跳空跌破前一日已知风控线"
                    elif curr_low <= prior_stop:
                        exit_reference = prior_stop
                        exit_reason = "盘中触及前一日已知风控线"

                if exit_reference is not None:
                    exec_price = max(round(exit_reference * (1.0 - self.slippage_rate), 2), 0.01)
                    proceeds = shares * exec_price
                    tax = proceeds * self.stamp_duty_rate
                    comm = max(proceeds * self.commission_rate, self.min_commission)
                    trans = proceeds * self.transfer_fee_rate
                    total_fee = tax + comm + trans
                    net_proceeds = proceeds - total_fee
                    cash += net_proceeds
                    entry_cost = shares * entry_price + entry_fee
                    pnl = net_proceeds - entry_cost
                    ret_pct = round(pnl / entry_cost * 100.0, 2) if entry_cost > 0 else 0.0
                    trades.append({
                        "entry_date": entry_date,
                        "exit_date": date_str,
                        "action": "SELL",
                        "shares": shares,
                        "entry_price": entry_price,
                        "exit_price": exec_price,
                        "return_pct": ret_pct,
                        "pnl": round(pnl, 2),
                        "fee": round(total_fee + entry_fee, 2),
                        "reason": exit_reason,
                        "is_oos": i >= oos_start_idx
                    })
                    shares = 0
                    entry_price = 0.0
                    entry_date = ""
                    entry_fee = 0.0
                    peak_price = 0.0
                    initial_stop = 0.0
                    pending_exit_reason = None
                    exited_today = True

            # ---------------- 2. T-1 收盘 BUY 信号在 T 日开盘尝试撮合 ----------------

            if shares == 0 and not exited_today and prev_analysis and prev_analysis.get("signal_type") == "BUY":
                # 严控一字涨停无法买入情况 (今日最高等于最低且涨幅>=9.8%)
                is_limit_up = (curr_high == curr_low) and (curr_open >= float(df_calc.iloc[i-1]['close']) * 1.095)
                if not is_limit_up and cash >= 2000.0:
                    exec_price = round(curr_open * (1.0 + self.slippage_rate), 2)
                    # 风险预算控制单笔最大损失，且不以近乎满仓代替组合仓位管理。
                    levels = prev_analysis.get("trade_levels", {})
                    stop_fraction = max(float(levels.get("stop_pct", 0.0)) / 100.0, 0.001)
                    planned_stop = max(
                        round(exec_price * (1.0 - stop_fraction), 2),
                        float(levels.get("initial_stop", exec_price * (1.0 - stop_fraction)))
                    )
                    # 若开盘已低于信号日止损位，跳过买入，避免买入即处于失效交易中。
                    valid_entry = curr_open > planned_stop
                    actual_stop_fraction = max((exec_price - planned_stop) / exec_price, 0.001) if valid_entry else 0.0
                    risk_budget = cash * max(portfolio_risk.risk_per_trade_pct, 0.0) / 100.0
                    risk_limited_capital = risk_budget / actual_stop_fraction if valid_entry else 0.0
                    allowed_exposure = (
                        portfolio_risk.bull_exposure
                        if not use_market_regime or prev_analysis.get("signal_type") == "BUY"
                        else 0.0
                    )
                    regime_capital = cash * min(
                        allowed_exposure,
                        portfolio_risk.max_single_pct,
                        allowed_exposure / max(portfolio_risk.max_positions, 1)
                    )
                    target_invest = min(risk_limited_capital, regime_capital)
                    raw_shares = int(target_invest / (exec_price * 1.001))
                    buy_shares = (raw_shares // 100) * 100 if valid_entry else 0

                    if buy_shares >= 100:
                        cost = buy_shares * exec_price
                        comm = max(cost * self.commission_rate, self.min_commission)
                        trans = cost * self.transfer_fee_rate
                        total_fee = comm + trans
                        if cash >= (cost + total_fee):
                            cash -= (cost + total_fee)
                            shares = buy_shares
                            entry_price = exec_price
                            entry_date = date_str
                            entry_fee = total_fee
                            peak_price = exec_price
                            initial_stop = planned_stop

                            # A股 T+1：入场日触及止损只排定下一交易日退出，不允许当日卖出。
                            if curr_open > initial_stop and curr_low <= initial_stop:
                                pending_exit_reason = "入场日触及初始风控线（受T+1限制，次日尝试退出）"

                            if shares > 0:
                                trades.append({
                                    "date": date_str,
                                    "action": "BUY",
                                    "shares": shares,
                                    "price": entry_price,
                                    "fee": round(total_fee, 2),
                                    "reason": prev_analysis.get("signal_desc", "多因子共振买点"),
                                    "is_oos": i >= oos_start_idx
                                })

            # 日内高点只更新次日使用的移动止损；收盘产生的退出信号顺延到下一开盘。
            if shares > 0:
                peak_price = max(peak_price, curr_high)
                should_exit, exit_reason = portfolio_risk.check_exit_signals(
                    current_price=curr_close,
                    entry_price=entry_price,
                    highest_price=peak_price,
                    initial_stop=initial_stop
                )
                if should_exit and not pending_exit_reason:
                    pending_exit_reason = exit_reason

            # ---------------- 3. 计算当日收盘总净值 ----------------
            current_equity = cash + (shares * curr_close)
            cum_ret_pct = round((current_equity - self.initial_capital) / self.initial_capital * 100.0, 2)

            equity_curve.append({
                "date": date_str,
                "portfolio_equity": round(current_equity, 2),
                "portfolio_return_pct": cum_ret_pct,
                "benchmark_return_pct": bench_return_pct,
                "is_oos": i >= oos_start_idx
            })

        # ---------------- 4. 统计机构级回测指标 ----------------
        if not equity_curve:
            return {"error": "回测时段未产生有效净值序列"}

        eq_series = pd.Series([x["portfolio_equity"] for x in equity_curve])
        final_equity = float(eq_series.iloc[-1])
        total_return_pct = round((final_equity - self.initial_capital) / self.initial_capital * 100.0, 2)

        # 最大回撤 (Max Drawdown)
        cum_max = eq_series.cummax()
        drawdowns = (eq_series - cum_max) / cum_max
        max_drawdown_pct = round(float(abs(drawdowns.min())) * 100.0, 2)

        # 年化收益率 (CAGR)
        trade_days = len(equity_curve)
        years = trade_days / 244.0
        cagr = round(((final_equity / self.initial_capital) ** (1.0 / years) - 1.0) * 100.0, 2) if years > 0 and final_equity > 0 else 0.0

        # 日收益率序列
        daily_ret = eq_series.pct_change().dropna()
        daily_std = float(daily_ret.std())
        annual_volatility = round(daily_std * np.sqrt(244) * 100.0, 2)

        # 夏普比率 (无风险利率 2.0%)
        rf_daily = 0.02 / 244.0
        excess_ret = daily_ret - rf_daily
        sharpe = round(float(excess_ret.mean() / daily_std * np.sqrt(244)), 2) if len(daily_ret) > 1 and daily_std > 0 else None

        # 索提诺比率 (仅惩罚下行波动)
        downside_std = float(excess_ret[excess_ret < 0].std())
        sortino = round(float(excess_ret.mean() / downside_std * np.sqrt(244)), 2) if downside_std > 0 else None

        # 卡玛比率 (Calmar Ratio = CAGR / Max Drawdown)
        calmar = round(cagr / max_drawdown_pct, 2) if max_drawdown_pct > 0 else None

        # 交易战绩统计
        sell_trades = [t for t in trades if t.get("action") == "SELL"]
        win_trades = [t for t in sell_trades if t.get("return_pct", 0) > 0]
        loss_trades = [t for t in sell_trades if t.get("return_pct", 0) <= 0]
        total_trades_count = len(sell_trades)
        win_rate = round(len(win_trades) / total_trades_count * 100.0, 1) if total_trades_count > 0 else None

        avg_win = float(np.mean([t["return_pct"] for t in win_trades])) if win_trades else None
        avg_loss = float(abs(np.mean([t["return_pct"] for t in loss_trades]))) if loss_trades else None
        profit_loss_ratio = round(avg_win / avg_loss, 2) if avg_win is not None and avg_loss is not None and avg_loss > 0 else None

        # 样本外 (OOS) 防过拟合检验
        oos_eq = [x for x in equity_curve if x["is_oos"]]
        if oos_eq:
            first_oos_idx = next(i for i, point in enumerate(equity_curve) if point["is_oos"])
            oos_start_eq = equity_curve[first_oos_idx - 1]["portfolio_equity"] if first_oos_idx > 0 else self.initial_capital
            oos_final_eq = oos_eq[-1]["portfolio_equity"]
            oos_return_pct = round((oos_final_eq - oos_start_eq) / oos_start_eq * 100.0, 2) if oos_start_eq > 0 else 0.0
        else:
            oos_return_pct = total_return_pct

        benchmark_final_ret = equity_curve[-1]["benchmark_return_pct"] if equity_curve and benchmark_complete else None
        alpha_pct = round(total_return_pct - benchmark_final_ret, 2) if benchmark_final_ret is not None else None

        return {
            "symbol": sym,
            "name": name,
            "days": trade_days,
            "start_date": equity_curve[0]["date"],
            "end_date": equity_curve[-1]["date"],
            "initial_capital": self.initial_capital,
            "final_equity": round(final_equity, 2),
            "total_return_pct": total_return_pct,
            "cagr": cagr,
            "benchmark_return_pct": benchmark_final_ret,
            "alpha_pct": alpha_pct,
            "benchmark_available": benchmark_final_ret is not None,
            "max_drawdown_pct": max_drawdown_pct,
            "annual_volatility_pct": annual_volatility,
            "sharpe_ratio": sharpe,
            "sortino_ratio": sortino,
            "calmar_ratio": calmar,
            "total_trades": total_trades_count,
            "win_rate": win_rate,
            "profit_loss_ratio": profit_loss_ratio,
            "oos_return_pct": oos_return_pct,
            "oos_method": "按日期先后切分的末段收益统计；策略参数未使用训练段拟合，不能视作独立盲测。",
            "execution_assumptions": "T-1 收盘信号在 T 日开盘撮合；卖出遵守A股T+1，收盘退出信号和入场日触发的止损于可卖出后的开盘尝试执行；盘中止损只使用前一日已知止损线。涨跌停按普通主板约10%近似，ST/IPO等历史状态未逐日还原。",
            "open_position": {
                "shares": shares,
                "entry_date": entry_date or None,
                "entry_price": round(entry_price, 2) if shares > 0 else None,
                "pending_exit": pending_exit_reason
            },
            "trades": trades[-30:],  # 最近30笔交易流水
            "equity_curve": equity_curve[::2]  # 抽样便于前端极速渲染
        }

backtest_engine_v2 = BacktestEngineV2()
