"""
P1 量化策略闭环、成本一致性与参数有效性专项回归测试套件
涵盖：
1. P1-1 做T 5元最低佣金动态扣费防微盈反噬 + 用户配置实时生效
2. P1-2 多因子过热上限拦截 + market_regime_bull 自动感知
3. P1-3 单股因果回测停牌零成交阻断 + 熊市基准敞口真生效
4. P1-4 signal_stats._friction 对齐统一费率配置
5. P1-5 财务排雷资不抵债识别 + 商誉缺失区分
6. P1-6 /api/config 热重载 tail_strategy_230 & 复盘实盘样本不被离线回测混合
"""
import sys
import tempfile
import unittest
from pathlib import Path
from datetime import datetime
from unittest.mock import patch

import tests_support  # noqa: F401
import numpy as np
import pandas as pd

if sys.platform.startswith("win") and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="ignore")
    except Exception:
        pass

from config import CONFIG_FILE, save_config, calc_round_trip_fees
from core.persistence import atomic_write_json
from core.t0_smart_engine import T0SmartEngine
from core.multi_factor_strategy import MultiFactorStrategy
from core.backtest_portfolio import BacktestEngineV2
from core.financial_engine import FinancialAnalysisEngine
from core.review_engine_v2 import ReviewEngineV2
import core.signal_stats as signal_stats


