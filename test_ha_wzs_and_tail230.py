"""
高可用断点重启恢复、2.30分时微结构战法、弱转强(Weak-to-Strong)专精打板自动化测试套件
"""
import os
import sys
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime
from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd

if sys.platform.startswith("win") and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="ignore")
    except Exception:
        pass

from core.persistence import atomic_write_json, safe_read_json, SchedulerCheckpointStore
from core.strategy_tail_230 import TailStrategy230, TailTradeTracker
from core.limit_up_engine import LimitUpEngine


def _make_base_daily(days: int = 35, start_price: float = 10.0) -> pd.DataFrame:
    dates = pd.date_range(end="2026-11-25", periods=days, freq="B")
    closes = [start_price * (1.004 ** i) for i in range(days)]
    rows = []
    for i, d in enumerate(dates):
        c = round(closes[i], 2)
        o = round(c * 0.992, 2)
        h = round(c * 1.012, 2)
        l = round(c * 0.985, 2)
        rows.append({
            "date": d.strftime("%Y-%m-%d"),
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "volume": 100000.0,
            "turnover": 180000000.0
        })
    return pd.DataFrame(rows)


def _make_minute_series(times, prices, bar_vols, prev_close: float) -> pd.DataFrame:
    cum_vol = np.cumsum(bar_vols)
    cum_amt = np.cumsum(np.array(prices) * np.array(bar_vols) * 100.0)
    vwaps = np.where(cum_vol > 0, cum_amt / (cum_vol * 100.0), prices)
    return pd.DataFrame({
        "time": times,
        "price": np.round(prices, 2),
        "volume": cum_vol,
        "amount": cum_amt,
        "vwap": np.round(vwaps, 3)
    })


