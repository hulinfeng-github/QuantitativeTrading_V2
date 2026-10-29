"""
通知管理调度中心 2.0 (NotificationManager 2.0)
统一处理钉钉与微信多渠道推送、每日去重防刷与信号历史持久化
"""
import json
import time
from datetime import datetime
from typing import Dict, Any, List
from config import load_config, SIGNALS_FILE
from notifier.dingtalk import DingTalkNotifier
from notifier.wechat import WeChatNotifier

class NotificationManager:
    def __init__(self):
        self._sent_cache = set()

    def send_signal(self, signal: Dict[str, Any], force: bool = False) -> Dict[str, bool]:
        symbol = signal.get("symbol", "")
        sig_type = signal.get("signal_type", "")
        today = datetime.now().strftime("%Y-%m-%d")
        cache_key = f"{symbol}_{sig_type}_{today}"

        if not force and cache_key in self._sent_cache:
            return {"dingtalk": False, "wechat": False, "skipped": True}

        cfg = load_config()
        results = {"dingtalk": False, "wechat": False, "skipped": False}

        # 钉钉推送
        if cfg.get("dingtalk_enabled") and cfg.get("dingtalk_webhook"):
            dt = DingTalkNotifier(webhook=cfg.get("dingtalk_webhook"), secret=cfg.get("dingtalk_secret", ""))
            results["dingtalk"] = dt.send_trade_signal(signal)

        # 微信推送
        if cfg.get("wechat_enabled") and cfg.get("wechat_token"):
            wx = WeChatNotifier(provider=cfg.get("wechat_provider", "pushplus"), token=cfg.get("wechat_token"))
            results["wechat"] = wx.send_trade_signal(signal)

        if results["dingtalk"] or results["wechat"]:
            self._sent_cache.add(cache_key)

        # 记录到信号历史文件
        if sig_type and sig_type not in ["HOLD", "HOLDING", "HOLD_T", "HOLDING_T"]:
            self._record_signal_history(signal)

        return results

    def send_markdown(self, title: str, text: str) -> Dict[str, bool]:
        cfg = load_config()
        results = {"dingtalk": False, "wechat": False}
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
