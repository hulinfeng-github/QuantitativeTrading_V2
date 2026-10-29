"""
A股智能量化投研与实战交易系统 2.0 全局配置中心
支持策略动态调优、多因子权重配比、风险敞口约束与多渠道推送
"""
import os
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "storage"
CONFIG_FILE = DATA_DIR / "user_config.json"
WATCHLIST_FILE = DATA_DIR / "watchlist.json"
SIGNALS_FILE = DATA_DIR / "signals_history.json"
T0_TRADES_FILE = DATA_DIR / "t0_trades.json"
TAIL_TRADES_FILE = DATA_DIR / "tail_trades_230.json"
REVIEW_FILE = DATA_DIR / "review_history.json"
OPTIMIZATION_FILE = DATA_DIR / "optimization_log.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG = {
    # ---------------- 1. 核心多因子波段策略参数 ----------------
    "rsrs_n": 18,                       # RSRS 回归统计窗口 (通常18天)
    "rsrs_m": 600,                      # RSRS Z-score标准化窗口 (通常600天)
    "rsrs_buy_threshold": 0.7,          # 修正RSRS买入阈值
    "rsrs_sell_threshold": -0.7,        # 修正RSRS卖出清仓阈值
    "rsrs_use_skew": True,              # 是否开启2.0偏度修正(过滤假脉冲假突破)
    
    # 因子权重配比 (总和 1.0)
    "weight_rsrs": 0.30,                # 修正RSRS因子权重
    "weight_alpha": 0.25,               # Qlib启发式动量与量价Alpha因子权重
    "weight_chip": 0.25,                # CYQ 2.0 高精主力筹码突破权重
    "weight_trend": 0.20,               # 多周期均线与动能配合权重
    
    # CYQ 筹码分布参数
    "chip_lookback": 120,               # 筹码回溯历史天数 (约半年)
    "chip_concentration_target": 0.12,  # 筹码集中度极限 (<=12% 为单峰密集)
    "chip_profit_ratio_min": 70.0,      # 突破时获利盘最低比例 (%)
    
    # 均线与动量周期
    "ma_short": 5,                      # 短期均线
    "ma_mid": 20,                       # 中期生命线
    "ma_long": 60,                      # 长期牛熊线
    "atr_period": 14,                   # ATR波动周期
    "atr_stop_multiplier": 2.0,         # 基础ATR硬止损倍数
    "trailing_stop_pct": 0.06,          # 最高点回撤动态止盈止损 (6%)
    
    # ---------------- 2. 选股与流动性过滤 ----------------
    "min_turnover_amount": 100000000,   # 最小日成交额 (1亿元，过滤死水冷门标的)
    "allow_main_board_only": True,      # 仅允许主板 (沪市60/深市00)
    "exclude_st": True,                 # 过滤ST/*ST/退市风险股
    "max_screener_results": 20,         # 每日精选输出上限
    
    # ---------------- 3. 组合风险与仓位配置 (Barra + 现代组合理论) ----------------
    "initial_capital": 100000.0,        # 组合模拟基准资金 (元)
    "max_portfolio_positions": 5,       # 组合最大同时持仓只数
    "max_single_position_pct": 0.25,    # 单只个股持仓上限 (25%)
    "hard_stop_loss_pct": 8.0,          # 持仓账户硬止损强制平仓线 (-8%)
    "use_volatility_parity": True,      # 是否启用波动率平价仓位配比
    "use_market_regime_adaptive": True, # 是否根据大盘牛熊自适应调整总仓位上限
    "bull_market_max_exposure": 0.95,   # 强多头大盘允许最高总仓位 (95%)
    "oscillation_max_exposure": 0.55,   # 震荡市允许最高总仓位 (55%)
    "bear_market_max_exposure": 0.20,   # 弱市/熊市防守最高总仓位 (20%)
    
    # ---------------- 4. A股 T+1 日内做T 2.0 参数 ----------------
    "t0_monitor_enabled": True,         # 开启持仓股日内做T监控
    "t0_use_dynamic_boll": True,        # 开启分时动态布林带 (%B)
    "t0_bias_buy": -1.5,                # 正T低吸分时均线负偏离阈值 (%)
    "t0_bias_sell": 2.0,                # 倒T高抛分时均线正偏离阈值 (%)
    "t0_stop_loss_pct": 1.5,            # 做T防守保护止损比例 (%)
    "t0_take_profit_pct": 1.5,          # 做T目标对冲止盈比例 (%)
    "t0_trailing_profit_pct": 0.5,      # 冲高回落动态对冲止盈回撤阈值 (%)
    "t0_max_trades_per_day": 1,         # 单只标的每日做T最大次数
    "t0_single_direction_only": True,   # 单日做T单向严格锁定
    "t0_trend_filter_enabled": True,    # 开启大趋势过滤 (顺势做T)
    "t0_eod_force_close_enabled": True, # 尾盘做T未对冲强平与底仓抉择提醒开关
    "t0_eod_check_time": "14:48",       # 尾盘做T强平核查时间 (14:45-14:55之间)
    
    # ---------------- 5. 消息通知渠道配置 ----------------
    "dingtalk_enabled": False,
    "dingtalk_webhook": "",
    "dingtalk_secret": "",
    
    "wechat_enabled": False,
    "wechat_provider": "pushplus",       # pushplus | serverchan | wecom
    "wechat_token": "",
    
    # ---------------- 6. 自动化后台调度 ----------------
    "scheduler_enabled": True,
    "scan_interval_minutes": 5,          # 盘中自选池与做T巡检间隔 (分钟)
    "market_review_time": "15:10",       # 收盘后全景复盘与参数自迭代时间
    "market_close_scan_time": "15:30",   # 盘后全市场智能精选时间
    
    # ---------------- 7. 2:30 尾盘突袭战法参数 ----------------
    "tail230_target_profit_pct": 2.5,   # 次日早盘冲高止盈目标 (%)
    "tail230_stop_loss_pct": 1.8,       # 次日防守底线止损线 (%)
    "tail230_auto_track_top_n": 3,      # 每日14:32自动纳入次日卖出监控的前N只标的
    "tail230_morning_check_enabled": True, # 开启次日 09:28 集合竞价操盘计划预警
    
    # ---------------- 8. Web 服务配置 (默认 8089 避开冲突) ----------------
    "web_port": 8089,
    "web_host": "127.0.0.1"
}