class TestP1Fixes(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name)
        if CONFIG_FILE.exists():
            CONFIG_FILE.unlink()

    def tearDown(self):
        if CONFIG_FILE.exists():
            CONFIG_FILE.unlink()
        self.tmpdir.cleanup()

    def test_p1_1_t0_min_commission_guard_and_config_respect(self):
        """P1-1: 小仓位必须受 5 元双边最低佣金扣费门槛保护，且用户阈值配置实时生效"""
        engine = T0SmartEngine()
        engine.tracker.storage_file = self.tmp_path / "t0_trades.json"
        engine.tracker.history_file = self.tmp_path / "t0_history.json"
        engine.tracker.reset_today()

        # 场景 A: 100股 @ 50元 (5000元)，双边最低佣金10元 + 印花2.5元 + 滑点5元 + 过户0.1元 ≈ 0.352%，安全垫门槛约 0.40%
        # 若冲高 +0.90% 后回落至 +0.26%，旧固定门槛 0.25% 会误平仓导致净亏；新动态门槛应阻止平仓 (HOLDING_T)
        engine.tracker.open_position("sz002636", "金安国纪", "BUY_T", 50.00, 49.25, 50.75, 100)
        engine.tracker.update_peak_price("sz002636", 50.45)  # +0.90% 触发敏捷激活
        min_df = pd.DataFrame([{"time": "10:30", "price": 50.13, "volume": 100, "vwap": 50.20}])  # +0.26%
        res_small = engine.analyze_t0(
            symbol="sz002636", name="金安国纪", current_price=50.13,
            df_minute=min_df, holding_cost=60.0, holding_shares=100, available_shares=100
        )
        self.assertEqual(res_small["signal_action"], "HOLDING_T")
        self.assertIn("5元低佣", res_small["signal_desc"])

        # 场景 B: 1000股 @ 50元 (50000元)，费率约0.25% < +0.50% -> 敏捷移动止盈应落袋且 net_pnl_amt > 0
        engine.tracker.reset_today()
        engine.tracker.open_position("sz002636", "金安国纪", "BUY_T", 50.00, 49.25, 50.75, 1000)
        engine.tracker.update_peak_price("sz002636", 50.45)
        min_df_ok = pd.DataFrame([{"time": "10:35", "price": 50.25, "volume": 100, "vwap": 50.20}])  # +0.50%
        res_big = engine.analyze_t0(
            symbol="sz002636", name="金安国纪", current_price=50.25,
            df_minute=min_df_ok, holding_cost=60.0, holding_shares=1000, available_shares=1000
        )
        self.assertEqual(res_big["signal_action"], "CLOSE_T")
        self.assertGreater(res_big["position"]["net_pnl_amt"], 0)

        # 场景 C: 修改 t0_etf_bias_buy 应真实作用于 ETF 阈值，而非被硬编码覆盖
        engine.tracker.reset_today()
        save_config({"t0_etf_bias_buy": -0.25, "t0_etf_bias_sell": 0.35})
        etf_df = pd.DataFrame([
            {"time": "10:00", "price": 1.000, "volume": 100, "vwap": 1.000},
            {"time": "10:05", "price": 0.996, "volume": 150, "vwap": 1.000},
            {"time": "10:10", "price": 0.997, "volume": 200, "vwap": 1.000},
        ])
        res_loose = engine.analyze_t0(
            symbol="sh512890", name="红利低波ETF", current_price=0.997,
            df_minute=etf_df, holding_cost=1.0, holding_shares=5000, available_shares=5000
        )
        save_config({"t0_etf_bias_buy": -2.50, "t0_etf_bias_sell": 2.50})
        res_tight = engine.analyze_t0(
            symbol="sh512890", name="红利低波ETF", current_price=0.997,
            df_minute=etf_df, holding_cost=1.0, holding_shares=5000, available_shares=5000
        )
        self.assertLess(abs(res_loose["vwap"] - res_loose["dyn_lower"]), abs(res_tight["vwap"] - res_tight["dyn_lower"]))

    def test_p1_2_multifactor_overheat_and_bull_auto_resolve(self):
        """P1-2: 多因子过热上限触发 WATCH 防追高；缺省 market_regime_bull 自动对齐情绪雷达"""
        mf = MultiFactorStrategy()
        mf.buy_threshold = 0.7
        mf.buy_upper_threshold = 1.3
        mf.score_buy_upper = 90.0

        dates = pd.date_range("2026-01-01", periods=65, freq="B")
        closes = [10.0 + i * 0.08 for i in range(65)]
        df = pd.DataFrame({
            "date": dates,
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": [100000.0] * 65,
            "ma5": closes,
            "ma20": [c * 0.96 for c in closes],
            "ma60": [c * 0.90 for c in closes],
            "rsrs_norm": [1.55] * 65,  # >= rsrs_buy_upper_threshold (1.3)
            "rsrs_beta": [1.1] * 65,
            "rsrs_r2": [0.9] * 65,
            "alpha_score": [82.0] * 65,
            "vol_ratio_5": [1.5] * 65,
            "atr": [0.3] * 65,
            "vp_corr_20": [0.4] * 65,
        })
        with patch("core.multi_factor_strategy.chip_engine.analyze_chips", return_value={
            "control_status": "主力突破起爆", "main_cost": 14.5, "bias_to_main": 4.0,
            "profit_chip_ratio": 85.0, "concentration_90": 8.0
        }):
            overheat_res = mf.analyze_bar("sh600519", "测试过热", df, idx=-1, market_regime_bull=True)
            self.assertEqual(overheat_res["signal_type"], "WATCH")
            self.assertIn("过热", overheat_res["signal_desc"])

            # 若 rsrs 处于黄金甜蜜区间 (0.7 <= 0.95 < 1.3) 且综合分 < 90.0 (30+11.25+25+20=86.25) 则允许 BUY
            df["rsrs_norm"] = 0.95
            df["alpha_score"] = 45.0
            sweet_res = mf.analyze_bar("sh600519", "测试正常", df, idx=-1, market_regime_bull=True)
            self.assertEqual(sweet_res["signal_type"], "BUY")

            # 若评分本身达到 score_buy_upper (>=90.0) 即使 RSRS < 1.3 也应防过热拥挤
            df["alpha_score"] = 85.0
            crowd_res = mf.analyze_bar("sh600519", "测试评分过热", df, idx=-1, market_regime_bull=True)
            self.assertEqual(crowd_res["signal_type"], "WATCH")
            self.assertIn("过热", crowd_res["signal_desc"])

        # 验证未传 market_regime_bull (None) 时自动对接 sentiment_radar
        df_plain = df[["date", "open", "high", "low", "close", "volume"]].copy()
        with patch("core.sentiment_radar.sentiment_radar.analyze_market_regime",
                   return_value={"data_available": True, "target_exposure": 0.90}) as mock_radar, \
             patch.object(mf, "analyze_bar", return_value={"score": 70.0, "signal_type": "WATCH", "reasons": [], "trade_levels": {}}) as mock_bar, \
             patch("core.financial_engine.financial_engine.analyze", return_value={"data_available": True, "data_quality": {"can_assess_risk": True}, "red_flags": {"has_major_risk": False}, "fundamental_score": 75.0}):
            mf.analyze("sh600519", "贵州茅台", df_plain, market_regime_bull=None)
            mock_radar.assert_called_once()
            self.assertTrue(mock_bar.call_args.kwargs.get("market_regime_bull"))

    def test_p1_3_backtest_suspension_and_bear_exposure(self):
        """P1-3: 回测跳过停牌零成交 bar，且熊市基准下最大单次敞口受 bear_exposure 约束"""
        bt = BacktestEngineV2(initial_capital=100000.0)
        dates = pd.date_range("2025-01-01", periods=90, freq="B")
        closes = [10.0 + i * 0.05 for i in range(90)]
        df_stock = pd.DataFrame({
            "date": dates,
            "open": closes,
            "high": [c * 1.02 for c in closes],
            "low": [c * 0.98 for c in closes],
            "close": closes,
            "volume": [100000.0] * 90,
        })
        # 故意将第 35~38 根 K 线设置为停牌 (volume=0)
        df_stock.loc[35:38, "volume"] = 0.0
        suspended_dates = {df_stock.loc[k, "date"].strftime("%Y-%m-%d") for k in range(35, 39)}

        # 构造熊市基准 (close 远低于 MA20 与 MA60)
        bench_df = pd.DataFrame({
            "date": dates,
            "open": [4000.0 - i * 10.0 for i in range(90)],
            "high": [4010.0 - i * 10.0 for i in range(90)],
            "low": [3990.0 - i * 10.0 for i in range(90)],
            "close": [4000.0 - i * 10.0 for i in range(90)],
            "volume": [1e8] * 90,
        })

        fake_buy = {
            "signal_type": "BUY",
            "signal_desc": "测试买点",
            "trade_levels": {"stop_pct": 1.0, "initial_stop": 1.0},
        }
        with patch("core.backtest_portfolio.data_engine.get_daily_kline", return_value=bench_df), \
             patch("core.backtest_portfolio.multi_factor_strategy.analyze_bar", return_value=fake_buy):
            res = bt.run_single_stock_backtest("sh600519", "贵州茅台", df=df_stock, days=90, use_market_regime=True)

        self.assertNotIn("error", res)
        buy_trades = [t for t in res["trades"] if t.get("action") == "BUY"]
        self.assertTrue(len(buy_trades) >= 1)
        for t in buy_trades:
            self.assertNotIn(t["date"], suspended_dates)
            # 熊市敞口 bear_exposure 默认为 0.10 (10000元)，绝不可能动用 95% (95000元) 或 bull_exposure (65%)
            self.assertLessEqual(t["shares"] * t["price"], 15000.0)

    def test_p1_4_signal_stats_reads_unified_fee_config(self):
        """P1-4: signal_stats._friction 必须响应用户配置修改的 t0/全局佣金参数"""
        save_config({"t0_commission_rate": 0.00025, "t0_min_commission": 5.0, "t0_stamp_tax_rate": 0.0005, "t0_slippage_rate": 0.0005})
        f_default = signal_stats._friction(10.0, 10.0, shares=100)
        save_config({"t0_min_commission": 10.0})
        f_higher = signal_stats._friction(10.0, 10.0, shares=100)
        self.assertGreater(f_higher, f_default)

    def test_p1_5_financial_insolvency_and_goodwill_handling(self):
        """P1-5: 资不抵债不得被 max(...,1.0) 掩盖；无商誉完整资产负债表不误杀，缺失资产负债表标记未知"""
        fe = FinancialAnalysisEngine()
        # 1. 资不抵债 (负债 > 资产) 必须触发重大风险红灯
        insolvent_raw = {
            "is_etf": False, "code": "600999",
            "dupont_list": [
                {"REPORT_DATE_NAME": "三季报", "TOTAL_ASSETS": 1e9, "TOTAL_LIABILITIES": 1.5e9,
                 "TOTAL_OPERATE_INCOME": 5e8, "PARENT_NETPROFIT": -1e8, "GOODWILL": None,
                 "ACCOUNTS_RECE": 1e8, "INVENTORY": 1e8, "DEBT_ASSET_RATIO": 150.0,
                 "NETCASH_OPERATE": -5e7, "ROE": -50.0, "TOTAL_ASSETS_TR": 0.5},
                {"REPORT_DATE_NAME": "去年同期", "ROE": 5.0, "DEBT_ASSET_RATIO": 70.0, "TOTAL_ASSETS_TR": 0.6},
            ],
            "indicator_list": [
                {"REPORT_DATE_NAME": "三季报", "ROEJQ": -50.0, "XSMLL": 10.0, "XSJLL": -20.0,
                 "TOTALOPERATEREVETZ": -10.0, "PARENTNETPROFITTZ": -100.0, "KCFJCXSYJL": -100.0, "ZCFZL": 150.0},
                {"REPORT_DATE_NAME": "去年同期", "XSMLL": 15.0},
            ],
        }
        with patch.object(fe, "fetch_raw_financials", return_value=insolvent_raw):
            res_ins = fe.analyze("sh600999", "测试资不抵债")
        self.assertTrue(res_ins["red_flags"]["has_major_risk"])
        self.assertTrue(res_ins["red_flags"]["is_insolvent"])

        # 2. 资产负债表缺失 TOTAL_ASSETS 时，goodwill_ratio 必须为 None 且 can_assess_risk=False
        missing_bs_raw = {
            "is_etf": False, "code": "600888",
            "dupont_list": [
                {"REPORT_DATE_NAME": "三季报", "TOTAL_ASSETS": None, "TOTAL_LIABILITIES": None,
                 "TOTAL_OPERATE_INCOME": 5e8, "PARENT_NETPROFIT": 1e8, "GOODWILL": None,
                 "ACCOUNTS_RECE": 1e7, "INVENTORY": None, "DEBT_ASSET_RATIO": 40.0,
                 "NETCASH_OPERATE": 1e8, "ROE": 15.0},
                {"REPORT_DATE_NAME": "去年同期", "ROE": 12.0},
            ],
            "indicator_list": [
                {"REPORT_DATE_NAME": "三季报", "ROEJQ": 15.0, "XSMLL": 30.0, "XSJLL": 20.0, "ZCFZL": 40.0},
                {"REPORT_DATE_NAME": "去年同期", "XSMLL": 28.0},
            ],
        }
        with patch.object(fe, "fetch_raw_financials", return_value=missing_bs_raw):
            res_miss = fe.analyze("sh600888", "测试缺资产表")
        self.assertIsNone(res_miss["red_flags"]["goodwill_ratio"])
        self.assertFalse(res_miss["data_quality"]["can_assess_risk"])

    def test_p1_6_hot_reload_and_review_tracking_purity(self):
        """P1-6: POST /api/config 热重载 tail_strategy_230；复盘不混入 2:30 离线回测交易充数"""
        import web.app as web_app
        from core.strategy_tail_230 import tail_strategy_230

        orig_min_pct = tail_strategy_230.min_pct
        resp = web_app.update_config({"tail230_min_pct": 2.34})
        self.assertTrue(resp["success"])
        self.assertEqual(tail_strategy_230.min_pct, 2.34)
        web_app.update_config({"tail230_min_pct": orig_min_pct})

        rev = ReviewEngineV2()
        mock_bt = {
            "win_rate": 80.0,
            "avg_return_pct": 1.2,
            "recent_trades": [
                {"symbol": "sh601138", "name": "工业富联", "signal_date": "2026-11-10", "buy_price": 40.0, "sell_price": 41.0, "return_pct": 2.5, "is_win": True}
            ] * 35,
        }
        with patch("core.strategy_tail_230.tail_strategy_230.get_cached_or_run_backtest", return_value=mock_bt):
            track = rev._track_recent_signals()
        self.assertEqual(track["total_signals"], 0)
        self.assertIsNotNone(track["tail230_backtest_ref"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
