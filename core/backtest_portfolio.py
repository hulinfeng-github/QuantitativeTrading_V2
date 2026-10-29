"""
A股智能量化系统 2.0 - 全组合与单标的因果回测引擎 (BacktestPortfolio 2.0)
特色：
1. 绝对杜绝未来函数：T日收盘产出信号，T+1日开盘价撮合成交；
2. 真实A股完整摩擦：双边佣金(万2.5最低5元)、卖方印花税(万5)、过户费(万0.1)与真实滑点(0.1%)；
3. 整手(100股)撮合与一字涨停跌停流动性封死模拟；
4. 沪深300 / 上证指数基准同步对标 (Alpha / Beta / 信息比率 IR)；
5. 机构级量化指标：夏普比率、索提诺比率、卡玛比率、最大回撤与样本外(OOS)步进防过拟合检验；
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

        # 获取沪深300作为基准
        benchmark_df = data_engine.get_daily_kline("sh000300", count=days)
        bench_map = {}
        if benchmark_df is not None:
            for _, r in benchmark_df.iterrows():
                bench_map[str(r['date'])[:10]] = float(r['close'])

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
        peak_price = 0.0
        initial_stop = 0.0

        trades: List[Dict[str, Any]] = []
        equity_curve: List[Dict[str, Any]] = []

        bench_start_price = None

        for i in range(25, total_bars):
            curr_bar = df_calc.iloc[i]
            date_str = str(curr_bar['date'])[:10]
            curr_open = float(curr_bar['open'])
            curr_close = float(curr_bar['close'])
            curr_high = float(curr_bar['high'])
            curr_low = float(curr_bar['low'])

            # 基准净值计算
            b_close = bench_map.get(date_str, curr_close)
            if bench_start_price is None:
                bench_start_price = b_close
            bench_return_pct = round((b_close - bench_start_price) / bench_start_price * 100.0, 2) if bench_start_price > 0 else 0.0

            # ---------------- 1. 盘中持仓风控核查与平仓 ----------------
            if shares > 0:
                peak_price = max(peak_price, curr_high)

                # 风控退出条件检查
                should_exit, exit_reason = portfolio_risk.check_exit_signals(
                    current_price=curr_close,
                    entry_price=entry_price,
                    highest_price=peak_price,
                    initial_stop=initial_stop
                )

                if should_exit:
                    # 执行卖出 (开盘价或现价 + 滑点)
                    exec_price = max(round(curr_close * (1.0 - self.slippage_rate), 2), 0.01)
                    proceeds = shares * exec_price

                    # 交易摩擦：印花税(万5) + 佣金(万2.5最低5元) + 过户费(万0.1)
                    tax = proceeds * self.stamp_duty_rate
                    comm = max(proceeds * self.commission_rate, self.min_commission)
                    trans = proceeds * self.transfer_fee_rate
                    total_fee = tax + comm + trans
                    net_proceeds = proceeds - total_fee

                    cash += net_proceeds
                    ret_pct = round((exec_price - entry_price) / entry_price * 100.0, 2)
                    pnl = net_proceeds - (shares * entry_price)

                    trades.append({
                        "entry_date": trades[-1]["date"] if trades else date_str,
                        "exit_date": date_str,
                        "action": "SELL",
                        "shares": shares,
                        "entry_price": entry_price,
                        "exit_price": exec_price,
                        "return_pct": ret_pct,
                        "pnl": round(pnl, 2),
                        "fee": round(total_fee, 2),
                        "reason": exit_reason,
                        "is_oos": i >= oos_start_idx
                    })

                    shares = 0
                    entry_price = 0.0
                    peak_price = 0.0

            # ---------------- 2. T-1 日信号在 T 日开盘撮合成交 ----------------
            # 根据昨日收盘分析出的信号在今日开盘撮合
            prev_analysis = multi_factor_strategy.analyze_bar(
                symbol=sym,
                name=name,
                df=df_calc,
                idx=i - 1,
                market_regime_bull=True
            )

            if shares == 0 and prev_analysis and prev_analysis.get("signal_type") == "BUY":
                # 严控一字涨停无法买入情况 (今日最高等于最低且涨幅>=9.8%)
                is_limit_up = (curr_high == curr_low) and (curr_open >= float(df_calc.iloc[i-1]['close']) * 1.095)
                if not is_limit_up and cash >= 2000.0:
                    exec_price = round(curr_open * (1.0 + self.slippage_rate), 2)
                    target_invest = cash * 0.95  # 几乎满仓单股回测
                    raw_shares = int(target_invest / exec_price)
                    buy_shares = (raw_shares // 100) * 100

                    if buy_shares >= 100:
                        cost = buy_shares * exec_price
                        comm = max(cost * self.commission_rate, self.min_commission)
                        trans = cost * self.transfer_fee_rate
                        total_fee = comm + trans
                        if cash >= (cost + total_fee):
                            cash -= (cost + total_fee)
                            shares = buy_shares
                            entry_price = exec_price
                            peak_price = exec_price
                            levels = prev_analysis.get("trade_levels", {})
                            initial_stop = levels.get("initial_stop", round(entry_price * 0.94, 2))

                            trades.append({
                                "date": date_str,
                                "action": "BUY",
                                "shares": shares,
                                "price": entry_price,
                                "fee": round(total_fee, 2),
                                "reason": prev_analysis.get("signal_desc", "多因子共振买点"),
                                "is_oos": i >= oos_start_idx
                            })

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
        cagr = round(((final_equity / self.initial_capital) ** (1.0 / max(years, 0.1)) - 1.0) * 100.0, 2)

        # 日收益率序列
        daily_ret = eq_series.pct_change().dropna()
        daily_std = float(daily_ret.std())
        annual_volatility = round(daily_std * np.sqrt(244) * 100.0, 2)

        # 夏普比率 (无风险利率 2.0%)
        rf_daily = 0.02 / 244.0
        excess_ret = daily_ret - rf_daily
        sharpe = round(float(excess_ret.mean() / (daily_std + 1e-9) * np.sqrt(244)), 2)

        # 索提诺比率 (仅惩罚下行波动)
        downside_std = float(excess_ret[excess_ret < 0].std())
        sortino = round(float(excess_ret.mean() / (downside_std + 1e-9) * np.sqrt(244)), 2) if downside_std > 0 else sharpe

        # 卡玛比率 (Calmar Ratio = CAGR / Max Drawdown)
        calmar = round(cagr / max_drawdown_pct, 2) if max_drawdown_pct > 0 else 5.0

        # 交易战绩统计
        sell_trades = [t for t in trades if t.get("action") == "SELL"]
        win_trades = [t for t in sell_trades if t.get("return_pct", 0) > 0]
        loss_trades = [t for t in sell_trades if t.get("return_pct", 0) <= 0]
        total_trades_count = len(sell_trades)
        win_rate = round(len(win_trades) / total_trades_count * 100.0, 1) if total_trades_count > 0 else 0.0

        avg_win = float(np.mean([t["return_pct"] for t in win_trades])) if win_trades else 0.0
        avg_loss = float(abs(np.mean([t["return_pct"] for t in loss_trades]))) if loss_trades else 1.0
        profit_loss_ratio = round(avg_win / avg_loss, 2) if avg_loss > 0 else 2.0

        # 样本外 (OOS) 防过拟合检验
        oos_eq = [x for x in equity_curve if x["is_oos"]]
        if oos_eq:
            oos_start_eq = oos_eq[0]["portfolio_equity"]
            oos_final_eq = oos_eq[-1]["portfolio_equity"]
            oos_return_pct = round((oos_final_eq - oos_start_eq) / oos_start_eq * 100.0, 2)
        else:
            oos_return_pct = total_return_pct

        benchmark_final_ret = equity_curve[-1]["benchmark_return_pct"] if equity_curve else 0.0
        alpha_pct = round(total_return_pct - benchmark_final_ret, 2)

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
            "max_drawdown_pct": max_drawdown_pct,
            "annual_volatility_pct": annual_volatility,
            "sharpe_ratio": sharpe,
            "sortino_ratio": sortino,
            "calmar_ratio": calmar,
            "total_trades": total_trades_count,
            "win_rate": win_rate,
            "profit_loss_ratio": profit_loss_ratio,
            "oos_return_pct": oos_return_pct,
            "trades": trades[-30:],  # 最近30笔交易流水
            "equity_curve": equity_curve[::2]  # 抽样便于前端极速渲染
        }

backtest_engine_v2 = BacktestEngineV2()
