"""
P2/P3 工程高可用、日志可观测、推送韧性、接口校验与清理回归测试套件
涵盖：
1. P2-1 关键风控警报持久化延迟队列 + 冷却解除自动补发 + 多群局部失败目标级去重 + WeChat 重试与弱转强格式
2. P2-2 RotatingFileHandler 分级轮转日志自动隔离写入 DATA_DIR/logs/quant.log
3. P2-3 DataEngine 20连接池复用 + 4秒分时并发缓存 + 新浪备用行情字段补齐 + 移除无效东财阻塞请求
4. P2-4 GET /api/t0/monitor 纯只读不外发推送 + Malformed 输入统一 400/422 校验 + 调度器有界线程池与优雅关闭
5. P3 config.DEFAULT_CONFIG 零死配置残留 + t0_smart_engine 去重校验
"""
import logging
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import tests_support  # noqa: F401
import pandas as pd

if sys.platform.startswith("win") and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="ignore")
    except Exception:
        pass

from fastapi.testclient import TestClient
from config import CONFIG_FILE, DATA_DIR, DEFAULT_CONFIG, SIGNALS_FILE, load_config
from core.backtest_portfolio import backtest_engine_v2
from core.data_engine import DataEngine
from core.limit_up_engine import LimitUpEngine
from core.logger import get_logger
from core.persistence import atomic_write_json, atomic_write_text, safe_read_json
from notifier import manager as nm_mod
from notifier.dingtalk import dingtalk_rate_limiter
from notifier.wechat import WeChatNotifier
import scheduler.task_runner as tr_mod
from scheduler.task_runner import QuantScheduler2
from web.app import app


