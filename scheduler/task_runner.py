"""
自动化定时调度引擎 2.0 (QuantScheduler 2.0 - 高可用断点自愈版)
核心特性：
1. A股交易时间自适应感知 (工作日 9:25~11:30, 13:00~15:00)；
2. 盘中自选股与做T高频并发巡检 (常态60秒，开盘09:30-10:00与尾盘14:40-15:00自适应提频至30秒)；
3. 盘中游资【弱转强专精打板】高灵敏巡检 (早盘09:30-10:35每2分钟，其余时段每3分钟)；
4. 尾盘 14:32 【2.30 分时承接+筹码RSRS战法】自动扫描推送；
5. 随时启停与断档自愈：所有执行水位写入原子持久化文件 scheduler_state.json，
   重启时比对交易日历与时间窗——安全窗口内自动精准补跑，过期买入窗口绝不补发过时信号，历史T+1台账自动回溯对齐，杜绝重复推送。
"""
import time
import threading
import sys
import ctypes
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from config import load_config, WATCHLIST_FILE, REVIEW_FILE, SCHEDULER_STATE_FILE
from core.logger import get_logger
from core.data_engine import data_engine
from core.multi_factor_strategy import multi_factor_strategy
from core.t0_smart_engine import t0_smart_engine
from core.review_engine_v2 import review_engine_v2
from core.strategy_tail_230 import tail_strategy_230, tail_trade_tracker
from core.portfolio_risk import portfolio_risk
from core.persistence import SchedulerCheckpointStore, safe_read_json
from notifier.manager import notification_manager

logger = get_logger("scheduler")


