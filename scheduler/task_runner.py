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
from config import load_config, WATCHLIST_FILE
from core.data_engine import data_engine
from core.multi_factor_strategy import multi_factor_strategy
from core.t0_smart_engine import t0_smart_engine
from core.review_engine_v2 import review_engine_v2
from core.strategy_tail_230 import tail_strategy_230, tail_trade_tracker
from notifier.manager import notification_manager

def set_windows_sleep_prevention(enable: bool = True):
    """
    通过 Windows 核心原生 API (SetThreadExecutionState) 防止操作系统自动睡眠/休眠
    - 允许屏幕正常熄灭以省电和保护隐私
    - 但强制锁定 CPU 与后台线程，保证定时任务在交易日 100% 准时执行
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

    def run_intraday_check(self):
        """盘中自选股巡检：检查波段信号与日内做T机会"""
        if not self.is_trading_time():
            return

        watchlist = self.get_watchlist()
        active_tail_positions = tail_trade_tracker.get_active_positions()
        if not watchlist and not active_tail_positions:
            return

        print(f"[{datetime.now().strftime('%H:%M:%S')}] 启动盘中巡检 (自选 {len(watchlist)} 只, 2.30昨日持仓 {len(active_tail_positions)} 只)...")
        symbols = [item['symbol'] for item in watchlist if item.get('symbol')]
        tail_syms = [p['symbol'] for p in active_tail_positions if p.get('symbol')]
        all_symbols = list(set(symbols + tail_syms))
        quotes = data_engine.get_realtime_quotes(all_symbols)

        for item in watchlist:
            sym = item.get('symbol')
            name = item.get('name', sym)
            cost = float(item.get('holding_cost') or item.get('entry_price') or 0.0)
            shares = int(item.get('holding_shares') or item.get('shares') or 100)

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
                            "严禁死扛或盲目补仓，必须严格执行量化纪律，建议立即挂单卖出平仓止损避险！"
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

            # 3. 检查日内做T机会
            t0_res = t0_smart_engine.analyze_t0(
                symbol=sym,
                name=name,
                current_price=cur_p,
                daily_kline=df_kline,
                holding_cost=cost,
                holding_shares=shares
            )
            act = t0_res.get("signal_action")
            if act in ["BUY_T", "SELL_T", "CLOSE_T", "STOP_T"]:
                sig_payload = {
                    "symbol": sym,
                    "name": name,
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "price": cur_p,
                    "signal_type": act,
                    "signal_desc": t0_res.get("signal_desc", ""),
                    "score": 88,
                    "reasons": t0_res.get("reasons", []),
                    "t0_direction": t0_res.get("t0_direction", ""),
                    "close_action": t0_res.get("close_action", ""),
                    "holding_shares": t0_res.get("shares", shares),
                    "holding_cost": t0_res.get("entry_price", cur_p),
                    "trade_levels": {
                        "entry_price": t0_res.get("entry_price", cur_p),
                        "initial_stop": t0_res.get("stop_price", cur_p * 0.985),
                        "target_price": t0_res.get("target_price", cur_p * 1.015)
                    }
                }
                notification_manager.send_signal(sig_payload)

        # 4. 尾盘时段 (14:45-14:55) 自动检查未闭环做T头寸，发出强制平仓/对冲决策
        cur_hm = datetime.now().strftime("%H:%M")
        if "14:45" <= cur_hm <= "14:55":
            eod_sigs = t0_smart_engine.check_eod_unclosed_positions()
            for es in eod_sigs:
                notification_manager.send_signal(es)

        # 5. 检查 2.30 战法昨日买入标的的次日卖出与平仓预警 (冲高止盈 / 破位止损 / 早盘衰竭 / 尾盘清仓)
        tail_exits = tail_trade_tracker.check_next_day_exits(quotes)
        for tx in tail_exits:
            notification_manager.send_signal(tx)

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

        # 过滤低流动性标的
        valid_stocks = [s for s in stocks if float(s.get("turnover", 0)) >= min_turnover][:total]
        self.scan_status["total"] = len(valid_stocks)

        def eval_stock(s):
            sym = s.get("symbol")
            name = s.get("name")
            try:
                df = data_engine.get_daily_kline(sym, count=180)
                if df is not None and len(df) >= 30:
                    res = multi_factor_strategy.analyze(sym, name, df)
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

        # 筛选符合多因子高确定性条件的标的 (BUY 信号 或 综合评分 >= 65)
        top_picks = [c for c in candidates if c.get("signal_type") == "BUY" or c.get("score", 0) >= 65][:6]

        if not top_picks:
            msg = (
                f"### 💎 A股收盘多因子智能选股雷达 ({date_str})\n\n"
                f"> **大盘宏观定调**：{macro['regime']} (情绪温度计: {macro['temp_score']}分 · {macro['cycle_phase']})\n"
                f"> **建议风险敞口**：{int(macro['target_exposure']*100)}% | 指导: {macro['action_guide']}\n\n"
                f"🛡️ **量化风控选股结论**：\n"
                f"- 今日全市场扫描评估，未发现满足『偏度修正 RSRS 超标 + CYQ 主力筹码集中起爆 + 财报排雷无瑕疵』的高确定性主板买入标的。\n"
                f"- 在当前大盘弱势压制环境下，系统根据自适应风控纪律已自动调高安全防守边际，严格杜绝假突破诱多与逆势接飞刀。\n"
                f"- **操盘指导**：保持空仓或轻仓防守观望，耐心等待大盘情绪回暖与右侧多因子共振买点确立。"
            )
            title = f"💎 收盘多因子选股: 今日严控风控·空仓观望 ({date_str})"
            notification_manager.send_markdown(title, msg)
            return

        lines = [
            f"### 💎 A股收盘多因子智能精选标的 ({date_str})\n",
            f"> 宏观大盘: **{macro['regime']}** (建议敞口: {int(macro['target_exposure']*100)}%) | 策略核心: **RSRS 2.0 + Alpha158 + CYQ筹码 + 财报排雷**\n",
            f"| 标的名称 | 代码 | 最新现价 | 综合评分 | 修正RSRS | 主力控盘状态 | 财报画像/排雷 | 建议止损 / 止盈 |",
            f"| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |"
        ]

        for c in top_picks:
            levels = c.get("trade_levels", {})
            fin = c.get("financial", {})
            rf = fin.get("red_flags", {})
            fin_persona = fin.get('persona', '主板标的')
            fin_tag = f"{fin_persona} · {rf.get('risk_level', '安全')}"
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
            f_score = fin.get("piotroski_f_score", {}).get("score", "-")
            fin_highlights = f"杜邦ROE {dp.get('roe', '-')}% | 扣非增速 {gw.get('deduct_profit_yoy', '-')}% | 皮氏F-Score {f_score}/9分"
            reasons_str = "；".join(c.get("reasons", [])[:2])
            lines.append(f"{idx}. **{c.get('name')}** (`{c.get('symbol')}`): {reasons_str} (📊 财报: {fin_highlights})")

        lines.append(
            "\n💡 **次日实操交易指引**：\n"
            "- 以上标的为多因子共振+财报排雷筛选之高景气/起爆标的，次日可结合早盘集合竞价表现择机介入。\n"
            "- 严格遵守建议止损保护位，单笔仓位建议控制在 20%~25% 以内，杜绝单一重仓盲赌。\n"
            "- *注：策略严格限定沪深主板高流动性标的，已自动剔除创业板/科创板/ST风险股票。*"
        )

        title = f"💎 收盘多因子主板精选: {top_picks[0].get('name')}等{len(top_picks)}只 ({date_str})"
        notification_manager.send_markdown(title, "\n".join(lines))

    def run_tail230_scan_and_notify(self):
        """尾盘 14:32 触发『2.30 突袭战法』主板精选标的扫描与推送"""
        now = datetime.now()
        if now.weekday() >= 5:
            return
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 启动尾盘 2:30 战法主板精选标的扫描与推送...")
        candidates = tail_strategy_230.scan_today_candidates(limit=8, force_refresh=True)
        date_str = now.strftime('%Y-%m-%d')
        if not candidates:
            msg = (
                f"### ⚡ 今日 2:30 尾盘主板突袭战法精选 ({date_str})\n\n"
                f"> **监测提示**：今日 14:30 市场情绪或量能未达严格主力抢筹标准（要求沪深主板、日内涨幅+2%~+5.5%、量比>1.25、站稳MA5/MA20、小箱体突破形态）。\n\n"
                f"🛡️ **策略风控提示**：今日无高确定性突袭标的入选，建议尾盘保持空仓观望或仅做好持仓对冲，规避次日低开风险。"
            )
            notification_manager.send_markdown("⚡ 2:30尾盘主板突袭雷达", msg)
            return

        lines = [
            f"### ⚡ 今日 2:30 尾盘主板突袭精选标的 ({date_str} 14:32)\n",
            f"> 🎯 **核心策略逻辑**：锁定主板尾盘温和放量、均线多头站稳、涨幅在+2.0%~+5.5%区间的主力增量抢筹标的，次日早盘冲高极速兑现离场。\n",
            f"| 股票名称 | 代码 | 最新价 | 涨跌幅 | 量比 | 目标止盈(+2.5%) | 防守止损(-1.8%) | 策略评分 |",
            f"| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |"
        ]
        for c in candidates[:6]:
            lines.append(
                f"| **{c.get('name')}** | `{c.get('symbol')}` | **{c.get('price'):.2f}** | "
                f"**+{c.get('change_pct'):.2f}%** | {c.get('vol_ratio', 1.0):.2f}倍 | "
                f"`{c.get('target_sell_price', 0):.2f}` | `{c.get('stop_loss_price', 0):.2f}` | "
                f"⭐ {c.get('score', 0):.1f} |"
            )

        lines.append("\n#### 📋 核心选股理由与技术特征：")
        for idx, c in enumerate(candidates[:3], 1):
            reasons_str = "；".join(c.get("reasons", []))
            lines.append(f"{idx}. **{c.get('name')}** (`{c.get('symbol')}`): {reasons_str}")

        lines.append(
            "\n💡 **实操交易指引**：\n"
            "- **买入窗口**：今日 **14:35 ~ 14:55** 尾盘分批挂单介入。\n"
            "- **次日兑现 (T+1)**：次日早盘 **09:30 ~ 09:45** 惯性冲高触及 +2.5% 或涨幅衰竭时果断止盈；若早盘破位跌破 -1.8% 止损位坚决执行纪律止损。\n"
            "- *注：策略严格限定沪深主板，已自动剔除创业板/科创板/ST风险股票。*"
        )

        title = f"⚡ 2:30尾盘主板突袭精选: {candidates[0].get('name')}等{len(candidates)}只"
        notification_manager.send_markdown(title, "\n".join(lines))

        # 自动将推荐标的纳入次日卖出与平仓跟踪引擎 (默认前3只)
        cfg = load_config()
        track_limit = cfg.get("tail230_auto_track_top_n", 3)
        tail_trade_tracker.record_recommendations(candidates, limit=track_limit)

    def run_tail230_morning_plan(self):
        """早盘 09:28 集合竞价结束，触发『2.30 战法昨日持仓早盘操盘作战计划』预警"""
        now = datetime.now()
        if now.weekday() >= 5:
            return
        cfg = load_config()
        if not cfg.get("tail230_morning_check_enabled", True):
            return

        plans = tail_trade_tracker.check_morning_plan()
        if not plans:
            return

        print(f"[{datetime.now().strftime('%H:%M:%S')}] 触发 2.30 战法次日早盘操盘计划推送 ({len(plans)} 只持仓)...")
        lines = [
            f"### 🌅 2.30 战法·今日早盘操盘计划与盯盘预警 ({now.strftime('%Y-%m-%d 09:28')})\n",
            f"> 🎯 **核心战法纪律**：昨日尾盘入场标的，今早集合竞价已定调。重点盯盘 **09:30 ~ 09:45** 冲高兑现窗口，触达目标果断挂单卖出！\n",
            f"| 股票名称 | 代码 | 昨日买入价 | 集合竞价开盘 | 开盘溢价 | 止盈目标(+2.5%) | 防守止损(-1.8%) |",
            f"| :--- | :--- | :--- | :--- | :--- | :--- | :--- |"
        ]
        for p in plans:
            lines.append(
                f"| **{p.get('name')}** | `{p.get('symbol')}` | {p.get('buy_price'):.2f}元 | "
                f"**{p.get('open_price'):.2f}元** | **{p.get('open_change_pct'):+.2f}%** | "
                f"`{p.get('target_sell_price'):.2f}` | `{p.get('stop_loss_price'):.2f}` |"
            )

        lines.append(
            "\n💡 **今早极速离场实战指令**：\n"
            "- **冲高止盈**：开盘后脉冲上攻触及 **+2.5% 止盈线** 时，立即市价全额卖出锁定利润！\n"
            "- **破位止损**：若直接低开破位跌破 **-1.8% 止损线**，坚决挂单止损，绝不恋战！\n"
            "- **动能衰竭时限**：若 **09:45** 前未达止盈线且上涨动能衰竭，建议分批择机离场，杜绝短线变套牢！"
        )

        title = f"🌅 2.30战法早盘操盘计划: {plans[0].get('name')}等{len(plans)}只"
        notification_manager.send_markdown(title, "\n".join(lines))

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
        for sig in signals:
            notification_manager.send_signal(sig)

    def run_post_market_review(self):
        """执行盘后 15:10 全景复盘与参数自进化（完整富文本推送到钉钉/微信，免网页端）"""
        now = datetime.now()
        if now.weekday() >= 5:
            return
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 启动收盘全景复盘与自动调优任务...")
        res = review_engine_v2.run_daily_review(auto_optimize=True)
        # 直接推送完整的富文本 Markdown 复盘长文报告，手机端直接阅读
        title = f"📊 A股收盘全景复盘与持仓体检 ({res.get('date')})"
        full_report = res.get("report_md", "")
        notification_manager.send_markdown(title, full_report)

    def start(self):
        if self.is_running:
            return
        cfg = load_config()
        interval = cfg.get("scan_interval_minutes", 5)

        # 1. 盘中巡检任务 (含自选股、持仓波段、日内做T与 2.30 次日卖出监控)
        self.scheduler.add_job(
            self.run_intraday_check,
            "interval",
            minutes=interval,
            id="intraday_check",
            replace_existing=True
        )

        # 2. 次日早盘 09:28 集合竞价 2.30 战法操盘作战计划预警
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

        # 3. 尾盘 14:48 做T未对冲强制平仓与底仓抉择检查
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

    def stop(self):
        if self.is_running:
            set_windows_sleep_prevention(False)
            self.scheduler.shutdown(wait=False)
            self.is_running = False

quant_scheduler2 = QuantScheduler2()