class TestP2P3Fixes(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_01_critical_alert_deferred_queue_and_post_cooldown_flush(self):
        """P2-1: 强平/止损关键警报在限流冷却期间持久落盘排队，冷却结束后自动补发且不烧光重试次数"""
        dedup_file = self.tmp_path / "push_dedup.json"
        sig_file = self.tmp_path / "signals_history.json"
        pending_file = self.tmp_path / "pending_critical_alerts.json"
        hook = "https://oapi.dingtalk.com/robot/send?access_token=p2_crit_test"

        # 触发该 webhook 冷却
        dingtalk_rate_limiter.trigger_cooldown(hook, errmsg="freq limit", errcode=130101)
        try:
            critical_sig = {
                "symbol": "sh600519",
                "name": "贵州茅台",
                "signal_type": "FORCE_EXIT",
                "signal_desc": "触发硬止损强平警报",
                "price": 1500.0,
                "holding_cost": 1650.0,
                "holding_shares": 100,
                "pnl_pct": -9.09,
                "pnl_amt": -15000.0,
                "reasons": ["回撤跌破硬止损线"]
            }

            cfg_mock = {
                "dingtalk_enabled": True,
                "dingtalk_webhook": hook,
                "dingtalk_secret": "SEC_crit",
                "wechat_enabled": False
            }

            with patch.object(nm_mod, "PUSH_DEDUP_FILE", dedup_file), \
                 patch.object(nm_mod, "SIGNALS_FILE", sig_file), \
                 patch.object(nm_mod, "PENDING_ALERTS_FILE", pending_file), \
                 patch.object(nm_mod, "load_config", return_value=cfg_mock), \
                 patch("core.t0_group_manager.t0_group_manager.get_groups_for_stock", return_value=[]):

                nm1 = nm_mod.NotificationManager()
                with patch("notifier.manager.DingTalkNotifier") as MockDT:
                    MockDT.return_value.send_trade_signal.return_value = False
                    res1 = nm1.send_signal(critical_sig)
                    self.assertFalse(res1["dingtalk"])

                queued = safe_read_json(pending_file, [])
                self.assertEqual(len(queued), 1)
                self.assertEqual(queued[0]["symbol"], "sh600519")
                self.assertEqual(queued[0]["signal_type"], "FORCE_EXIT")

                # 重启重建实例：仍在冷却期时 flush 不应耗尽 attempts，也不应调用外发
                nm2 = nm_mod.NotificationManager()
                with patch("notifier.manager.DingTalkNotifier") as MockDT_cd:
                    flushed_cd = nm2.flush_pending_critical_alerts()
                    self.assertEqual(flushed_cd, 0)
                    MockDT_cd.assert_not_called()
                queued_still = safe_read_json(pending_file, [])
                self.assertEqual(len(queued_still), 1)
                self.assertEqual(queued_still[0]["attempts"], 0)

                # 解除冷却后，flush 自动补发成功并清空队列
                key = dingtalk_rate_limiter._normalize_key(hook)
                with dingtalk_rate_limiter._lock:
                    dingtalk_rate_limiter._cooldown_until.pop(key, None)

                with patch("notifier.manager.DingTalkNotifier") as MockDT_ok:
                    MockDT_ok.return_value.send_trade_signal.return_value = True
                    flushed_ok = nm2.flush_pending_critical_alerts()
                    self.assertEqual(flushed_ok, 1)
                    MockDT_ok.return_value.send_trade_signal.assert_called_once()

                self.assertEqual(safe_read_json(pending_file, []), [])
                # 完全送达后全局键也已落库，再次发送应跳过
                with patch("notifier.manager.DingTalkNotifier") as MockDT_after:
                    res_after = nm2.send_signal(critical_sig)
                    self.assertTrue(res_after["skipped"])
                    MockDT_after.assert_not_called()
        finally:
            key = dingtalk_rate_limiter._normalize_key(hook)
            with dingtalk_rate_limiter._lock:
                dingtalk_rate_limiter._cooldown_until.pop(key, None)
                dingtalk_rate_limiter._stats.pop(key, None)

    def test_02_multi_group_partial_failure_per_target_dedup(self):
        """P2-1: 多群局部失败仅抑制已成功群，未送达群允许后续重试补达且不重复写信号台账"""
        dedup_file = self.tmp_path / "push_dedup.json"
        sig_file = self.tmp_path / "signals_history.json"
        pending_file = self.tmp_path / "pending_critical_alerts.json"
        hook_a = "https://oapi.dingtalk.com/robot/send?access_token=groupA_ok"
        hook_b = "https://oapi.dingtalk.com/robot/send?access_token=groupB_fail"

        groups_for_stock = [
            {"id": "gA", "name": "分组A", "enabled": True, "push_t0": True, "dingtalk_webhook": hook_a, "dingtalk_secret": "sA"},
            {"id": "gB", "name": "分组B", "enabled": True, "push_t0": True, "dingtalk_webhook": hook_b, "dingtalk_secret": "sB"},
        ]
        t0_sig = {
            "symbol": "sh601138",
            "name": "工业富联",
            "signal_type": "BUY_T",
            "signal_desc": "正T低吸触发",
            "price": 24.5,
            "reasons": ["回踩VWAP企稳"]
        }

        called_hooks_1 = []
        called_hooks_2 = []

        def make_notifier_pass1(webhook="", secret=""):
            m = MagicMock()
            def _send(sig):
                called_hooks_1.append(webhook)
                return webhook == hook_a  # 仅 A 成功，B 失败
            m.send_trade_signal.side_effect = _send
            return m

        def make_notifier_pass2(webhook="", secret=""):
            m = MagicMock()
            def _send(sig):
                called_hooks_2.append(webhook)
                return True
            m.send_trade_signal.side_effect = _send
            return m

        with patch.object(nm_mod, "PUSH_DEDUP_FILE", dedup_file), \
             patch.object(nm_mod, "SIGNALS_FILE", sig_file), \
             patch.object(nm_mod, "PENDING_ALERTS_FILE", pending_file), \
             patch.object(nm_mod, "load_config", return_value={"dingtalk_enabled": False, "wechat_enabled": False}), \
             patch("core.t0_group_manager.t0_group_manager.get_groups_for_stock", return_value=groups_for_stock):

            nm = nm_mod.NotificationManager()
            with patch("notifier.manager.DingTalkNotifier", side_effect=make_notifier_pass1):
                r1 = nm.send_signal(t0_sig)
                self.assertTrue(r1["dingtalk"])
            self.assertEqual(called_hooks_1, [hook_a, hook_b])

            # 第 2 次重试：只能发往未成功的分组B，绝不能重复轰炸分组A
            with patch("notifier.manager.DingTalkNotifier", side_effect=make_notifier_pass2):
                r2 = nm.send_signal(t0_sig)
                self.assertTrue(r2["dingtalk"])
            self.assertEqual(called_hooks_2, [hook_b])

            # 第 3 次：两群均已完成送达，全局去重跳过
            with patch("notifier.manager.DingTalkNotifier") as MockDT3:
                r3 = nm.send_signal(t0_sig)
                self.assertTrue(r3["skipped"])
                MockDT3.assert_not_called()

            # 信号历史台账只记 1 条，绝不重复
            hist = safe_read_json(sig_file, [])
            self.assertEqual(len(hist), 1)

    def test_03_wechat_retry_session_and_weak_to_strong_format(self):
        """P2-1: WeChatNotifier 具备持久会话连接池、失败重试退避及弱转强 LIMIT_UP 卡片格式"""
        wx = WeChatNotifier(provider="pushplus", token="wx_test_token")
        adapter = wx.session.get_adapter("https://")
        self.assertGreaterEqual(adapter._pool_maxsize, 4)

        resp_fail = MagicMock(status_code=502)
        resp_ok = MagicMock(status_code=200)
        resp_ok.json.return_value = {"code": 200}

        captured_payloads = []
        def _fake_post(url, json=None, data=None, timeout=6):
            captured_payloads.append(json or data)
            if len(captured_payloads) < 3:
                return resp_fail
            return resp_ok

        lu_sig = {
            "symbol": "sh601127",
            "name": "赛力斯",
            "signal_type": "LIMIT_UP",
            "signal_desc": "T-1炸板分歧 · 早盘弱转强",
            "board_type": "炸板反包弱转强",
            "ladder_count": 2,
            "hardness_score": 89.5,
            "seal_time": "09:42",
            "t1_divergence_type": "T-1炸板长上影",
            "open_count": 0,
            "seal_turnover_ratio": 18.4,
            "price": 120.5,
            "reasons": ["早盘放量反包站稳VWAP"]
        }

        with patch.object(wx.session, "post", side_effect=_fake_post), \
             patch("notifier.wechat.time.sleep", return_value=None):
            ok = wx.send_trade_signal(lu_sig)

        self.assertTrue(ok)
        self.assertEqual(len(captured_payloads), 3)
        body = captured_payloads[-1]["content"]
        self.assertIn("弱转强形态", body)
        self.assertIn("T-1炸板长上影", body)
        self.assertIn("89.5 分", body)
        self.assertNotIn("仅使用日线数据生成的候选信号", body)

    def test_04_rotating_logger_writes_to_data_dir(self):
        """P2-2: 轮转日志引擎将结构化日志落盘至隔离的 DATA_DIR/logs/quant.log"""
        probe_token = f"ROTATING_LOG_PROBE_{datetime.now().strftime('%H%M%S%f')}"
        lg = get_logger("p2_probe")
        lg.info(probe_token)

        for h in logging.getLogger("qt2").handlers:
            h.flush()

        log_file = Path(DATA_DIR) / "logs" / "quant.log"
        self.assertTrue(log_file.exists(), f"Expected rotating log file at {log_file}")
        content = log_file.read_text(encoding="utf-8", errors="ignore")
        self.assertIn(probe_token, content)

    def test_05_data_engine_pool_minute_cache_and_sina_fallback_enrichment(self):
        """P2-3: DataEngine 20连接池、4秒分时并发缓存、新浪备用行情 VWAP/涨跌停补全、移除无用东财调用"""
        de = DataEngine()
        self.assertEqual(de.session.get_adapter("https://")._pool_maxsize, 20)
        self.assertEqual(de.session.get_adapter("http://")._pool_maxsize, 20)

        min_resp = MagicMock(status_code=200)
        min_resp.json.return_value = {
            "data": {
                "sh600519": {
                    "data": {
                        "data": [
                            "0930 1600.00 100 16000000.0",
                            "0931 1605.00 200 32050000.0",
                        ]
                    }
                }
            }
        }

        with patch.object(de.session, "get", return_value=min_resp) as mock_get:
            df1 = de.get_minute_data("sh600519")
            df2 = de.get_minute_data("sh600519")
            self.assertIsNotNone(df1)
            self.assertIsNotNone(df2)
            self.assertEqual(mock_get.call_count, 1)
            df1.iloc[0, df1.columns.get_loc("price")] = 9999.0
            self.assertNotEqual(float(df2.iloc[0]["price"]), 9999.0)

        sina_raw = 'var hq_str_sh600519="贵州茅台,1600.00,1590.00,1620.00,1630.00,1595.00,0,0,10000,16200000.0";'
        sina_resp = MagicMock(status_code=200, content=sina_raw.encode("gbk"))
        with patch.object(de.session, "get", return_value=sina_resp):
            quotes = de._fetch_sina_quotes_fallback(["sh600519"])
        q = quotes["sh600519"]
        self.assertEqual(q["volume"], 100.0)
        self.assertEqual(q["vwap"], 1620.0)
        self.assertEqual(q["limit_up_price"], round(1590.0 * 1.10, 2))
        self.assertEqual(q["limit_down_price"], round(1590.0 * 0.90, 2))
        self.assertEqual(q["turnover_rate"], 0.0)
        self.assertEqual(q["vol_ratio"], 0.0)

        # get_market_breadth 不应发起 push2.eastmoney.com 阻塞请求
        with patch.object(de, "get_realtime_quotes", return_value={"sh000001": {"price": 3200.0, "change_pct": 0.9}}), \
             patch.object(de.session, "get") as mock_session_get:
            breadth = de.get_market_breadth()
            self.assertEqual(breadth["regime"], "强势单边多头")
            mock_session_get.assert_not_called()

    def test_06_web_t0_monitor_read_only_and_input_validation(self):
        """P2-4: GET /api/t0/monitor 纯只读不推钉钉；畸形参数返回 400/422 而非 500；损坏历史文件安全返回 []"""
        client = TestClient(app)

        fake_t0 = {
            "symbol": "sh600519",
            "name": "贵州茅台",
            "signal_action": "BUY_T",
            "current_price": 1600.0
        }
        with patch("web.app.load_config", return_value={"t0_monitor_enabled": True}), \
             patch("web.app.load_watchlist", return_value=[{"symbol": "sh600519", "name": "贵州茅台", "holding_cost": 1620.0, "holding_shares": 100, "available_shares": 100}]), \
             patch("web.app.data_engine.get_realtime_quotes", return_value={"sh600519": {"price": 1600.0, "change_pct": -1.2}}), \
             patch("web.app.data_engine.get_daily_kline", return_value=pd.DataFrame({"close": [1600.0] * 60})), \
             patch("web.app.sentiment_radar.analyze_market_regime", return_value={"data_available": True, "target_exposure": 0.65}), \
             patch("web.app.t0_smart_engine.analyze_t0", return_value=fake_t0), \
             patch("web.app.t0_group_manager.get_groups", return_value=[]), \
             patch("web.app.notification_manager.send_signal") as mock_send:
            resp = client.get("/api/t0/monitor")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(len(resp.json()), 1)
            mock_send.assert_not_called()

        with patch("web.app.data_engine.resolve_symbol", return_value={"symbol": "sh600519", "name": "贵州茅台", "code": "600519"}):
            r_bad_wl = client.post("/api/watchlist", json={"symbol": "sh600519", "holding_cost": "not_a_number"})
            self.assertIn(r_bad_wl.status_code, (400, 422))

        r_bad_tail = client.post("/api/tail230/trades/add", json={"symbol": "sh600519", "buy_price": "nan"})
        self.assertIn(r_bad_tail.status_code, (400, 422))

        r_bad_close = client.post("/api/tail230/trades/close", json={"symbol": "sh600519", "exit_price": "oops"})
        self.assertIn(r_bad_close.status_code, (400, 422))

        r_bad_lu = client.post("/api/limitup/record", json={"top_n": "invalid_int"})
        self.assertIn(r_bad_lu.status_code, (400, 422))

        atomic_write_text(SIGNALS_FILE, "{corrupted_json")
        r_hist = client.get("/api/signals/history")
        self.assertEqual(r_hist.status_code, 200)
        self.assertEqual(r_hist.json(), [])
        atomic_write_json(SIGNALS_FILE, [])

    def test_07_scheduler_bounded_executor_and_clean_stop(self):
        """P2-4: QuantScheduler2 使用有界 ThreadPoolExecutor 且 stop() 能干净关闭线程池"""
        sched = QuantScheduler2(state_file=self.tmp_path / "sched_p2.json")
        self.assertIsInstance(sched._bg_executor, ThreadPoolExecutor)
        self.assertEqual(sched._bg_executor._max_workers, 2)

        with patch.object(sched, "_scan_market_worker", return_value=None):
            sched.run_market_scan_async(limit=20, notify=False)
        sched.stop()
        self.assertIsNone(sched._bg_executor)
        self.assertFalse(sched.is_running)

    def test_08_config_dead_keys_removed_and_t0_no_duplicate_msg(self):
        """P3: 校验 21 个未使用死配置已从 DEFAULT_CONFIG 移除，且 t0_smart_engine 无重复描述赋值"""
        removed_keys = [
            "chip_lookback", "chip_concentration_target", "chip_profit_ratio_min",
            "ma_short", "ma_mid", "ma_long", "atr_period",
            "allow_main_board_only", "exclude_st", "max_screener_results",
            "use_volatility_parity", "use_market_regime_adaptive",
            "t0_cost_dilution_notify", "t0_minute_min_bars",
            "t0_min_market_exposure", "t0_min_market_exposure_sell",
            "t0_single_direction_only", "t0_eod_check_time",
            "market_review_time", "tail230_auto_track_top_n",
            "limit_up_strategy_mode"
        ]
        for k in removed_keys:
            self.assertNotIn(k, DEFAULT_CONFIG, f"Dead config key still present: {k}")

        t0_src = (Path(__file__).resolve().parent / "core" / "t0_smart_engine.py").read_text(encoding="utf-8")
        dup_phrase = 'signal_desc = "触及高抛上轨·等待动能拐点共振"'
        self.assertEqual(t0_src.count(dup_phrase), 1)

    def test_09_stop_t_cross_bucket_flush_and_post_close_scheduler_flush(self):
        """审计#1: STOP_T 跨30分钟窗补发按原始去重键结算，且盘后15:05调度心跳仍可冲洗延迟警报"""
        dedup_file = self.tmp_path / "push_dedup.json"
        sig_file = self.tmp_path / "signals_history.json"
        pending_file = self.tmp_path / "pending_critical_alerts.json"
        hook = "https://oapi.dingtalk.com/robot/send?access_token=p2_stopt_bucket"
        today_str = datetime.now().strftime("%Y-%m-%d")

        t_1428 = datetime.strptime(f"{today_str} 14:28:00", "%Y-%m-%d %H:%M:%S")
        t_1439 = datetime.strptime(f"{today_str} 14:39:00", "%Y-%m-%d %H:%M:%S")
        orig_bucket_key = nm_mod.NotificationManager._dedup_key("sh601138", "STOP_T", t_1428)
        next_bucket_key = nm_mod.NotificationManager._dedup_key("sh601138", "STOP_T", t_1439)
        self.assertNotEqual(orig_bucket_key, next_bucket_key)

        atomic_write_json(pending_file, [{
            "symbol": "sh601138",
            "signal_type": "STOP_T",
            "expire_date": today_str,
            "dedup_base_key": orig_bucket_key,
            "queued_at": t_1428.strftime("%Y-%m-%d %H:%M:%S"),
            "attempts": 0,
            "max_attempts": 12,
            "failed_dt_targets": [[hook, "SEC_bucket", "做T组"]],
            "failed_wechat": False,
            "signal": {
                "symbol": "sh601138",
                "name": "工业富联",
                "signal_type": "STOP_T",
                "signal_desc": "止损触发",
                "price": 24.0
            }
        }])

        class FakeDT(datetime):
            current = t_1439
            @classmethod
            def now(cls, tz=None):
                return cls.current

        with patch.object(nm_mod, "PUSH_DEDUP_FILE", dedup_file), \
             patch.object(nm_mod, "SIGNALS_FILE", sig_file), \
             patch.object(nm_mod, "PENDING_ALERTS_FILE", pending_file), \
             patch.object(nm_mod, "load_config", return_value={"dingtalk_enabled": False, "wechat_enabled": False}), \
             patch.object(nm_mod, "datetime", FakeDT), \
             patch("notifier.manager.DingTalkNotifier") as MockDT:
            MockDT.return_value.send_trade_signal.return_value = True
            nm = nm_mod.NotificationManager()
            flushed = nm.flush_pending_critical_alerts()
            self.assertEqual(flushed, 1)
            self.assertIn(orig_bucket_key, nm._sent_cache)
            self.assertNotIn(next_bucket_key, nm._sent_cache)

        # 盘后 15:05 虽 is_trading_time=False，但调度器仍应允许按节流冲洗当日延迟关键警报
        sched = QuantScheduler2(state_file=self.tmp_path / "sched_post_flush.json")
        try:
            post_close_wed = datetime(2026, 11, 25, 15, 5, 0)
            self.assertFalse(sched.is_trading_time(post_close_wed))

            class PostCloseDT(datetime):
                @classmethod
                def now(cls, tz=None):
                    return post_close_wed

            with patch.object(tr_mod, "datetime", PostCloseDT), \
                 patch.object(tr_mod.notification_manager, "flush_pending_critical_alerts", return_value=1) as mock_flush:
                sched.run_intraday_check()
                mock_flush.assert_called_once()
        finally:
            sched.stop()

    def test_10_config_validation_backtest_reload_and_limitup_t1_field(self):
        """审计#2/#4/#5/#6: 配置拒绝null/NaN/错误类型，合法更新热重载回测引擎，弱转强透传t1_divergence_type"""
        self.assertIn("t0_trailing_profit_pct", DEFAULT_CONFIG)
        self.assertIn("t0_signal_cooldown_minutes", DEFAULT_CONFIG)

        client = TestClient(app)
        try:
            r_null = client.post("/api/config", json={"rsrs_buy_threshold": None})
            self.assertEqual(r_null.status_code, 400)
            r_str = client.post("/api/config", json={"rsrs_buy_threshold": "not_a_float"})
            self.assertEqual(r_str.status_code, 400)
            r_bool = client.post("/api/config", json={"max_portfolio_positions": True})
            self.assertEqual(r_bool.status_code, 400)
            # 配置未被写坏，仍保留浮点默认值
            self.assertIsInstance(load_config().get("rsrs_buy_threshold"), float)

            r_ok = client.post("/api/config", json={"commission_rate": 0.0005})
            self.assertEqual(r_ok.status_code, 200)
            self.assertAlmostEqual(backtest_engine_v2.commission_rate, 0.0005, places=6)
        finally:
            if CONFIG_FILE.exists():
                CONFIG_FILE.unlink()
            backtest_engine_v2.reload_config()

        lu = LimitUpEngine()
        with patch("notifier.manager.notification_manager.send_signal", return_value={"dingtalk": True, "wechat": False}) as mock_send:
            res = lu.send_limit_up_signal({
                "symbol": "sh600501",
                "name": "航天晨光",
                "price": 12.0,
                "seal_price": 12.0,
                "t1_divergence_type": "爆量烂板弱转强",
                "sector": "商业航天"
            })
            self.assertTrue(res["success"])
            mock_send.assert_called_once()
            self.assertEqual(res["signal"].get("t1_divergence_type"), "爆量烂板弱转强")
            self.assertEqual(res["signal"].get("sector"), "商业航天")

    def test_11_data_engine_non_200_fallback_and_cache_pruning(self):
        """审计#3/#7: 腾讯实时行情非200响应自动回退新浪，内存缓存超限按时间戳安全裁剪"""
        de = DataEngine()
        resp_403 = MagicMock(status_code=403)
        sina_raw = 'var hq_str_sh600519="贵州茅台,1600.00,1590.00,1610.00,1620.00,1595.00,0,0,10000,16100000.0";'
        resp_sina = MagicMock(status_code=200, content=sina_raw.encode("gbk"))

        with patch.object(de.session, "get", side_effect=[resp_403, resp_sina]):
            quotes = de.get_realtime_quotes(["sh600519"])
        self.assertIn("sh600519", quotes)
        self.assertEqual(quotes["sh600519"]["price"], 1610.0)

        dummy_cache = {f"k{i}": (float(i), i) for i in range(50)}
        DataEngine._prune_cache(dummy_cache, max_size=20, keep_size=10)
        self.assertEqual(len(dummy_cache), 10)
        self.assertIn("k49", dummy_cache)
        self.assertNotIn("k0", dummy_cache)

    def test_12_test_notification_does_not_mark_dedup_or_history(self):
        """审计: is_test=True 测试推送既不写信号台账也不污染实盘去重缓存"""
        dedup_file = self.tmp_path / "push_dedup.json"
        sig_file = self.tmp_path / "signals_history.json"
        pending_file = self.tmp_path / "pending_critical_alerts.json"
        cfg_mock = {
            "dingtalk_enabled": True,
            "dingtalk_webhook": "https://oapi.dingtalk.com/robot/send?access_token=mock_test_flag",
            "dingtalk_secret": "SEC_t",
            "wechat_enabled": False
        }
        with patch.object(nm_mod, "PUSH_DEDUP_FILE", dedup_file), \
             patch.object(nm_mod, "SIGNALS_FILE", sig_file), \
             patch.object(nm_mod, "PENDING_ALERTS_FILE", pending_file), \
             patch.object(nm_mod, "load_config", return_value=cfg_mock), \
             patch("core.t0_group_manager.t0_group_manager.get_groups_for_stock", return_value=[]):
            nm = nm_mod.NotificationManager()
            test_sig = {"symbol": "sh600519", "name": "贵州茅台", "signal_type": "BUY", "price": 1680.0, "is_test": True}
            real_sig = {"symbol": "sh600519", "name": "贵州茅台", "signal_type": "BUY", "price": 1685.0}
            with patch("notifier.manager.DingTalkNotifier") as MockDT:
                MockDT.return_value.send_trade_signal.return_value = True
                r_test = nm.send_signal(test_sig, force=True)
                self.assertTrue(r_test["dingtalk"])
                self.assertEqual(safe_read_json(sig_file, []), [])
                r_real = nm.send_signal(real_sig)
                self.assertFalse(r_real.get("skipped", False))
                self.assertTrue(r_real["dingtalk"])
                self.assertEqual(len(safe_read_json(sig_file, [])), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
