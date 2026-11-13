"""
A股智能量化投研与实战交易系统 2.0 全局配置中心
支持策略动态调优、多因子权重配比、风险敞口约束与多渠道推送
"""
import os
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_env_file():
    """轻量级解析 .env 文件（避免引入外部依赖）；须在确定数据目录之前加载。"""
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


_load_env_file()

# 数据目录：默认在项目内 storage/；可用环境变量 QUANT_DATA_DIR 指向仓库外的私有目录，
# 便于把个人持仓、推送密钥等隐私数据与代码仓库彻底隔离（不随 git 上传）。
DATA_DIR = Path(os.environ.get("QUANT_DATA_DIR") or (BASE_DIR / "storage")).resolve()
CONFIG_FILE = DATA_DIR / "user_config.json"
WATCHLIST_FILE = DATA_DIR / "watchlist.json"
SIGNALS_FILE = DATA_DIR / "signals_history.json"
T0_TRADES_FILE = DATA_DIR / "t0_trades.json"
T0_HISTORY_FILE = DATA_DIR / "t0_history.json"
TAIL_TRADES_FILE = DATA_DIR / "tail_trades_230.json"
REVIEW_FILE = DATA_DIR / "review_history.json"
OPTIMIZATION_FILE = DATA_DIR / "optimization_log.json"
LIMIT_UP_TRADES_FILE = DATA_DIR / "limit_up_trades.json"
LIMIT_UP_CSV_FILE = DATA_DIR / "limit_up_ledger.csv"
T0_GROUPS_FILE = DATA_DIR / "t0_groups.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG = {
    # ---------------- 1. 核心多因子波段策略参数 ----------------
    "rsrs_n": 18,                       # RSRS 回归统计窗口 (通常18天)
    "rsrs_m": 600,                      # RSRS Z-score标准化窗口 (通常600天)
    "rsrs_buy_threshold": 0.7,          # 修正RSRS买入阈值
    "rsrs_buy_upper_threshold": 1.3,    # 修正RSRS买入上限 (>=该值视为过热，历史后续收益明显转差)
    "score_buy_upper_threshold": 90.0,  # 综合评分买入上限 (>=该值视为过度拥挤，历史后续收益明显转差)
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
    "max_single_position_pct": 0.15,    # 单只个股持仓上限 (15%)
    "risk_per_trade_pct": 0.5,          # 单笔初始止损风险预算占净值比例 (0.5%)
    "hard_stop_loss_pct": 6.0,          # 持仓账户硬止损观察线 (-6%，隔夜跳空仍可能扩大损失)
    "use_volatility_parity": True,      # 是否启用波动率平价仓位配比
    "use_market_regime_adaptive": True, # 是否根据大盘牛熊自适应调整总仓位上限
    "bull_market_max_exposure": 0.65,   # 强多头大盘允许最高总仓位 (65%)
    "oscillation_max_exposure": 0.35,  # 震荡市允许最高总仓位 (35%)
    "bear_market_max_exposure": 0.10,  # 弱市/熊市防守最高总仓位 (10%)
    
    # ---------------- 4. A股 T+1 日内做T 2.0 参数 ----------------
    "t0_monitor_enabled": True,         # 开启T0研究信号监控；信号不会自动提交订单
    "t0_use_dynamic_boll": True,        # 开启分时自适应带宽 (基于价格相对VWAP的平均偏离)
    "t0_band_dev_k": 1.0,               # 自适应带宽 = k × 分时价格相对VWAP的平均绝对偏离(%)
                                         # k 越大带宽越宽、信号越少；1.0 为兼顾频率的默认值
    "t0_band_min_pct": 0.6,             # 自适应带宽下限 (%)
    "t0_band_max_pct": 2.8,             # 自适应带宽上限 (%)
    "t0_sell_asymmetry": 1.15,          # 倒T(卖出)带宽 = 正T带宽 × 该系数 (上轨略宽)
    "t0_bias_buy": -1.5,                # 分时数据不足时的固定低吸偏离阈值 (兜底)
    "t0_bias_sell": 2.0,                # 分时数据不足时的固定高抛偏离阈值 (兜底)
    "t0_reversal_bars": 1,              # 反转确认所需连续同向分钟K线数 (1=更灵敏，2=更严格)
    "t0_reversal_volume_confirm": False, # 反转放量确认 (默认关闭以保证信号频率，可改 True 加强质量)
    "t0_reversal_volume_mult": 1.0,     # 放量确认倍数 (反转分钟量 >= 近期均量 × 该系数)
    "t0_vwap_slope_filter": False,      # 日内容均价VWAP斜率过滤 (默认关闭；开启后趋势日不逆势做T，但信号更少)
    "t0_vwap_slope_lookback": 15,       # VWAP斜率回看分钟数
    "t0_max_vwap_slope_pct": 0.8,       # VWAP斜率绝对值上限(%)，超过视为单边趋势日
    "t0_stop_loss_pct": 1.5,            # 做T防守保护止损比例 (%)
    "t0_take_profit_vwap_pct": 0.25,    # 目标止盈 = 回到VWAP上下方该比例 (%)
    "t0_time_decay_enabled": True,      # 开启尾盘时间衰减 (剩余时间越少目标越贴近、止损越紧)
    "t0_late_window_start": "14:00",    # 此后进入尾盘时间衰减
    "t0_late_target_scale": 0.6,        # 尾盘目标止盈缩放 (更贴近现价)
    "t0_late_stop_scale": 0.8,          # 尾盘止损缩放 (更紧)
    "t0_commission_rate": 0.00025,      # 双边佣金费率 (万2.5)
    "t0_min_commission": 5.0,           # 单边最低佣金 (元)
    "t0_stamp_tax_rate": 0.0005,        # 印花税 (仅卖出，0.05%)
    "t0_slippage_rate": 0.0005,         # 单边滑点估算 (0.05%)
    "t0_minute_min_bars": 30,           # 开仓信号最低有效分时条数
    "t0_minute_max_age_minutes": 5,     # 分时末条最大允许延迟分钟数
    "t0_min_market_exposure": 0.35,     # 仅在市场状态不低于该风险敞口时生成新T0信号
                                         # 0.35=多头震荡及以上允许(空头震荡也允许做T摊薄)；0.65=仅多头；0=不限制
    "t0_min_market_exposure_sell": 0.0, # 倒T(高抛)的市场敞口下限；0=弱市也允许高抛防御，仅要求指数数据可用
    "t0_max_trades_per_day": 1,         # 单只标的每日做T最大次数
    "t0_single_direction_only": True,   # 单日做T单向严格锁定
    "t0_trend_filter_enabled": True,    # 开启大趋势过滤 (顺势做T)
    "t0_eod_force_close_enabled": True, # 开启尾盘风险核验提醒；不会自动平仓
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
    "scan_interval_minutes": 5,          # 盘中常态自选池与做T巡检间隔 (分钟)
    "opening_high_freq_enabled": True,   # 开启早盘开盘脉冲期(09:30-09:40)高频巡检(1分钟高灵敏度)
    "closing_high_freq_enabled": True,   # 开启尾盘决胜期(14:35-15:00)提频巡检(2分钟)
    "auction_snapshot_enabled": True,    # 开启集合竞价(09:25:05)定价秒级感知
    "market_review_time": "15:10",       # 收盘后全景复盘与参数自迭代时间
    "market_close_scan_time": "15:30",   # 盘后全市场智能精选时间
    "review_auto_optimize_enabled": True,  # 自动调参仍需达到最小已结算样本门槛
    
    # ---------------- 7. 2:30 尾盘突袭战法参数 ----------------
    "tail230_target_profit_pct": 2.5,   # 次日早盘冲高止盈目标 (%)
    "tail230_stop_loss_pct": 1.8,       # 次日防守底线止损线 (%)
    "tail230_auto_track_top_n": 3,      # 每日14:32自动纳入次日卖出监控的前N只标的
    "tail230_morning_check_enabled": True, # 开启次日 09:28 集合竞价操盘计划预警
    "tail230_min_pct": 2.0,             # 最小日内涨幅 (%)
    "tail230_max_pct": 5.5,             # 最大日内涨幅 (%，防过热追高)
    "tail230_min_vol_ratio": 1.25,      # 最小量比 (主力抢筹特征)
    "tail230_min_turnover": 120000000.0,# 最小日成交额 (1.2亿元，保障流动性)
    "tail230_backtest_universe_size": 80, # 回测代表性样本数 (按成交额取沪深主板流动性居前标的)
    
    # ---------------- 8. 游资龙虎打板量化参数 ----------------
    "limit_up_auto_notify_enabled": True,  # 自动推送涨停候选研究摘要
    "limit_up_scan_interval_minutes": 10,  # 盘中打板监控扫描间隔 (分钟)
    "limit_up_min_hardness": 80.0,         # 自动推送要求的最低封板硬度分
    "limit_up_auto_track_top_n": 2,        # 每次自动推送并跟踪的精选标的数
    "tail230_auto_scan_enabled": True,     # 自动扫描尾盘研究候选；不自动记录成交

    # ---------------- 9. Web 服务配置 (默认 8089 避开冲突) ----------------
    "web_port": 8089,
    "web_host": "127.0.0.1"
}


def load_config() -> dict:
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
