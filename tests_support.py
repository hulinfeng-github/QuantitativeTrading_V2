"""
单元测试存储隔离与沙箱环境初始化 (必须在导入 config / core / notifier / web / scheduler 之前导入)
确保运行任意测试文件永远不会污染生产 `storage/` 目录或触发 .env 真实通知渠道。
"""
import os
import atexit
import shutil
import tempfile
from pathlib import Path

_ISOLATED_TMP_DIR: str = ""


def _cleanup_tmp_dir(tmp_path: str):
    try:
        import logging
        logging.shutdown()
        for h_logger_name in ("qt2",):
            lg = logging.getLogger(h_logger_name)
            for h in list(lg.handlers):
                try:
                    h.close()
                except Exception:
                    pass
                lg.removeHandler(h)
    except Exception:
        pass
    shutil.rmtree(tmp_path, ignore_errors=True)


def ensure_isolated_test_env() -> Path:
    global _ISOLATED_TMP_DIR
    project_root = Path(__file__).resolve().parent
    prod_storage = (project_root / "storage").resolve()
    cur_env = os.environ.get("QUANT_DATA_DIR", "").strip()

    if not cur_env or Path(cur_env).resolve() == prod_storage:
        tmp = tempfile.mkdtemp(prefix="qt_test_storage_")
        os.environ["QUANT_DATA_DIR"] = tmp
        _ISOLATED_TMP_DIR = tmp
        atexit.register(lambda p=tmp: _cleanup_tmp_dir(p))

    # 阻止 config._load_env_file() 把生产 .env 里的真实机器人密钥注入测试进程
    for env_key in ("DINGTALK_WEBHOOK", "DINGTALK_SECRET", "WECHAT_TOKEN", "WECHAT_PROVIDER"):
        os.environ.setdefault(env_key, "")

    return Path(os.environ["QUANT_DATA_DIR"]).resolve()


TEST_DATA_DIR = ensure_isolated_test_env()
