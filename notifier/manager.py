"""
通知管理调度中心 2.0 (NotificationManager 2.0)
统一处理钉钉与微信多渠道推送、做T分组多钉钉群路由分发、业务模块独立使能开关、每日去重防刷与信号历史持久化
"""
import hashlib
import threading
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
from config import DATA_DIR, load_config, SIGNALS_FILE, PUSH_DEDUP_FILE
from core.logger import get_logger
from core.persistence import atomic_write_json, safe_read_json
from notifier.dingtalk import DingTalkNotifier, dingtalk_rate_limiter
from notifier.wechat import WeChatNotifier

logger = get_logger("notifier")

CRITICAL_ALERT_TYPES = {"FORCE_EXIT", "STOP_T", "EOD_FORCE_CLOSE_T", "TAIL_STOP_EXIT"}
PENDING_ALERTS_FILE = DATA_DIR / "pending_critical_alerts.json"
_DEFAULT_PENDING_ALERTS_FILE = PENDING_ALERTS_FILE

class NotificationManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._sent_cache = set()
        self._sent_by_date: Dict[str, List[str]] = {}
        self._load_sent_cache()

    def _load_sent_cache(self):
        """系统启动或重载时从 push_dedup_cache.json 与 signals_history.json 恢复今日已发推送去重缓存，彻底防止重启重复刷屏"""
        today = datetime.now().strftime("%Y-%m-%d")
        cutoff = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
        with self._lock:
            # 1. 从持久化去重账本恢复（包含 Markdown 摘要与全量信号 Key）
            persisted = safe_read_json(PUSH_DEDUP_FILE, {})
            if isinstance(persisted, dict):
                cleaned: Dict[str, List[str]] = {}
                for d, keys in persisted.items():
                    if str(d) >= cutoff and isinstance(keys, list):
                        cleaned[str(d)] = [str(k) for k in keys]
                        if str(d) == today:
                            self._sent_cache.update(cleaned[str(d)])
                self._sent_by_date = cleaned

            # 2. 兼容兜底：从 signals_history.json 补全今日历史信号 Key
            history = safe_read_json(SIGNALS_FILE, [])
            if isinstance(history, list):
                for sig in history:
                    if isinstance(sig, dict) and sig.get("date") == today:
                        sym = sig.get("symbol", "")
                        st = sig.get("signal_type", "")
                        if not (sym and st):
                            continue
                        ts_raw = str(sig.get("timestamp") or "")
                        try:
                            when = datetime.strptime(ts_raw, "%Y-%m-%d %H:%M:%S")
                        except ValueError:
                            when = datetime.now()
                        k = self._dedup_key(sym, st, when)
                        self._sent_cache.add(k)
                        day_keys = self._sent_by_date.setdefault(today, [])
                        if k not in day_keys:
                            day_keys.append(k)

    def _mark_dedup_sent(self, cache_key: str, day_str: Optional[str] = None):
        today = day_str or datetime.now().strftime("%Y-%m-%d")
        cutoff = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
        with self._lock:
            self._sent_cache.add(cache_key)
            self._sent_by_date = {d: ks for d, ks in self._sent_by_date.items() if d >= cutoff}
            day_keys = self._sent_by_date.setdefault(today, [])
            if cache_key not in day_keys:
                day_keys.append(cache_key)
                atomic_write_json(PUSH_DEDUP_FILE, self._sent_by_date)

    @staticmethod
    def _dedup_key(symbol: str, sig_type: str, when: Optional[datetime] = None) -> str:
        when = when or datetime.now()
        day = when.strftime("%Y-%m-%d")
        if sig_type in ("BUY_T", "SELL_T", "CLOSE_T", "STOP_T"):
            bucket = (when.minute // 30) * 30
            return f"{symbol}_{sig_type}_{day}_{when.strftime('%H')}{bucket:02d}"
        return f"{symbol}_{sig_type}_{day}"

    @staticmethod
    def _target_dedup_key(base_key: str, target_id: str) -> str:
        digest = hashlib.md5(str(target_id).encode("utf-8")).hexdigest()[:10]
        return f"{base_key}@{digest}"

    @staticmethod
    def _get_pending_alerts_file() -> Path:
        if PENDING_ALERTS_FILE != _DEFAULT_PENDING_ALERTS_FILE:
            return PENDING_ALERTS_FILE
        return PUSH_DEDUP_FILE.parent / "pending_critical_alerts.json"

    def _resolve_dingtalk_targets(
        self,
        symbol: str,
        sig_type: str,
        cfg: Dict[str, Any],
        signal: Optional[Dict[str, Any]] = None
    ) -> List[Tuple[str, str, str]]:
        """解析信号应投递的去重钉钉目标列表: [(webhook, secret, group_name), ...]"""
        from core.t0_group_manager import t0_group_manager
        module_key = self._get_signal_module_key(sig_type)
        targets: List[Tuple[str, str, str]] = []
        seen_hooks = set()

        if sig_type == "LIMIT_UP":
            subscribed = t0_group_manager.get_groups_subscribed_to("push_limitup")
            for grp in subscribed:
                hook = (grp.get("dingtalk_webhook") or "").strip()
                sec = (grp.get("dingtalk_secret") or "").strip()
                if hook and hook not in seen_hooks:
                    targets.append((hook, sec, grp.get("name", "")))
                    seen_hooks.add(hook)
            if cfg.get("dingtalk_enabled") and cfg.get("dingtalk_webhook"):
                ghook = cfg.get("dingtalk_webhook").strip()
                if ghook and ghook not in seen_hooks:
                    targets.append((ghook, cfg.get("dingtalk_secret", "").strip(), "全局默认"))
                    seen_hooks.add(ghook)
            return targets

        target_groups = t0_group_manager.get_groups_for_stock(symbol) if symbol else []
        if target_groups:
            if signal is not None and not signal.get("group_name"):
                signal["group_name"] = target_groups[0].get("name", "")
            for grp in target_groups:
                if not grp.get("enabled", True) or not grp.get(module_key, True):
                    continue
                hook = (grp.get("dingtalk_webhook") or "").strip()
                sec = (grp.get("dingtalk_secret") or "").strip()
                if not hook and cfg.get("dingtalk_enabled") and cfg.get("dingtalk_webhook"):
                    hook = cfg.get("dingtalk_webhook").strip()
                    sec = cfg.get("dingtalk_secret", "").strip()
                if hook and hook not in seen_hooks:
                    targets.append((hook, sec, grp.get("name", "")))
                    seen_hooks.add(hook)
        elif cfg.get("dingtalk_enabled") and cfg.get("dingtalk_webhook"):
            ghook = cfg.get("dingtalk_webhook").strip()
            if ghook:
                targets.append((ghook, cfg.get("dingtalk_secret", "").strip(), ""))
        return targets

    def _enqueue_critical_alert(
        self,
        signal: Dict[str, Any],
        failed_dt_targets: List[Tuple[str, str, str]],
        failed_wechat: bool,
        day_str: str,
        base_key: Optional[str] = None
    ):
        """将频控或网络导致发送失败的强平/止损关键警报写入持久化延迟队列。"""
        sig_type = signal.get("signal_type", "")
        if sig_type not in CRITICAL_ALERT_TYPES:
            return
        if not failed_dt_targets and not failed_wechat:
            return

        symbol = str(signal.get("symbol", ""))
        pending_file = self._get_pending_alerts_file()
        dt_entries = [[h, s, g] for (h, s, g) in failed_dt_targets]
        resolved_base_key = base_key or self._dedup_key(symbol, sig_type, datetime.now())

        with self._lock:
            queue = safe_read_json(pending_file, [])
            if not isinstance(queue, list):
                queue = []
            queue = [q for q in queue if isinstance(q, dict) and q.get("expire_date") == day_str]

            updated = False
            for item in queue:
                if item.get("symbol") == symbol and item.get("signal_type") == sig_type and item.get("expire_date") == day_str:
                    existing_hooks = {t[0] for t in item.get("failed_dt_targets", []) if isinstance(t, list) and t}
                    merged_dt = list(item.get("failed_dt_targets", []))
                    for entry in dt_entries:
                        if entry[0] not in existing_hooks:
                            merged_dt.append(entry)
                            existing_hooks.add(entry[0])
                    item["failed_dt_targets"] = merged_dt
                    item["failed_wechat"] = bool(item.get("failed_wechat") or failed_wechat)
                    item["dedup_base_key"] = item.get("dedup_base_key") or resolved_base_key
                    item["signal"] = signal.copy()
                    updated = True
                    break

            if not updated:
                queue.append({
                    "symbol": symbol,
                    "signal_type": sig_type,
                    "expire_date": day_str,
                    "dedup_base_key": resolved_base_key,
                    "queued_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "attempts": 0,
                    "max_attempts": 12,
                    "failed_dt_targets": dt_entries,
                    "failed_wechat": bool(failed_wechat),
                    "signal": signal.copy()
                })

            atomic_write_json(pending_file, queue)
            logger.warning(f"🚨 关键风控警报已加入延迟补发队列: {symbol} ({sig_type}), 待补发钉钉目标={len(dt_entries)}, 微信={failed_wechat}")

    def flush_pending_critical_alerts(self) -> int:
        """在冷却/熔断解除后自动补发持久化的强平/止损关键警报。返回本次完全/部分成功送达的警报条数。"""
        pending_file = self._get_pending_alerts_file()
        today = datetime.now().strftime("%Y-%m-%d")

        with self._lock:
            queue = safe_read_json(pending_file, [])
            if not isinstance(queue, list) or not queue:
                return 0
            cfg = load_config()
            remaining: List[Dict[str, Any]] = []
            delivered_count = 0
            dirty = False

            for item in queue:
                if not isinstance(item, dict):
                    dirty = True
                    continue
                if item.get("expire_date") != today:
                    dirty = True
                    continue
                attempts = int(item.get("attempts", 0) or 0)
                max_attempts = int(item.get("max_attempts", 12) or 12)
                if attempts >= max_attempts:
                    dirty = True
                    logger.error(f"关键风控警报补发达到最大次数({max_attempts})已放弃: {item.get('symbol')} ({item.get('signal_type')})")
                    continue

                sig = item.get("signal") or {}
                symbol = sig.get("symbol", item.get("symbol", ""))
                sig_type = sig.get("signal_type", item.get("signal_type", ""))
                base_key = str(item.get("dedup_base_key") or "") or self._dedup_key(symbol, sig_type, datetime.now())

                new_failed_dt = []
                tried_network = False
                delivered_this_item = False

                for t in item.get("failed_dt_targets", []):
                    if not isinstance(t, (list, tuple)) or not t:
                        continue
                    hook = str(t[0]).strip()
                    sec = str(t[1]).strip() if len(t) > 1 else ""
                    gname = str(t[2]) if len(t) > 2 else ""
                    tkey = self._target_dedup_key(base_key, f"dt:{hook}")
                    if tkey in self._sent_cache:
                        delivered_this_item = True
                        dirty = True
                        continue

                    in_cd, _ = dingtalk_rate_limiter.is_in_cooldown(hook)
                    if in_cd:
                        new_failed_dt.append([hook, sec, gname])
                        continue

                    tried_network = True
                    dt = DingTalkNotifier(webhook=hook, secret=sec)
                    sig_copy = sig.copy()
                    if gname and gname != "全局默认":
                        sig_copy["group_name"] = gname
                    if dt.send_trade_signal(sig_copy):
                        self._mark_dedup_sent(tkey, today)
                        delivered_this_item = True
                        dirty = True
                    else:
                        new_failed_dt.append([hook, sec, gname])

                still_failed_wx = False
                if item.get("failed_wechat"):
                    wx_tkey = self._target_dedup_key(base_key, "wechat")
                    if wx_tkey in self._sent_cache:
                        delivered_this_item = True
                        dirty = True
                    elif cfg.get("wechat_enabled") and cfg.get("wechat_token"):
                        tried_network = True
                        wx = WeChatNotifier(provider=cfg.get("wechat_provider", "pushplus"), token=cfg.get("wechat_token"))
                        if wx.send_trade_signal(sig):
                            self._mark_dedup_sent(wx_tkey, today)
                            delivered_this_item = True
                            dirty = True
                        else:
                            still_failed_wx = True

                if delivered_this_item:
                    delivered_count += 1

                if new_failed_dt or still_failed_wx:
                    if tried_network:
                        item["attempts"] = attempts + 1
                        dirty = True
                    item["failed_dt_targets"] = new_failed_dt
                    item["failed_wechat"] = still_failed_wx
                    remaining.append(item)
                else:
                    self._mark_dedup_sent(base_key, today)
                    dirty = True

            if dirty or len(remaining) != len(queue):
                atomic_write_json(pending_file, remaining)
            if delivered_count > 0:
                logger.info(f"关键风控警报补发完成: 成功送达 {delivered_count} 项, 队列剩余 {len(remaining)} 项")
            return delivered_count

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
        cache_key = self._dedup_key(symbol, sig_type, datetime.now())

        if not force:
            with self._lock:
                if cache_key in self._sent_cache:
                    return {"dingtalk": False, "wechat": False, "skipped": True}

        cfg = load_config()
        results = {"dingtalk": False, "wechat": False, "skipped": False}

        dt_targets = self._resolve_dingtalk_targets(symbol, sig_type, cfg, signal=signal)
        dt_sent = False
        failed_dt_targets: List[Tuple[str, str, str]] = []
        succeeded_tkeys: List[str] = []
        attempted_targets = 0

        for hook, sec, gname in dt_targets:
            tkey = self._target_dedup_key(cache_key, f"dt:{hook}")
            if not force:
                with self._lock:
                    if tkey in self._sent_cache:
                        continue
            attempted_targets += 1
            dt = DingTalkNotifier(webhook=hook, secret=sec)
            sig_copy = signal.copy()
            if gname and gname != "全局默认":
                sig_copy["group_name"] = gname
            elif sig_type == "LIMIT_UP" and gname == "全局默认":
                pass
            if dt.send_trade_signal(sig_copy):
                dt_sent = True
                succeeded_tkeys.append(tkey)
            else:
                failed_dt_targets.append((hook, sec, gname))
        results["dingtalk"] = dt_sent

        # 微信推送
        failed_wechat = False
        if cfg.get("wechat_enabled") and cfg.get("wechat_token"):
            wx_tkey = self._target_dedup_key(cache_key, "wechat")
            wx_already = False
            if not force:
                with self._lock:
                    wx_already = wx_tkey in self._sent_cache
            if not wx_already:
                attempted_targets += 1
                wx = WeChatNotifier(provider=cfg.get("wechat_provider", "pushplus"), token=cfg.get("wechat_token"))
                if wx.send_trade_signal(signal):
                    results["wechat"] = True
                    succeeded_tkeys.append(wx_tkey)
                else:
                    failed_wechat = True

        # 如果所有待发目标都已被目标级去重跳过（且无新失败目标）
        if not force and attempted_targets == 0 and (dt_targets or (cfg.get("wechat_enabled") and cfg.get("wechat_token"))):
            return {"dingtalk": False, "wechat": False, "skipped": True}

        is_test = bool(signal.get("is_test"))
        if not is_test:
            for tk in succeeded_tkeys:
                self._mark_dedup_sent(tk, today)
            # 仅当全部配置目标均未失败时才标记全局去重键；局部失败允许下轮仅重试未送达目标
            if (results["dingtalk"] or results["wechat"]) and not failed_dt_targets and not failed_wechat:
                self._mark_dedup_sent(cache_key, today)

        # 强平/止损关键警报若发送失败则进入持久化延迟队列
        if not is_test and sig_type in CRITICAL_ALERT_TYPES and (failed_dt_targets or failed_wechat):
            self._enqueue_critical_alert(signal, failed_dt_targets, failed_wechat, today, base_key=cache_key)

        # 记录到信号历史文件 (同一去重窗口不重复写台账；测试信号绝不污染实盘历史)
        hist_key = f"hist:{cache_key}"
        if not is_test and sig_type and sig_type not in ["HOLD", "HOLDING", "HOLD_T", "HOLDING_T"]:
            should_record = True
            if not force:
                with self._lock:
                    should_record = hist_key not in self._sent_cache
            if should_record:
                self._record_signal_history(signal)
                self._mark_dedup_sent(hist_key, today)

        return results

    def send_signals_batch(self, signals: List[Dict[str, Any]], force: bool = False) -> Dict[str, Any]:
        """
        批量发送一组交易信号
        - 自动按目标 Webhook 分组聚合为合并卡片发送
        - 大幅缩减外发请求频次，从根源规避钉钉 20条/分 频控限制与群消息刷屏
        """
        if not signals:
            return {"total": 0, "sent": 0, "skipped": 0, "dingtalk": False, "wechat": False}

        cfg = load_config()
        today = datetime.now().strftime("%Y-%m-%d")

        # 1. 过滤已完全发送的去重缓存
        filtered_signals = []
        skipped_count = 0
        now_dt = datetime.now()
        with self._lock:
            for s in signals:
                sym = s.get("symbol", "")
                st = s.get("signal_type", "")
                ck = self._dedup_key(sym, st, now_dt)
                if not force and ck in self._sent_cache:
                    skipped_count += 1
                    continue
                filtered_signals.append(s)

        if not filtered_signals:
            return {"total": len(signals), "sent": 0, "skipped": skipped_count, "dingtalk": False, "wechat": False}

        # 2. 按目标钉钉 Webhook 归集信号: (webhook, secret, group_name) -> list of (idx, signal)
        dt_targets: Dict[Any, List[Tuple[int, Dict[str, Any]]]] = {}
        sig_base_keys = [self._dedup_key(s.get("symbol", ""), s.get("signal_type", ""), now_dt) for s in filtered_signals]
        sig_failed_dt: Dict[int, List[Tuple[str, str, str]]] = {i: [] for i in range(len(filtered_signals))}
        sig_failed_wx: Dict[int, bool] = {i: False for i in range(len(filtered_signals))}
        sig_any_sent: Dict[int, bool] = {i: False for i in range(len(filtered_signals))}

        for idx, sig in enumerate(filtered_signals):
            sym = sig.get("symbol", "")
            sig_type = sig.get("signal_type", "")
            resolved = self._resolve_dingtalk_targets(sym, sig_type, cfg, signal=sig)
            for hook, sec, gname in resolved:
                tkey = self._target_dedup_key(sig_base_keys[idx], f"dt:{hook}")
                if not force:
                    with self._lock:
                        if tkey in self._sent_cache:
                            continue
                label = gname or "全局默认"
                dt_targets.setdefault((hook, sec, label), []).append((idx, sig))

        # 3. 逐个 Webhook 进行聚合卡片或单卡片推送 (经由滑动时间窗口平滑限流保护)
        dt_sent = False
        for (hook, sec, gname), idx_sigs in dt_targets.items():
            dt = DingTalkNotifier(webhook=hook, secret=sec)
            sig_list = [s for _, s in idx_sigs]
            if len(sig_list) == 1:
                sig_copy = sig_list[0].copy()
                if gname and gname != "全局默认":
                    sig_copy["group_name"] = gname
                ok = dt.send_trade_signal(sig_copy)
            else:
                ok = dt.send_batch_signals(sig_list, group_name=gname if gname != "全局默认" else "")

            if ok:
                dt_sent = True
                for idx, _ in idx_sigs:
                    sig_any_sent[idx] = True
                    self._mark_dedup_sent(self._target_dedup_key(sig_base_keys[idx], f"dt:{hook}"), today)
            else:
                for idx, _ in idx_sigs:
                    sig_failed_dt[idx].append((hook, sec, gname))

        # 4. 微信渠道推送
        wx_sent = False
        if cfg.get("wechat_enabled") and cfg.get("wechat_token"):
            wx = WeChatNotifier(provider=cfg.get("wechat_provider", "pushplus"), token=cfg.get("wechat_token"))
            for idx, s in enumerate(filtered_signals):
                wx_tkey = self._target_dedup_key(sig_base_keys[idx], "wechat")
                if not force:
                    with self._lock:
                        if wx_tkey in self._sent_cache:
                            continue
                if wx.send_trade_signal(s):
                    wx_sent = True
                    sig_any_sent[idx] = True
                    self._mark_dedup_sent(wx_tkey, today)
                else:
                    sig_failed_wx[idx] = True

        # 5. 更新全局去重缓存、关键警报延迟队列与历史持久化
        for idx, s in enumerate(filtered_signals):
            st = s.get("signal_type", "")
            base_key = sig_base_keys[idx]
            if sig_any_sent[idx] and not sig_failed_dt[idx] and not sig_failed_wx[idx]:
                self._mark_dedup_sent(base_key, today)
            if st in CRITICAL_ALERT_TYPES and (sig_failed_dt[idx] or sig_failed_wx[idx]):
                self._enqueue_critical_alert(s, sig_failed_dt[idx], sig_failed_wx[idx], today, base_key=base_key)
            if st and st not in ["HOLD", "HOLDING", "HOLD_T", "HOLDING_T"]:
                hist_key = f"hist:{base_key}"
                should_rec = True
                if not force:
                    with self._lock:
                        should_rec = hist_key not in self._sent_cache
                if should_rec:
                    self._record_signal_history(s)
                    self._mark_dedup_sent(hist_key, today)

        return {
            "total": len(signals),
            "sent": len(filtered_signals),
            "skipped": skipped_count,
            "dingtalk": dt_sent,
            "wechat": wx_sent
        }

    def send_markdown(
        self,
        title: str,
        text: str,
        group_id: Optional[str] = None,
        broadcast_groups: bool = False,
        module_filter: Optional[str] = None,
        dedup_key: Optional[str] = None,
        force: bool = False
    ) -> Dict[str, Any]:
        """
        发送 Markdown 消息
        - group_id: 指定特定做T分组的钉钉群
        - broadcast_groups: 是否向所有已开启的做T分组钉钉群广播
        - module_filter: 模块订阅过滤开关 (如 'push_screener', 'push_tail230', 'push_limitup', 'push_review')
        - dedup_key: 跨重启幂等去重 Key（如 'tail230_scan:2026-11-25'）
        """
        if dedup_key and not force:
            with self._lock:
                if dedup_key in self._sent_cache:
                    return {"dingtalk": False, "wechat": False, "skipped": True}

        cfg = load_config()
        results = {"dingtalk": False, "wechat": False, "skipped": False}
        from core.t0_group_manager import t0_group_manager

        if broadcast_groups:
            groups = t0_group_manager.get_groups(include_stocks=False)
            dt_sent = False
            dt_any_failed = False
            sent_hooks = set()
            for grp in groups:
                if grp.get("enabled", True):
                    if module_filter and not grp.get(module_filter, True):
                        continue
                    hook = (grp.get("dingtalk_webhook") or "").strip()
                    sec = (grp.get("dingtalk_secret") or "").strip()
                    if hook and hook not in sent_hooks:
                        sent_hooks.add(hook)
                        tkey = self._target_dedup_key(dedup_key, f"dt:{hook}") if dedup_key else None
                        if tkey and not force:
                            with self._lock:
                                if tkey in self._sent_cache:
                                    continue
                        dt = DingTalkNotifier(webhook=hook, secret=sec)
                        if dt.send_markdown(title, text):
                            dt_sent = True
                            if tkey:
                                self._mark_dedup_sent(tkey)
                        else:
                            dt_any_failed = True
            # 全局默认机器人
            if cfg.get("dingtalk_enabled") and cfg.get("dingtalk_webhook"):
                hook = cfg.get("dingtalk_webhook").strip()
                if hook not in sent_hooks:
                    sent_hooks.add(hook)
                    tkey = self._target_dedup_key(dedup_key, f"dt:{hook}") if dedup_key else None
                    already = False
                    if tkey and not force:
                        with self._lock:
                            already = tkey in self._sent_cache
                    if not already:
                        dt = DingTalkNotifier(webhook=hook, secret=cfg.get("dingtalk_secret", ""))
                        if dt.send_markdown(title, text):
                            dt_sent = True
                            if tkey:
                                self._mark_dedup_sent(tkey)
                        else:
                            dt_any_failed = True
            results["dingtalk"] = dt_sent
        elif group_id:
            dt_any_failed = False
            grp = t0_group_manager.get_group(group_id)
            if grp and grp.get("enabled", True):
                if not module_filter or grp.get(module_filter, True):
                    hook = (grp.get("dingtalk_webhook") or "").strip()
                    sec = (grp.get("dingtalk_secret") or "").strip()
                    if hook:
                        dt = DingTalkNotifier(webhook=hook, secret=sec)
                        results["dingtalk"] = dt.send_markdown(title, text)
                        if not results["dingtalk"]:
                            dt_any_failed = True
        else:
            dt_any_failed = False
            if cfg.get("dingtalk_enabled") and cfg.get("dingtalk_webhook"):
                dt = DingTalkNotifier(webhook=cfg.get("dingtalk_webhook"), secret=cfg.get("dingtalk_secret", ""))
                results["dingtalk"] = dt.send_markdown(title, text)
                if not results["dingtalk"]:
                    dt_any_failed = True

        wx_failed = False
        if cfg.get("wechat_enabled") and cfg.get("wechat_token"):
            wx_tkey = self._target_dedup_key(dedup_key, "wechat") if dedup_key else None
            already_wx = False
            if wx_tkey and not force:
                with self._lock:
                    already_wx = wx_tkey in self._sent_cache
            if not already_wx:
                wx = WeChatNotifier(provider=cfg.get("wechat_provider", "pushplus"), token=cfg.get("wechat_token"))
                results["wechat"] = wx.send_markdown(title, text)
                if results["wechat"] and wx_tkey:
                    self._mark_dedup_sent(wx_tkey)
                elif not results["wechat"]:
                    wx_failed = True
        if dedup_key and (results["dingtalk"] or results["wechat"]) and not dt_any_failed and not wx_failed:
            self._mark_dedup_sent(dedup_key)
        return results

    def get_dingtalk_status(self) -> Dict[str, Any]:
        """获取系统配置的所有钉钉机器人 Webhook 的实时频控状态与配额数据"""
        from notifier.dingtalk import dingtalk_rate_limiter
        from core.t0_group_manager import t0_group_manager
        cfg = load_config()

        status_list = []
        seen_keys = set()

        # 全局机器人
        if cfg.get("dingtalk_webhook"):
            hook = cfg.get("dingtalk_webhook").strip()
            st = dingtalk_rate_limiter.get_status(hook)
            st["name"] = "全局默认机器人"
            st["enabled"] = bool(cfg.get("dingtalk_enabled", False))
            status_list.append(st)
            seen_keys.add(st["key"])

        # 各分组机器人
        groups = t0_group_manager.get_groups(include_stocks=False)
        for grp in groups:
            hook = (grp.get("dingtalk_webhook") or "").strip()
            if hook:
                st = dingtalk_rate_limiter.get_status(hook)
                if st["key"] not in seen_keys:
                    st["name"] = f"分组: {grp.get('name', '未命名')}"
                    st["enabled"] = bool(grp.get("enabled", True))
                    status_list.append(st)
                    seen_keys.add(st["key"])

        return {
            "status": "ok",
            "max_requests_per_minute": dingtalk_rate_limiter.MAX_REQUESTS_PER_MINUTE,
            "window_seconds": dingtalk_rate_limiter.WINDOW_SECONDS,
            "min_interval_seconds": dingtalk_rate_limiter.MIN_INTERVAL_SECONDS,
            "cooldown_on_error_seconds": dingtalk_rate_limiter.COOLDOWN_ON_ERROR,
            "webhooks": status_list
        }

    def _record_signal_history(self, signal: Dict[str, Any]):
        try:
            sym = str(signal.get("symbol", "")).strip().lower()
            # 过滤单元测试产生的虚构标的或测试标识，杜绝污染历史实盘胜率统计
            if not sym or "test" in sym or sym == "sz002999" or sym.startswith("sz99") or sym.startswith("sh99"):
                return
            if signal.get("is_test"):
                return

            with self._lock:
                history = safe_read_json(SIGNALS_FILE, [])
                if not isinstance(history, list):
                    history = []
                entry = signal.copy()
                entry["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                history.insert(0, entry)
                history = history[:150]  # 保留最新150条
                atomic_write_json(SIGNALS_FILE, history)
        except Exception as e:
            logger.warning(f"记录信号历史失败: {e}")

notification_manager = NotificationManager()
