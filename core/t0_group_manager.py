"""
A股智能量化系统 2.0 - 做T标的分组与多钉钉群分流推送管理引擎 (T0GroupManager 2.0)
功能：
1. 做T标的多维分组维护 (支持创建、编辑、启停、删除分组)；
2. 每个分组独立绑定钉钉群机器人 Webhook 与加签 Secret；
3. 每个分组支持独立的模块推送使能配置 (日内做T、多因子选股、尾盘2:30战法、游资打板、每日定制复盘)；
4. 自选股与持仓标的的分组归属、支持不同分组包含重合股票 (多对多关联与精准多群分流)；
5. 分组消息独立测试推送与连通性自检。
"""
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Set

from config import T0_GROUPS_FILE, WATCHLIST_FILE, load_config
from notifier.dingtalk import DingTalkNotifier

DEFAULT_MODULE_SWITCHES = {
    "push_t0": True,         # 日内做T高抛低吸、做T平仓、尾盘强平
    "push_screener": True,   # 多因子波段买卖点与盘后 15:30 智能选股雷达
    "push_tail230": True,    # 尾盘 2:30 突袭战法抢筹与次日早盘操盘
    "push_limitup": True,    # 游资龙虎榜打板量化与操盘指南
    "push_review": True      # 每日收盘全景复盘总结 (定制本组标的报告)
}

