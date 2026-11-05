"""
通知管理调度中心 2.0 (NotificationManager 2.0)
统一处理钉钉与微信多渠道推送、做T分组多钉钉群路由分发、业务模块独立使能开关、每日去重防刷与信号历史持久化
"""
import json
import time
from datetime import datetime
from typing import Dict, Any, List, Optional
from config import load_config, SIGNALS_FILE
from notifier.dingtalk import DingTalkNotifier
from notifier.wechat import WeChatNotifier

class NotificationManager:
    def __init__(self):
        self._sent_cache = set()
        self._load_sent_cache()

    def _load_sent_cache(self):
        """系统启动或重载时从 signals_history.json 预加载今日已发推送去重缓存，防止服务重启后重复刷屏"""
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            if SIGNALS_FILE.exists():
                with open(SIGNALS_FILE, "r", encoding="utf-8") as f:
                    history = json.load(f)
                    if isinstance(history, list):
                        for sig in history:
                            if isinstance(sig, dict) and sig.get("date") == today:
                                sym = sig.get("symbol", "")
                                st = sig.get("signal_type", "")
                                if sym and st:
                                    self._sent_cache.add(f"{sym}_{st}_{today}")
        except Exception:
            pass

    def _get_signal_module_key(self, sig_type: str) -> str:
        """根据信号类别映射对应的分组业务模块配置开关"""
        if sig_type in ["BUY_T", "SELL_T", "CLOSE_T", "STOP_T", "EOD_FORCE_CLOSE_T", "EOD_HEDGE_ALERT_T"]:
            return "push_t0"
        elif sig_type in ["BUY", "SELL", "FORCE_EXIT"]:
            return "push_screener"
        elif sig_type in ["TAIL_PROFIT_EXIT", "TAIL_STOP_EXIT", "TAIL_TIMED_EXIT", "TAIL_EOD_EXIT"]:
            return "push_tail230"
        elif sig_type == "LIMIT_UP":
            return "push_limitup"
        return "push_screener"

    def send_signal(self, signal: Dict[str, Any], force: bool = False) -> Dict[str, bool]:
        symbol = signal.get("symbol", "")
        sig_type = signal.get("signal_type", "")
        today = datetime.now().strftime("%Y-%m-%d")
        cache_key = f"{symbol}_{sig_type}_{today}"

        if not force and cache_key in self._sent_cache:
            return {"dingtalk": False, "wechat": False, "skipped": True}

        cfg = load_config()
        results = {"dingtalk": False, "wechat": False, "skipped": False}
        module_key = self._get_signal_module_key(sig_type)

        from core.t0_group_manager import t0_group_manager

        # 特殊处理：LIMIT_UP (游资打板信号) 属于全市场异动机会，向所有启用了 push_limitup 的活跃分组分发
        if sig_type == "LIMIT_UP":
            subscribed_groups = t0_group_manager.get_groups_subscribed_to("push_limitup")
            dt_sent = False
            sent_webhooks = set()
            for grp in subscribed_groups:
                hook = (grp.get("dingtalk_webhook") or "").strip()
                sec = (grp.get("dingtalk_secret") or "").strip()
                if hook and hook not in sent_webhooks:
                    dt = DingTalkNotifier(webhook=hook, secret=sec)
                    sig_copy = signal.copy()
                    sig_copy["group_name"] = grp.get("name", "")
                    if dt.send_trade_signal(sig_copy):
                        dt_sent = True
                    sent_webhooks.add(hook)
            # 若全局钉钉机器人开启且未发送，发送之
            if cfg.get("dingtalk_enabled") and cfg.get("dingtalk_webhook"):
                global_hook = cfg.get("dingtalk_webhook").strip()
                if global_hook not in sent_webhooks:
                    dt = DingTalkNotifier(webhook=global_hook, secret=cfg.get("dingtalk_secret", ""))
                    if dt.send_trade_signal(signal):
                        dt_sent = True
            results["dingtalk"] = dt_sent

        else:
            # 针对特定股票信号 (日内做T、波段买卖点、尾盘2:30等)，精准路由至所属的所有做T分组 (支持不同分组股票重叠归属)
            target_groups = t0_group_manager.get_groups_for_stock(symbol) if symbol else []
            dt_sent = False
            sent_webhooks = set()

            if target_groups:
                if not signal.get("group_name"):
                    signal["group_name"] = target_groups[0].get("name", "")
                for grp in target_groups:
                    if not grp.get("enabled", True):
                        continue
                    if not grp.get(module_key, True):
                        continue
                    hook = (grp.get("dingtalk_webhook") or "").strip()
                    sec = (grp.get("dingtalk_secret") or "").strip()
                    if not hook and cfg.get("dingtalk_enabled") and cfg.get("dingtalk_webhook"):
                        hook = cfg.get("dingtalk_webhook").strip()
                        sec = cfg.get("dingtalk_secret", "").strip()

                    if hook and hook not in sent_webhooks:
                        dt = DingTalkNotifier(webhook=hook, secret=sec)
                        sig_copy = signal.copy()
                        sig_copy["group_name"] = grp.get("name", "")
                        if dt.send_trade_signal(sig_copy):
                            dt_sent = True
                        sent_webhooks.add(hook)
                results["dingtalk"] = dt_sent
            else:
                # 若无所属分组，平滑回退到全局默认钉钉机器人
                if cfg.get("dingtalk_enabled") and cfg.get("dingtalk_webhook"):
                    target_webhook = cfg.get("dingtalk_webhook").strip()
                    target_secret = cfg.get("dingtalk_secret", "").strip()
                    dt = DingTalkNotifier(webhook=target_webhook, secret=target_secret)
                    results["dingtalk"] = dt.send_trade_signal(signal)

        # 微信推送 (保持原有通道逻辑)
        if cfg.get("wechat_enabled") and cfg.get("wechat_token"):
            wx = WeChatNotifier(provider=cfg.get("wechat_provider", "pushplus"), token=cfg.get("wechat_token"))
            results["wechat"] = wx.send_trade_signal(signal)

        if results["dingtalk"] or results["wechat"]:
            self._sent_cache.add(cache_key)

        # 记录到信号历史文件
        if sig_type and sig_type not in ["HOLD", "HOLDING", "HOLD_T", "HOLDING_T"]:
            self._record_signal_history(signal)

        return results

    def send_markdown(
        self,
        title: str,
        text: str,
        group_id: Optional[str] = None,
        broadcast_groups: bool = False,
        module_filter: Optional[str] = None
    ) -> Dict[str, bool]:
        """
        发送 Markdown 消息
        - group_id: 指定特定做T分组的钉钉群
        - broadcast_groups: 是否向所有已开启的做T分组钉钉群广播
        - module_filter: 模块订阅过滤开关 (如 'push_screener', 'push_tail230', 'push_limitup', 'push_review')
        """
        cfg = load_config()
        results = {"dingtalk": False, "wechat": False}
        from core.t0_group_manager import t0_group_manager

        if broadcast_groups:
            groups = t0_group_manager.get_groups(include_stocks=False)
            dt_sent = False
            sent_hooks = set()
            for grp in groups:
                if grp.get("enabled", True):
                    if module_filter and not grp.get(module_filter, True):
                        continue
                    hook = (grp.get("dingtalk_webhook") or "").strip()
                    sec = (grp.get("dingtalk_secret") or "").strip()
                    if hook and hook not in sent_hooks:
                        dt = DingTalkNotifier(webhook=hook, secret=sec)
                        if dt.send_markdown(title, text):
                            dt_sent = True
                        sent_hooks.add(hook)
            # 全局默认机器人
            if cfg.get("dingtalk_enabled") and cfg.get("dingtalk_webhook"):
                hook = cfg.get("dingtalk_webhook").strip()
                if hook not in sent_hooks:
                    dt = DingTalkNotifier(webhook=hook, secret=cfg.get("dingtalk_secret", ""))
                    if dt.send_markdown(title, text):
                        dt_sent = True
            results["dingtalk"] = dt_sent
        elif group_id:
            grp = t0_group_manager.get_group(group_id)
            if grp and grp.get("enabled", True):
                if not module_filter or grp.get(module_filter, True):
                    hook = (grp.get("dingtalk_webhook") or "").strip()
                    sec = (grp.get("dingtalk_secret") or "").strip()
                    if hook:
                        dt = DingTalkNotifier(webhook=hook, secret=sec)
                        results["dingtalk"] = dt.send_markdown(title, text)
        else:
            if cfg.get("dingtalk_enabled") and cfg.get("dingtalk_webhook"):
                dt = DingTalkNotifier(webhook=cfg.get("dingtalk_webhook"), secret=cfg.get("dingtalk_secret", ""))
                results["dingtalk"] = dt.send_markdown(title, text)

        if cfg.get("wechat_enabled") and cfg.get("wechat_token"):
            wx = WeChatNotifier(provider=cfg.get("wechat_provider", "pushplus"), token=cfg.get("wechat_token"))
            results["wechat"] = wx.send_markdown(title, text)
        return results

    def _record_signal_history(self, signal: Dict[str, Any]):
        try:
            history = []
            if SIGNALS_FILE.exists():
                with open(SIGNALS_FILE, "r", encoding="utf-8") as f:
                    history = json.load(f)
            entry = signal.copy()
            entry["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            history.insert(0, entry)
            history = history[:100]  # 保留最新100条
            with open(SIGNALS_FILE, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

notification_manager = NotificationManager()