def set_windows_sleep_prevention(enable: bool = True):
    """
    通过 Windows 核心原生 API (SetThreadExecutionState) 防止操作系统自动睡眠/休眠
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
    def __init__(self, state_file=None):
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
        self._last_limit_up_check: float = 0.0
        self._last_alert_flush: float = 0.0
        self._op_lock = threading.RLock()
        self._exec_lock = threading.RLock()
        self._bg_executor: Optional[ThreadPoolExecutor] = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="qt2-bg"
        )
        self.checkpoint = SchedulerCheckpointStore(state_file or SCHEDULER_STATE_FILE)

    def _submit_bg(self, fn, *args, **kwargs):
        with self._exec_lock:
            if self._bg_executor is None:
                self._bg_executor = ThreadPoolExecutor(
                    max_workers=2, thread_name_prefix="qt2-bg"
                )
            return self._bg_executor.submit(fn, *args, **kwargs)

    @staticmethod
    def is_trading_time(now_dt: Optional[datetime] = None) -> bool:
        """判断当前是否为 A 股交易时间 (周一至周五 09:25-11:30, 13:00-15:00)"""
        now = now_dt or datetime.now()
        if now.weekday() >= 5:
            return False
        curr_time = now.strftime("%H:%M")
        morning = "09:25" <= curr_time <= "11:30"
        afternoon = "13:00" <= curr_time <= "15:00"
        return morning or afternoon

    def get_watchlist(self) -> List[Dict[str, Any]]:
        """获取用户自选股清单"""
        data = safe_read_json(WATCHLIST_FILE, [])
        return data if isinstance(data, list) else []

    def _flush_pending_alerts_throttled(self, now_dt: Optional[datetime] = None):
        """按 30 秒节奏安全冲洗持久化延迟队列，盘中及收盘后 16:00 前均可执行，避免 14:52~14:55 限流警报跨收盘丢失。"""
        now = now_dt or datetime.now()
        if now.weekday() >= 5:
            return
        cur_hm = now.strftime("%H:%M")
        if not ("09:20" <= cur_hm <= "16:00"):
            return
        now_ts = time.time()
        if (now_ts - self._last_alert_flush) < 28.0:
            return
        self._last_alert_flush = now_ts
        try:
            notification_manager.flush_pending_critical_alerts()
        except Exception as e:
            logger.warning(f"[Scheduler 2.0] 补发延迟关键警报异常: {e}")

    def run_intraday_check(self, force: bool = False):
        """盘中自选股巡检：检查波段信号、日内做T与 2.30 昨日持仓次日分时止盈止损"""
        now = datetime.now()
        self._flush_pending_alerts_throttled(now)

        if not self.is_trading_time(now):
            return

        curr_hm = now.strftime("%H:%M")
        cfg = load_config()

        if not force:
            if "09:30" <= curr_hm <= "10:00" and cfg.get("opening_high_freq_enabled", True):
                step_interval = 30
            elif "14:40" <= curr_hm <= "15:00" and cfg.get("closing_high_freq_enabled", True):
                step_interval = 30
            elif "09:25" <= curr_hm < "09:30":
                step_interval = 60
            else:
                step_interval = int(cfg.get("scan_interval_seconds", cfg.get("scan_interval_minutes", 1) * 60))

            if (time.time() - self._last_intraday_check) < (step_interval - 5):
                return

        self._last_intraday_check = time.time()
        self.checkpoint.record_heartbeat(trading_date=now.strftime("%Y-%m-%d"), now_dt=now)

        watchlist = self.get_watchlist()
        active_tail_positions = tail_trade_tracker.get_active_positions()
        if not watchlist and not active_tail_positions:
            return

        logger.info(f"[{datetime.now().strftime('%H:%M:%S')}] 启动盘中巡检 (自选 {len(watchlist)} 只, 2.30昨日持仓 {len(active_tail_positions)} 只)...")
        symbols = [item["symbol"] for item in watchlist if item.get("symbol")]
        tail_syms = [p["symbol"] for p in active_tail_positions if p.get("symbol")]
        all_symbols = list(set(symbols + tail_syms))
        quotes = data_engine.get_realtime_quotes(all_symbols)

        t0_market_exposure = None
        market_regime_bull = False
        try:
            from core.sentiment_radar import sentiment_radar
            market_state = sentiment_radar.analyze_market_regime()
            if market_state.get("data_available"):
                if market_state.get("target_exposure") is not None:
                    t0_market_exposure = float(market_state["target_exposure"])
                market_regime_bull = bool(market_state.get("target_exposure", 0.0) >= 0.65)
        except Exception as e:
            logger.warning(f"[Scheduler 2.0] 读取市场情绪雷达失败: {e}")
            t0_market_exposure = None
            market_regime_bull = False

        for item in watchlist:
            try:
                sym = item.get("symbol")
                name = item.get("name", sym)
                cost = float(item.get("holding_cost") or item.get("entry_price") or 0.0)
                shares = int(item.get("holding_shares") or item.get("shares") or 0)
                available_shares = portfolio_risk.resolve_available_shares(item)

                q = quotes.get(sym)
                if not q:
                    continue

                cur_p = q.get("price", 0.0)
                if cur_p <= 0:
                    continue

                pnl_pct = round((cur_p - cost) / cost * 100.0, 2) if cost > 0 else 0.0
                pnl_amt = round((cur_p - cost) * shares, 2) if cost > 0 else 0.0

                df_kline = data_engine.get_daily_kline(sym, count=120)
                analysis_res = None
                if df_kline is not None and len(df_kline) >= 30:
                    analysis_res = multi_factor_strategy.analyze(
                        sym, name, df_kline, market_regime_bull=market_regime_bull
                    )

                # 1. 持仓硬止损检查
                if cost > 0 and shares > 0:
                    hard_stop = cfg.get("hard_stop_loss_pct", 8.0)
                    control_st = analysis_res.get("control_status", "") if analysis_res else ""

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
                                "模型风险阈值已触发；系统未提交委托，请先核对账户与持仓。"
                            ],
                            "trade_levels": {
                                "entry_price": cur_p,
                                "suggested_sell_price": cur_p,
                                "initial_stop": round(cost * (1.0 - hard_stop / 100.0), 2)
                            }
                        }
                        notification_manager.send_signal(force_signal)

                # 2. 波段策略多因子信号
                if analysis_res:
                    sig_type = analysis_res.get("signal_type")
                    if sig_type in ["BUY", "SELL"]:
                        if sig_type == "SELL" and cost > 0:
                            analysis_res["holding_cost"] = cost
                            analysis_res["holding_shares"] = shares
                            analysis_res["pnl_pct"] = pnl_pct
                        notification_manager.send_signal(analysis_res)

                # 3. 日内做T机会检查
                if (cfg.get("t0_monitor_enabled", True)
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
                logger.warning(f"[Scheduler 2.0] 巡检个股 {item.get('symbol', '')} 异常: {e}")

        # 4. 尾盘时段 (14:45-14:55) 检查未闭环做T头寸
        cur_hm = datetime.now().strftime("%H:%M")
        if cfg.get("t0_eod_force_close_enabled", True) and "14:45" <= cur_hm <= "14:55":
            eod_sigs = t0_smart_engine.check_eod_unclosed_positions()
            if eod_sigs:
                notification_manager.send_signals_batch(eod_sigs)

        # 5. 2.30 战法实际买入持仓次日分时止盈止损预警
        tail_exits = tail_trade_tracker.check_next_day_exits(quotes)
        if tail_exits:
            notification_manager.send_signals_batch(tail_exits)

    def run_market_scan_async(self, limit: int = 100, notify: bool = True):
        """异步执行全市场主板智能精选"""
        if self.scan_status["is_scanning"]:
            return
        self._submit_bg(self._scan_market_worker, limit, notify)

    def _scan_market_worker(self, limit: int = 100, notify: bool = True):
        self.scan_status["is_scanning"] = True
        self.scan_status["progress"] = 0
        self.scan_status["message"] = "正在拉取A股主板标的池..."

        try:
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
                except Exception as e:
                    logger.debug(f"[Scheduler 2.0] 全市场扫描个股 {sym} 异常: {e}")
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
            self.scan_status["message"] = f"扫描完成，筛选出 {len(candidates)} 只优质标的"
            self.scan_status["last_scan_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            today_str = datetime.now().strftime("%Y-%m-%d")
            self.checkpoint.mark_task_completed("post_scan", today_str, status="DONE", note=f"{len(candidates)} picks")

            if notify:
                self._notify_market_scan_results(candidates)
        except Exception as e:
            logger.exception(f"[Scheduler 2.0] 全市场扫描任务异常: {e}")
            self.scan_status["message"] = f"扫描异常: {e}"
        finally:
            self.scan_status["is_scanning"] = False

    def _notify_market_scan_results(self, candidates: List[Dict[str, Any]]):
        """将收盘后多因子主板精选标的（含深度财报体检）幂等推送至钉钉/微信"""
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        from core.sentiment_radar import sentiment_radar
        macro = sentiment_radar.analyze_market_regime()

        top_picks = [c for c in candidates if c.get("signal_type") == "BUY" or c.get("score", 0) >= 65][:6]

        if not top_picks:
            msg = (
                f"### 💎 A股收盘多因子智能选股雷达 ({date_str})\n\n"
                f"> **大盘宏观定调**：{macro['regime']} (情绪温度计: {macro['temp_score']}分 · {macro['cycle_phase']})\n"
                f"> **建议风险敞口**：{int(macro['target_exposure']*100)}% | 指导: {macro['action_guide']}\n\n"
                f"🛡️ **筛选结论**：\n"
                f"- 今日没有通过当前市场过滤、技术规则和财务数据检查的新增候选。数据不完整时系统会暂停新增仓位。\n"
                f"- 系统没有生成委托。"
            )
            title = f"💎 收盘多因子筛选: 今日无新增候选 ({date_str})"
            notification_manager.send_markdown(
                title, msg, broadcast_groups=True, module_filter="push_screener",
                dedup_key=f"post_scan_md:{date_str}"
            )
            return

        lines = [
            f"### 💎 A股收盘多因子智能精选标的 ({date_str})\n",
            f"> 宏观大盘: **{macro['regime']}** (建议敞口: {int(macro['target_exposure']*100)}%) | 策略核心: **RSRS 2.0 + Alpha158 + CYQ筹码 + 财报排雷**\n",
            "| 标的名称 | 代码 | 行情参考价 | 规则评分 | 修正RSRS | 筹码指标 | 财报数据状态 | 模型止损 / 目标参考 |",
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |"
        ]

        for c in top_picks:
            levels = c.get("trade_levels", {})
            fin = c.get("financial", {})
            rf = fin.get("red_flags", {})
            fin_persona = fin.get("persona", "主板标的")
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
            roe_text = f"{dp['roe']:.1f}%" if isinstance(dp.get("roe"), (int, float)) else "未知"
            growth_value = gw.get("deduct_profit_yoy")
            growth_text = f"{growth_value:+.1f}%" if isinstance(growth_value, (int, float)) else "未知"
            fin_highlights = f"杜邦ROE {roe_text} | 扣非增速 {growth_text} | 可用F-Score字段 {f_score}"
            reasons_str = "；".join(c.get("reasons", [])[:2])
            lines.append(f"{idx}. **{c.get('name')}** (`{c.get('symbol')}`): {reasons_str} (📊 财报: {fin_highlights})")

        lines.append(
            "\n以上为模型研究候选，止损和目标仅作规则参考，不是可成交价格或仓位建议。系统不会提交委托。"
        )

        title = f"💎 收盘多因子主板精选: {top_picks[0].get('name')}等{len(top_picks)}只 ({date_str})"
        notification_manager.send_markdown(
            title, "\n".join(lines), broadcast_groups=True, module_filter="push_screener",
            dedup_key=f"post_scan_md:{date_str}"
        )

        for c in top_picks:
            sig_entry = {
                "symbol": c.get("symbol"),
                "name": c.get("name"),
                "date": date_str,
                "price": c.get("price"),
                "signal_type": "BUY",
                "source": "multi_factor_screener",
                "score": c.get("score"),
                "trade_levels": c.get("trade_levels", {}),
                "timestamp": now.strftime("%Y-%m-%d %H:%M:%S")
            }
            notification_manager._record_signal_history(sig_entry)

    def run_tail230_scan_and_notify(self, force: bool = False):
        """尾盘 14:32 触发『2.30 分时承接+筹码RSRS』主板精选扫描与幂等推送"""
        if not load_config().get("tail230_auto_scan_enabled", True):
            return
        now = datetime.now()
        if now.weekday() >= 5 and not force:
            return
        date_str = now.strftime("%Y-%m-%d")
        cur_hm = now.strftime("%H:%M")

        # 严格时间窗与幂等锁：超过 14:45 不补发过时尾盘买入信号（防止停机重启误推送）
        with self._op_lock:
            if not force:
                if cur_hm < "14:25" or cur_hm > "14:45":
                    return
                if self.checkpoint.is_task_completed("tail230_scan", date_str):
                    return

        from core.sentiment_radar import sentiment_radar
        market = sentiment_radar.analyze_market_regime()
        gate = tail_strategy_230.evaluate_market_gate(market)
        if not gate["allowed"]:
            logger.info(f"[Tail230] {gate['reason']}")
            self.checkpoint.mark_task_completed("tail230_scan", date_str, status="BLOCKED_REGIME", note=gate["reason"])
            return

        logger.info(f"[{datetime.now().strftime('%H:%M:%S')}] 启动尾盘 2:30 战法(分时VWAP承接+筹码RSRS)扫描与推送 [{gate['mode']}]...")
        candidates = tail_strategy_230.scan_today_candidates(limit=8, force_refresh=True)
        if not candidates:
            msg = (
                f"### ⚡ 今日 2.30 尾盘分时微结构精选 ({date_str})\n\n"
                f"> 大盘环境：**{gate['mode']}** | 筛选结果：今日盘中未发现同时满足 VWAP 承接、尾盘量能脉冲与 CYQ 筹码保护的标的，保持空仓防御。\n\n"
                f"本系统未生成委托。"
            )
            notification_manager.send_markdown(
                f"⚡ 2:30尾盘雷达: 今日空仓观望 ({date_str})",
                msg,
                broadcast_groups=True,
                module_filter="push_tail230",
                dedup_key=f"tail230_scan_md:{date_str}",
                force=force
            )
            self.checkpoint.mark_task_completed("tail230_scan", date_str, status="DONE_EMPTY")
            return

        lines = [
            f"### ⚡ 2.30 尾盘分时承接与筹码共振精选 ({date_str})\n",
            f"> 大盘定调：**{gate['mode']}** ({gate['reason']}) | 策略核心：**14:30分时VWAP承接 + 尾盘抢筹脉冲 + CYQ筹码 + RSRS**\n",
            "| 股票名称 | 代码 | 现价 | 涨幅 | 折算量比 | VWAP站稳率 | 筹码状态 | 目标 / 止损 | 综合分 |",
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |"
        ]
        for c in candidates[:6]:
            vol_ratio = c.get("vol_ratio")
            vol_ratio_text = f"{vol_ratio:.2f}倍" if vol_ratio is not None else "未知"
            v_hr = c.get("vwap_hold_ratio")
            v_hr_text = f"{v_hr*100:.0f}%" if v_hr is not None else "-"
            lines.append(
                f"| **{c.get('name')}** | `{c.get('symbol')}` | **{c.get('price'):.2f}** | "
                f"**+{c.get('change_pct'):.2f}%** | {vol_ratio_text} | {v_hr_text} | "
                f"{c.get('control_status', '-')} | "
                f"`{c.get('target_sell_price', 0):.2f}` / `{c.get('stop_loss_price', 0):.2f}` | "
                f"⭐ {c.get('score', 0):.1f} |"
            )

        lines.append("\n#### 📋 核心分时承接与筹码逻辑：")
        for idx, c in enumerate(candidates[:3], 1):
            reasons_str = "；".join(c.get("reasons", [])[:3])
            lines.append(f"{idx}. **{c.get('name')}** (`{c.get('symbol')}`): {reasons_str}")

        lines.append(
            "\n次日兑现纪律：早盘冲高钝化跌破分时均价线(VWAP)或触及目标即落袋；若低开破止损线及时止损。"
        )

        title = f"⚡ 2:30尾盘主板精选: {candidates[0].get('name')}等{len(candidates)}只 ({date_str})"
        notification_manager.send_markdown(
            title, "\n".join(lines), broadcast_groups=True, module_filter="push_tail230",
            dedup_key=f"tail230_scan_md:{date_str}", force=force
        )
        self.checkpoint.mark_task_completed("tail230_scan", date_str, status="DONE", note=f"{len(candidates)} candidates")

        for c in candidates:
            sig_entry = {
                "symbol": c.get("symbol"),
                "name": c.get("name"),
                "date": date_str,
                "price": c.get("price"),
                "signal_type": "TAIL_BUY",
                "source": "tail230_strategy",
                "score": c.get("score"),
                "trade_levels": {
                    "initial_stop": c.get("stop_loss_price"),
                    "target_price": c.get("target_sell_price")
                },
                "timestamp": now.strftime("%Y-%m-%d %H:%M:%S")
            }
            notification_manager._record_signal_history(sig_entry)

    def run_morning_auction_snapshot(self):
        """早盘 09:25:05 集合竞价官方开盘定价秒级感知"""
        now = datetime.now()
        if now.weekday() >= 5:
            return
        cfg = load_config()
        if not cfg.get("auction_snapshot_enabled", True):
            return

        today_str = now.strftime("%Y-%m-%d")
        logger.info(f"[{now.strftime('%H:%M:%S')}] 触发 09:25:05 集合竞价秒级开盘定价感知扫描...")
        self.run_intraday_check(force=True)
        self.checkpoint.mark_task_completed("morning_auction", today_str, status="DONE")

    def run_tail230_morning_plan(self, force: bool = False):
        """早盘 09:28 汇总用户确认买入记录的行情参考（带跨重启幂等保护）"""
        now = datetime.now()
        if now.weekday() >= 5 and not force:
            return
        cfg = load_config()
        if not cfg.get("tail230_morning_check_enabled", True):
            return
        date_str = now.strftime("%Y-%m-%d")
        cur_hm = now.strftime("%H:%M")

        with self._op_lock:
            if not force:
                if cur_hm < "09:25" or cur_hm > "09:50":
                    return
                if self.checkpoint.is_task_completed("tail230_morning_plan", date_str):
                    return

        plans = tail_trade_tracker.check_morning_plan()
        self.checkpoint.mark_task_completed("tail230_morning_plan", date_str, status="DONE", note=f"{len(plans)} plans")
        if not plans:
            return

        logger.info(f"[{datetime.now().strftime('%H:%M:%S')}] 触发尾盘策略实际成交记录行情提醒 ({len(plans)} 条)...")
        lines = [
            f"### 🌅 尾盘策略实际成交记录行情提醒 ({now.strftime('%Y-%m-%d 09:28')})\n",
            "> 以下记录由用户确认买入成交后建立；开盘若显著低开破位请严格执行止损，冲高回落破VWAP即锁利。\n",
            "| 股票名称 | 代码 | 已记录买入成交价 | 当前行情参考 | 价格变化 | 模型目标参考价 | 模型止损参考价 |",
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |"
        ]
        for p in plans:
            lines.append(
                f"| **{p.get('name')}** | `{p.get('symbol')}` | {p.get('buy_price'):.2f}元 | "
                f"**{p.get('open_price'):.2f}元** | **{p.get('open_change_pct'):+.2f}%** | "
                f"`{p.get('target_sell_price'):.2f}` | `{p.get('stop_loss_price'):.2f}` |"
            )

        title = f"🌅 尾盘策略行情提醒: {plans[0].get('name')}等{len(plans)}条"
        notification_manager.send_markdown(
            title, "\n".join(lines), broadcast_groups=True, module_filter="push_tail230",
            dedup_key=f"tail230_morning_plan:{date_str}", force=force
        )

    def run_eod_t0_check(self, force: bool = False):
        """尾盘（14:48）检查未对冲做T头寸，发出强制平仓/底仓对冲预警"""
        now = datetime.now()
        if now.weekday() >= 5 and not force:
            return
        cfg = load_config()
        if not cfg.get("t0_eod_force_close_enabled", True):
            return
        date_str = now.strftime("%Y-%m-%d")

        logger.info(f"[{now.strftime('%H:%M:%S')}] 启动尾盘未闭环做T头寸强制平仓/底仓抉择审查...")
        signals = t0_smart_engine.check_eod_unclosed_positions()
        if signals:
            notification_manager.send_signals_batch(signals)
        self.checkpoint.mark_task_completed("eod_t0_check", date_str, status="DONE", note=f"{len(signals or [])} signals")

    def run_limit_up_intraday_scan(self, force: bool = False):
        """
        盘中【弱转强专精打板】动态扫描与幂等推送：
        - 早盘 09:30~10:35 黄金弱转强窗口默认每 2 分钟巡检；其余时段每 3 分钟巡检；
        - 14:52 之后禁止产生新打板信号。
        """
        if not self.is_trading_time():
            return

        now = datetime.now()
        cur_hm = now.strftime("%H:%M")
        if cur_hm > "14:52" and not force:
            return

        cfg = load_config()
        if not cfg.get("limit_up_auto_notify_enabled", True):
            return

        if not force:
            if "09:30" <= cur_hm <= "10:35":
                step_min = int(cfg.get("limit_up_wzs_prime_interval_minutes", 2))
            else:
                step_min = int(cfg.get("limit_up_scan_interval_minutes", 3))
            if (time.time() - self._last_limit_up_check) < max(30, step_min * 60 - 5):
                return

        self._last_limit_up_check = time.time()

        from core.sentiment_radar import sentiment_radar
        market = sentiment_radar.analyze_market_regime()
        if not market.get("data_available"):
            return
        if float(market.get("temp_score", 0.0)) < 25.0 and float(market.get("index_pct", 0.0)) < -0.5:
            return

        min_score = float(cfg.get("limit_up_min_hardness", 75.0))
        top_n = int(cfg.get("limit_up_auto_track_top_n", 3))

        try:
            from core.limit_up_engine import limit_up_engine
            candidates = limit_up_engine.scan_today_candidates(limit=15)
            if not candidates:
                return

            qualified = [c for c in candidates if float(c.get("hardness_score", 0)) >= min_score][:top_n]
            if not qualified:
                return

            logger.info(f"[{datetime.now().strftime('%H:%M:%S')}] 触发盘中【弱转强专精打板】监控，精选出 {len(qualified)} 只标的...")

            try:
                limit_up_engine.record_board_hitting_trades(qualified, top_n=top_n)
            except Exception as e:
                logger.warning(f"[LimitUp] 自动记账异常: {e}")

            pushed_count = 0
            for c in qualified:
                res = limit_up_engine.send_limit_up_signal(c, force=False)
                if res.get("res", {}).get("dingtalk") or res.get("res", {}).get("wechat"):
                    pushed_count += 1

            if pushed_count > 0:
                logger.info(f"[{datetime.now().strftime('%H:%M:%S')}] 成功发送 {pushed_count} 条弱转强打板量化卡片。")
        except Exception as e:
            logger.warning(f"[Scheduler 2.0] 盘中弱转强打板监控异常: {e}")

    def run_post_market_review(self, notify: bool = True, force: bool = False):
        """执行盘后 15:10 全景复盘与参数自进化（支持跨重启幂等不重推）"""
        now = datetime.now()
        if now.weekday() >= 5 and not force:
            return
        logger.info(f"[{now.strftime('%H:%M:%S')}] 启动收盘全景复盘与自动调优任务...")
        res = review_engine_v2.run_daily_review(auto_optimize=True)
        date_str = res.get("date", now.strftime("%Y-%m-%d"))
        self.checkpoint.mark_task_completed("post_review", date_str, status="DONE")

        if not notify:
            return
        if not force and self.checkpoint.is_task_completed("post_review_notify", date_str):
            logger.info(f"[PostReview] {date_str} 复盘报告已推送过，跳过重复推送。")
            return

        from core.t0_group_manager import t0_group_manager
        from notifier.dingtalk import DingTalkNotifier

        groups = t0_group_manager.get_groups(include_stocks=True)
        pushed_group_webhooks = set()
        any_sent = False

        for grp in groups:
            if not grp.get("enabled", True) or not grp.get("push_review", True):
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
                if notifier.send_markdown(group_title, group_md):
                    pushed_group_webhooks.add(webhook)
                    any_sent = True
                    logger.info(f"[PostReview] 成功推送定制复盘至分组: {group_name}")
                else:
                    logger.warning(f"[PostReview] 推送定制复盘至分组 {group_name} 失败 (网络/响应异常)")
            except Exception as e:
                logger.warning(f"[PostReview] 推送定制复盘至分组 {group_name} 异常: {e}")

        full_report = res.get("report_md", "")
        global_title = f"📊 A股收盘全景复盘与持仓体检 ({date_str})"
        cfg = load_config()
        global_webhook = cfg.get("dingtalk_webhook", "").strip()

        if global_webhook and global_webhook not in pushed_group_webhooks and cfg.get("dingtalk_enabled", False):
            try:
                notifier = DingTalkNotifier(global_webhook, cfg.get("dingtalk_secret", ""))
                if notifier.send_markdown(global_title, full_report):
                    any_sent = True
            except Exception as e:
                logger.warning(f"[PostReview] 推送全局钉钉复盘失败: {e}")

        if cfg.get("wechat_enabled"):
            try:
                from notifier.wechat import WeChatNotifier
                wechat_notifier = WeChatNotifier(
                    provider=cfg.get("wechat_provider", "pushplus"),
                    token=cfg.get("wechat_token", "")
                )
                if wechat_notifier.send_markdown(global_title, full_report):
                    any_sent = True
            except Exception as e:
                logger.warning(f"[PostReview] 推送微信复盘失败: {e}")

        if any_sent:
            self.checkpoint.mark_task_completed("post_review_notify", date_str, status="DONE")

    def start(self):
        if self.is_running:
            return
        cfg = load_config()

        # 1. 盘中巡检引擎 (底层以 30 秒高敏心跳驱动，由内部动态适配 30s/60s 频率)
        self.scheduler.add_job(
            self.run_intraday_check,
            "interval",
            seconds=30,
            id="intraday_check",
            coalesce=True,
            max_instances=1,
            replace_existing=True
        )

        # 2. 早盘 09:25:05 集合竞价官方开盘定价秒级感知
        self.scheduler.add_job(
            self.run_morning_auction_snapshot,
            "cron",
            day_of_week="mon-fri",
            hour=9,
            minute=25,
            second=5,
            misfire_grace_time=120,
            id="morning_auction_snapshot",
            replace_existing=True
        )

        # 3. 次日早盘 09:28 尾盘持仓行情提醒
        self.scheduler.add_job(
            self.run_tail230_morning_plan,
            "cron",
            day_of_week="mon-fri",
            hour=9,
            minute=28,
            misfire_grace_time=300,
            id="tail230_morning_plan",
            replace_existing=True
        )

        # 4. 尾盘 14:32 『2.30 战法』主板精选扫描与推送
        self.scheduler.add_job(
            self.run_tail230_scan_and_notify,
            "cron",
            day_of_week="mon-fri",
            hour=14,
            minute=32,
            misfire_grace_time=300,
            id="tail230_scan_and_notify",
            replace_existing=True
        )

        # 5. 盘中弱转强打板引擎 (每 1 分钟高频检测，内部在 09:30-10:35 自动按 2 分钟提频)
        self.scheduler.add_job(
            self.run_limit_up_intraday_scan,
            "interval",
            minutes=1,
            id="limit_up_intraday_scan",
            coalesce=True,
            max_instances=1,
            replace_existing=True
        )

        # 6. 尾盘 14:48 做T未对冲强制平仓检查
        self.scheduler.add_job(
            self.run_eod_t0_check,
            "cron",
            day_of_week="mon-fri",
            hour=14,
            minute=48,
            misfire_grace_time=240,
            id="eod_t0_check",
            replace_existing=True
        )

        # 7. 盘后 15:10 全景复盘
        self.scheduler.add_job(
            self.run_post_market_review,
            "cron",
            day_of_week="mon-fri",
            hour=15,
            minute=10,
            misfire_grace_time=600,
            id="post_market_review",
            replace_existing=True
        )

        # 8. 盘后 15:30 全市场智能选股
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
            misfire_grace_time=600,
            id="post_market_scan",
            replace_existing=True
        )

        self.scheduler.start()
        self.is_running = True
        set_windows_sleep_prevention(True)
        logger.info("[Scheduler 2.0] 自动化后台调度引擎已就绪启动 (30s高敏心跳 + 断档自愈恢复已激活)。")

        self.check_and_backfill_missed_tasks()

    def check_and_backfill_missed_tasks(
        self,
        current_dt: Optional[datetime] = None,
        wait_seconds: float = 2.0,
        sync: bool = False
    ):
        """
        高可用开机自检与断档智能恢复引擎：
        1. 永远优先自动对齐停机期间过期的 2.30 尾盘头寸与弱转强打板 T+1 结算（无任何过期误报警）；
        2. 比对交易日历与当前时段：
           - 若重启落入安全交易窗口（09:26~09:50 早盘计划、09:30~14:50 盘中巡检/弱转强、14:25~14:42 尾盘 2.30、14:45~14:56 做T强平）且今日尚未执行 -> 立即补跑；
           - 若买入窗口已过（如 14:45 后才启动）-> 严格标记 SKIPPED_EXPIRED，绝不补发过时买入信号；
        3. 若已收盘或周末重启且最近收盘交易日缺少复盘报告或选股池 -> 后台静默补全，确保数据与参数闭环不断档。
        """
        def _backfill_worker():
            if wait_seconds > 0:
                time.sleep(wait_seconds)
            now = current_dt or datetime.now()
            today_str = now.strftime("%Y-%m-%d")
            cur_hm = now.strftime("%H:%M")

            last_hb = self.checkpoint.get_last_heartbeat()
            gap_desc = f"上次运行水位: {last_hb.strftime('%Y-%m-%d %H:%M:%S')}" if last_hb else "首次启动/无历史水位"
            logger.info(f"[Scheduler 2.0] 启动断档自检与恢复 ({now.strftime('%Y-%m-%d %H:%M:%S')}, {gap_desc})...")

            # 1. 历史台账离线补结算（永远安全执行）
            try:
                reconciled = tail_trade_tracker.reconcile_and_clean_expired_positions(current_dt=now)
                if reconciled:
                    logger.info(f"[Scheduler 2.0] 成功自动回溯补结 {len(reconciled)} 笔离线期间过期的 2.30 头寸。")
            except Exception as e:
                logger.warning(f"[Scheduler 2.0] 自检回溯 2.30 头寸异常: {e}")

            try:
                from core.limit_up_engine import limit_up_engine
                lu_stats = limit_up_engine.review_and_update_trades(review_date=today_str)
                if lu_stats.get("settled_count", 0) > 0:
                    logger.info(f"[Scheduler 2.0] 成功自动回溯核算 {lu_stats['settled_count']} 笔离线打板交易 T+1 表现。")
            except Exception as e:
                logger.warning(f"[Scheduler 2.0] 自检核算打板交易异常: {e}")

            # 2. 基于真实交易日历校准“最近已收盘交易日”
            trading_dates = data_engine.get_trading_dates(count=30)
            is_weekday = now.weekday() < 5
            is_today_trading = is_weekday and (not trading_dates or today_str in trading_dates or cur_hm < "15:10")

            # 确定最近需要完成复盘的交易日
            completed_dates = [d for d in trading_dates if d < today_str or (d == today_str and cur_hm >= "15:10")]
            target_review_date = completed_dates[-1] if completed_dates else (today_str if (is_weekday and cur_hm >= "15:10") else "")

            # 3. 盘中安全时间窗内精准补跑（仅在当前真实处于该窗口时补跑，绝不向盘后补发过时买入信号）
            if is_today_trading and self.is_trading_time(now):
                # 早盘 09:26~09:50 补发次日盯盘计划
                if "09:26" <= cur_hm <= "09:50" and not self.checkpoint.is_task_completed("tail230_morning_plan", today_str):
                    self.run_tail230_morning_plan()
                elif cur_hm > "09:50" and not self.checkpoint.is_task_completed("tail230_morning_plan", today_str):
                    self.checkpoint.mark_task_completed("tail230_morning_plan", today_str, status="SKIPPED_EXPIRED", now_dt=now)

                # 尾盘 14:30~14:42 补跑 2.30 选股雷达；过时仅标记跳过
                if "14:30" <= cur_hm <= "14:42" and not self.checkpoint.is_task_completed("tail230_scan", today_str):
                    self.run_tail230_scan_and_notify()
                elif cur_hm > "14:42" and not self.checkpoint.is_task_completed("tail230_scan", today_str):
                    self.checkpoint.mark_task_completed("tail230_scan", today_str, status="SKIPPED_EXPIRED", now_dt=now)

                # 尾盘 14:45~14:56 补跑做T未平仓强平检查
                if "14:45" <= cur_hm <= "14:56" and not self.checkpoint.is_task_completed("eod_t0_check", today_str):
                    self.run_eod_t0_check()

                # 若处于盘中交易时段且未过期，立即拉起一次做T巡检与弱转强扫板
                if "09:30" <= cur_hm <= "14:52":
                    try:
                        self.run_intraday_check(force=True)
                    except Exception as e:
                        logger.warning(f"[Scheduler 2.0] 重启立即做T巡检异常: {e}")
                    try:
                        self.run_limit_up_intraday_scan(force=True)
                    except Exception as e:
                        logger.warning(f"[Scheduler 2.0] 重启立即弱转强扫板异常: {e}")
            elif is_weekday and cur_hm > "15:00":
                # 盘后启动时标记当日过时盘中买入/尾盘检查窗口为已跳过
                if not self.checkpoint.is_task_completed("tail230_scan", today_str):
                    self.checkpoint.mark_task_completed("tail230_scan", today_str, status="SKIPPED_EXPIRED", now_dt=now)
                if not self.checkpoint.is_task_completed("tail230_morning_plan", today_str):
                    self.checkpoint.mark_task_completed("tail230_morning_plan", today_str, status="SKIPPED_EXPIRED", now_dt=now)
                if not self.checkpoint.is_task_completed("eod_t0_check", today_str):
                    self.checkpoint.mark_task_completed("eod_t0_check", today_str, status="SKIPPED_EXPIRED", now_dt=now)

            # 4. 检查最近应复盘交易日是否缺失复盘与收盘选股池
            if target_review_date:
                hist = safe_read_json(REVIEW_FILE, [])
                dates_in_file = [x.get("date") for x in hist] if isinstance(hist, list) else []
                review_done = self.checkpoint.is_task_completed("post_review", target_review_date) or (target_review_date in dates_in_file)
                if not review_done:
                    logger.info(f"[Scheduler 2.0] 检测到 {target_review_date} 收盘复盘缺失，正在静默补跑...")
                    try:
                        rev_res = review_engine_v2.run_daily_review(auto_optimize=True, target_date=target_review_date)
                        self.checkpoint.mark_task_completed("post_review", rev_res.get("date", target_review_date), status="DONE_BACKFILL", now_dt=now)
                    except Exception as e:
                        logger.warning(f"[Scheduler 2.0] 补跑收盘复盘异常: {e}")

            if (is_weekday and cur_hm >= "15:30") or (not is_weekday):
                if not self.latest_scan_results and not sync:
                    logger.info("[Scheduler 2.0] 全市场多因子选股池为空，后台静默执行盘后补选...")
                    self.run_market_scan_async(limit=120, notify=False)

            self.checkpoint.record_heartbeat(trading_date=today_str, now_dt=now)
            logger.info("[Scheduler 2.0] 开机自检与断档恢复就绪，所有量化水位与台账已对齐。")

        if sync:
            _backfill_worker()
        else:
            self._submit_bg(_backfill_worker)

    def stop(self):
        if self.is_running:
            self.checkpoint.record_heartbeat()
            set_windows_sleep_prevention(False)
            self.scheduler.shutdown(wait=False)
            self.is_running = False
        with self._exec_lock:
            if self._bg_executor is not None:
                self._bg_executor.shutdown(wait=False, cancel_futures=True)
                self._bg_executor = None
        logger.info("[Scheduler 2.0] 自动化后台调度引擎与工作线程池已关闭。")


quant_scheduler2 = QuantScheduler2()
