"""
自动化定时调度引擎 2.0 (QuantScheduler 2.0)
功能：
1. A股交易时间自适应感知 (工作日 9:25~11:30, 13:00~15:00)；
2. 盘中自选股与持仓日内做T高频并发巡检 (默认每5分钟)；
3. 盘后 15:10 全景量化复盘与参数自进化演进；
4. 盘后 15:30 全市场主板多因子量化选股雷达更新；
5. 多线程并发加速与断网保护。
"""
import time
import json
import threading
import sys
import ctypes
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from config import load_config, WATCHLIST_FILE, REVIEW_FILE
from core.data_engine import data_engine
from core.multi_factor_strategy import multi_factor_strategy
from core.t0_smart_engine import t0_smart_engine
from core.review_engine_v2 import review_engine_v2
from core.strategy_tail_230 import tail_strategy_230, tail_trade_tracker
from core.portfolio_risk import portfolio_risk
from notifier.manager import notification_manager

def set_windows_sleep_prevention(enable: bool = True):
    """
    通过 Windows 核心原生 API (SetThreadExecutionState) 防止操作系统自动睡眠/休眠
    - 允许屏幕正常熄灭以省电和保护隐私
    - 定时任务依赖主机持续运行、系统时钟和数据源，按配置尽力触发
    """
    if sys.platform.startswith("win"):
        try:
            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            if enable:
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            else:
                ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        except Exception:
            pass

