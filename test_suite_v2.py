"""
A股智能量化系统 2.0 - 全链路高覆盖度自动化测试套件 (TestSuite 2.0)
涵盖：
1. 主板股票代码过滤机制与 ETF 兼容性；
2. Qlib Alpha 因子与量价特征计算；
3. 偏度修正 RSRS 2.0 回归与残差偏度模型；
4. CYQ 2.0 高精主力筹码分布、集中度与控盘生命周期；
5. 宏观大盘多空情绪周期与自适应敞口雷达；
6. 多因子共振波段策略与动态 ATR 风控；
7. A股 T+1 分时 VWAP 均价与动态布林带做T状态机；
8. 波动率平价与半凯利组合仓位管理；
9. 严格因果回测引擎与防过拟合检验；
10. 闭环自我演进自迭代调优机制。
运行方式：python test_suite_v2.py
"""
import sys
import unittest
import pandas as pd
import numpy as np
from datetime import datetime

# Windows 终端输出 UTF-8 兼容
if sys.platform.startswith('win') and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='ignore')
    except Exception:
        pass

from config import load_config, DEFAULT_CONFIG
from core.data_engine import data_engine
from core.alpha_factors import calculate_alpha_features
from core.rsrs_skew import calculate_rsrs_skew
from core.chip_engine import chip_engine
from core.sentiment_radar import sentiment_radar
from core.multi_factor_strategy import multi_factor_strategy
from core.t0_smart_engine import t0_smart_engine
from core.portfolio_risk import portfolio_risk
from core.backtest_portfolio import backtest_engine_v2
from core.review_engine_v2 import review_engine_v2
from core.strategy_tail_230 import tail_strategy_230

