import sys
sys.stdout.reconfigure(encoding='utf-8')
import unittest
import pandas as pd
import numpy as np

from core.t0_smart_engine import (
    t0_smart_engine, evaluate_market_regime, compute_intraday_divergence
)
from notifier.dingtalk import format_batch_signals_markdown

class TestT0RegimeAndDilution(unittest.TestCase):
    def setUp(self):
        t0_smart_engine.tracker.reset_today()
        # 清理测试标的在历史成交中的持久化记录，防止测试用例相互干扰频次限制
        hist_file = t0_smart_engine.tracker.history_file
        if hist_file.exists():
            import json
            try:
                with open(hist_file, "r", encoding="utf-8") as f:
                    h = json.load(f)
                h = [x for x in h if x.get("symbol") != "sh600000"]
                with open(hist_file, "w", encoding="utf-8") as f:
                    json.dump(h, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

    def test_01_evaluate_market_regime_bull(self):
        """测试严谨多因子牛市通道识别"""
        # 持续上升趋势
        closes = [10.0 + i * 0.1 for i in range(30)]
        df_daily = pd.DataFrame({"close": closes, "date": [f"2026-10-{i+1:02d}" for i in range(30)]})
        cur_p = closes[-1] + 0.2
        regime, ma20, reason = evaluate_market_regime(df_daily, cur_p, symbol="sh600000")
        self.assertEqual(regime, "BULL")
        self.assertTrue(cur_p >= ma20)
        print(f"  -> 牛市通道识别验证通过: 状态={regime}, MA20={ma20:.2f}, 原因={reason}")

    def test_02_evaluate_market_regime_bear(self):
        """测试严谨多因子熊市通道识别"""
        # 持续阴跌趋势
        closes = [20.0 - i * 0.2 for i in range(30)]
        df_daily = pd.DataFrame({"close": closes, "date": [f"2026-10-{i+1:02d}" for i in range(30)]})
        cur_p = closes[-1] - 0.3
        regime, ma20, reason = evaluate_market_regime(df_daily, cur_p, symbol="sh600000")
        self.assertEqual(regime, "BEAR")
        self.assertTrue(cur_p <= ma20)
        print(f"  -> 熊市通道识别验证通过: 状态={regime}, MA20={ma20:.2f}, 原因={reason}")

    def test_03_evaluate_market_regime_neutral_hysteresis(self):
        """测试均线附近迟滞缓冲区间(防频繁假突破横跳)"""
        # 价格在MA20附近微幅波动 (MA20约10.0)
        closes = [10.0] * 30
        df_daily = pd.DataFrame({"close": closes, "date": [f"2026-10-{i+1:02d}" for i in range(30)]})
        # 现价只偏离0.05元 (0.5%，在1.2%迟滞区间内)
        cur_p = 10.05
        regime, ma20, reason = evaluate_market_regime(df_daily, cur_p, symbol="sh600000")
        self.assertEqual(regime, "NEUTRAL")
        self.assertIn("迟滞平衡区间", reason)
        print(f"  -> 迟滞缓冲滤波验证通过: 状态={regime}, MA20={ma20:.2f}, 原因={reason}")

    def test_04_bear_regime_strategy_switching(self):
        """测试熊市弱势通道中策略切换: 严禁盲目正T接飞刀，主做顺势倒T"""
        closes = [20.0 - i * 0.2 for i in range(30)]
        df_daily = pd.DataFrame({"close": closes, "date": [f"2026-10-{i+1:02d}" for i in range(30)]})
        cur_p = closes[-1]

        # 模拟跌破下轨但无底背离 (单边下泄)
        mock_min_fall = pd.DataFrame([
            {"time": "10:00", "price": cur_p * 0.99, "volume": 100, "vwap": cur_p * 1.01},
            {"time": "10:05", "price": cur_p * 0.985, "volume": 150, "vwap": cur_p * 1.01},
            {"time": "10:10", "price": cur_p * 0.98, "volume": 200, "vwap": cur_p * 1.01},
            {"time": "10:15", "price": cur_p * 0.975, "volume": 250, "vwap": cur_p * 1.01},
        ])
        res_fall = t0_smart_engine.analyze_t0(
            symbol="sh600000", name="测试股", current_price=cur_p * 0.975,
            df_minute=mock_min_fall, daily_kline=df_daily, holding_cost=20.0, holding_shares=1000
        )
        # 熊市中无底背离应该拦截BUY_T
        self.assertNotEqual(res_fall["signal_action"], "BUY_T")
        self.assertIn("空头", res_fall["signal_desc"])
        print(f"  -> 熊市防接飞刀拦截验证通过: 动作=[{res_fall['signal_action']}], 描述=[{res_fall['signal_desc']}]")

        # 模拟冲高突破上轨 (倒T机会触发)
        mock_min_rise = pd.DataFrame([
            {"time": "09:50", "price": cur_p * 1.01, "volume": 100, "vwap": cur_p * 0.99},
            {"time": "09:55", "price": cur_p * 1.02, "volume": 150, "vwap": cur_p * 0.99},
            {"time": "10:00", "price": cur_p * 1.03, "volume": 200, "vwap": cur_p * 0.99},
            {"time": "10:05", "price": cur_p * 1.028, "volume": 250, "vwap": cur_p * 0.99},
        ])
        res_rise = t0_smart_engine.analyze_t0(
            symbol="sh600000", name="测试股", current_price=cur_p * 1.028,
            df_minute=mock_min_rise, daily_kline=df_daily, holding_cost=20.0, holding_shares=1000
        )
        self.assertEqual(res_rise["signal_action"], "SELL_T")
        print(f"  -> 熊市顺势倒T触发验证通过: 动作=[{res_rise['signal_action']}], 描述=[{res_rise['signal_desc']}]")

    def test_05_agile_trailing_take_profit(self):
        """测试敏捷动态移动止盈 (0.6%锁定 + 0.2%回撤落袋)"""
        # 开仓买入正T: 成本 10.00
        t0_smart_engine.tracker.open_position("sh600000", "测试股", "BUY_T", 10.00, 9.85, 10.20, 1000)
        
        # 价格冲高至 10.08 (+0.8%，越过0.6%启动线)
        t0_smart_engine.tracker.update_peak_price("sh600000", 10.08)
        
        # 现价回撤至 10.05 (+0.5%，从最高点 10.08 回撤了 0.3%，超过0.2%回撤线)
        mock_min = pd.DataFrame([
            {"time": "10:30", "price": 10.05, "volume": 100, "vwap": 10.0}
        ])
        res = t0_smart_engine.analyze_t0(
            symbol="sh600000", name="测试股", current_price=10.05,
            df_minute=mock_min, holding_cost=10.0, holding_shares=1000
        )
        self.assertEqual(res["signal_action"], "CLOSE_T")
        self.assertIn("敏捷移动止盈", res["signal_desc"])
        pos = res["position"]
        self.assertIsNotNone(pos)
        self.assertEqual(pos["status"], "FINISHED")
        print(f"  -> 敏捷移动止盈验证通过: 动作=[{res['signal_action']}], 净利={pos.get('net_pnl_amt')}元, 每股摊薄={pos.get('dilution_per_share')}元")

    def test_06_cost_dilution_and_dingtalk_card(self):
        """测试做T成本摊薄计算与钉钉卡片渲染"""
        # 开仓倒T: 均价 20.00，股数 1000
        t0_smart_engine.tracker.open_position("sh600000", "测试股", "SELL_T", 20.00, 20.30, 19.80, 1000)
        # 低吸平仓买回: 均价 19.80
        t0_smart_engine.tracker.close_position("sh600000", "FINISHED", 19.80, "目标止盈买回")
        
        pos = t0_smart_engine.tracker.get_position("sh600000")
        self.assertIsNotNone(pos)
        self.assertIn("dilution_per_share", pos)
        self.assertIn("new_holding_cost", pos)
        self.assertTrue(pos["net_pnl_amt"] > 0)
        self.assertTrue(pos["new_holding_cost"] < 20.00)
        print(f"  -> 解套摊薄计算通过: 原成本=20.00元, 做T净利={pos['net_pnl_amt']}元, 新持仓成本={pos['new_holding_cost']}元 (每股摊薄-{pos['dilution_per_share']}元)")

        # 验证推送卡片生成
        t0_res = {
            "symbol": "sh600000", "name": "测试股", "current_price": 19.80,
            "signal_action": "CLOSE_T", "signal_desc": "倒T回踩低吸买入平仓",
            "score": 90, "t0_score": 90, "t0_direction": "SELL_T", "close_action": "BUY_BACK",
            "shares": 1000, "entry_price": 20.00, "reasons": ["测试平仓"],
            "regime_status": "BEAR", "regime_reason": "弱势通道顺势倒T",
            "net_pnl_amt": pos["net_pnl_amt"], "dilution_per_share": pos["dilution_per_share"],
            "new_holding_cost": pos["new_holding_cost"]
        }
        payload = t0_smart_engine.build_signal_payload(t0_res)
        title, md_text = format_batch_signals_markdown([payload], group_name="测试解套组")
        self.assertIn("解套成本摊薄", md_text)
        self.assertIn("市场通道研判", md_text)
        print(f"  -> 钉钉解套卡片渲染验证通过:\n{md_text}")

if __name__ == "__main__":
    unittest.main()
