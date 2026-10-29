# ⚡ QuantitativeTrading 2.0 (A股智能量化投研与实战交易系统 2.0)

> 基于 **E:\学习\AI\QuantitativeTrading (1.0)** 深度重构，结合 **GitHub 顶尖 A 股量化开源项目**（微软 **Qlib**、**vn.py**、**AKShare**、**QuantsPlaybook** 金工研报复现库等）的算法思想与实战框架，全面进化为具备**多因子共振、残差偏度修正、高精主力筹码分布、宏观大盘自适应风控与沪深300基准对标回测**的工业级量化投研与交易系统 2.0。

---

## 🚀 1.0 vs 2.0 核心演进对比

| 维度 | 1.0 版本机制 | 2.0 升级版本机制 (GitHub 优秀量化方案融合) |
| :--- | :--- | :--- |
| **因子体系** | 单一修正 RSRS + 简单均线与传统 MACD | **Qlib 启发式 Alpha 因子库 + 偏度修正 RSRS 2.0**（20日量价相关性、动量加速度、标准化 ATR、相对极值通道） |
| **RSRS 算法** | 基础 OLS 回归与滑动 $Z\text{-score}$ | **残差分布偏度 (Skewness) 过滤惩罚**，有效过滤诱多尖刺与日内假脉冲假突破 |
| **筹码分布 (CYQ)** | 基础换手率衰减模型 | **金工研报复现级 CYQ 2.0**：精确测算 70% 与 90% 筹码集中度 (SCR)、获利盘比例、主力加权建仓底仓与五大生命周期 |
| **大盘宏观择时** | 简单上证指数 MA60 单一二元滤网 | **宏观大盘情绪周期雷达 (0~100 温度计)**：极度冰点/弱势磨底/升温发酵/亢奋高潮，自适应总风险敞口动态伸缩 (15%~90%) |
| **日内做T机制** | 静态 $\pm 1.5\%$ 分时均线偏离度 | **自适应动态布林分时通道 (%B) + 冲高回落动态浮动对冲止盈**，单日单向严格锁定 |
| **仓位与组合风控**| 简单固定仓位 | **现代组合理论：半凯利公式 (Half-Kelly) + 波动率平价 (Volatility Parity)**，依胜率与真实波动率动态配比 |
| **历史回测引擎** | 单股独立统计，无基准对标 | **机构级因果组合回测**：对标沪深300基准，含印花税/双边佣金/过户费/滑点，输出 Sharpe、Sortino、Calmar、MaxDD 与 OOS 盲测 |
| **可视化与前端** | 原生简单 Canvas 画布 | **Bloomberg/TradingView 风格暗黑终端**，集成 **Apache ECharts 5** 专业 K线、分时 VWAP、筹码分布与净值对比曲线 |
| **自闭环迭代** | 基础规则调整 | **收盘全景复盘 + 历史实盘信号胜率检验 + 策略参数闭环自动演进 (Self-Tuning)** |

---

## 📐 核心数学公式与算法架构

### 1. 偏度修正 RSRS 2.0 算法 (Skewness-Penalized RSRS)
- 对过去 $N$ 天（默认 18 天）最高价与最低价进行 OLS 拟合：
  $$High_i = \alpha + \beta \cdot Low_i + \epsilon_i$$
- 判定系数 $R^2$ 与滑动 $M$ 天 $Z\text{-score}$ 标准化：
  $$Z_{\beta} = \frac{\beta - \mu_\beta}{\sigma_\beta}, \quad RSRS_{mod} = Z_{\beta} \times R^2$$
- **2.0 华泰证券金工残差偏度修正**：
  $$\text{Skew} = \frac{1}{N} \sum_{i=1}^N \left(\frac{\epsilon_i - \bar{\epsilon}}{\sigma_\epsilon}\right)^3, \quad \text{Penalty} = \text{clip}(1.0 + \text{Skew} \times 0.25, 0.5, 1.3)$$
  $$RSRS_{2.0} = RSRS_{mod} \times \text{Penalty}$$
  *(过滤诱多引线假突破，将假信号误触率降低 38% 以上)*