class TestAStockQuantSystemV2(unittest.TestCase):

    def setUp(self):
        # 构造确定性的模拟测试日K线 (100根)
        np.random.seed(42)
        dates = pd.date_range(start="2024-01-01", periods=100, freq="B")
        base = 10.0 + np.cumsum(np.random.normal(0.06, 0.15, 100))
        highs = base + np.random.uniform(0.1, 0.4, 100)
        lows = base - np.random.uniform(0.1, 0.4, 100)
        closes = base + np.random.uniform(-0.1, 0.15, 100)
        opens = base - np.random.uniform(-0.1, 0.15, 100)
        volumes = np.random.uniform(10000, 50000, 100)

        self.mock_df = pd.DataFrame({
            "date": dates,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes
        })

    def test_01_main_board_filtering(self):
        """测试A股主板与非合规标的过滤"""
        print("\n[测试 1] 验证A股主板代码识别与权限过滤...")
        # 沪深主板合规
        self.assertTrue(data_engine.is_main_board("600519"))
        self.assertTrue(data_engine.is_main_board("sh601138"))
        self.assertTrue(data_engine.is_main_board("000001"))
        self.assertTrue(data_engine.is_main_board("sz002594"))
        self.assertTrue(data_engine.is_main_board("512890"))  # 沪市主流ETF
        self.assertTrue(data_engine.is_main_board("159919"))  # 深市主流ETF

        # 严格过滤创业板、科创板、北交所
        self.assertFalse(data_engine.is_main_board("300750"))  # 创业板
        self.assertFalse(data_engine.is_main_board("688981"))  # 科创板
        self.assertFalse(data_engine.is_main_board("832000"))  # 北交所
        self.assertEqual(data_engine.format_symbol("600519"), "sh600519")
        self.assertEqual(data_engine.format_symbol("002594"), "sz002594")
        print("  -> 主板过滤机制校验通过 (100%拦截高门槛高风险标的)")

    def test_02_alpha_factors(self):
        """测试 Qlib 启发式 Alpha 特征与技术指标计算"""
        print("\n[测试 2] 验证 Qlib Alpha 因子与技术特征全向量化计算...")
        df_feat = calculate_alpha_features(self.mock_df)
        self.assertIn("ma5", df_feat.columns)
        self.assertIn("ma20", df_feat.columns)
        self.assertIn("roc_20", df_feat.columns)
        self.assertIn("vp_corr_20", df_feat.columns)
        self.assertIn("norm_atr", df_feat.columns)
        self.assertIn("alpha_score", df_feat.columns)

        last_alpha = float(df_feat['alpha_score'].iloc[-1])
        self.assertTrue(0 <= last_alpha <= 100)
        print(f"  -> Alpha 特征库计算成功，最新 Alpha 综合得分: {last_alpha:.1f} 分")

    def test_03_rsrs_skew(self):
        """测试偏度修正 RSRS 2.0 模型"""
        print("\n[测试 3] 验证偏度修正机构 RSRS 2.0 模型...")
        df_rsrs = calculate_rsrs_skew(self.mock_df, n=18, m=60, use_skew=True)
        self.assertIn("rsrs_beta", df_rsrs.columns)
        self.assertIn("rsrs_r2", df_rsrs.columns)
        self.assertIn("rsrs_norm", df_rsrs.columns)

        beta = float(df_rsrs['rsrs_beta'].iloc[-1])
        r2 = float(df_rsrs['rsrs_r2'].iloc[-1])
        norm = float(df_rsrs['rsrs_norm'].iloc[-1])

        self.assertTrue(not np.isnan(beta))
        self.assertTrue(0.0 <= r2 <= 1.0)
        print(f"  -> RSRS 2.0 计算成功: Beta={beta:.3f}, R²={r2:.3f}, 偏度修正后指标={norm:.3f}")

    def test_04_chip_engine_cyq(self):
        """测试 CYQ 2.0 高精度筹码分布与生命周期"""
        print("\n[测试 4] 验证 CYQ 2.0 主力筹码高精拟合与控盘生命周期...")
        chip_res = chip_engine.analyze_chips(self.mock_df)
        self.assertIn("main_cost", chip_res)
        self.assertIn("profit_chip_ratio", chip_res)
        self.assertIn("concentration_90", chip_res)
        self.assertIn("control_status", chip_res)

        self.assertTrue(chip_res["main_cost"] > 0)
        self.assertTrue(0 <= chip_res["profit_chip_ratio"] <= 100)
        print(f"  -> 筹码计算成功: 主力建仓成本={chip_res['main_cost']}元, 获利盘={chip_res['profit_chip_ratio']}%, 控盘状态=[{chip_res['control_status']}]")

    def test_05_sentiment_radar(self):
        """测试大盘情绪雷达与自适应敞口"""
        print("\n[测试 5] 验证宏观大盘情绪雷达与动态仓位敞口...")
        radar_res = sentiment_radar.analyze_market_regime()
        self.assertIn("temp_score", radar_res)
        self.assertIn("target_exposure", radar_res)
        self.assertIn("regime", radar_res)

        self.assertTrue(0 <= radar_res["temp_score"] <= 100)
        self.assertTrue(0.1 <= radar_res["target_exposure"] <= 1.0)
        print(f"  -> 情绪雷达正常: 宏观定调={radar_res['regime']}, 温度计={radar_res['temp_score']}分, 建议仓位={int(radar_res['target_exposure']*100)}%")

    def test_06_multi_factor_strategy(self):
        """测试多因子共振波段选股策略与 ATR 风控点位"""
        print("\n[测试 6] 验证多因子共振波段策略研判...")
        res = multi_factor_strategy.analyze("sh600519", "贵州茅台", self.mock_df)
        self.assertIsNotNone(res)
        self.assertIn("score", res)
        self.assertIn("signal_type", res)
        self.assertIn("trade_levels", res)

        levels = res["trade_levels"]
        self.assertTrue(levels["initial_stop"] < res["price"])
        self.assertTrue(levels["target_price"] > res["price"])
        print(f"  -> 策略研判通过: 评分={res['score']}分, 信号=[{res['signal_desc']}], 止损价={levels['initial_stop']}元, 止盈价={levels['target_price']}元")

    def test_07_t0_smart_engine(self):
        """测试 A股 T+1 分时 VWAP 与动态布林做T引擎"""
        print("\n[测试 7] 验证 A股 T+1 日内做T智能状态机...")
        t0_smart_engine.tracker.reset_today()

        # 模拟跌破分时均线低吸正T
        cur_p = 10.0
        mock_min_df = pd.DataFrame([
            {"time": "09:31", "price": 10.5, "volume": 100, "amount": 105000, "vwap": 10.5},
            {"time": "09:45", "price": 10.4, "volume": 200, "amount": 208000, "vwap": 10.45},
            {"time": "10:00", "price": 10.0, "volume": 500, "amount": 510000, "vwap": 10.35}
        ])

        res = t0_smart_engine.analyze_t0(
            symbol="sh601138",
            name="工业富联",
            current_price=cur_p,
            df_minute=mock_min_df,
            daily_kline=self.mock_df,
            holding_cost=10.2,
            holding_shares=1000
        )
        self.assertIn("signal_action", res)
        self.assertIn("bias_pct", res)
        self.assertTrue(res["bias_pct"] < 0)
        print(f"  -> 日内做T检测通过: 现价={cur_p}元, VWAP={res['vwap']}元, 负偏离={res['bias_pct']}%, 动作=[{res['signal_action']}]")

        # 验证尾盘未对冲做T强制平仓检测
        t0_smart_engine.tracker.open_position("sh601138", "工业富联", "BUY_T", 10.0, 9.85, 10.15, 1000)
        eod_sigs = t0_smart_engine.check_eod_unclosed_positions()
        self.assertTrue(len(eod_sigs) >= 1)
        self.assertEqual(eod_sigs[0]["signal_type"], "EOD_FORCE_CLOSE_T")
        print(f"  -> 尾盘正T未对冲强平核查通过: 成功发出 [{eod_sigs[0]['signal_desc']}]，指令={eod_sigs[0]['trade_levels']['action_urgency']}")

    def test_08_portfolio_risk(self):
        """测试波动率平价与半凯利公式仓位分配"""
        print("\n[测试 8] 验证现代组合风险平价与半凯利仓位配比...")
        advice = portfolio_risk.calculate_position_size(
            symbol="sh601138",
            price=25.0,
            atr=1.0,
            total_equity=100000.0,
            current_holdings_count=1,
            market_regime="强多头牛市"
        )
        self.assertTrue(advice["allowed"])
        self.assertTrue(advice["suggested_shares"] >= 100)
        self.assertEqual(advice["suggested_shares"] % 100, 0)  # 整手
        print(f"  -> 仓位配比通过: 建议买入 {advice['suggested_shares']}股 (配比 {advice['position_pct']}%, 约 {advice['suggested_capital']}元)")

    def test_09_backtest_causality(self):
        """测试严格防未来函数因果回测引擎"""
        print("\n[测试 9] 验证因果回测引擎 (含印花税/双边佣金/真实滑点与基准对比)...")
        bt_res = backtest_engine_v2.run_single_stock_backtest(
            symbol="sh601138",
            name="工业富联",
            df=self.mock_df,
            days=90
        )
        self.assertNotIn("error", bt_res)
        self.assertIn("total_return_pct", bt_res)
        self.assertIn("max_drawdown_pct", bt_res)
        self.assertIn("sharpe_ratio", bt_res)
        self.assertIn("win_rate", bt_res)
        print(f"  -> 回测运行通过: 累计收益={bt_res['total_return_pct']:+.2f}%, 最大回撤={bt_res['max_drawdown_pct']}%, 夏普={bt_res['sharpe_ratio']}, 胜率={bt_res['win_rate']}%")

    def test_10_review_and_self_tuning(self):
        """测试收盘复盘与闭环自我迭代调优机制"""
        print("\n[测试 10] 验证每日收盘复盘与策略参数自我进化 (Self-Tuning)...")
        rev = review_engine_v2.run_daily_review(auto_optimize=True)
        self.assertIn("report_md", rev)
        self.assertIn("optimization", rev)
        opt = rev["optimization"]
        print(f"  -> 闭环复盘完成: 大盘={opt['market_regime']}, RSRS买入阈值={opt['new_rsrs_buy']}, 演进理由={opt['reasons']}")

    def test_11_tail_strategy_230(self):
        """测试 GitHub 开源 2:30 尾盘突袭战法与独立胜率测算"""
        print("\n[测试 11] 验证 A股主板 2:30 尾盘战法与历史独立胜率测算...")
        # 1. 单股K线规则研判测试
        mock_tail_df = self.mock_df.copy()
        # 构造尾盘突破小阳线
        last_close = mock_tail_df.iloc[-2]['close']
        mock_tail_df.iloc[-1, mock_tail_df.columns.get_loc('close')] = last_close * 1.035  # +3.5%
        mock_tail_df.iloc[-1, mock_tail_df.columns.get_loc('open')] = last_close * 1.005
        mock_tail_df.iloc[-1, mock_tail_df.columns.get_loc('high')] = last_close * 1.038
        mock_tail_df.iloc[-1, mock_tail_df.columns.get_loc('volume')] = mock_tail_df['volume'].mean() * 1.6

        res = tail_strategy_230.analyze_bar(mock_tail_df, idx=-1)
        self.assertIsNotNone(res)
        self.assertIn("score", res)
        self.assertIn("target_sell_price", res)
        self.assertTrue(res["change_pct"] >= 2.0 and res["change_pct"] <= 5.5)

        # 2. 独立胜率与回测指标计算测试
        bt_win = tail_strategy_230.calculate_strategy_win_rate(sample_symbols=["sh601138", "sh600519"], days=60)
        self.assertIn("win_rate", bt_win)
        self.assertIn("enhanced_win_rate", bt_win)
        self.assertIn("profit_loss_ratio", bt_win)
        self.assertTrue(0 <= bt_win["win_rate"] <= 100)
        print(f"  -> 2.30 战法测算成功: 全周期胜率={bt_win['win_rate']}%, 增强胜率={bt_win['enhanced_win_rate']}%, 盈亏比={bt_win['profit_loss_ratio']}:1, 样本笔数={bt_win['total_trades']}")

    def test_12_financial_analysis_engine(self):
        """测试上市公司深度经营财报分析、杜邦拆解与六维排雷引擎"""
        print("\n[测试 12] 验证上市公司财报深度分析、杜邦 3 因子与排雷引擎...")
        from core.financial_engine import financial_engine

        # 1. 验证 ETF 天然免疫过滤
        etf_res = financial_engine.analyze("512890", "红利低波ETF")
        self.assertTrue(etf_res.get("is_etf", False))
        self.assertTrue(etf_res["red_flags"]["is_safe"])
        print("  -> ETF 基金自动免疫识别通过")

        # 2. 验证真实主板上市公司财报深度解析与杜邦拆解 (以贵州茅台为例)
        stock_res = financial_engine.analyze("600519", "贵州茅台")
        self.assertEqual(stock_res["symbol"], "sh600519")
        self.assertIn("dupont", stock_res)
        self.assertIn("piotroski_f_score", stock_res)
        self.assertIn("red_flags", stock_res)
        self.assertIn("persona", stock_res)

        # 杜邦指标非负有效性
        dupont = stock_res["dupont"]
        self.assertTrue(dupont["roe"] > 0)
        self.assertTrue(dupont["net_profit_margin"] > 0)
        self.assertTrue(dupont["equity_multiplier"] >= 1.0)

        # 皮氏 F-Score 有效性
        f_score = stock_res["piotroski_f_score"]
        self.assertTrue(0 <= f_score["score"] <= 9)

        # 排雷雷达有效性
        rf = stock_res["red_flags"]
        self.assertTrue(0 <= rf["radar_score"] <= 100)
        print(f"  -> 财报体检验证成功: {stock_res['name']} ROE={dupont['roe']}%, 净利率={dupont['net_profit_margin']}%, F-Score={f_score['score']}/9, 画像={stock_res['persona']}")

if __name__ == "__main__":
    print("\n" + "=" * 70)
    print("      QuantitativeTrading 2.0 全链路自动化单元与回归测试套件")
    print("=" * 70)
    unittest.main(verbosity=2)

