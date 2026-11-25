"""
P0 严重缺陷修复与工程安全底线专项回归测试套件
涵盖：
1. P0-1 仓位配比 /100 量纲修复
2. P0-2 做T T+1 available_shares=0 严格锁仓防穿透
3. P0-3 测试目录与生产 storage/ 彻底隔离校验
4. P0-4 Watchlist / Group 原子写与并发无锁竞争/防死锁压力测试
5. P0-5 情绪雷达同日瞬时超时降级缓存 + 过期/冷启动安全清零
6. P0-6 停牌/无报价 -100% 误算防护 & 断档复盘目标交易日对齐
"""
import os
import sys
import json
import time
import tempfile
import threading
import unittest
from pathlib import Path
from datetime import datetime
from unittest.mock import patch

import tests_support  # noqa: F401 - 隔离测试目录
import pandas as pd
import numpy as np

if sys.platform.startswith("win") and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="ignore")
    except Exception:
        pass

from config import BASE_DIR, DATA_DIR, WATCHLIST_FILE, REVIEW_FILE, T0_GROUPS_FILE
from core.persistence import atomic_write_json, load_watchlist, mutate_watchlist, safe_read_json
from core.portfolio_risk import portfolio_risk
from core.sentiment_radar import SentimentRadar
from core.t0_smart_engine import T0SmartEngine
from core.t0_group_manager import T0GroupManager
from core.review_engine_v2 import ReviewEngineV2


def _make_daily(slope: float = -0.2, start: float = 20.0, days: int = 65) -> pd.DataFrame:
    dates = pd.date_range(end=datetime.now().strftime("%Y-%m-%d"), periods=days, freq="B")
    closes = [max(1.0, start + i * slope) for i in range(days)]
    return pd.DataFrame({
        "date": [d.strftime("%Y-%m-%d") for d in dates],
        "open": closes,
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "close": closes,
        "volume": [100000.0] * days,
    })