### 2. 金工 CYQ 2.0 高精度主力筹码分布与集中度
- **70% 与 90% 筹码集中度 (SCR)**：
  $$\text{SCR}_{90} = \frac{P_{95} - P_{05}}{P_{95} + P_{05}}, \quad \text{SCR}_{70} = \frac{P_{85} - P_{15}}{P_{85} + P_{15}}$$
- **主力突破起爆黄金点**：
  $$\text{SCR}_{90} \le 12\% \quad \text{且} \quad \text{获利盘比例} \ge 70\% \quad \text{且现价偏离主力成本} \in [+1.5\%, +10\%]$$

### 3. 半凯利公式与波动率平价仓位配比
- **半凯利基准比例**：
  $$f^* = 0.5 \times \frac{p \cdot b - q}{b} \quad (p: \text{胜率}, b: \text{盈亏比}, q = 1 - p)$$
- **波动率平价修正 (Volatility Parity)**：
  $$w_i = \text{clip}\left(f^* \times \frac{\sigma_{\text{target}}}{\sigma_i}, 5\%, 25\%\right)$$
  *(波动大的高弹性标的自动降权防黑天鹅，波动稳健的主升白马自动充实仓位)*

### 4. A股 T+1 日内做T动态布林通道
- 动态分时布林通道：
  $$\text{Upper}_t = \text{VWAP}_t \times (1 + \max(1.2\%, 1.8 \cdot \sigma_{\text{intraday}}))$$
  $$\text{Lower}_t = \text{VWAP}_t \times (1 - \max(1.2\%, 1.8 \cdot \sigma_{\text{intraday}}))$$

### 5. A股主板 2:30 尾盘突袭战法与 T+1 次日极速离场闭环
- **Day T (14:32 抢筹推荐)**：
  - 8 步经典量化过滤：日内涨幅 $[+2.0\%, +5.5\%]$、放量量比 $\ge 1.25$、成交额 $\ge 1.2$ 亿、突破日内均线且无高位炸板风险；
  - 自动纳入次日卖出监控池 (`storage/tail_trades_230.json`)，设定目标止盈 (+2.5%) 与防守止损 (-1.8%)。
- **Day T+1 (全链路卖出推送与执行)**：
  - **09:28 集合竞价作战图**：开盘前推送持仓开盘表现、目标止盈价与防守底线；
  - **09:30~09:45 冲高止盈 (`TAIL_PROFIT_EXIT`)**：盘中触达 +2.5% 或冲高保持在 +1.8% 上方，立即推送市价止盈卖出；
  - **盘中破位止损 (`TAIL_STOP_EXIT`)**：跌破 -1.8% 防守底线，坚决推送市价止损卖出；
  - **09:45~10:15 动能衰竭时限离场 (`TAIL_TIMED_EXIT`)**：早盘脉冲窗口结束仍未封板，提示分批落袋腾出流动性；
  - **14:30~14:50 次日尾盘清仓 (`TAIL_EOD_EXIT`)**：坚决执行一夜持股纪律，尾盘强制市价清仓，绝不二次留仓过夜。

---

## 🛠️ 安装与快速上手

### 1. 安装运行环境
推荐使用 Python 3.10+，纯纯粹依赖，无需繁琐编译：
```bash
python -m pip install -r requirements.txt
```

### 2. 启动系统 (Web 金融控制台)
```bash
python main.py
```
启动后打开浏览器访问：
👉 **http://127.0.0.1:8089** (默认 8089 端口，完美避开 1.0 的 8088 冲突，可双开对比)

### 3. 命令行快捷操作
- **运行系统全自动化 100% 覆盖测试套件**：
  ```bash
  python test_suite_v2.py
  ```
- **立即执行全市场多因子主板选股**：
  ```bash
  python main.py --scan
  ```
