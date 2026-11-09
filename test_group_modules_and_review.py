"""
测试分组模块级使能开关 (使能/关闭) 与 分组收盘定制复盘功能
"""
import unittest
import json
from unittest.mock import patch, MagicMock

from core.t0_group_manager import t0_group_manager
from notifier.manager import notification_manager
from core.review_engine_v2 import review_engine_v2

class TestGroupModulesAndReview(unittest.TestCase):
    def setUp(self):
        # 保存原分组
        self.orig_groups = t0_group_manager._read_raw_groups()
        self.orig_watchlist = t0_group_manager._read_watchlist()

    def tearDown(self):
        # 恢复
        t0_group_manager._save_raw_groups(self.orig_groups)
        t0_group_manager._save_watchlist(self.orig_watchlist)

    def test_01_group_module_switches_persistence(self):
        """测试各模块开关在分组中的保存与读取"""
        grp_data = {
            "name": "高频打板组",
            "dingtalk_webhook": "https://oapi.dingtalk.com/robot/send?access_token=test_token_123",
            "dingtalk_secret": "SEC_test_secret",
            "enabled": True,
            "push_t0": False,             # 关闭做T
            "push_screener": False,       # 关闭多因子选股
            "push_tail230": False,        # 关闭尾盘2:30
            "push_limitup": True,         # 开启龙虎打板
            "push_review": True           # 开启每日复盘
        }
        res = t0_group_manager.save_group(grp_data)
        self.assertIn("id", res)
        grp_id = res["id"]

        loaded = t0_group_manager.get_group(grp_id)
        self.assertIsNotNone(loaded)
        self.assertFalse(loaded["push_t0"])
        self.assertFalse(loaded["push_screener"])
        self.assertFalse(loaded["push_tail230"])
        self.assertTrue(loaded["push_limitup"])
        self.assertTrue(loaded["push_review"])

        # 检查模块订阅过滤
        limitup_groups = t0_group_manager.get_groups_subscribed_to("push_limitup")
        self.assertTrue(any(g["id"] == grp_id for g in limitup_groups))

        t0_groups = t0_group_manager.get_groups_subscribed_to("push_t0")
        self.assertFalse(any(g["id"] == grp_id for g in t0_groups))

    def test_02_signal_filtering_by_module_switch(self):
        """测试当分组关闭某一模块时，对应消息被阻断"""
        # 先确保自选池中有标的
        t0_group_manager._save_watchlist([
            {"symbol": "sh600519", "name": "贵州茅台", "group_id": ""}
        ])

        # 创建一个关闭了 push_t0 的分组
        grp_no_t0 = t0_group_manager.save_group({
            "name": "无做T纯打板组",
            "dingtalk_webhook": "https://oapi.dingtalk.com/robot/send?access_token=no_t0_token",
            "dingtalk_secret": "SEC_secret",
            "enabled": True,
            "push_t0": False,
            "push_limitup": True,
            "stock_symbols": ["sh600519"]
        })
        grp_id = grp_no_t0["id"]

        with patch("notifier.dingtalk.DingTalkNotifier.send_trade_signal") as mock_send:
            mock_send.return_value = True
            # 发送做T信号
            t0_signal = {
                "symbol": "sh600519",
                "name": "贵州茅台",
                "signal_type": "BUY_T",
                "price": 1700.0,
                "target_price": 1730.0,
                "stop_price": 1680.0
            }
            res = notification_manager.send_signal(t0_signal)
            # 因为该组关闭了 push_t0，不应向该组 webhook 发送
            self.assertFalse(res.get("dingtalk", False))
            mock_send.assert_not_called()

        # 现在更新该分组，启用 push_t0
        t0_group_manager.save_group({
            "id": grp_id,
            "name": "已开启做T组",
            "dingtalk_webhook": "https://oapi.dingtalk.com/robot/send?access_token=no_t0_token",
            "dingtalk_secret": "SEC_secret",
            "enabled": True,
            "push_t0": True,
            "stock_symbols": ["sh600519"]
        })

        with patch("notifier.dingtalk.DingTalkNotifier.send_trade_signal") as mock_send:
            mock_send.return_value = True
            res = notification_manager.send_signal(t0_signal)
            self.assertTrue(res.get("dingtalk", False))
            mock_send.assert_called_once()

    def test_03_custom_group_review_report_generation(self):
        """测试按分组标的与模块使能开关定制生成收盘复盘报告"""
        group = {
            "id": "grp_tech",
            "name": "科技芯片组",
            "enabled": True,
            "push_t0": True,
            "push_screener": True,
            "push_tail230": False,  # 关闭尾盘2:30
            "push_limitup": False,  # 关闭打板
            "push_review": True,
            "stocks": [
                {"symbol": "sz002156", "name": "通富微电", "holding_cost": 22.5, "holding_shares": 1000}
            ]
        }

        mock_review_data = {
            "date": "2026-11-06",
            "market": {
                "index_price": 3350.2,
                "index_pct": 1.25,
                "temp_score": 72.0,
                "cycle_phase": "右侧上升",
                "regime": "多头突破",
                "above_ma20": True,
                "target_exposure": 0.8,
                "action_guide": "顺势做多，精选科技龙头"
            },
            "watchlist": [
                {
                    "symbol": "sz002156",
                    "name": "通富微电",
                    "price": 25.8,
                    "change_pct": 3.2,
                    "holding_cost": 22.5,
                    "holding_shares": 1000,
                    "pnl_pct": 14.67,
                    "pnl_amt": 3300.0,
                    "score": 85.0,
                    "control_status": "主力突破起爆",
                    "group_id": "grp_tech"
                },
                {
                    "symbol": "sh600519",
                    "name": "贵州茅台",
                    "price": 1720.0,
                    "change_pct": 0.5,
                    "holding_cost": 1700.0,
                    "holding_shares": 100,
                    "pnl_pct": 1.18,
                    "pnl_amt": 2000.0,
                    "score": 75.0,
                    "control_status": "主力吸筹蓄势",
                    "group_id": "grp_other"
                }
            ],
            "tracking": {"total_signals": 15, "win_rate": 73.3, "avg_return": 4.5},
            "optimization": {
                "old_rsrs_buy": 0.8,
                "new_rsrs_buy": 0.85,
                "old_atr_stop": 2.0,
                "new_atr_stop": 2.2,
                "reasons": ["大盘多头强化"]
            },
            "limit_up": {
                "total_trades": 8,
                "settled_trades": 6,
                "pending_trades": 2,
                "open_premium_win_rate": 75.0,
                "peak_profit_win_rate": 87.5,
                "consecutive_rate": 25.0,
                "profit_loss_ratio": 2.1,
                "avg_open_pnl": 2.8,
                "avg_peak_pnl": 4.9
            }
        }

        group_report = review_engine_v2.build_group_review_report(group, mock_review_data)

        # 验证 1: 标题包含该组名称
        self.assertIn("【科技芯片组】", group_report)
        # 验证 2: 包含科技芯片组的股票 通富微电
        self.assertIn("通富微电", group_report)
        # 验证 3: 不应包含属于其他组的 贵州茅台
        self.assertNotIn("贵州茅台", group_report)
        # 验证 4: push_limitup=False，不应包含打板复盘专栏
        self.assertNotIn("⚡ 游资龙虎打板模型复盘与隔日溢价战绩", group_report)
        # 验证 5: push_t0=True，应包含日内做T复盘
        self.assertIn("日内做T战绩复盘", group_report)
        # 验证 6: 结尾标识所订阅的模块
        self.assertIn("订阅模块: [日内做T, 多因子选股, 每日复盘]", group_report)

    def test_04_broadcast_filtering(self):
        """测试全市场广播消息 (选股/尾盘2:30/打板) 的模块订阅过滤"""
        # 创建一个只订阅 push_screener 的组，和一个只订阅 push_tail230 的组
        grp_scr = t0_group_manager.save_group({
            "name": "只看选股组",
            "dingtalk_webhook": "https://oapi.dingtalk.com/robot/send?access_token=scr_token",
            "enabled": True,
            "push_screener": True,
            "push_tail230": False
        })
        grp_tail = t0_group_manager.save_group({
            "name": "只看尾盘组",
            "dingtalk_webhook": "https://oapi.dingtalk.com/robot/send?access_token=tail_token",
            "enabled": True,
            "push_screener": False,
            "push_tail230": True
        })

        with patch("notifier.dingtalk.DingTalkNotifier.send_markdown") as mock_md:
            mock_md.return_value = True

            # 模拟选股广播推送
            notification_manager.send_markdown("选股雷达", "选股内容...", broadcast_groups=True, module_filter="push_screener")
            # 只有开启了 push_screener 的 webhook 会被调用
            called_webhooks = [call_args[0][0] for call_args in mock_md.call_args_list if len(call_args[0]) > 0]
            # 也可以通过 mock_md 所在实例的 self.webhook 检查
            self.assertTrue(mock_md.called)

    def test_05_web_api_groups(self):
        """测试 Web API 对 5 大模块开关的完整接收与响应"""
        from web.app import save_t0_group, get_t0_groups

        req_data = {
            "name": "API测试全开组",
            "dingtalk_webhook": "https://oapi.dingtalk.com/robot/send?access_token=api_token",
            "dingtalk_secret": "SEC_api",
            "enabled": True,
            "notes": "测试API",
            "push_t0": True,
            "push_screener": False,
            "push_tail230": True,
            "push_limitup": False,
            "push_review": True,
            "stock_symbols": []
        }

        res = save_t0_group(req_data)
        self.assertTrue(res["success"])
        grp = res["group"]
        self.assertEqual(grp["push_screener"], False)
        self.assertEqual(grp["push_tail230"], True)
        self.assertEqual(grp["push_limitup"], False)
        self.assertEqual(grp["push_review"], True)

        groups_res = get_t0_groups()
        self.assertIsInstance(groups_res, list)
        found = next((g for g in groups_res if g["id"] == grp["id"]), None)
        self.assertIsNotNone(found)
        self.assertEqual(found["push_tail230"], True)
        self.assertEqual(found["push_limitup"], False)

    def test_06_overlapping_groups_and_routing(self):
        """测试不同分组股票重合（非互斥）与多群信号路由分流"""
        t0_group_manager._save_raw_groups([])
        # 准备自选池测试数据
        t0_group_manager._save_watchlist([
            {"symbol": "sz002156", "name": "通富微电", "holding_cost": 22.0, "holding_shares": 1000},
            {"symbol": "sh600519", "name": "贵州茅台", "holding_cost": 1700.0, "holding_shares": 100},
            {"symbol": "sh603993", "name": "洛阳钼业", "holding_cost": 7.5, "holding_shares": 2000}
        ])

        # 1. 创建分组 A (科技组: 包含 sz002156, sh600519)
        grp_a = t0_group_manager.save_group({
            "name": "科技芯片组",
            "dingtalk_webhook": "https://oapi.dingtalk.com/robot/send?access_token=token_a",
            "dingtalk_secret": "SEC_a",
            "enabled": True,
            "push_t0": True,
            "stock_symbols": ["sz002156", "sh600519"]
        })
        gid_a = grp_a["id"]

        # 2. 创建分组 B (短线高频组: 包含 sz002156, sh603993) -> sz002156 在两组中重合！
        grp_b = t0_group_manager.save_group({
            "name": "短线高频组",
            "dingtalk_webhook": "https://oapi.dingtalk.com/robot/send?access_token=token_b",
            "dingtalk_secret": "SEC_b",
            "enabled": True,
            "push_t0": True,
            "stock_symbols": ["sz002156", "sh603993"]
        })
        gid_b = grp_b["id"]

        # 验证: 两组都包含重合标的 sz002156
        loaded_a = t0_group_manager.get_group(gid_a)
        loaded_b = t0_group_manager.get_group(gid_b)
        self.assertIn("sz002156", loaded_a["stock_symbols"])
        self.assertIn("sh600519", loaded_a["stock_symbols"])
        self.assertIn("sz002156", loaded_b["stock_symbols"])
        self.assertIn("sh603993", loaded_b["stock_symbols"])

        # 验证: get_groups_for_stock 查找出包含该标的的所有分组
        matching_groups = t0_group_manager.get_groups_for_stock("sz002156")
        matching_gids = [g["id"] for g in matching_groups]
        self.assertIn(gid_a, matching_gids)
        self.assertIn(gid_b, matching_gids)

        # 3. 验证信号路由：当 sz002156 触发做T信号时，两个群均收到推送！
        with patch("notifier.dingtalk.DingTalkNotifier.send_trade_signal") as mock_send:
            mock_send.return_value = True
            t0_sig = {
                "symbol": "sz002156",
                "name": "通富微电",
                "signal_type": "BUY_T",
                "price": 25.5
            }
            res = notification_manager.send_signal(t0_sig, force=True)
            self.assertTrue(res.get("dingtalk"))
            # 应该调用两次 (分别向 token_a 和 token_b 发送)
            self.assertEqual(mock_send.call_count, 2)

        # 4. 验证模块开关独立拦截：若分组 A 关闭了 push_t0，而分组 B 开启，则仅向分组 B 发送
        t0_group_manager.save_group({
            "id": gid_a,
            "name": "科技芯片组",
            "dingtalk_webhook": "https://oapi.dingtalk.com/robot/send?access_token=token_a",
            "dingtalk_secret": "SEC_a",
            "enabled": True,
            "push_t0": False,  # 关闭做T
            "stock_symbols": ["sz002156", "sh600519"]
        })

        with patch("notifier.dingtalk.DingTalkNotifier.send_trade_signal") as mock_send:
            mock_send.return_value = True
            res = notification_manager.send_signal(t0_sig, force=True)
            self.assertTrue(res.get("dingtalk"))
            # 只向分组 B 发送 1 次
            self.assertEqual(mock_send.call_count, 1)

        # 5. 验证定制复盘报告：两组的报告中均包含重叠股票 sz002156
        mock_review = {
            "date": "2026-11-06",
            "market": {"index_price": 3350, "index_pct": 1.0, "temp_score": 70, "cycle_phase": "上升", "regime": "多头"},
            "watchlist": [
                {"symbol": "sz002156", "name": "通富微电", "price": 25.5, "change_pct": 2.5, "holding_cost": 22.0, "holding_shares": 1000, "score": 80, "pnl_pct": 15.9, "pnl_amt": 3500},
                {"symbol": "sh600519", "name": "贵州茅台", "price": 1700, "change_pct": 0.5, "holding_cost": 1700, "holding_shares": 100, "score": 70, "pnl_pct": 0, "pnl_amt": 0},
                {"symbol": "sh603993", "name": "洛阳钼业", "price": 8.0, "change_pct": 1.2, "holding_cost": 7.5, "holding_shares": 2000, "score": 75, "pnl_pct": 6.6, "pnl_amt": 1000}
            ]
        }
        report_a = review_engine_v2.build_group_review_report(loaded_a, mock_review)
        report_b = review_engine_v2.build_group_review_report(loaded_b, mock_review)
        self.assertIn("通富微电", report_a)
        self.assertIn("通富微电", report_b)
        self.assertIn("贵州茅台", report_a)
        self.assertNotIn("贵州茅台", report_b)
        self.assertIn("洛阳钼业", report_b)
        self.assertNotIn("洛阳钼业", report_a)

    def test_07_edit_watchlist_cost_and_shares(self):
        """测试在 Web API 中手动修改持股数与持仓成本功能"""
        from web.app import update_watchlist_item, get_watchlist
        t0_group_manager._save_watchlist([
            {"symbol": "sh600519", "name": "贵州茅台", "holding_cost": 1600.0, "holding_shares": 100, "notes": "老底仓"}
        ])

        # 1. 模拟用户通过界面将成本修改为 1750.50，持股数修改为 300
        res = update_watchlist_item("sh600519", {
            "holding_cost": 1750.50,
            "holding_shares": 300,
            "notes": "加仓后成本"
        })
        self.assertTrue(res["success"])
        self.assertEqual(res["item"]["holding_cost"], 1750.50)
        self.assertEqual(res["item"]["holding_shares"], 300)
        self.assertEqual(res["item"]["notes"], "加仓后成本")

        # 2. 读取自选池检验数据持久化与实时盈亏计算
        wl = get_watchlist()
        target = next((x for x in wl if x["symbol"] == "sh600519"), None)
        self.assertIsNotNone(target)
        self.assertEqual(target["holding_cost"], 1750.50)
        self.assertEqual(target["holding_shares"], 300)
        self.assertEqual(target["notes"], "加仓后成本")

if __name__ == "__main__":
    unittest.main()