class TestP0Fixes(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_p0_1_position_size_scale(self):
        """P0-1: 仓位建议金额与百分比不得出现 100 倍放大"""
        advice = portfolio_risk.calculate_position_size(
            symbol="sh601138",
            price=25.0,
            atr=1.0,
            total_equity=100000.0,
            current_holdings_count=1,
            market_regime="强多头牛市",
        )
        self.assertTrue(advice["allowed"])
        self.assertLessEqual(advice["position_pct"], portfolio_risk.max_single_pct * 100.0)
        self.assertLessEqual(advice["suggested_capital"], 100000.0 * portfolio_risk.max_single_pct)
        self.assertGreater(advice["suggested_shares"], 0)
        self.assertEqual(advice["suggested_shares"] % 100, 0)

    def test_p0_2_t0_respects_zero_available_shares(self):
        """P0-2: 当日可用可卖为 0 (T+1 锁定) 时，严禁发出 SELL_T 或 BUY_T"""
        engine = T0SmartEngine()
        engine.tracker.storage_file = self.tmp_path / "t0_trades.json"
        engine.tracker.history_file = self.tmp_path / "t0_history.json"
        engine.tracker.reset_today()

        df_daily = _make_daily(slope=-0.2, start=20.0, days=35)
        cur_p = float(df_daily.iloc[-1]["close"])
        min_rise = pd.DataFrame([
            {"time": "09:50", "price": cur_p * 1.01, "volume": 100, "vwap": cur_p * 0.99},
            {"time": "09:55", "price": cur_p * 1.02, "volume": 150, "vwap": cur_p * 0.99},
            {"time": "10:00", "price": cur_p * 1.03, "volume": 200, "vwap": cur_p * 0.99},
            {"time": "10:05", "price": cur_p * 1.028, "volume": 250, "vwap": cur_p * 0.99},
        ])

        # 显式传 available_shares=0 (T+1 锁仓)，必须拦截 SELL_T 且 suggested_shares == 0
        locked_res = engine.analyze_t0(
            symbol="sh600000",
            name="测试锁仓股",
            current_price=cur_p * 1.028,
            df_minute=min_rise,
            daily_kline=df_daily,
            holding_cost=20.0,
            holding_shares=1000,
            available_shares=0,
        )
        self.assertNotEqual(locked_res["signal_action"], "SELL_T")
        self.assertEqual(locked_res["shares"], 0)
        self.assertIn("T+1锁定", locked_res["signal_desc"])

        # 下跌低吸场景：available_shares=0 时也禁止正T防止尾盘无法卖出旧底仓
        min_fall = pd.DataFrame([
            {"time": "10:00", "price": cur_p * 0.99, "volume": 100, "vwap": cur_p * 1.01},
            {"time": "10:05", "price": cur_p * 0.98, "volume": 150, "vwap": cur_p * 1.01},
            {"time": "10:10", "price": cur_p * 0.975, "volume": 200, "vwap": cur_p * 1.01},
            {"time": "10:15", "price": cur_p * 0.976, "volume": 250, "vwap": cur_p * 1.01},
        ])
        locked_buy = engine.analyze_t0(
            symbol="sh600000",
            name="测试锁仓股",
            current_price=cur_p * 0.976,
            df_minute=min_fall,
            daily_kline=df_daily,
            holding_cost=20.0,
            holding_shares=1000,
            available_shares=0,
        )
        self.assertNotEqual(locked_buy["signal_action"], "BUY_T")
        self.assertEqual(locked_buy["shares"], 0)

        # 未传 available_shares (None) 时向后兼容总持仓，能正常触发 SELL_T
        compat_res = engine.analyze_t0(
            symbol="sh600000",
            name="测试锁仓股",
            current_price=cur_p * 1.028,
            df_minute=min_rise,
            daily_kline=df_daily,
            holding_cost=20.0,
            holding_shares=1000,
        )
        self.assertEqual(compat_res["signal_action"], "SELL_T")
        self.assertEqual(compat_res["shares"], 1000)

    def test_p0_3_storage_isolated_from_production(self):
        """P0-3: 验证测试隔离目录绝不等于生产 storage/"""
        prod_storage = (BASE_DIR / "storage").resolve()
        self.assertNotEqual(DATA_DIR.resolve(), prod_storage)
        self.assertTrue(str(WATCHLIST_FILE.resolve()).startswith(str(DATA_DIR.resolve())))

    def test_p0_4_concurrent_watchlist_and_group_atomic_io(self):
        """P0-4: 并发写入 watchlist 与分组配置不发生 JSON 损坏、更新丢失或死锁"""
        wl_path = self.tmp_path / "watchlist.json"
        grp_path = self.tmp_path / "t0_groups.json"
        atomic_write_json(wl_path, [{"symbol": f"sh60000{i}", "holding_shares": 100, "group_id": ""} for i in range(6)])
        gm = T0GroupManager(storage_file=grp_path, watchlist_file=wl_path)

        errors = []

        def worker_wl(idx: int):
            try:
                for step in range(8):
                    def _mut(items):
                        for it in items:
                            if it["symbol"] == f"sh60000{idx}":
                                it["holding_shares"] = 100 + step
                        return True, None
                    mutate_watchlist(wl_path, _mut)
            except Exception as exc:
                errors.append(exc)

        def worker_grp(idx: int):
            try:
                for step in range(5):
                    gm.save_group({
                        "id": f"grp_concurrent_{idx}",
                        "name": f"并发组{idx}_{step}",
                        "stock_symbols": [f"sh60000{idx}"],
                    })
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker_wl, args=(i,)) for i in range(6)]
        threads += [threading.Thread(target=worker_grp, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
            self.assertFalse(t.is_alive())

        self.assertEqual(errors, [])
        final_wl = load_watchlist(wl_path)
        self.assertEqual(len(final_wl), 6)
        raw_grps = safe_read_json(grp_path, [])
        self.assertTrue(isinstance(raw_grps, list) and len(raw_grps) >= 5)

    def test_p0_5_sentiment_radar_transient_fallback_and_expiry(self):
        """P0-5: 情绪雷达冷启动缺失严格清零；同日短暂日K超时沿用同日结构；超15分钟过期严格退回防守"""
        radar = SentimentRadar()
        df_bull = _make_daily(slope=0.1, start=3000.0, days=80)

        # 1. 冷启动时日K缺失 -> 必须退回 _default_regime (data_available=False)
        with patch("core.sentiment_radar.data_engine.get_realtime_quote", return_value={"price": 3200.0, "change_pct": 0.5}), \
             patch("core.sentiment_radar.data_engine.get_daily_kline", return_value=None):
            cold = radar.analyze_market_regime()
            self.assertFalse(cold["data_available"])
            self.assertEqual(cold["target_exposure"], 0.0)

        # 2. 先成功计算一次有效同日状态，并断言不再冗余请求 sh000300
        with patch("core.sentiment_radar.data_engine.get_realtime_quote", return_value={"price": 3205.0, "change_pct": 0.6}), \
             patch("core.sentiment_radar.data_engine.get_daily_kline", return_value=df_bull) as mock_kline:
            ok_res = radar.analyze_market_regime()
            self.assertTrue(ok_res["data_available"])
            self.assertFalse(ok_res.get("data_stale", False))
            mock_kline.assert_called_once_with("sh000001", count=100)

        # 3. 同日 15 分钟内日K瞬时超时但实时快照存在 -> 平滑沿用结构并标记 data_stale=True
        with patch("core.sentiment_radar.data_engine.get_realtime_quote", return_value={"price": 3210.0, "change_pct": 0.75}), \
             patch("core.sentiment_radar.data_engine.get_daily_kline", return_value=None):
            stale_res = radar.analyze_market_regime()
            self.assertTrue(stale_res["data_available"])
            self.assertTrue(stale_res.get("data_stale"))
            self.assertEqual(stale_res["index_price"], 3210.0)
            self.assertGreater(stale_res["target_exposure"], 0.0)

        # 4. 超出 15 分钟容忍窗口 -> 严格清零防守
        radar._last_sentiment_ts -= 1000.0
        with patch("core.sentiment_radar.data_engine.get_realtime_quote", return_value={"price": 3210.0, "change_pct": 0.75}), \
             patch("core.sentiment_radar.data_engine.get_daily_kline", return_value=None):
            expired_res = radar.analyze_market_regime()
            self.assertFalse(expired_res["data_available"])
            self.assertEqual(expired_res["target_exposure"], 0.0)

    def test_p0_6_zero_quote_guard_and_review_backfill_date(self):
        """P0-6: 停牌/无报价不误算 -100% 巨亏，且断档补跑复盘按目标历史交易日写入"""
        import web.app as web_app
        from scheduler.task_runner import QuantScheduler2

        atomic_write_json(WATCHLIST_FILE, [{
            "symbol": "sz002636",
            "code": "002636",
            "name": "金安国纪",
            "holding_cost": 50.0,
            "holding_shares": 100,
            "available_shares": 100,
            "group_id": "",
            "t0_enabled": True,
        }])

        with patch("web.app.data_engine.get_realtime_quotes", return_value={}):
            wl_resp = web_app.get_watchlist()
        self.assertEqual(len(wl_resp), 1)
        self.assertFalse(wl_resp[0]["quote_available"])
        self.assertEqual(wl_resp[0]["pnl_pct"], 0.0)
        self.assertEqual(wl_resp[0]["pnl_val"], 0.0)

        rev = ReviewEngineV2()
        with patch("core.review_engine_v2.data_engine.get_realtime_quotes", return_value={}), \
             patch("core.review_engine_v2.data_engine.get_daily_kline", return_value=None):
            reviewed = rev._review_watchlist()
        self.assertEqual(len(reviewed), 1)
        self.assertFalse(reviewed[0]["price_available"])
        self.assertIsNone(reviewed[0]["pnl_pct"])
        row_md = rev._format_watchlist_review_row(reviewed[0])
        self.assertIn("停牌/无报价", row_md)
        self.assertNotIn("-100.00%", row_md)
        self.assertNotIn("破位杀跌·建议止损减仓", row_md)

        # 验证断档复盘将目标历史交易日 (2026-11-20) 透传给 run_daily_review 并记录正确 checkpoint
        sched = QuantScheduler2(state_file=self.tmp_path / "scheduler_state.json")
        captured_dates = []

        def fake_daily_review(auto_optimize=True, target_date=None):
            captured_dates.append(target_date)
            return {"date": target_date or "wrong-date"}

        monday_preopen = datetime(2026, 11, 23, 9, 10, 0)
        with patch("scheduler.task_runner.tail_trade_tracker.reconcile_and_clean_expired_positions", return_value=[]), \
             patch("core.limit_up_engine.limit_up_engine.review_and_update_trades", return_value={"settled_count": 0}), \
             patch("scheduler.task_runner.data_engine.get_trading_dates", return_value=["2026-11-19", "2026-11-20", "2026-11-23"]), \
             patch("scheduler.task_runner.review_engine_v2.run_daily_review", side_effect=fake_daily_review):
            sched.check_and_backfill_missed_tasks(current_dt=monday_preopen, wait_seconds=0.0, sync=True)

        self.assertEqual(captured_dates, ["2026-11-20"])
        self.assertTrue(sched.checkpoint.is_task_completed("post_review", "2026-11-20"))
        self.assertFalse(sched.checkpoint.is_task_completed("post_review", "2026-11-23"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