def _load_env_file():
    """轻量级解析 .env 文件（避免引入外部依赖）"""
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip("'\"")
                    if k and k not in os.environ:
                        os.environ[k] = v
        except Exception:
            pass

def load_config() -> dict:
    _load_env_file()
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    cfg.update(data)
        except Exception:
            pass

    # 环境变量安全覆盖 (适用于云服务器或 Docker)
    if os.getenv("DINGTALK_WEBHOOK"):
        cfg["dingtalk_webhook"] = os.getenv("DINGTALK_WEBHOOK")
        cfg["dingtalk_enabled"] = True
    if os.getenv("DINGTALK_SECRET"):
        cfg["dingtalk_secret"] = os.getenv("DINGTALK_SECRET")
    if os.getenv("WECHAT_PROVIDER"):
        cfg["wechat_provider"] = os.getenv("WECHAT_PROVIDER")
    if os.getenv("WECHAT_TOKEN"):
        cfg["wechat_token"] = os.getenv("WECHAT_TOKEN")
        cfg["wechat_enabled"] = True
    if os.getenv("WEB_PORT"):
        try:
            cfg["web_port"] = int(os.getenv("WEB_PORT"))
        except ValueError:
            pass
    if os.getenv("WEB_HOST"):
        cfg["web_host"] = os.getenv("WEB_HOST")

    return cfg

def save_config(new_config: dict) -> bool:
    try:
        cfg = load_config()
        cfg.update(new_config)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f"保存配置失败: {e}")
        return False