class TestHAWZSAndTail230(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_01_atomic_persistence_and_checkpoint_restart(self):
        """验证原子写无残余临时文件，以及调度器断点水位跨重启恢复"""
        state_file = self.tmp_path / "scheduler_state.json"
        store1 = SchedulerCheckpointStore(state_file)
        store1.mark_task_completed("tail230_scan", "2026-11-25", status="DONE", note="3 candidates")
        store1.record_heartbeat("2026-11-25", datetime(2026, 11, 25, 14, 33, 0))

        tmp_leftovers = [p for p in self.tmp_path.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(tmp_leftovers, [])

        # 模拟服务强制重启重建实例
        store2 = SchedulerCheckpointStore(state_file)
        self.assertTrue(store2.is_task_completed("tail230_scan", "2026-11-25"))
        self.assertFalse(store2.is_task_completed("tail230_scan", "2026-11-26"))
        self.assertEqual(store2.get_last_heartbeat(), datetime(2026, 11, 25, 14, 33, 0))

        # TailTradeTracker 幂等追加与重启保留
        tracker_file = self.tmp_path / "tail_trades.json"
        tr1 = TailTradeTracker(storage_file=tracker_file)
        tr1.add_manual_trade("sz002842", "翔鹭钨业", 19.50, buy_date="2026-11-25")
        tr1.add_manual_trade("sz002842", "翔鹭钨业", 19.50, buy_date="2026-11-25")  # 重复调用应幂等
        self.assertEqual(len(tr1.get_active_positions()), 1)

        tr2 = TailTradeTracker(storage_file=tracker_file)
        self.assertEqual(len(tr2.get_active_positions()), 1)

    def test_02_notification_idempotency_across_restarts(self):
        """验证 NotificationManager 跨重启幂等去重：重启动不重推 Markdown 雷达与打板信号"""
        from notifier import manager as nm_mod
        dedup_file = self.tmp_path / "push_dedup.json"
        sig_file = self.tmp_path / "signals_history.json"
        grp_file = self.tmp_path / "groups.json"
        atomic_write_json(grp_file, [])

        today_str = datetime.now().strftime("%Y-%m-%d")
        with patch.object(nm_mod, "PUSH_DEDUP_FILE", dedup_file), \
             patch.object(nm_mod, "SIGNALS_FILE", sig_file), \
             patch.object(nm_mod, "load_config", return_value={
                 "dingtalk_enabled": True,
                 "dingtalk_webhook": "https://oapi.dingtalk.com/robot/send?access_token=mock_restart",
                 "dingtalk_secret": "SEC_mock",
                 "wechat_enabled": False
             }), \
             patch("core.t0_group_manager.t0_group_manager.get_groups", return_value=[]), \
             patch("core.t0_group_manager.t0_group_manager.get_groups_subscribed_to", return_value=[]):

            nm1 = nm_mod.NotificationManager()
            with patch("notifier.manager.DingTalkNotifier") as MockDT:
                MockDT.return_value.send_markdown.return_value = True
                MockDT.return_value.send_trade_signal.return_value = True

                # 第 1 次发送 Markdown 与 LIMIT_UP 均应成功外发
                r1 = nm1.send_markdown(
                    "尾盘雷达", "内容", broadcast_groups=True,
                    module_filter="push_tail230", dedup_key=f"tail230_scan_md:{today_str}"
                )
                self.assertTrue(r1["dingtalk"])
                lu_sig = {
                    "symbol": "sh601127",
                    "name": "赛力斯",
                    "date": today_str,
                    "price": 120.0,
                    "signal_type": "LIMIT_UP",
                    "signal_desc": "炸板反包弱转强",
                    "hardness_score": 88.0,
                    "reasons": ["T-1炸板分歧，早盘放量反包"]
                }
                r_lu1 = nm1.send_signal(lu_sig)
                self.assertTrue(r_lu1["dingtalk"])

            # 模拟进程崩溃并重新创建 NotificationManager 实例
            nm2 = nm_mod.NotificationManager()
            with patch("notifier.manager.DingTalkNotifier") as MockDT2:
                r2 = nm2.send_markdown(
                    "尾盘雷达", "内容", broadcast_groups=True,
                    module_filter="push_tail230", dedup_key=f"tail230_scan_md:{today_str}"
                )
                r_lu2 = nm2.send_signal(lu_sig)
                self.assertTrue(r2["skipped"])
                self.assertFalse(r2["dingtalk"])
                self.assertTrue(r_lu2["skipped"])
                self.assertFalse(r_lu2["dingtalk"])
                MockDT2.assert_not_called()

    def test_03_scheduler_gap_backfill_and_stale_window_guard(self):
        """验证调度器：过期时段重启绝不补发过时买入信号；安全窗口重启精准补跑且不重复"""
        from scheduler.task_runner import QuantScheduler2

        state_file = self.tmp_path / "sched_state.json"
        sched = QuantScheduler2(state_file=state_file)
        today_str = "2026-11-25"  # 星期三 (交易日)

        with patch("scheduler.task_runner.data_engine.get_trading_dates", return_value=["2026-11-24", "2026-11-25"]), \
             patch("scheduler.task_runner.tail_trade_tracker.reconcile_and_clean_expired_positions", return_value=[]), \
             patch("core.limit_up_engine.limit_up_engine.review_and_update_trades", return_value={"settled_count": 0}), \
             patch("scheduler.task_runner.review_engine_v2.run_daily_review", return_value={"date": "2026-11-25"}), \
             patch.object(sched, "run_tail230_scan_and_notify") as mock_tail, \
             patch.object(sched, "run_tail230_morning_plan") as mock_morn:

            # 1. 盘后 19:30 启动：绝不能补发 14:32 尾盘买入或 09:28 早盘计划！
            sched.check_and_backfill_missed_tasks(
                current_dt=datetime(2026, 11, 25, 19, 30, 0), wait_seconds=0, sync=True
            )
            mock_tail.assert_not_called()
            mock_morn.assert_not_called()
            self.assertTrue(sched.checkpoint.is_task_completed("tail230_scan", today_str))
            self.assertEqual(
                sched.checkpoint.get_day_tasks(today_str)["tail230_scan"]["status"],
                "SKIPPED_EXPIRED"
            )

        # 2. 新状态模拟在 14:36 安全补跑窗口重启（错过了 14:32 定时触发）
        state_file2 = self.tmp_path / "sched_state2.json"
        sched2 = QuantScheduler2(state_file=state_file2)
        with patch("scheduler.task_runner.data_engine.get_trading_dates", return_value=["2026-11-24", "2026-11-25"]), \
             patch("scheduler.task_runner.tail_trade_tracker.reconcile_and_clean_expired_positions", return_value=[]), \
             patch("core.limit_up_engine.limit_up_engine.review_and_update_trades", return_value={"settled_count": 0}), \
             patch("scheduler.task_runner.review_engine_v2.run_daily_review", return_value={"date": "2026-11-25"}), \
             patch.object(sched2, "run_intraday_check"), \
             patch.object(sched2, "run_limit_up_intraday_scan"), \
             patch.object(sched2, "run_tail230_scan_and_notify", wraps=lambda: sched2.checkpoint.mark_task_completed("tail230_scan", today_str)) as mock_tail2:

            sched2.check_and_backfill_missed_tasks(
                current_dt=datetime(2026, 11, 25, 14, 36, 0), wait_seconds=0, sync=True
            )
            mock_tail2.assert_called_once()

            # 14:39 再次重启：因为已有 checkpoint，不应再次执行！
            sched2.check_and_backfill_missed_tasks(
                current_dt=datetime(2026, 11, 25, 14, 39, 0), wait_seconds=0, sync=True
            )
            mock_tail2.assert_called_once()

    def test_04_tail230_microstructure_rejections_and_acceptance(self):
        """验证 2.30 战法真实分时微结构识别与系统性杀跌/偷袭诱多/滞涨派发拦截"""
        eng = TailStrategy230()
        df = _make_base_daily(35, start_price=10.0)
        prev_close = float(df.iloc[-2]["close"])

        # 今日稳健收阳 +3.4%，量比 1.6x
        idx_last = df.index[-1]
        df.loc[idx_last, "open"] = round(prev_close * 1.006, 2)
        df.loc[idx_last, "close"] = round(prev_close * 1.034, 2)
        df.loc[idx_last, "high"] = round(prev_close * 1.038, 2)
        df.loc[idx_last, "low"] = round(prev_close * 1.001, 2)
        df.loc[idx_last, "volume"] = 160000.0

        times = [
            "09:35", "09:45", "10:00", "10:30", "11:00", "11:30",
            "13:05", "13:30", "13:55", "14:05", "14:15", "14:20", "14:25", "14:30"
        ]
        bull_market = {"data_available": True, "temp_score": 68.0, "regime": "多头震荡市", "target_exposure": 0.65, "index_pct": 0.8}
        crash_market = {"data_available": True, "temp_score": 18.0, "regime": "系统性杀跌", "target_exposure": 0.15, "index_pct": -1.6}

        # 1. 优质稳站 VWAP + 尾盘抢筹脉冲上升
        p_good = [prev_close * (1 + pct / 100.0) for pct in [0.6, 1.0, 1.4, 1.8, 1.9, 2.0, 2.1, 2.2, 2.3, 2.5, 2.7, 2.9, 3.2, 3.4]]
        v_good = [120, 100, 90, 80, 75, 70, 75, 80, 85, 110, 120, 130, 145, 160]
        m_good = _make_minute_series(times, p_good, v_good, prev_close)

        res_good = eng.analyze_bar(df, idx=-1, minute_df=m_good, market_regime=bull_market, cutoff_hm="14:30")
        self.assertIsNotNone(res_good)
        self.assertGreaterEqual(res_good["vwap_hold_ratio"], 0.75)
        self.assertGreaterEqual(res_good["score"], 68.0)

        # 2. 系统性杀跌大盘环境硬拦截
        res_crash = eng.analyze_bar(df, idx=-1, minute_df=m_good, market_regime=crash_market, cutoff_hm="14:30")
        self.assertIsNone(res_crash)

        # 3. 拦截“全天阴跌最后几分钟偷袭诱多”
        p_sneak = [prev_close * (1 + pct / 100.0) for pct in [0.2, -0.5, -0.9, -1.2, -1.4, -1.5, -1.3, -1.2, -1.0, -0.8, -0.6, -0.4, 1.8, 3.4]]
        m_sneak = _make_minute_series(times, p_sneak, v_good, prev_close)
        res_sneak = eng.analyze_bar(df, idx=-1, minute_df=m_sneak, market_regime=bull_market, cutoff_hm="14:30")
        self.assertIsNone(res_sneak)

        # 4. 拦截“现价破位分时VWAP弱势派发”
        p_weak_vwap = [prev_close * (1 + pct / 100.0) for pct in [4.5, 4.8, 5.2, 5.0, 4.6, 4.4, 4.1, 3.9, 3.7, 3.6, 3.5, 3.5, 3.4, 3.4]]
        v_weak_vwap = [300, 300, 350, 280, 200, 150, 120, 100, 100, 90, 80, 80, 70, 70]
        m_weak = _make_minute_series(times, p_weak_vwap, v_weak_vwap, prev_close)
        res_weak = eng.analyze_bar(df, idx=-1, minute_df=m_weak, market_regime=bull_market, cutoff_hm="14:30")
        self.assertIsNone(res_weak)

    def test_05_limit_up_strict_weak_to_strong_only(self):
        """验证打板引擎严格仅做【弱转强】：屏蔽普通首板/一字板/高开低走诱多"""
        eng = LimitUpEngine()
        df = _make_base_daily(30, start_price=10.0)
        t2_close = float(df.iloc[-3]["close"])

        # Case 1: 普通平盘首板（T-1 完全平淡无分歧）-> 必须被严格剔除 (None)
        df_plain = df.copy()
        t1_c_plain = float(df_plain.iloc[-2]["close"])
        df_plain.loc[df_plain.index[-1], "open"] = round(t1_c_plain * 1.01, 2)
        df_plain.loc[df_plain.index[-1], "high"] = round(t1_c_plain * 1.10, 2)
        df_plain.loc[df_plain.index[-1], "low"] = round(t1_c_plain * 1.00, 2)
        df_plain.loc[df_plain.index[-1], "close"] = round(t1_c_plain * 1.10, 2)
        df_plain.loc[df_plain.index[-1], "volume"] = 220000.0
        self.assertIsNone(eng.evaluate_limit_up_bar("sh600501", "普通首板测试", df_plain))

        # 构造真实 T-1 炸板分歧母本 (曾触及 +10% 涨停，炸板回落收 +2.2%，放量 2.1x)
        df_wzs = df.copy()
        df_wzs.loc[df_wzs.index[-2], "open"] = round(t2_close * 1.03, 2)
        df_wzs.loc[df_wzs.index[-2], "high"] = round(t2_close * 1.10, 2)
        df_wzs.loc[df_wzs.index[-2], "low"] = round(t2_close * 1.01, 2)
        df_wzs.loc[df_wzs.index[-2], "close"] = round(t2_close * 1.022, 2)
        df_wzs.loc[df_wzs.index[-2], "volume"] = 210000.0
        t1_close = float(df_wzs.loc[df_wzs.index[-2], "close"])
        t0_limit = round(t1_close * 1.10, 2)

        # Case 2: T 日一字板无买点 -> 必须被剔除
        df_one = df_wzs.copy()
        df_one.loc[df_one.index[-1], ["open", "high", "low", "close"]] = t0_limit
        self.assertIsNone(eng.evaluate_limit_up_bar("sh600501", "一字板剔除", df_one))

        # Case 3: T 日合理高开 +3.8% 并迅速打板转强 + 分时稳站 VWAP 且早盘抢筹放量达标
        df_wzs.loc[df_wzs.index[-1], "open"] = round(t1_close * 1.038, 2)
        df_wzs.loc[df_wzs.index[-1], "low"] = round(t1_close * 1.032, 2)
        df_wzs.loc[df_wzs.index[-1], "high"] = t0_limit
        df_wzs.loc[df_wzs.index[-1], "close"] = t0_limit
        df_wzs.loc[df_wzs.index[-1], "volume"] = 180000.0

        m_times = ["09:35", "09:40", "09:45", "09:50", "10:00", "10:30", "11:30", "14:00", "15:00"]
        m_prices = [
            round(t1_close * 1.045, 2), round(t1_close * 1.072, 2), t0_limit,
            t0_limit, t0_limit, t0_limit, t0_limit, t0_limit, t0_limit
        ]
        m_vols = [28000.0, 25000.0, 22000.0, 6000.0, 5000.0, 4000.0, 3000.0, 2000.0, 1500.0]
        m_wzs = _make_minute_series(m_times, m_prices, m_vols, t1_close)

        market_ok = {"data_available": True, "temp_score": 66.0, "index_pct": 0.6}
        plate_hot = {"primary_sector": "小金属", "hot_concepts": ["钨概念(涨停4)"], "max_sector_zt": 4}
        quote_seal = {"turnover": 450000000.0, "bid1": t0_limit, "bid1_vol": 60000.0, "ask1_vol": 0.0}

        res_ok = eng.evaluate_limit_up_bar(
            "sh600501", "航天晨光", df_wzs,
            minute_df=m_wzs, market_state=market_ok, plate_info=plate_hot, quote=quote_seal
        )
        self.assertIsNotNone(res_ok)
        self.assertIn("弱转强", res_ok["board_type"])
        self.assertEqual(res_ok["seal_time"], "09:45")
        self.assertEqual(res_ok["open_count"], 0)
        self.assertGreaterEqual(res_ok["hardness_score"], 80.0)
        self.assertIsNotNone(res_ok["seal_turnover_ratio"])

        # Case 4: 假弱转强诱多（开盘直接爆量砸穿开盘价 -4.5% 且跌破昨收，而后才尾盘偷鸡拉板）-> 必须剔除
        m_trap = _make_minute_series(
            ["09:35", "09:40", "09:45", "10:00", "14:30", "15:00"],
            [round(t1_close * 1.038, 2), round(t1_close * 0.985, 2), round(t1_close * 0.975, 2), round(t1_close * 1.02, 2), t0_limit, t0_limit],
            [25000.0, 45000.0, 35000.0, 15000.0, 15000.0, 5000.0],
            t1_close
        )
        res_trap = eng.evaluate_limit_up_bar("sh600501", "假弱转强", df_wzs, minute_df=m_trap, market_state=market_ok)
        self.assertIsNone(res_trap)

        # Case 5: 早盘量能枯竭虚高（30分钟量仅占 T-1 的 2%）-> 必须剔除
        m_dry = _make_minute_series(
            ["09:35", "09:45", "10:00", "10:30", "15:00"],
            [round(t1_close * 1.05, 2), t0_limit, t0_limit, t0_limit, t0_limit],
            [1200.0, 1500.0, 1000.0, 500.0, 300.0],
            t1_close
        )
        res_dry = eng.evaluate_limit_up_bar("sh600501", "无量虚高", df_wzs, minute_df=m_dry, market_state=market_ok)
        self.assertIsNone(res_dry)


if __name__ == "__main__":
    unittest.main(verbosity=2)
