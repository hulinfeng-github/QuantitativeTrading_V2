"""
钉钉推送频控限流器 (DingTalkRateLimiter) 与多标的聚合推送单元测试套件
验证:
1. 单机器人每分钟 20 条上限的滑动窗口平滑限流拦截机制
2. 最小 1.0 秒间隔防毫秒级突发 (burst)
3. 官方 130101/429 报错自动触发 10 分钟熔断冷却
4. 同一 Webhook 加签动态参数的 Token 归一化聚合
5. 多标的信号批量聚合为单张 Markdown 卡片防刷屏
"""
import unittest
import time
from unittest.mock import patch, MagicMock

from notifier.dingtalk import (
    DingTalkRateLimiter,
    dingtalk_rate_limiter,
    DingTalkNotifier,
    format_batch_signals_markdown
)
from notifier.manager import NotificationManager

class TestDingTalkRateLimiter(unittest.TestCase):
    def setUp(self):
        self.limiter = DingTalkRateLimiter()
        self.test_webhook = "https://oapi.dingtalk.com/robot/send?access_token=test_limiter_token_888"

    def test_01_token_normalization(self):
        """测试 Webhook URL 归一化：动态加签参数与时间戳应归一化为同一底层 Token 标识"""
        url1 = "https://oapi.dingtalk.com/robot/send?access_token=token_abc123"
        url2 = "https://oapi.dingtalk.com/robot/send?access_token=token_abc123&timestamp=170000000&sign=xyz"
        key1 = self.limiter._normalize_key(url1)
        key2 = self.limiter._normalize_key(url2)
        self.assertEqual(key1, key2)
        self.assertEqual(key1, "token_token_abc123")

    def test_02_single_acquire_success(self):
        """测试单次请求能够正常无等待获取令牌"""
        allowed, wait_sec, reason = self.limiter.acquire(self.test_webhook, wait=False)
        self.assertTrue(allowed)
        self.assertEqual(wait_sec, 0.0)
        self.assertEqual(reason, "OK")

        status = self.limiter.get_status(self.test_webhook)
        self.assertEqual(status["current_window_count"], 1)
        self.assertEqual(status["remaining_quota"], 18)  # 19 - 1 = 18

    def test_03_min_interval_anti_burst(self):
        """测试最小 1.0 秒间隔限制：连续无等待申请令牌应被拦截"""
        allowed1, _, _ = self.limiter.acquire(self.test_webhook, wait=False)
        self.assertTrue(allowed1)

        # 立即申请第 2 次（wait=False 应被拦截）
        allowed2, wait_sec, reason = self.limiter.acquire(self.test_webhook, wait=False)
        self.assertFalse(allowed2)
        self.assertGreater(wait_sec, 0.0)
        self.assertLessEqual(wait_sec, 1.0)
        self.assertIn("需等待", reason)

    def test_04_sliding_window_rate_limit_19(self):
        """测试滑动窗口达到上限 (19条/分) 时的严格拦截与保护"""
        now = time.time()
        key = self.limiter._normalize_key(self.test_webhook)
        self.limiter._send_timestamps[key] = self.limiter._send_timestamps.get(key, None)
        from collections import deque
        # 模拟在过去的 30 秒内已经发送了 19 条
        self.limiter._send_timestamps[key] = deque([now - 30 + i for i in range(19)], maxlen=38)
        self.limiter._last_send_times[key] = now - 5.0

        status = self.limiter.get_status(self.test_webhook)
        self.assertEqual(status["current_window_count"], 19)
        self.assertEqual(status["remaining_quota"], 0)

        # 当配额已满时申请，若 wait=False 必须被拦截以保护机器人不被封禁 10 分钟
        allowed, wait_sec, reason = self.limiter.acquire(self.test_webhook, wait=False)
        self.assertFalse(allowed)
        self.assertGreater(wait_sec, 25.0)  # 最早一条是 now - 30，需再等约 30 秒
        self.assertIn("已发19条", reason)

    def test_05_circuit_breaker_on_dingtalk_error(self):
        """测试触发钉钉官方限流错误 (130101) 时自动启动 10 分钟熔断冷却"""
        in_cd, _ = self.limiter.is_in_cooldown(self.test_webhook)
        self.assertFalse(in_cd)

        # 触发熔断
        self.limiter.trigger_cooldown(self.test_webhook, errmsg="send too fast, freq limit", errcode=130101)
        in_cd, remain = self.limiter.is_in_cooldown(self.test_webhook)
        self.assertTrue(in_cd)
        self.assertGreater(remain, 590.0)

        # 处于熔断期间，任何申请均直接阻断
        allowed, wait_sec, reason = self.limiter.acquire(self.test_webhook, wait=False)
        self.assertFalse(allowed)
        self.assertIn("处于10分钟限流熔断期中", reason)

    def test_06_batch_signals_markdown_formatting(self):
        """测试多标的信号聚合格式化 Markdown 卡片"""
        sigs = [
            {
                "symbol": "sz002156",
                "name": "通富微电",
                "signal_type": "BUY_T",
                "signal_desc": "正T低吸机会触发",
                "price": 76.5,
                "trade_levels": {"target_price": 78.0, "initial_stop": 75.0},
                "group_name": "科技芯片组"
            },
            {
                "symbol": "sh603993",
                "name": "洛阳钼业",
                "signal_type": "SELL_T",
                "signal_desc": "倒T高抛机会触发",
                "price": 7.8,
                "trade_levels": {"target_price": 7.5, "initial_stop": 8.0},
                "group_name": "有色资源组"
            },
            {
                "symbol": "sh600519",
                "name": "贵州茅台",
                "signal_type": "FORCE_EXIT",
                "signal_desc": "🚨 强制平仓警报",
                "price": 1550.0,
                "holding_cost": 1700.0,
                "pnl_pct": -8.82,
                "group_name": "科技芯片组"
            }
        ]

        title, md = format_batch_signals_markdown(sigs, group_name="科技芯片组")
        self.assertIn("多标的量化交易指令聚合 (3 只)", title)
        self.assertIn("通富微电", md)
        self.assertIn("洛阳钼业", md)
        self.assertIn("贵州茅台", md)
        self.assertIn("正T低吸", md)
        self.assertIn("倒T高抛", md)
        self.assertIn("强制平仓", md)
        self.assertIn("聚合推送防限流引擎", md)

    def test_07_notifier_send_batch_signals(self):
        """测试 DingTalkNotifier.send_batch_signals 发送单条聚合卡片，仅消耗 1 次配额"""
        dt = DingTalkNotifier(webhook=self.test_webhook)
        sigs = [
            {"symbol": "sz002156", "name": "通富微电", "signal_type": "BUY_T", "price": 76.5},
            {"symbol": "sh603993", "name": "洛阳钼业", "signal_type": "SELL_T", "price": 7.8}
        ]

        with patch("notifier.dingtalk.requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            mock_post.return_value.json.return_value = {"errcode": 0, "errmsg": "ok"}

            ok = dt.send_batch_signals(sigs, group_name="自动化测试组")
            self.assertTrue(ok)
            # 两个信号被合并为 1 次 HTTP 请求
            self.assertEqual(mock_post.call_count, 1)

            # 验证 payload 类型为 markdown
            call_kwargs = mock_post.call_args[1]
            payload = call_kwargs["json"]
            self.assertEqual(payload["msgtype"], "markdown")
            self.assertIn("多标的量化交易指令聚合", payload["markdown"]["title"])

    def test_08_manager_send_signals_batch_routing(self):
        """测试 NotificationManager.send_signals_batch 按不同目标群自动合并归集分流"""
        nm = NotificationManager()
        sigs = [
            {"symbol": "sz002999", "name": "标的A", "signal_type": "BUY_T", "price": 10.0},
            {"symbol": "sz002999", "name": "标的A", "signal_type": "SELL_T", "price": 10.5}
        ]

        with patch("notifier.dingtalk.DingTalkNotifier.send_batch_signals") as mock_batch:
            mock_batch.return_value = True
            res = nm.send_signals_batch(sigs, force=True)
            self.assertEqual(res["total"], 2)
            self.assertTrue(res["dingtalk"])

    def test_09_manager_get_dingtalk_status(self):
        """测试获取全局与各分组钉钉机器人的配额与频控状态"""
        nm = NotificationManager()
        status_data = nm.get_dingtalk_status()
        self.assertEqual(status_data["status"], "ok")
        self.assertEqual(status_data["max_requests_per_minute"], 19)
        self.assertIn("webhooks", status_data)
        self.assertIsInstance(status_data["webhooks"], list)

if __name__ == "__main__":
    unittest.main()