class T0GroupManager:
    def __init__(self, storage_file: Optional[Path] = None, watchlist_file: Optional[Path] = None):
        self.storage_file = storage_file or T0_GROUPS_FILE
        self.watchlist_file = watchlist_file or WATCHLIST_FILE
        self._ensure_initialized()

    def _ensure_initialized(self):
        """若分组配置文件不存在，则自动初始化（若存在全局钉钉配置则预填默认分组）"""
        if not self.storage_file.exists():
            cfg = load_config()
            global_webhook = cfg.get("dingtalk_webhook", "").strip()
            global_secret = cfg.get("dingtalk_secret", "").strip()
            
            initial_groups = [
                {
                    "id": "group_default",
                    "name": "默认做T组",
                    "dingtalk_webhook": global_webhook,
                    "dingtalk_secret": global_secret,
                    "enabled": True,
                    "notes": "系统预设做T分组 (关联全局默认钉钉机器人)",
                    "push_t0": True,
                    "push_screener": True,
                    "push_tail230": True,
                    "push_limitup": True,
                    "push_review": True,
                    "stock_symbols": [],
                    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                }
            ]
            try:
                self.storage_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self.storage_file, "w", encoding="utf-8") as f:
                    json.dump(initial_groups, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"[T0GroupManager] 初始化分组文件失败: {e}")

    def _read_raw_groups(self) -> List[Dict[str, Any]]:
        if not self.storage_file.exists():
            self._ensure_initialized()
        try:
            with open(self.storage_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    # 确保旧数据平滑升级，拥有所有模块开关与股票列表默认值
                    for g in data:
                        for k, v in DEFAULT_MODULE_SWITCHES.items():
                            if k not in g:
                                g[k] = v
                        if "stock_symbols" not in g or not isinstance(g["stock_symbols"], list):
                            g["stock_symbols"] = []
                    return data
        except Exception as e:
            print(f"[T0GroupManager] 读取分组配置异常: {e}")
        return []

    def _save_raw_groups(self, groups: List[Dict[str, Any]]) -> bool:
        try:
            self.storage_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.storage_file, "w", encoding="utf-8") as f:
                json.dump(groups, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"[T0GroupManager] 保存分组配置失败: {e}")
            return False

    def _read_watchlist(self) -> List[Dict[str, Any]]:
        if not self.watchlist_file.exists():
            return []
        try:
            with open(self.watchlist_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception:
            pass
        return []

    def _save_watchlist(self, watchlist: List[Dict[str, Any]]) -> bool:
        try:
            self.watchlist_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.watchlist_file, "w", encoding="utf-8") as f:
                json.dump(watchlist, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"[T0GroupManager] 保存自选池失败: {e}")
            return False

    def get_groups(self, include_stocks: bool = True) -> List[Dict[str, Any]]:
        """获取所有分组详情，包含各组关联的股票信息与统计（支持不同分组包含重合股票）"""
        groups = self._read_raw_groups()
        if not include_stocks:
            return groups

        watchlist = self._read_watchlist()
        watchlist_by_symbol = {
            item.get("symbol", "").strip().lower(): item 
            for item in watchlist if item.get("symbol")
        }

        results = []
        for grp in groups:
            gid = grp.get("id", "")
            grp_copy = grp.copy()
            for k, v in DEFAULT_MODULE_SWITCHES.items():
                if k not in grp_copy:
                    grp_copy[k] = v

            # 汇总属于该组的股票代码 (不互斥，支持多组重叠归属)
            assigned_symbols: Set[str] = set()

            # 1. 优先从分组自身维护的 stock_symbols 读取
            if "stock_symbols" in grp and isinstance(grp["stock_symbols"], list):
                for s in grp["stock_symbols"]:
                    if s:
                        assigned_symbols.add(s.strip().lower())

            # 2. 从 watchlist 的 group_ids 或 group_id 读取
            for item in watchlist:
                sym = item.get("symbol", "").strip().lower()
                gids = item.get("group_ids")
                gid_single = item.get("group_id") or ""
                
                if gid == "group_default":
                    # 默认组作为兜底：仅当股票未归属任何其他自定义分组且默认组未指定特定标的时展示
                    has_custom = False
                    if isinstance(gids, list):
                        has_custom = any(g and g != "group_default" for g in gids)
                    elif gid_single and gid_single != "group_default":
                        has_custom = True
                    if not has_custom and not grp.get("stock_symbols"):
                        assigned_symbols.add(sym)
                else:
                    if isinstance(gids, list) and gid in gids:
                        assigned_symbols.add(sym)
                    elif gid_single == gid:
                        assigned_symbols.add(sym)

            assigned_stocks = []
            for sym in assigned_symbols:
                item = watchlist_by_symbol.get(sym)
                if item:
                    assigned_stocks.append({
                        "symbol": item.get("symbol", ""),
                        "code": item.get("code", ""),
                        "name": item.get("name", ""),
                        "holding_cost": item.get("holding_cost", 0.0),
                        "holding_shares": item.get("holding_shares", 0),
                        "t0_enabled": item.get("t0_enabled", True)
                    })
                else:
                    assigned_stocks.append({
                        "symbol": sym,
                        "code": sym[2:] if len(sym) > 2 else sym,
                        "name": sym,
                        "holding_cost": 0.0,
                        "holding_shares": 0,
                        "t0_enabled": True
                    })

            assigned_stocks.sort(key=lambda x: x.get("symbol", ""))
            grp_copy["stock_symbols"] = [s.get("symbol") for s in assigned_stocks]
            grp_copy["stock_count"] = len(assigned_stocks)
            grp_copy["stocks"] = assigned_stocks
            results.append(grp_copy)

        return results

    def get_group(self, group_id: str) -> Optional[Dict[str, Any]]:
        """获取单个分组"""
        groups = self.get_groups(include_stocks=True)
        for grp in groups:
            if grp.get("id") == group_id:
                return grp
        return None

    def save_group(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        创建或更新做T分组 (支持不同分组股票重叠归属)
        支持入参: id, name, dingtalk_webhook, dingtalk_secret, enabled, notes,
        push_t0, push_screener, push_tail230, push_limitup, push_review,
        stock_symbols (关联股票代码列表)
        """
        groups = self._read_raw_groups()
        gid = data.get("id", "").strip()
        name = data.get("name", "").strip() or "未命名做T组"
        webhook = data.get("dingtalk_webhook", "").strip()
        secret = data.get("dingtalk_secret", "").strip()
        enabled = bool(data.get("enabled", True))
        notes = data.get("notes", "").strip()

        # 各业务模块独立推送使能开关
        push_t0 = bool(data.get("push_t0", True))
        push_screener = bool(data.get("push_screener", True))
        push_tail230 = bool(data.get("push_tail230", True))
        push_limitup = bool(data.get("push_limitup", True))
        push_review = bool(data.get("push_review", True))

        stock_symbols = data.get("stock_symbols")

        is_new = False
        target_group = None

        if gid:
            for grp in groups:
                if grp.get("id") == gid:
                    target_group = grp
                    break

        if not target_group:
            is_new = True
            gid = gid or f"grp_{int(time.time() * 1000)}"
            target_group = {
                "id": gid,
                "name": name,
                "dingtalk_webhook": webhook,
                "dingtalk_secret": secret,
                "enabled": enabled,
                "notes": notes,
                "push_t0": push_t0,
                "push_screener": push_screener,
                "push_tail230": push_tail230,
                "push_limitup": push_limitup,
                "push_review": push_review,
                "stock_symbols": [],
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            groups.append(target_group)
        else:
            target_group["name"] = name
            target_group["dingtalk_webhook"] = webhook
            target_group["dingtalk_secret"] = secret
            target_group["enabled"] = enabled
            target_group["notes"] = notes
            target_group["push_t0"] = push_t0
            target_group["push_screener"] = push_screener
            target_group["push_tail230"] = push_tail230
            target_group["push_limitup"] = push_limitup
            target_group["push_review"] = push_review
            target_group["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 核心改动：支持分组股票非互斥重叠！
        if stock_symbols is not None and isinstance(stock_symbols, list):
            clean_syms = list(dict.fromkeys([s.strip().lower() for s in stock_symbols if s and s.strip()]))
            target_group["stock_symbols"] = clean_syms

            # 同步更新 watchlist.json 中的 group_ids
            watchlist = self._read_watchlist()
            sym_set = set(clean_syms)
            changed = False
            for item in watchlist:
                sym = item.get("symbol", "").strip().lower()
                gids = item.get("group_ids")
                if not isinstance(gids, list):
                    gids = [item.get("group_id")] if item.get("group_id") else []

                if sym in sym_set:
                    # 分配给自定义组时，从默认组脱离并加入该组
                    if gid != "group_default":
                        gids = [g for g in gids if g != "group_default"]
                    if gid not in gids:
                        gids.append(gid)
                        changed = True
                else:
                    if gid in gids:
                        gids = [g for g in gids if g != gid]
                        changed = True

                item["group_ids"] = gids
                item["group_id"] = gids[0] if gids else ""

            if changed:
                self._save_watchlist(watchlist)

        self._save_raw_groups(groups)

        target_group["stock_count"] = len(target_group.get("stock_symbols", []))
        return target_group

    def delete_group(self, group_id: str) -> bool:
        """删除分组，并从各股票所属的 group_ids 集合中移除该分组（不影响股票在其他组的归属）"""
        groups = self._read_raw_groups()
        filtered = [g for g in groups if g.get("id") != group_id]
        if len(filtered) == len(groups):
            return False

        self._save_raw_groups(filtered)

        watchlist = self._read_watchlist()
        changed = False
        for item in watchlist:
            gids = item.get("group_ids")
            if isinstance(gids, list) and group_id in gids:
                item["group_ids"] = [g for g in gids if g != group_id]
                item["group_id"] = item["group_ids"][0] if item["group_ids"] else ""
                changed = True
            elif item.get("group_id") == group_id:
                item["group_id"] = ""
                item["group_ids"] = []
                changed = True
        if changed:
            self._save_watchlist(watchlist)

        return True

    def get_groups_for_stock(self, symbol: str) -> List[Dict[str, Any]]:
        """
        根据股票代码查找其归属的所有做T分组（支持重叠归属于多个自定义分组）
        """
        if not symbol:
            return []

        sym_norm = symbol.strip().lower()
        groups = self._read_raw_groups()
        groups_map = {g.get("id"): g for g in groups}

        matched_gids: Set[str] = set()

        # 1. 检查各个自定义分组自带的 stock_symbols (优先排除 group_default)
        for grp in groups:
            gid = grp.get("id")
            if gid == "group_default":
                continue
            syms = [s.strip().lower() for s in grp.get("stock_symbols", []) if isinstance(s, str)]
            if sym_norm in syms:
                matched_gids.add(gid)

        # 2. 检查 watchlist 中的归属
        watchlist = self._read_watchlist()
        for item in watchlist:
            s = item.get("symbol", "").strip().lower()
            c = item.get("code", "").strip().lower()
            if s == sym_norm or c == sym_norm:
                gids = item.get("group_ids")
                if isinstance(gids, list):
                    for g in gids:
                        if g and g != "group_default" and g in groups_map:
                            matched_gids.add(g)
                gid_single = item.get("group_id")
                if gid_single and gid_single != "group_default" and gid_single in groups_map:
                    matched_gids.add(gid_single)
                break

        results = []
        for gid in matched_gids:
            if gid in groups_map:
                res = groups_map[gid].copy()
                for k, v in DEFAULT_MODULE_SWITCHES.items():
                    if k not in res:
                        res[k] = v
                results.append(res)

        # 若未匹配到任何自定义分组，且存在默认组，则兜底回退到默认组
        if not results and "group_default" in groups_map:
            res = groups_map["group_default"].copy()
            for k, v in DEFAULT_MODULE_SWITCHES.items():
                if k not in res:
                    res[k] = v
            results.append(res)

        return results

    def get_group_for_stock(self, symbol: str) -> Optional[Dict[str, Any]]:
        """根据股票代码精确查找其归属的做T分组配置（保留兼容，返回首个匹配组）"""
        groups = self.get_groups_for_stock(symbol)
        return groups[0] if groups else None

    def get_groups_subscribed_to(self, module_key: str) -> List[Dict[str, Any]]:
        """获取启用了特定推送模块且配置了有效 Webhook 的所有活跃分组"""
        groups = self.get_groups(include_stocks=True)
        return [
            g for g in groups
            if g.get("enabled", True) and g.get(module_key, True) and (g.get("dingtalk_webhook") or "").strip()
        ]

    def set_stock_group(self, symbol: str, group_id: str) -> bool:
        """为单只股票指定做T分组"""
        return self.batch_set_stock_group([symbol], group_id)

    def batch_set_stock_group(self, symbols: List[str], group_id: str) -> bool:
        """批量为多只股票分配做T分组 (将 group_id 加入归属集合)"""
        if not symbols:
            return False
        sym_set = {s.strip().lower() for s in symbols}
        watchlist = self._read_watchlist()
        changed = False
        for item in watchlist:
            s = item.get("symbol", "").strip().lower()
            c = item.get("code", "").strip().lower()
            if s in sym_set or c in sym_set:
                gids = item.get("group_ids")
                if not isinstance(gids, list):
                    gids = [item.get("group_id")] if item.get("group_id") else []
                if group_id and group_id != "group_default":
                    gids = [g for g in gids if g != "group_default"]
                    if group_id not in gids:
                        gids.append(group_id)
                        changed = True
                elif not group_id or group_id == "group_default":
                    gids = []
                    changed = True
                item["group_ids"] = gids
                item["group_id"] = gids[0] if gids else ""

        if changed:
            return self._save_watchlist(watchlist)
        return True

    def test_group_notification(
        self,
        group_id: Optional[str] = None,
        webhook: Optional[str] = None,
        secret: Optional[str] = None
    ) -> Dict[str, Any]:
        """向指定做T分组的钉钉机器人发送测试通知卡片，验证 Webhook 与加签配置"""
        target_name = "做T分组测试通道"
        grp = None
        if group_id:
            grp = self.get_group(group_id)
            if not grp:
                return {"success": False, "message": f"未找到分组 ID: {group_id}"}
            webhook = webhook or grp.get("dingtalk_webhook", "")
            secret = secret or grp.get("dingtalk_secret", "")
            target_name = grp.get("name", target_name)

        if not webhook:
            return {"success": False, "message": "未配置钉钉 Webhook 地址，无法发送测试"}

        dt = DingTalkNotifier(webhook=webhook, secret=secret or "")
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 订阅模块清单
        sub_list = []
        if grp:
            if grp.get("push_t0", True): sub_list.append("🎯日内做T")
            if grp.get("push_screener", True): sub_list.append("💎多因子选股")
            if grp.get("push_tail230", True): sub_list.append("⚡尾盘2:30")
            if grp.get("push_limitup", True): sub_list.append("🔥游资打板")
            if grp.get("push_review", True): sub_list.append("📊定制复盘")
        sub_str = " · ".join(sub_list) if sub_list else "全模块已订阅"

        title = f"🔔 【做T分组推送连通性测试】{target_name}"
        text = (
            f"### 🔔 【量化2.0 · 做T分组测试】{target_name}\n"
            f"- **测试状态**: **通道连通正常 (HTTP 200)**\n"
            f"- **所属分组**: **{target_name}**\n"
            f"- **订阅模块**: **{sub_str}**\n"
            f"- **加签验证**: {'已启用加签防伪 (HMAC-SHA256)' if secret else '无加签配置'}\n"
            f"- **路由机制**: 该分组下已勾选的推送消息将精准独占推送到本钉钉群，未勾选的模块已自动拦截！\n"
            f"---\n"
            f"> QuantitativeTrading 2.0 · 分组推送测试 · {now_str}"
        )
        ok = dt.send_markdown(title=title, text=text)
        return {
            "success": ok,
            "group_name": target_name,
            "message": "测试消息已成功发送至钉钉群！" if ok else "钉钉推送失败，请检查 Webhook 与 Secret 是否正确有效"
        }

t0_group_manager = T0GroupManager()
