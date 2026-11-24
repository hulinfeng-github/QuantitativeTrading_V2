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
    "t0_sell_asymmetry": 1.15,          # 倒T(卖出)带宽 = 正T带宽 * 该系数 (上轨略宽)
    "t0_bias_buy": -0.9,                # 个股正T基准偏离阈值(%) (常态-0.9%，超跌反弹-0.75%，熊市极端-1.6%)
    "t0_bias_sell": 1.1,                # 个股倒T基准偏离阈值(%) (常态+1.1%，弱市顺势+0.85%，多头+2.2%)
    "t0_require_dual_resonance": True,  # 开启双重共振 (偏离阈值 AND 动态布林触轨同时满足，规避单边假信号)
    "t0_golden_window_only": True,      # 仅在黄金交易窗口开新仓 (自动屏蔽 11:00-11:30 假突破陷阱期)
    "t0_avoid_early_morning_short": True, # 开启早盘防倒T保护 (09:45前主力抢筹脉冲期严格限制倒T，非极端不抛底仓)
    "t0_divergence_filter_enabled": True, # 开启分时右侧动能拐点与共振预测 (MACD绿柱缩短/金叉/底背离)
    "t0_agile_trailing_enabled": True,  # 开启敏捷动态移动止盈 (扣费后高正期望保护)
    "t0_agile_trailing_active": 0.70,   # 个股敏捷移动止盈激活阈值(%) (0.7%起步)
    "t0_agile_trailing_callback": 0.25, # 基础敏捷移动止盈回撤锁定阈值(%) (0.25%)
    "t0_agile_trailing_tiered": True,   # 开启阶梯动态移动止盈 (分层放宽回撤容忍度，让利润奔跑，防盘口假反弹过早洗出)
    "t0_agile_callback_tier1": 0.25,    # 个股初级浮盈(0.7%~1.2%)回撤容忍阈值(%) (0.25%锁定正利保本)
    "t0_agile_callback_tier2": 0.40,    # 个股中高浮盈(>1.2%)回撤容忍阈值(%) (放宽至0.40%让波段利润奔跑)
    "t0_agile_callback_etf_tier1": 0.18,# ETF初级浮盈(0.45%~0.8%)回撤容忍阈值(%)
    "t0_agile_callback_etf_tier2": 0.28,# ETF高浮盈(>0.8%)回撤容忍阈值(%)
    "t0_afternoon_buy_cutoff": "14:00", # 下午正T低吸开仓截止时间 (14:00后禁止新开正T，留足日内波段对冲时间，规避14:48强平摩擦)
    "t0_afternoon_sell_cutoff": "14:15",# 下午倒T高抛开仓截止时间 (14:15后全天停止任何新做T开仓)
    "t0_etf_share_ratio": 0.8,          # ETF做T建议股数占可用底仓的比例 (默认80%底仓弹性轮动，普通个股按单批次控制)
    "t0_etf_bias_buy": -0.55,           # ETF正T偏离阈值(%) (-0.55%)
    "t0_etf_bias_sell": 0.65,           # ETF倒T偏离阈值(%) (+0.65%)
    "t0_etf_agile_active": 0.45,        # ETF敏捷移动止盈激活阈值(%) (ETF波动平滑，0.45%起步)
    "t0_etf_take_profit_pct": 0.8,      # ETF静态目标止盈幅度(%) (0.8%)
    "t0_etf_stop_loss_pct": 0.85,       # ETF硬止损保护幅度(%) (0.85%)
    "t0_cost_dilution_notify": True,    # 开启持仓成本动态摊薄与解套进度追踪通知
    "t0_reversal_bars": 2,              # 反转确认所需连续同向分钟K线数 (2根不创新低/不创新高)
    "t0_reversal_volume_confirm": True, # 反转放量确认 (微观结构量能确认)
    "t0_reversal_volume_mult": 0.85,    # 放量确认倍数 (反转分钟量 >= 近期均量 × 0.85)
    "t0_vwap_slope_filter": True,       # 日内均价VWAP斜率平走保护 (陡峭下泄严禁接飞刀，单边急涨严禁倒T)
    "t0_vwap_slope_lookback": 15,       # VWAP斜率回看分钟数
    "t0_max_vwap_slope_pct": 0.50,      # VWAP斜率绝对值上限(%)，超过视为单边急跌/急涨
    "t0_stop_loss_pct": 1.5,            # 个股做T防守保护止损比例 (%)
    "t0_take_profit_pct": 1.5,          # 个股做T静态目标止盈比例 (%)
    "t0_take_profit_vwap_pct": 0.30,    # 目标止盈 = 回到VWAP上下方该比例 (%)
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
    "t0_max_trades_per_day": 2,         # 单只标的每日做T最大次数 (以1笔为主，出现高确定性极值允许第2笔，硬封顶2笔)
    "t0_daily_stop_circuit_breaker": True, # 开启单日同向止损熔断 (当日止损后不再开同向仓位)
    "t0_single_direction_only": False,  # 允许顺势日内多空反转波段
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
    "scan_interval_minutes": 1,          # 盘中常态自选池与做T巡检间隔 (分钟，提频至1分钟杜绝尖点拐点错过)
    "scan_interval_seconds": 60,         # 盘中常态巡检秒数
    "opening_high_freq_enabled": True,   # 开启早盘开盘脉冲期(09:30-10:00)高频巡检(30秒高灵敏度)
    "closing_high_freq_enabled": True,   # 开启尾盘决胜期(14:40-15:00)提频巡检(30秒)
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