class QuantScheduler2:
    def __init__(self):
        self.scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        self.is_running = False
        self.latest_scan_results: List[Dict[str, Any]] = []
        self.scan_status = {
            "is_scanning": False,
            "progress": 0,
            "total": 0,
            "message": "空闲",
            "last_scan_time": ""
        }
        self._last_intraday_check: float = 0.0

    @staticmethod
    def is_trading_time() -> bool:
        """判断当前是否为 A 股交易时间 (周一至周五 09:25-11:30, 13:00-15:00)"""
        now = datetime.now()
        if now.weekday() >= 5:  # 周末不交易
            return False
        curr_time = now.strftime("%H:%M")
        morning = "09:25" <= curr_time <= "11:30"
        afternoon = "13:00" <= curr_time <= "15:00"
        return morning or afternoon

    def get_watchlist(self) -> List[Dict[str, Any]]:
        """获取用户自选股清单"""
        if WATCHLIST_FILE.exists():
            try:
                with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    def run_intraday_check(self, force: bool = False):
        """盘中自选股巡检：检查波段信号与日内做T机会 (支持开盘脉冲黄金期自适应提频)"""
        if not self.is_trading_time():
            return

        now = datetime.now()
        curr_hm = now.strftime("%H:%M")
        cfg = load_config()

        if not force:
            # 动态感知市场博弈节奏与自适应巡检频控
            if "09:30" <= curr_hm <= "09:40" and cfg.get("opening_high_freq_enabled", True):
                step_interval = 60  # 开盘脉冲黄金期：提频至 60 秒高灵敏巡检，捕捉日内高低极值
            elif "14:35" <= curr_hm <= "15:00" and cfg.get("closing_high_freq_enabled", True):
                step_interval = 120 # 尾盘决胜决断期：提频至 120 秒，防范尾盘突袭或强平滞后
            elif "09:25" <= curr_hm < "09:30":
                step_interval = 180 # 集合竞价等待期：维持 3 分钟
            else:
                step_interval = cfg.get("scan_interval_minutes", 5) * 60 # 常态交易时段：默认 5 分钟

            if (time.time() - self._last_intraday_check) < (step_interval - 5):
                return

        self._last_intraday_check = time.time()

        watchlist = self.get_watchlist()
        active_tail_positions = tail_trade_tracker.get_active_positions()
        if not watchlist and not active_tail_positions:
            return

        print(f"[{datetime.now().strftime('%H:%M:%S')}] 启动盘中巡检 (自选 {len(watchlist)} 只, 2.30昨日持仓 {len(active_tail_positions)} 只)...")
        symbols = [item['symbol'] for item in watchlist if item.get('symbol')]
        tail_syms = [p['symbol'] for p in active_tail_positions if p.get('symbol')]
        all_symbols = list(set(symbols + tail_syms))
        quotes = data_engine.get_realtime_quotes(all_symbols)

        t0_market_exposure = None
        if load_config().get("t0_monitor_enabled", True) and any(
            item.get("t0_enabled", True) and data_engine.is_main_board(item.get("symbol", ""))
            for item in watchlist
        ):
            try:
                from core.sentiment_radar import sentiment_radar
                market_state = sentiment_radar.analyze_market_regime()
                if market_state.get("data_available") and market_state.get("target_exposure") is not None:
                    t0_market_exposure = float(market_state["target_exposure"])
            except Exception:
                t0_market_exposure = None

        for item in watchlist:
            try:
                sym = item.get('symbol')
                name = item.get('name', sym)
                cost = float(item.get('holding_cost') or item.get('entry_price') or 0.0)
                shares = int(item.get('holding_shares') or item.get('shares') or 0)
                available_shares = portfolio_risk.resolve_available_shares(item)

                q = quotes.get(sym)
                if not q:
                    continue

                cur_p = q.get('price', 0.0)
                if cur_p <= 0:
                    continue

                # 1. 检查真实持仓强制平仓/硬止损风控
                pnl_pct = round((cur_p - cost) / cost * 100.0, 2) if cost > 0 else 0.0
                pnl_amt = round((cur_p - cost) * shares, 2) if cost > 0 else 0.0

                df_kline = data_engine.get_daily_kline(sym, count=120)
                analysis_res = None
                if df_kline is not None and len(df_kline) >= 30:
                    analysis_res = multi_factor_strategy.analyze(sym, name, df_kline)

                # 持仓硬止损检查 (如果存在真实持仓)
                if cost > 0 and shares > 0:
                    cfg = load_config()
                    hard_stop = cfg.get("hard_stop_loss_pct", 8.0)
                    control_st = analysis_res.get("control_status", "") if analysis_res else ""

                    # 触发条件: 浮亏超过硬止损线 (如 -8%)，或主力破位杀跌且严重亏损
                    if pnl_pct <= -hard_stop or (control_st == "主力破位杀跌" and pnl_pct <= -5.0):
                        force_signal = {
                            "symbol": sym,
                            "name": name,
                            "date": datetime.now().strftime("%Y-%m-%d"),
                            "price": cur_p,
                            "signal_type": "FORCE_EXIT",
                            "signal_desc": "🚨 强制平仓/硬止损离场警报",
                            "score": analysis_res.get("score", 0) if analysis_res else 0,
                            "holding_cost": cost,
                            "holding_shares": shares,
                            "pnl_pct": pnl_pct,
                            "pnl_amt": pnl_amt,
                            "control_status": control_st or "严重破位",
                            "main_cost": analysis_res.get("main_cost", cost) if analysis_res else cost,
                            "rsrs_norm": analysis_res.get("rsrs_norm", -1.0) if analysis_res else -1.0,
                            "reasons": [
                                f"持仓浮亏达 {pnl_pct:+.2f}% (亏损 {abs(pnl_amt):.2f} 元)，已触及 -{hard_stop}% 账户硬止损保护线",
                                f"现价({cur_p}元)较买入成本({cost}元)大幅下挫，技术面处于[{control_st}]",
                                "模型风险阈值已触发。行情、实际持仓和可卖数量尚未核验；请先核对账户与委托状态，系统未提交订单。"
                            ],
                            "trade_levels": {
                                "entry_price": cur_p,
                                "suggested_sell_price": cur_p,
                                "initial_stop": round(cost * (1.0 - hard_stop / 100.0), 2)
                            }
                        }
                        notification_manager.send_signal(force_signal)

                # 2. 检查波段策略多因子信号
                if analysis_res:
                    sig_type = analysis_res.get("signal_type")
                    if sig_type in ["BUY", "SELL"]:
                        if sig_type == "SELL" and cost > 0:
                            analysis_res["holding_cost"] = cost
                            analysis_res["holding_shares"] = shares
                            analysis_res["pnl_pct"] = pnl_pct
                        notification_manager.send_signal(analysis_res)

                # T0仅推送通过持仓、趋势与分时质量过滤的研究信号，不会自动下单。
                # 做T同时支持主板个股与场内ETF，故使用 is_main_board 而非 is_main_board_equity
                if (load_config().get("t0_monitor_enabled", True)
                        and item.get("t0_enabled", True)
                        and data_engine.is_main_board(sym)):
                    t0_res = t0_smart_engine.analyze_t0(
                        symbol=sym,
                        name=name,
                        current_price=cur_p,
                        daily_kline=df_kline,
                        holding_cost=cost,
                        holding_shares=shares,
                        available_shares=available_shares,
                        market_exposure=t0_market_exposure
                    )
                    act = t0_res.get("signal_action")
                    if act in ["BUY_T", "SELL_T", "CLOSE_T", "STOP_T"]:
                        sig_payload = t0_smart_engine.build_signal_payload(t0_res)
                        if item.get("group_id"):
                            sig_payload["group_id"] = item.get("group_id")
                        notification_manager.send_signal(sig_payload)
            except Exception as e:
                print(f"[Scheduler 2.0] 巡检个股 {item.get('symbol', '')} 异常: {e}")

        # 4. 尾盘时段 (14:45-14:55) 自动检查未闭环做T头寸，发出强制平仓/对冲决策
        cur_hm = datetime.now().strftime("%H:%M")
        if load_config().get("t0_eod_force_close_enabled", True) and "14:45" <= cur_hm <= "14:55":
            eod_sigs = t0_smart_engine.check_eod_unclosed_positions()
            if eod_sigs:
                notification_manager.send_signals_batch(eod_sigs)

        # 5. 仅对用户确认实际买入的记录发送行情提醒；提醒不会自动结算成交。
        tail_exits = tail_trade_tracker.check_next_day_exits(quotes)
        if tail_exits:
            notification_manager.send_signals_batch(tail_exits)

    def run_market_scan_async(self, limit: int = 100, notify: bool = True):
        """异步执行全市场主板智能精选"""
        if self.scan_status["is_scanning"]:
            return
        t = threading.Thread(target=self._scan_market_worker, args=(limit, notify), daemon=True)
        t.start()

    def _scan_market_worker(self, limit: int = 100, notify: bool = True):
        self.scan_status["is_scanning"] = True
        self.scan_status["progress"] = 0
        self.scan_status["message"] = "正在拉取A股主板标的池..."

        stocks = data_engine.get_main_board_stock_list()
        total = min(len(stocks), limit)
        self.scan_status["total"] = total
        candidates = []

        cfg = load_config()
        min_turnover = cfg.get("min_turnover_amount", 100000000)
        from core.sentiment_radar import sentiment_radar
        market = sentiment_radar.analyze_market_regime()
        market_regime_bull = bool(
            market.get("data_available") and market.get("target_exposure", 0.0) >= 0.65
        )

        # 过滤低流动性标的
        valid_stocks = [s for s in stocks if float(s.get("turnover", 0)) >= min_turnover][:total]
        self.scan_status["total"] = len(valid_stocks)

        def eval_stock(s):
            sym = s.get("symbol")
            name = s.get("name")
            try:
                df = data_engine.get_daily_kline(sym, count=180)
                if df is not None and len(df) >= 30:
                    res = multi_factor_strategy.analyze(
                        sym, name, df, market_regime_bull=market_regime_bull
                    )
                    if res and res.get("score", 0) >= 60:
                        return res
            except Exception:
                pass
            return None

        completed = 0
        with ThreadPoolExecutor(max_workers=6) as executor:
            future_to_stock = {executor.submit(eval_stock, s): s for s in valid_stocks}
            for future in as_completed(future_to_stock):
                completed += 1
                self.scan_status["progress"] = completed
                self.scan_status["message"] = f"正在扫描多因子特征 ({completed}/{len(valid_stocks)})..."
                res = future.result()
                if res:
                    candidates.append(res)

        candidates.sort(key=lambda x: x.get("score", 0), reverse=True)
        self.latest_scan_results = candidates
        self.scan_status["is_scanning"] = False
        self.scan_status["message"] = f"扫描完成，筛选出 {len(candidates)} 只优质标的"
        self.scan_status["last_scan_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 触发收盘多因子智能选股与风控推送 (钉钉 / 微信)
        if notify:
            self._notify_market_scan_results(candidates)

    def _notify_market_scan_results(self, candidates: List[Dict[str, Any]]):
        """将收盘后多因子主板精选标的（含深度财报体检）格式化并推送至钉钉/微信"""
        now = datetime.now()
        date_str = now.strftime('%Y-%m-%d')
        from core.sentiment_radar import sentiment_radar
        macro = sentiment_radar.analyze_market_regime()

        # 仅在市场数据有效且风险敞口处于结构性多头区间时推送 BUY 候选。
        can_open = bool(macro.get("data_available") and macro.get("target_exposure", 0.0) >= 0.65)
        top_picks = [c for c in candidates if c.get("signal_type") == "BUY"][:6] if can_open else []

        if not top_picks:
            msg = (
                f"### 💎 A股收盘多因子智能选股雷达 ({date_str})\n\n"
                f"> **大盘宏观定调**：{macro['regime']} (情绪温度计: {macro['temp_score']}分 · {macro['cycle_phase']})\n"
                f"> **建议风险敞口**：{int(macro['target_exposure']*100)}% | 指导: {macro['action_guide']}\n\n"
                f"🛡️ **筛选结论**：\n"
                f"- 今日没有通过当前市场过滤、技术规则和财务数据检查的新增候选。数据不完整时系统会暂停新增仓位。\n"
                f"- 该过滤不能保证避免亏损，也不表示现有持仓需要买入或卖出。系统没有生成委托。"
            )
            title = f"💎 收盘多因子筛选: 今日无新增候选 ({date_str})"
            notification_manager.send_markdown(title, msg, broadcast_groups=True, module_filter="push_screener")
            return

        lines = [
            f"### 💎 A股收盘多因子智能精选标的 ({date_str})\n",
            f"> 宏观大盘: **{macro['regime']}** (建议敞口: {int(macro['target_exposure']*100)}%) | 策略核心: **RSRS 2.0 + Alpha158 + CYQ筹码 + 财报排雷**\n",
            f"| 标的名称 | 代码 | 行情参考价 | 规则评分 | 修正RSRS | 筹码指标 | 财报数据状态 | 模型止损 / 目标参考 |",
            f"| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |"
        ]

        for c in top_picks:
            levels = c.get("trade_levels", {})
            fin = c.get("financial", {})
            rf = fin.get("red_flags", {})
            fin_persona = fin.get('persona', '主板标的')
            fin_tag = f"{fin_persona} · {rf.get('risk_level') or '未知'}"
            stop_str = f"{levels.get('initial_stop', '-')}"
            target_str = f"{levels.get('target_price', '-')}"

            lines.append(
                f"| **{c.get('name')}** | `{c.get('symbol')}` | **{c.get('price'):.2f}** | "
                f"⭐ **{c.get('score', 0):.1f}** | `{c.get('rsrs_norm', 0):.2f}` | "
                f"{c.get('control_status', '-')} | {fin_tag} | "
                f"`{stop_str}` / `{target_str}` |"
            )

        lines.append("\n#### 🔬 核心量化逻辑与深度财报体检亮点：")
        for idx, c in enumerate(top_picks[:3], 1):
            fin = c.get("financial", {})
            dp = fin.get("dupont", {})
            gw = fin.get("growth", {})
            f_score = fin.get("piotroski_f_score", {}).get("score")
            if f_score is None:
                f_score = "不可用"
            roe_text = f"{dp['roe']:.1f}%" if isinstance(dp.get('roe'), (int, float)) else "未知"
            growth_value = gw.get('deduct_profit_yoy')
            growth_text = f"{growth_value:+.1f}%" if isinstance(growth_value, (int, float)) else "未知"
            fin_highlights = f"杜邦ROE {roe_text} | 扣非增速 {growth_text} | 可用F-Score字段 {f_score}"
            reasons_str = "；".join(c.get("reasons", [])[:2])
            lines.append(f"{idx}. **{c.get('name')}** (`{c.get('symbol')}`): {reasons_str} (📊 财报: {fin_highlights})")

        lines.append(
            "\n以上为模型研究候选，止损和目标仅作规则参考，不是可成交价格或仓位建议。系统不会提交委托；请核对实际行情、账户持仓、可卖数量和交易状态。"
        )

        title = f"💎 收盘多因子主板精选: {top_picks[0].get('name')}等{len(top_picks)}只 ({date_str})"
        notification_manager.send_markdown(title, "\n".join(lines), broadcast_groups=True, module_filter="push_screener")

    def run_tail230_scan_and_notify(self):
        """尾盘 14:32 触发『2.30 突袭战法』主板精选标的扫描与推送"""
        if not load_config().get("tail230_auto_scan_enabled", True):
            return
        now = datetime.now()
        if now.weekday() >= 5:
            return
        from core.sentiment_radar import sentiment_radar
        market = sentiment_radar.analyze_market_regime()
        if not market.get("data_available") or market.get("target_exposure", 0.0) < 0.65:
            print("[Tail230] 市场状态未确认多头或指数数据不可用，跳过隔夜策略扫描。")
            return
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 启动尾盘 2:30 战法主板精选标的扫描与推送...")
        candidates = tail_strategy_230.scan_today_candidates(limit=8, force_refresh=True)
        date_str = now.strftime('%Y-%m-%d')
        if not candidates:
            msg = (
                f"### ⚡ 今日尾盘日线规则筛选 ({date_str})\n\n"
                f"> 当前日线行情未筛出候选。该筛选不能还原14:30成交、集合竞价队列或次日盘中路径。\n\n"
                f"本系统没有生成委托建议；请以实际行情、持仓和风险限制为准。"
            )
            notification_manager.send_markdown("⚡ 2:30尾盘主板突袭雷达", msg, broadcast_groups=True, module_filter="push_tail230")
            return

        lines = [
            f"### ⚡ 尾盘日线规则候选研究摘要 ({date_str})\n",
            "> 以下结果来自日线/行情代理规则，不代表14:30时点信号、实际成交、次日收益或投资建议。\n",
            f"| 股票名称 | 代码 | 当前行情参考 | 涨跌幅 | 量比 | 模型目标参考价 | 模型止损参考价 | 规则分 |",
            f"| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |"
        ]
        for c in candidates[:6]:
            vol_ratio = c.get('vol_ratio')
            vol_ratio_text = f"{vol_ratio:.2f}倍" if vol_ratio is not None else "未知"
            lines.append(
                f"| **{c.get('name')}** | `{c.get('symbol')}` | **{c.get('price'):.2f}** | "
                f"**+{c.get('change_pct'):.2f}%** | {vol_ratio_text} | "
                f"`{c.get('target_sell_price', 0):.2f}` | `{c.get('stop_loss_price', 0):.2f}` | "
                f"⭐ {c.get('score', 0):.1f} |"
            )

        lines.append("\n#### 📋 核心选股理由与技术特征：")
        for idx, c in enumerate(candidates[:3], 1):
            reasons_str = "；".join(c.get("reasons", []))
            lines.append(f"{idx}. **{c.get('name')}** (`{c.get('symbol')}`): {reasons_str}")

        lines.append(
            "\n模型价位仅用于研究展示，不是可执行委托；日线数据无法证明目标价或止损价曾可成交。\n"
            "未录入用户确认的实际买入成交前，系统不会把候选加入持仓监控或计算账户收益。"
        )

        title = f"⚡ 2:30尾盘主板突袭精选: {candidates[0].get('name')}等{len(candidates)}只"
        notification_manager.send_markdown(title, "\n".join(lines), broadcast_groups=True, module_filter="push_tail230")

        # Research candidates are not user positions and are never added to the fill ledger.

    def run_morning_auction_snapshot(self):
        """早盘 09:25:05 集合竞价撮合结束，执行官方开盘定价秒级感知与 2.30 战法高开止盈先手挂单"""
        now = datetime.now()
        if now.weekday() >= 5:
            return
        cfg = load_config()
        if not cfg.get("auction_snapshot_enabled", True):
            return

        print(f"[{datetime.now().strftime('%H:%M:%S')}] 触发 09:25:05 集合竞价秒级开盘定价感知扫描...")
        # 强制立即执行一次自选股与持仓巡检，利用 09:25 官方撮合价提前锁定止盈/止损点位
        self.run_intraday_check(force=True)

    def run_tail230_morning_plan(self):
        """早盘汇总用户确认买入记录的行情参考，不生成委托或成交。"""
        now = datetime.now()
        if now.weekday() >= 5:
            return
        cfg = load_config()
        if not cfg.get("tail230_morning_check_enabled", True):
            return

        plans = tail_trade_tracker.check_morning_plan()
        if not plans:
            return

        print(f"[{datetime.now().strftime('%H:%M:%S')}] 触发尾盘策略实际成交记录行情提醒 ({len(plans)} 条)...")
        lines = [
            f"### 🌅 尾盘策略实际成交记录行情提醒 ({now.strftime('%Y-%m-%d 09:28')})\n",
            "> 以下记录由用户确认买入成交后建立；表内价位是模型观察线，通知不代表委托或成交。\n",
            f"| 股票名称 | 代码 | 已记录买入成交价 | 当前行情参考 | 价格变化 | 模型目标参考价 | 模型止损参考价 |",
            f"| :--- | :--- | :--- | :--- | :--- | :--- | :--- |"
        ]
        for p in plans:
            lines.append(
                f"| **{p.get('name')}** | `{p.get('symbol')}` | {p.get('buy_price'):.2f}元 | "
                f"**{p.get('open_price'):.2f}元** | **{p.get('open_change_pct'):+.2f}%** | "
                f"`{p.get('target_sell_price'):.2f}` | `{p.get('stop_loss_price'):.2f}` |"
            )

        lines.append(
            "\n行情触及模型价位时系统只发送提醒。请核对实际可卖数量、涨跌停/停牌状态和委托成交；本程序不会自动下单或将提醒记为实际平仓。"
        )

        title = f"🌅 尾盘策略行情提醒: {plans[0].get('name')}等{len(plans)}条"
        notification_manager.send_markdown(title, "\n".join(lines), broadcast_groups=True, module_filter="push_tail230")

    def run_eod_t0_check(self):
        """尾盘（14:48）检查未对冲做T头寸，发出强制平仓/底仓对冲抉择预警"""
        now = datetime.now()
        if now.weekday() >= 5:
            return
        cfg = load_config()
        if not cfg.get("t0_eod_force_close_enabled", True):
            return

        print(f"[{datetime.now().strftime('%H:%M:%S')}] 启动尾盘未闭环做T头寸强制平仓/底仓抉择审查...")
        signals = t0_smart_engine.check_eod_unclosed_positions()
        if signals:
            notification_manager.send_signals_batch(signals)

    def run_limit_up_intraday_scan(self):
        """盘中涨停候选日线筛选与可选研究摘要推送。"""
        if not self.is_trading_time():
            return

        cfg = load_config()
        if not cfg.get("limit_up_auto_notify_enabled", True):
            return

        from core.sentiment_radar import sentiment_radar
        market = sentiment_radar.analyze_market_regime()
        if not market.get("data_available") or market.get("target_exposure", 0.0) < 0.65:
            return

        min_score = float(cfg.get("limit_up_min_hardness", 80.0))
        top_n = int(cfg.get("limit_up_auto_track_top_n", 2))

        try:
            from core.limit_up_engine import limit_up_engine
            candidates = limit_up_engine.scan_today_candidates(limit=15)
            if not candidates:
                return

            qualified = [c for c in candidates if float(c.get("hardness_score", 0)) >= min_score][:top_n]
            if not qualified:
                return

            print(f"[{datetime.now().strftime('%H:%M:%S')}] 触发盘中游资打板量化监控，精选出 {len(qualified)} 只优质涨停突破标的...")

            # Record candidates for daily-bar follow-up; this is not a fill ledger.
            try:
                limit_up_engine.record_board_hitting_trades(qualified, top_n=top_n)
            except Exception as e:
                print(f"[LimitUp] 自动记账异常: {e}")

            # Send research summaries (force=False keeps each symbol to one alert per day).
            pushed_count = 0
            for c in qualified:
                res = limit_up_engine.send_limit_up_signal(c, force=False)
                if res.get("res", {}).get("dingtalk") or res.get("res", {}).get("wechat"):
                    pushed_count += 1

            if pushed_count > 0:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 成功向推送通道发送 {pushed_count} 条打板量化操盘卡片。")
        except Exception as e:
            print(f"[Scheduler 2.0] 盘中打板监控异常: {e}")

    def run_post_market_review(self):
        """执行盘后 15:10 全景复盘与参数自进化（按分组定制报告内容与模块后精准推送）"""
        now = datetime.now()
        if now.weekday() >= 5:
            return
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 启动收盘全景复盘与自动调优任务...")
        res = review_engine_v2.run_daily_review(auto_optimize=True)
        date_str = res.get("date", datetime.now().strftime("%Y-%m-%d"))

        # 1. 针对各个开启了 push_review 的分组，生成并推送专属定制复盘
        from core.t0_group_manager import t0_group_manager
        from notifier.dingtalk import DingTalkNotifier
        
        groups = t0_group_manager.get_groups(include_stocks=True)
        pushed_group_webhooks = set()

        for grp in groups:
            if not grp.get("enabled", True):
                continue
            if not grp.get("push_review", True):
                continue

            webhook = grp.get("dingtalk_webhook", "").strip()
            secret = grp.get("dingtalk_secret", "").strip()
            if not webhook:
                continue

            group_name = grp.get("name", "未命名组")
            group_md = review_engine_v2.build_group_review_report(grp, res)
            group_title = f"📊 A股收盘复盘 - 【{group_name}】 ({date_str})"

            try:
                notifier = DingTalkNotifier(webhook, secret)
                notifier.send_markdown(group_title, group_md)
                pushed_group_webhooks.add(webhook)
                print(f"[PostReview] 成功推送定制复盘至分组: {group_name}")
            except Exception as e:
                print(f"[PostReview] 推送定制复盘至分组 {group_name} 失败: {e}")

        # 2. 推送全局通用复盘至微信(若启用)以及尚未接收到的全局钉钉配置
        full_report = res.get("report_md", "")
        global_title = f"📊 A股收盘全景复盘与持仓体检 ({date_str})"

        cfg = load_config()
        global_webhook = cfg.get("dingtalk_webhook", "").strip()

        # 如果全局 webhook 已经作为某个启用 push_review 的分组发送过定制复盘，则无需重复发送全局复盘
        if global_webhook and global_webhook not in pushed_group_webhooks and cfg.get("dingtalk_enabled", True):
            try:
                notifier = DingTalkNotifier(global_webhook, cfg.get("dingtalk_secret", ""))
                notifier.send_markdown(global_title, full_report)
            except Exception as e:
                print(f"[PostReview] 推送全局钉钉复盘失败: {e}")

        # 微信通知 (若启用)
        if cfg.get("wechat_enabled"):
            try:
                from notifier.wechat import WeChatNotifier
                wechat_notifier = WeChatNotifier(cfg.get("wechat_corp_id"), cfg.get("wechat_agent_id"), cfg.get("wechat_secret"))
                wechat_notifier.send_markdown(global_title, full_report)
            except Exception as e:
                print(f"[PostReview] 推送微信复盘失败: {e}")

    def start(self):
        if self.is_running:
            return
        cfg = load_config()
        interval = cfg.get("scan_interval_minutes", 5)

        # 1. 盘中巡检引擎 (底层以 1 分钟基础心跳驱动，结合时段自适应动态提频至 60 秒或常态平稳巡检)
        self.scheduler.add_job(
            self.run_intraday_check,
            "interval",
            minutes=1,
            id="intraday_check",
            replace_existing=True
        )

        # 2. 早盘 09:25:05 集合竞价官方开盘定价秒级感知任务
        self.scheduler.add_job(
            self.run_morning_auction_snapshot,
            "cron",
            day_of_week="mon-fri",
            hour=9,
            minute=25,
            second=5,
            id="morning_auction_snapshot",
            replace_existing=True
        )

        # 3. 次日早盘行情参考提醒（只针对用户确认的实际买入记录）
        self.scheduler.add_job(
            self.run_tail230_morning_plan,
            "cron",
            day_of_week="mon-fri",
            hour=9,
            minute=28,
            id="tail230_morning_plan",
            replace_existing=True
        )

        # 3. 尾盘 14:32 『2.30 突袭战法』主板精选标的扫描与推送
        self.scheduler.add_job(
            self.run_tail230_scan_and_notify,
            "cron",
            day_of_week="mon-fri",
            hour=14,
            minute=32,
            id="tail230_scan_and_notify",
            replace_existing=True
        )

        # 4. 盘中游资打板扫描与精选通知 (默认每 10 分钟自动扫描全市场主板涨停板并推送)
        lu_interval = cfg.get("limit_up_scan_interval_minutes", 10)
        self.scheduler.add_job(
            self.run_limit_up_intraday_scan,
            "interval",
            minutes=lu_interval,
            id="limit_up_intraday_scan",
            replace_existing=True
        )

        # 5. 尾盘 14:48 做T未对冲强制平仓与底仓抉择检查
        self.scheduler.add_job(
            self.run_eod_t0_check,
            "cron",
            day_of_week="mon-fri",
            hour=14,
            minute=48,
            id="eod_t0_check",
            replace_existing=True
        )

        # 4. 盘后 15:10 全景复盘
        self.scheduler.add_job(
            self.run_post_market_review,
            "cron",
            day_of_week="mon-fri",
            hour=15,
            minute=10,
            id="post_market_review",
            replace_existing=True
        )

        # 5. 盘后 15:30 全市场智能选股与通知推送
        scan_time = cfg.get("market_close_scan_time", "15:30")
        try:
            scan_hour, scan_minute = [int(x) for x in scan_time.split(":")]
        except Exception:
            scan_hour, scan_minute = 15, 30

        self.scheduler.add_job(
            lambda: self.run_market_scan_async(limit=150, notify=True),
            "cron",
            day_of_week="mon-fri",
            hour=scan_hour,
            minute=scan_minute,
            id="post_market_scan",
            replace_existing=True
        )

        self.scheduler.start()
        self.is_running = True
        # 针对 Windows 用户：启动系统级防休眠锁 (允许屏幕熄灭省电，但绝不冻结后台调度与推送)
        set_windows_sleep_prevention(True)
        print("[Scheduler 2.0] 自动化后台调度引擎已就绪启动 (Windows 交易时段防休眠保护已激活)。")

        # 触发开机自检与错失任务智能补跑
        self.check_and_backfill_missed_tasks()

    def check_and_backfill_missed_tasks(self):
        """
        开机自检与错失任务智能补跑引擎：
        当用户因忘记开机、休眠或隔断时间重新启动系统时，自动执行健康自检与数据对齐：
        1. 自动调用 tail_trade_tracker 与 limit_up_engine 进行断档历史订单回溯核对与结算；
        2. 若当前已过 15:10，检查今日（或最近已收盘交易日）是否已产出收盘复盘报告；若缺失，后台静默补跑；
        3. 若当前已过 15:30，检查最新全市场多因子选股池是否为空；若为空，后台静默执行补选；
        4. 全程后台线程异步运行，零阻塞 Web 服务启动。
        """
        def _backfill_worker():
            time.sleep(3.0)  # 等待主应用初始化完成
            now = datetime.now()
            today_str = now.strftime("%Y-%m-%d")
            cur_hm = now.strftime("%H:%M")

            print(f"[Scheduler 2.0] 启动开机自检与断档恢复机制 (当前系统时间: {now.strftime('%Y-%m-%d %H:%M:%S')})...")

            # 1. 自动对齐和回溯平仓停机期间过期的 2.30 尾盘订单与打板流水
            try:
                from core.strategy_tail_230 import tail_trade_tracker
                reconciled = tail_trade_tracker.reconcile_and_clean_expired_positions()
                if reconciled:
                    print(f"[Scheduler 2.0] 成功自动回溯补结 {len(reconciled)} 笔离线期间已过期的 2.30 战法头寸。")
            except Exception as e:
                print(f"[Scheduler 2.0] 自检回溯 2.30 头寸异常: {e}")

            try:
                from core.limit_up_engine import limit_up_engine
                lu_stats = limit_up_engine.review_and_update_trades()
                if lu_stats.get("settled_count", 0) > 0:
                    print(f"[Scheduler 2.0] 成功自动回溯核算 {lu_stats['settled_count']} 笔离线打板交易 T+1 表现。")
            except Exception as e:
                print(f"[Scheduler 2.0] 自检核算打板交易异常: {e}")

            # 2. 检查最近收盘交易日的复盘报告与选股池
            need_review = False
            is_weekday = now.weekday() < 5
            is_post_close = cur_hm >= "15:10"

            if (is_weekday and is_post_close) or (not is_weekday):
                has_today_review = False
                if REVIEW_FILE.exists():
                    try:
                        with open(REVIEW_FILE, "r", encoding="utf-8") as f:
                            hist = json.load(f)
                            if hist and isinstance(hist, list):
                                dates = [x.get("date") for x in hist]
                                if is_weekday and today_str in dates:
                                    has_today_review = True
                                elif not is_weekday and len(dates) > 0:
                                    has_today_review = True
                    except Exception:
                        has_today_review = False

                if not has_today_review:
                    need_review = True

            if need_review:
                print(f"[Scheduler 2.0] 检测到收盘量化复盘报告缺失 (因日间未运行)，正在后台自动补跑...")
                try:
                    review_engine_v2.run_daily_review(auto_optimize=True)
                    print(f"[Scheduler 2.0] 收盘复盘报告与参数自迭代已成功补跑完成！")
                except Exception as e:
                    print(f"[Scheduler 2.0] 补跑收盘复盘异常: {e}")

            # 3. 检查全市场主板选股池是否需要补跑
            if (is_weekday and cur_hm >= "15:30") or (not is_weekday):
                if not self.latest_scan_results:
                    print(f"[Scheduler 2.0] 全市场多因子选股池为空，正在后台自动执行盘后补选...")
                    self.run_market_scan_async(limit=120, notify=False)

            print(f"[Scheduler 2.0] 开机自检与断档恢复就绪，所有量化数据与参数已对齐。")

        t = threading.Thread(target=_backfill_worker, daemon=True)
        t.start()

    def stop(self):
        if self.is_running:
            set_windows_sleep_prevention(False)
            self.scheduler.shutdown(wait=False)
            self.is_running = False

quant_scheduler2 = QuantScheduler2()