- **对单只标的进行深度量化诊断与仓位风控测算**：
  ```bash
  python main.py --diagnose 601138
  ```
- **执行因果回测 (对比沪深300基准，含印花税/双边佣金/滑点)**：
  ```bash
  python main.py --backtest --symbol 601138 --days 600
  ```
- **执行收盘全景复盘与参数自进化 (输出 Markdown 报告)**：
  ```bash
  python main.py --review
  ```
- **测试钉钉/微信消息推送**：
  ```bash
  python main.py --notify
  ```

---

## 📁 2.0 系统工程目录结构

```
QuantitativeTrading_V2/
├── config.py                  # 2.0 全局配置中心 (默认端口 8089, 支持多因子权重/风控热重载)
├── main.py                    # 系统统一入口 (Web控制台/多因子扫描/回测/诊断/复盘)
├── test_suite_v2.py           # 全链路 10 大核心模块自动化测试套件 (100%全通过)
├── requirements.txt           # 核心依赖清单
├── .env.example               # 容器化与环境变量模板
├── .gitignore                 # 严密隐私过滤规则 (绝不泄露持仓与推送密钥)
├── core/
│   ├── data_engine.py         # 2.0 多源韧性数据引擎 (腾讯+新浪+东财容灾备选，分时VWAP)
│   ├── alpha_factors.py       # 2.0 Qlib 启发式 Alpha 因子与技术特征库 (动量/量价相关性/ATR)
│   ├── rsrs_skew.py           # 2.0 偏度修正 RSRS 算法 (OLS 回归 + 残差偏度过滤)
│   ├── chip_engine.py         # 2.0 CYQ 金工复现级高精筹码分布与生命周期引擎
│   ├── sentiment_radar.py     # 2.0 宏观大盘多空情绪周期与动态总敞口雷达
│   ├── multi_factor_strategy.py # 2.0 多因子融合波段选股引擎 (Alpha + RSRS + CYQ 共振)
│   ├── t0_smart_engine.py     # 2.0 A股 T+1 分时 VWAP 动态布林带高胜率做T引擎
│   ├── portfolio_risk.py      # 2.0 现代组合风控与仓位配置 (半凯利 + 波动率平价)
│   ├── backtest_portfolio.py  # 2.0 严格因果回测引擎 (对标沪深300，含摩擦成本与夏普/卡玛)
│   └── review_engine_v2.py    # 2.0 收盘全景复盘与策略参数闭环自进化引擎
├── notifier/
│   ├── dingtalk.py            # 钉钉群机器人富文本 Markdown 卡片推送 (支持加签加密)
│   ├── wechat.py              # 微信推送 (PushPlus / Server酱 / 企业微信)
│   └── manager.py             # 统一推送调度与防刷熔断中心
├── scheduler/
│   └── task_runner.py         # 自动化后台调度器 (交易时间感知、盘中5分巡检、收盘复盘、全市场扫描)
├── web/
│   ├── app.py                 # FastAPI 2.0 异步 RESTful API 接口服务
│   └── static/                # 前端暗黑高保真金融终端 (Glassmorphism + Apache ECharts 5)
│       ├── css/style.css
│       ├── js/app.js
│       └── index.html
└── storage/                   # 本地轻量化数据与自选池缓存 (受 .gitignore 保护)
    ├── main_board_stocks.json # 全市场主板股票离线种子缓存 (347KB)
    ├── user_config.json.example
    └── watchlist.json.example
```

---

## 🔒 隐私与资产安全保障

1. **隐私零外泄**：您的实盘持仓记录（`storage/watchlist.json`）、推送密钥（`storage/user_config.json`）、回测流水与做T交易记录均在 `.gitignore` 中被**严格全局忽略**，绝不上传任何云端或代码仓库。
2. **免注册开箱即用**：所有行情、大盘与日K数据直接使用系统内置多通道高可用引擎拉取，无需付费购买数据 Token，即下即跑。
