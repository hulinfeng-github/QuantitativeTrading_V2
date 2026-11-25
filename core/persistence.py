"""
A股智能量化系统 2.0 - 原子持久化与断点状态存储引擎 (Persistence 2.0)
确保系统在随时启停、调试中断、代码热更新、断电重启等场景下：
1. 杜绝半截写入导致 JSON/CSV 损坏（基于同目录唯一临时文件 + flush/fsync + os.replace 原子替换）；
2. 维护调度器运行水位与任务幂等完成账本（scheduler_state.json），支持重启断档自检与防重复执行。
"""
import os
import json
import logging
import threading
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional

_PERSIST_LOGGER = logging.getLogger("qt2.persistence")


_FILE_IO_LOCK = threading.RLock()
WATCHLIST_LOCK = threading.RLock()


def load_watchlist(path: Path) -> List[Dict[str, Any]]:
    """线程安全读取自选股列表。"""
    with WATCHLIST_LOCK:
        data = safe_read_json(path, [])
        return data if isinstance(data, list) else []


def save_watchlist(path: Path, items: List[Dict[str, Any]]) -> bool:
    """线程安全且原子地写入自选股列表。"""
    with WATCHLIST_LOCK:
        return atomic_write_json(path, items)


def mutate_watchlist(path: Path, mutator) -> Any:
    """在互斥锁内执行 read-modify-write，杜绝 Web 与分组管理器并发覆写丢失更新。"""
    with WATCHLIST_LOCK:
        data = safe_read_json(path, [])
        items: List[Dict[str, Any]] = data if isinstance(data, list) else []
        changed, result = mutator(items)
        if changed:
            ok = atomic_write_json(path, items)
            if not ok:
                raise IOError(f"原子写入自选文件失败: {path}")
        return result


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> bool:
    """
    原子写入文本文件：先写同目录临时文件并刷盘，再通过 os.replace 原子覆盖目标文件。
    """
    if path is None:
        return False
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    tmp_path = target.with_name(tmp_name)
    with _FILE_IO_LOCK:
        try:
            with open(tmp_path, "w", encoding=encoding, newline="" if encoding == "utf-8-sig" else None) as f:
                f.write(content)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(tmp_path, target)
            return True
        except Exception as e:
            _PERSIST_LOGGER.warning(f"原子写入失败 ({target}): {e}")
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            return False


def atomic_write_json(path: Path, data: Any, indent: int = 2) -> bool:
    """
    原子写入 JSON 文件：先完整序列化为字符串（防止中途序列化异常破坏原文件），再原子落盘。
    """
    try:
        payload = json.dumps(data, ensure_ascii=False, indent=indent)
    except Exception as e:
        _PERSIST_LOGGER.warning(f"JSON 序列化失败 ({path}): {e}")
        return False
    return atomic_write_text(path, payload, encoding="utf-8")


def safe_read_json(path: Path, default: Any = None) -> Any:
    """
    安全读取 JSON 文件，异常时返回 default 的浅拷贝/默认值而不覆盖受损文件。
    """
    target = Path(path)
    if not target.exists():
        return default
    try:
        with open(target, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        _PERSIST_LOGGER.warning(f"读取 JSON 异常 ({target}): {e}")
        return default


class SchedulerCheckpointStore:
    """
    调度器运行状态与任务完成幂等水位存储（支持任意时刻启停恢复与防重复触发）。
    """

    def __init__(self, state_file: Path):
        self.state_file = Path(state_file)
        self._lock = threading.RLock()
        self._state: Dict[str, Any] = {
            "last_heartbeat": "",
            "last_trading_date": "",
            "completed_tasks": {},
            "meta": {}
        }
        self._load()

    def _load(self):
        with self._lock:
            loaded = safe_read_json(self.state_file, None)
            if isinstance(loaded, dict):
                self._state["last_heartbeat"] = str(loaded.get("last_heartbeat") or "")
                self._state["last_trading_date"] = str(loaded.get("last_trading_date") or "")
                tasks = loaded.get("completed_tasks")
                self._state["completed_tasks"] = tasks if isinstance(tasks, dict) else {}
                meta = loaded.get("meta")
                self._state["meta"] = meta if isinstance(meta, dict) else {}

    def _save(self):
        # 仅保留近 15 个交易日的任务完成标记，避免文件无限膨胀
        tasks = self._state.get("completed_tasks", {})
        if isinstance(tasks, dict) and len(tasks) > 15:
            sorted_days = sorted(tasks.keys(), reverse=True)[:15]
            self._state["completed_tasks"] = {d: tasks[d] for d in sorted_days}
        atomic_write_json(self.state_file, self._state)

    def reload(self):
        self._load()

    def record_heartbeat(self, trading_date: Optional[str] = None, now_dt: Optional[datetime] = None):
        now = now_dt or datetime.now()
        with self._lock:
            self._state["last_heartbeat"] = now.strftime("%Y-%m-%d %H:%M:%S")
            if trading_date:
                self._state["last_trading_date"] = str(trading_date)[:10]
            self._save()

    def get_last_heartbeat(self) -> Optional[datetime]:
        with self._lock:
            raw = self._state.get("last_heartbeat", "")
        if not raw:
            return None
        try:
            return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    def is_task_completed(self, task_name: str, trade_date: str) -> bool:
        d = str(trade_date)[:10]
        with self._lock:
            day_tasks = self._state.get("completed_tasks", {}).get(d, {})
            return task_name in day_tasks

    def mark_task_completed(
        self,
        task_name: str,
        trade_date: str,
        status: str = "DONE",
        note: str = "",
        now_dt: Optional[datetime] = None
    ):
        now = now_dt or datetime.now()
        d = str(trade_date)[:10]
        with self._lock:
            tasks = self._state.setdefault("completed_tasks", {})
            day_tasks = tasks.setdefault(d, {})
            day_tasks[task_name] = {
                "status": status,
                "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
                "note": note
            }
            self._state["last_heartbeat"] = now.strftime("%Y-%m-%d %H:%M:%S")
            self._state["last_trading_date"] = d
            self._save()

    def get_day_tasks(self, trade_date: str) -> Dict[str, Any]:
        d = str(trade_date)[:10]
        with self._lock:
            return dict(self._state.get("completed_tasks", {}).get(d, {}))

    def get_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._state, ensure_ascii=False))
