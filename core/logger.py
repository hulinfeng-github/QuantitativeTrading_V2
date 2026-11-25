"""
A股智能量化系统 2.0 - 轻量级分级轮转日志引擎 (Logger 2.0)
输出至终端并按 5MB × 5 份自动轮转落盘到 DATA_DIR/logs/quant.log，
便于后台静默运行、断点重启后精确追溯问题。
"""
import logging
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config import DATA_DIR

_LOG_LOCK = threading.RLock()
_CONFIGURED_ROOT = False


def _ensure_root_configured() -> logging.Logger:
    global _CONFIGURED_ROOT
    root_logger = logging.getLogger("qt2")
    with _LOG_LOCK:
        if _CONFIGURED_ROOT and root_logger.handlers:
            return root_logger

        root_logger.setLevel(logging.INFO)
        root_logger.propagate = False
        fmt = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        # 控制台输出
        has_stream = any(isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler) for h in root_logger.handlers)
        if not has_stream:
            sh = logging.StreamHandler()
            sh.setLevel(logging.INFO)
            sh.setFormatter(fmt)
            root_logger.addHandler(sh)

        # 文件轮转日志 (自动隔离到当前 DATA_DIR/logs)
        has_file = any(isinstance(h, RotatingFileHandler) for h in root_logger.handlers)
        if not has_file:
            try:
                log_dir = Path(DATA_DIR) / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)
                fh = RotatingFileHandler(
                    filename=str(log_dir / "quant.log"),
                    maxBytes=5 * 1024 * 1024,
                    backupCount=5,
                    encoding="utf-8",
                )
                fh.setLevel(logging.INFO)
                fh.setFormatter(fmt)
                root_logger.addHandler(fh)
            except OSError:
                # 无磁盘权限时仍保留控制台日志输出
                pass

        _CONFIGURED_ROOT = True
        return root_logger


def get_logger(module_name: str = "qt2") -> logging.Logger:
    _ensure_root_configured()
    short = str(module_name or "qt2").strip()
    if short == "qt2":
        return logging.getLogger("qt2")
    return logging.getLogger(f"qt2.{short}")


def shutdown_logging():
    """关闭文件句柄（用于测试结束释放临时目录锁或服务优雅退出）。"""
    global _CONFIGURED_ROOT
    with _LOG_LOCK:
        root_logger = logging.getLogger("qt2")
        for h in list(root_logger.handlers):
            try:
                h.flush()
                h.close()
            except Exception:
                pass
            root_logger.removeHandler(h)
        _CONFIGURED_ROOT = False
