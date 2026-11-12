"""A-share late-session heuristic candidate scanner and daily-bar proxy replay.

The inputs do not reconstruct 14:30 executions or next-session intraday paths.
The replay is for research only and does not establish executable performance.
"""
import math
import json
import threading
from pathlib import Path
import concurrent.futures
import numpy as np
import pandas as pd
from typing import Dict, Any, List, Optional
from datetime import datetime

from config import load_config, TAIL_TRADES_FILE
from core.data_engine import data_engine
from core.sentiment_radar import sentiment_radar

class TailStrategy230:
    def __init__(self):
        self._cached_backtest = None
        self._cache_time = None
        self._cached_candidates = None
        self._candidates_cache_time = None
        self._scan_lock = threading.Lock()
        self.reload_config()

    def reload_config(self):
        """从全局配置动态加载 2.30 战法参数 (支持热加载调优)"""
        cfg = load_config()
        self.min_pct = float(cfg.get("tail230_min_pct", 2.0))
        self.max_pct = float(cfg.get("tail230_max_pct", 5.5))
        self.min_vol_ratio = float(cfg.get("tail230_min_vol_ratio", 1.25))
        self.min_turnover = float(cfg.get("tail230_min_turnover", 120000000.0))
        self.target_profit_pct = float(cfg.get("tail230_target_profit_pct", 2.5))
        self.stop_loss_pct = float(cfg.get("tail230_stop_loss_pct", 1.8))
        self.backtest_universe_size = int(cfg.get("tail230_backtest_universe_size", 80))

    def _default_backtest_universe(self) -> List[str]:
        """从全市场主板按成交额取流动性居前的代表性样本作为回测标的池。"""
        try:
            stocks = data_engine.get_main_board_stock_list()
            elig = [
                s for s in stocks
                if data_engine.is_main_board_equity(s.get("symbol", ""))
                and float(s.get("turnover", 0) or 0) >= self.min_turnover
            ]
            elig.sort(key=lambda x: float(x.get("turnover", 0) or 0), reverse=True)
            symbols = [s["symbol"] for s in elig[:self.backtest_universe_size]]
            if symbols:
                return symbols
        except Exception:
            pass
        # 兜底：跨行业代表性大盘流动性标的
        return [
            "sh601138", "sh600519", "sh601899", "sh600036",
            "sz002475", "sh600900", "sz000333", "sh600276",
            "sz000651", "sh601318", "sz002594", "sh601668"
        ]

    def analyze_bar(self, df: pd.DataFrame, idx: int = -1) -> Optional[Dict[str, Any]]:
        """
        判断指定K线位置是否满足 2.30 尾盘选股条件
        """
        length = len(df)
        if idx < 0:
            idx = length + idx
        if idx < 20 or idx >= length:
            return None

        curr = df.iloc[idx]
        prev = df.iloc[idx - 1]

        close = float(curr['close'])
        prev_close = float(prev['close'])
        open_price = float(curr['open'])
        high = float(curr['high'])
        low = float(curr['low'])
        volume = float(curr['volume'])

        if prev_close <= 0:
            return None

        pct_change = round((close - prev_close) / prev_close * 100.0, 2)

        # 1. 涨幅区间过滤：2.0% ~ 5.5%
        if pct_change < self.min_pct or pct_change > self.max_pct:
            return None

        # 2. 阳线实体过滤 (收盘价需高于开盘价，且上影线不过长)
        if close <= open_price:
            return None
        upper_shadow = (high - close) / prev_close * 100.0
        body = (close - open_price) / prev_close * 100.0
        if upper_shadow > body * 0.8:  # 拒绝长上影线冲高回落
            return None

        # 3. 均线形态 (位于 MA5 与 MA20 之上)
        ma5 = float(df['close'].iloc[max(0, idx - 4):idx + 1].mean())
        ma20 = float(df['close'].iloc[max(0, idx - 19):idx + 1].mean())
        if close < ma5 or close < ma20:
            return None

        # 4. 放量特征 (成交量高于过去 5 日均量 1.2 倍)
        volume_window = pd.to_numeric(df['volume'].iloc[max(0, idx - 5):idx], errors='coerce')
        volume_window = volume_window.replace([np.inf, -np.inf], np.nan).dropna()
        if len(volume_window) < 5 or not np.isfinite(volume) or float(volume_window.mean()) <= 0:
            return None
        vol_ma5 = float(volume_window.mean())
        vol_ratio = round(volume / vol_ma5, 2)
        if vol_ratio < self.min_vol_ratio:
            return None

        # 5. 突破近期小箱体高点 (近 10 日)
        recent_high = float(df['high'].iloc[max(0, idx - 10):idx].max())
        is_breakout = close >= recent_high * 0.985

        # 综合打分 (0 ~ 100)
        score = 60.0
        score += min(vol_ratio * 10.0, 20.0)
        if is_breakout:
            score += 15.0
        if ma5 > ma20:
            score += 10.0
        score = min(score, 99.0)

        reasons = [
            f"尾盘涨幅健康({pct_change:+.2f}%)，处于2.0%~5.5%黄金冲高区间",
            f"量比放量明显({vol_ratio:.2f}倍)，显示尾盘增量资金主动抢筹",
            f"股价稳固站上MA5({ma5:.2f}元)与MA20({ma20:.2f}元)多头生命线"
        ]
        if is_breakout:
            reasons.append(f"有效突破近10日箱体高点({recent_high:.2f}元)，上攻阻力较小")

        return {
            "symbol": str(curr.get('symbol', '')),
            "date": str(curr.get('date', ''))[:10],
            "price": close,
            "change_pct": pct_change,
            "score": round(score, 1),
            "vol_ratio": vol_ratio,
            "ma5": round(ma5, 2),
            "ma20": round(ma20, 2),
            "reasons": reasons,
            "target_sell_price": round(close * (1.0 + self.target_profit_pct / 100.0), 2),
            "target_pct": self.target_profit_pct,
            "stop_loss_price": round(close * (1.0 - self.stop_loss_pct / 100.0), 2),
            "stop_pct": self.stop_loss_pct
        }

    def scan_today_candidates(self, limit: int = 40, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        今日全市场主板 2.30 战法标的实时扫描 (内置5分钟缓存防频繁网络穿透与防抖)
        """
        now = datetime.now()
        if not force_refresh and self._cached_candidates is not None and self._candidates_cache_time:
            if (now - self._candidates_cache_time).total_seconds() < 300:
                return self._cached_candidates[:limit]

        with self._scan_lock:
            # 双重检查锁，防止并发重复扫描
            now = datetime.now()
            if not force_refresh and self._cached_candidates is not None and self._candidates_cache_time:
                if (now - self._candidates_cache_time).total_seconds() < 300:
                    return self._cached_candidates[:limit]

            stocks = data_engine.get_main_board_stock_list()

            # 1. 黄金规则预先过滤：日内涨幅在 2.0% ~ 5.5% 且成交额 >= 1.2亿元
            matched = []
            for s in stocks:
                try:
                    chg = float(s.get("change_pct", 0))
                    to = float(s.get("turnover", 0))
                    if self.min_pct <= chg <= self.max_pct and to >= self.min_turnover:
                        matched.append(s)
                except (ValueError, TypeError):
                    continue

            # 按成交额降序排序，选取流动性与主力参与度最高的候选 (前25只极速评估)
            matched.sort(key=lambda x: float(x.get("turnover", 0)), reverse=True)
            target_stocks = matched[:25]

            def _check_one(s):
                sym = s["symbol"]
                name = s["name"]
                try:
                    df = data_engine.get_daily_kline(sym, count=40)
                    if df is not None and len(df) >= 20:
                        res = self.analyze_bar(df, idx=-1)
                        if res:
                            res["name"] = name
                            res["symbol"] = sym
                            return res
                except Exception:
                    pass
                return None

            candidates = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
                results = executor.map(_check_one, target_stocks)
                for r in results:
                    if r:
                        candidates.append(r)

            candidates.sort(key=lambda x: x["score"], reverse=True)
            self._cached_candidates = candidates
            self._candidates_cache_time = datetime.now()
            return candidates[:limit]

    def calculate_strategy_win_rate(
        self,
        sample_symbols: Optional[List[str]] = None,
        days: int = 150
    ) -> Dict[str, Any]:
        """
        用日线代理规则回放尾盘策略；日线无法还原14:30成交和次日早盘路径
        """
        # 代表性流动性主板样本池 (跨周期跨行业)；可用 sample_symbols 显式覆盖
        if not sample_symbols:
            sample_symbols = self._default_backtest_universe()
        sample_symbols = [sym for sym in sample_symbols if data_engine.is_main_board_equity(sym)]

        # 获取上证指数基准用于宏观多空分层统计
        sh_df = data_engine.get_daily_kline("sh000001", count=days + 60)
        sh_bull_dates = set()
        if sh_df is not None and len(sh_df) >= 20:
            sh_ma20 = sh_df['close'].rolling(20).mean()
            for idx, r in sh_df.iterrows():
                if not np.isnan(sh_ma20.iloc[idx]) and float(r['close']) >= float(sh_ma20.iloc[idx]):
                    sh_bull_dates.add(str(r['date'])[:10])

        total_trades = 0
        win_trades = 0
        loss_trades = 0
        total_pnl_pct = 0.0
        win_pnl_list = []
        loss_pnl_list = []
        morning_premiums = []
        trade_logs = []

        # 分层统计
        bull_trades = 0
        bull_wins = 0
        bear_trades = 0
        bear_wins = 0

        # Approximate two-way costs; this is not observed fill or slippage data.
        friction_pct = 0.20

        # 并发极速预拉取样本标的历史K线
        symbol_dfs = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            future_to_sym = {executor.submit(data_engine.get_daily_kline, sym, days + 30): sym for sym in sample_symbols}
            for f in concurrent.futures.as_completed(future_to_sym):
                s = future_to_sym[f]
                try:
                    df = f.result()
                    if df is not None and len(df) >= 40:
                        symbol_dfs[s] = df
                except Exception:
                    pass

        for sym, df in symbol_dfs.items():
            info = data_engine.resolve_symbol(sym)
            name = info.get("name", sym) if info else sym

            total_len = len(df)
            start_i = max(25, total_len - days)

            for i in range(start_i, total_len - 2):
                res = self.analyze_bar(df, idx=i)
                if not res:
                    continue

                # The signal is known at T close. Use T+1 open as a proxy entry,
                # then T+2 daily OHLC for exits so the model respects A-share T+1.
                signal_date = str(df.iloc[i]['date'])[:10]
                signal_close = float(df.iloc[i]['close'])
                entry_bar = df.iloc[i + 1]
                buy_date = str(entry_bar['date'])[:10]
                buy_price = float(entry_bar['open'])
                if buy_price <= 0:
                    continue
                # Approximate an unfillable one-price limit-up entry using a
                # 10% ordinary-board threshold; historical special limits vary.
                if np.isclose(float(entry_bar['high']), float(entry_bar['low']), atol=0.005) and buy_price >= signal_close * 1.095:
                    continue
                is_market_bull = (signal_date in sh_bull_dates)

                exit_bar = df.iloc[i + 2]
                next_date = str(exit_bar['date'])[:10]
                next_open = float(exit_bar['open'])
                next_high = float(exit_bar['high'])
                next_low = float(exit_bar['low'])
                next_close = float(exit_bar['close'])

                # Daily bars do not reveal whether the high occurred in the
                # 09:30-09:45 window; report the full exit-session high instead.
                exit_session_high_premium = (next_high - buy_price) / buy_price * 100.0
                morning_premiums.append(exit_session_high_premium)

                target_p = buy_price * (1.0 + self.target_profit_pct / 100.0)
                stop_p = buy_price * (1.0 - self.stop_loss_pct / 100.0)

                exit_price = next_close
                exit_reason = "次日平仓"
                is_win = False

                # Daily bars cannot reveal intraday event order. Use the open
                # for gap exits and assume stop first if both barriers are hit.
                if next_open >= target_p:
                    exit_price = next_open
                    exit_reason = "T+2开盘止盈代理"
                    is_win = True
                elif next_open <= stop_p:
                    exit_price = next_open
                    exit_reason = "T+2开盘止损代理"
                    is_win = False
                # 2. 盘中波动研判：防未来函数双触及保守检验
                elif next_high >= target_p and next_low <= stop_p:
                    exit_price = stop_p
                    exit_reason = "T+2双触及按止损保守估算"
                    is_win = False
                elif next_high >= target_p:
                    exit_price = target_p
                    exit_reason = "T+2触及目标位代理"
                    is_win = True
                elif next_low <= stop_p:
                    exit_price = stop_p
                    exit_reason = "T+2触及止损位代理"
                    is_win = False
                else:
                    # 3. 正常次日收盘平仓
                    exit_price = next_close
                    exit_reason = "T+2收盘退出代理"
                    is_win = (exit_price > buy_price)

                # 扣除摩擦后的净收益
                raw_return = (exit_price - buy_price) / buy_price * 100.0
                net_return = round(raw_return - friction_pct, 2)

                total_trades += 1
                total_pnl_pct += net_return

                if net_return > 0:
                    win_trades += 1
                    win_pnl_list.append(net_return)
                else:
                    loss_trades += 1
                    loss_pnl_list.append(net_return)

                if is_market_bull:
                    bull_trades += 1
                    if net_return > 0:
                        bull_wins += 1
                else:
                    bear_trades += 1
                    if net_return > 0:
                        bear_wins += 1

                trade_logs.append({
                    "symbol": sym,
                    "name": name,
                    "signal_date": signal_date,
                    "buy_date": buy_date,
                    "buy_price": buy_price,
                    "sell_date": next_date,
                    "sell_price": round(exit_price, 2),
                    "return_pct": net_return,
                    "exit_session_high_pct": round(exit_session_high_premium, 2),
                    "next_session_high_pct": round(exit_session_high_premium, 2),  # legacy API alias; full exit-session high
                    "morning_high_pct": round(exit_session_high_premium, 2),  # legacy API alias; not a morning-only value
                    "is_win": net_return > 0,
                    "market_regime": "多头/平衡市" if is_market_bull else "弱势杀跌市",
                    "reason": exit_reason
                })

        win_rate = round(win_trades / total_trades * 100.0, 1) if total_trades > 0 else None
        bull_win_rate = round(bull_wins / bull_trades * 100.0, 1) if bull_trades > 0 else None
        bear_win_rate = round(bear_wins / bear_trades * 100.0, 1) if bear_trades > 0 else None

        avg_win = round(float(np.mean(win_pnl_list)), 2) if win_pnl_list else None
        avg_loss = round(float(np.mean(loss_pnl_list)), 2) if loss_pnl_list else None
        avg_return = round(total_pnl_pct / total_trades, 2) if total_trades > 0 else None
        avg_morning_prem = round(float(np.mean(morning_premiums)), 2) if morning_premiums else None
        pl_ratio = round(abs(avg_win / avg_loss), 2) if avg_win is not None and avg_loss is not None and avg_loss != 0 else None

        # 按时间倒序保留近 30 笔交易明细
        trade_logs.sort(key=lambda x: x["buy_date"], reverse=True)

        result = {
            "strategy_name": "A股主板尾盘日线规则代理回放",
            "github_reference": "songxiaoyan/stock-picker & hyan1985/stock-predict",
            "backtest_days": days,
            "total_trades": total_trades,
            "win_trades": win_trades,
            "loss_trades": loss_trades,
            "win_rate": win_rate,
            "enhanced_win_rate": bull_win_rate,
            "bear_win_rate": bear_win_rate,
            "bull_trades": bull_trades,
            "bear_trades": bear_trades,
            "avg_return_pct": avg_return,
            "avg_win_pct": avg_win,
            "avg_loss_pct": avg_loss,
            "profit_loss_ratio": pl_ratio,
            "avg_exit_session_high_premium_pct": avg_morning_prem,
            "avg_next_session_high_premium_pct": avg_morning_prem,  # legacy API alias; full exit-session high
            "avg_morning_premium_pct": avg_morning_prem,   # legacy API alias; not a morning-only value
            "backtest_method": "daily_bar_proxy_signal_T_close_entry_T1_open_exit_T2_daily_OHLC",
            "execution_limitations": "T日收盘产生信号、T+1开盘作为代理买入价、T+2日线OHLC模拟卖出；不代表14:30成交或真实成交。日线不能还原盘中事件顺序、涨跌停排队或滑点，历史主板特殊涨跌幅状态未逐日还原；同日止盈止损双触及时按止损估算。",
            "recent_trades": trade_logs[:30],
            "rules_summary": [
                "主板合规：严格限定沪深主板，过滤ST/科创/创业板",
                "日线涨幅规则：+2.0% ~ +5.5% (日线无法校验14:30时点)",
                "放量抢筹：当日量比 >= 1.25，尾盘量能明显放大",
                "均线多头：收盘站上 MA5 与 MA20 且处于突破整理形态",
                "时序代理：T日收盘信号、T+1开盘进场，遵守T+1在T+2日线模拟离场；价位是研究假设"
            ]
        }
        self._cached_backtest = result
        self._cache_time = datetime.now()
        return result

    def get_cached_or_run_backtest(self, force_refresh: bool = False) -> Dict[str, Any]:
        if not force_refresh and self._cached_backtest is not None:
            return self._cached_backtest
        return self.calculate_strategy_win_rate()

tail_strategy_230 = TailStrategy230()


class TailTradeTracker:
    """
    跟踪用户确认的实际买入成交并发送行情提醒；候选和提醒不等于持仓或成交。
    实际卖出成交需人工录入，系统不根据日线高低价推断平仓。
    """
    def __init__(self, storage_file: Optional[Path] = None, trades_file: Optional[Path] = None):
        target_file = storage_file or trades_file
        if target_file is None:
            try:
                self.storage_file = TAIL_TRADES_FILE
            except Exception:
                self.storage_file = Path(__file__).resolve().parent.parent / "storage" / "tail_trades_230.json"
        else:
            self.storage_file = target_file

        self._trades: List[Dict[str, Any]] = []
        self._load()

    def _load(self):
        try:
            if self.storage_file and self.storage_file.exists():
                with open(self.storage_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        self._trades = data
        except Exception as e:
            print(f"读取 2.30 战法持仓记录异常: {e}")
            self._trades = []

    def _save(self):
        try:
            if self.storage_file:
                self.storage_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self.storage_file, "w", encoding="utf-8") as f:
                    json.dump(self._trades, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存 2.30 战法持仓记录异常: {e}")

    def record_recommendations(self, candidates: List[Dict[str, Any]], limit: int = 3) -> List[Dict[str, Any]]:
        """记录研究信号；候选信号不能被登记为用户实际持仓。"""
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M:%S")

        added_trades = []
        for c in candidates[:limit]:
            sym = c.get("symbol")
            if not sym:
                continue

            existing = next((t for t in self._trades if t.get("symbol") == sym and t.get("buy_date") == date_str), None)
            if existing:
                continue

            buy_price = float(c.get("price", 0.0))
            target_p = float(c.get("target_sell_price") or round(buy_price * 1.025, 2))
            stop_p = float(c.get("stop_loss_price") or round(buy_price * 0.982, 2))

            trade_item = {
                "id": f"{sym}_{date_str}",
                "symbol": sym,
                "name": c.get("name", sym),
                "buy_date": date_str,
                "buy_time": time_str,
                "buy_price": buy_price,
                "target_sell_price": target_p,
                "target_pct": float(c.get("target_pct", 2.5)),
                "stop_loss_price": stop_p,
                "stop_pct": float(c.get("stop_pct", 1.8)),
                "score": float(c.get("score", 70.0)),
                "vol_ratio": float(c.get("vol_ratio", 1.0)),
                "change_pct": float(c.get("change_pct", 0.0)),
                "source": "research_signal",
                "status": "SIGNAL_ONLY",
                "peak_price": buy_price,
                "reasons": c.get("reasons", []),
                "exit_date": None,
                "exit_time": None,
                "exit_price": None,
                "exit_reason": None,
                "return_pct": None,
                "alerted_flags": []
            }
            self._trades.insert(0, trade_item)
            added_trades.append(trade_item)

        if added_trades:
            self._save()
        return added_trades

    def add_manual_trade(
        self,
        symbol: str,
        name: str,
        buy_price: float,
        buy_date: Optional[str] = None,
        target_pct: float = 2.5,
        stop_pct: float = 1.8
    ) -> Dict[str, Any]:
        """记录用户确认的实际买入成交；只记录价格，不推断数量或账户收益。"""
        now = datetime.now()
        b_date = buy_date or now.strftime("%Y-%m-%d")
        b_time = now.strftime("%H:%M:%S")

        target_p = round(buy_price * (1.0 + target_pct / 100.0), 2)
        stop_p = round(buy_price * (1.0 - stop_pct / 100.0), 2)

        trade_item = {
            "id": f"{symbol}_{b_date}",
            "source": "manual_actual_fill",
            "entry_fill_confirmed": True,
            "symbol": symbol,
            "name": name,
            "buy_date": b_date,
            "buy_time": b_time,
            "buy_price": float(buy_price),
            "target_sell_price": target_p,
            "target_pct": target_pct,
            "stop_loss_price": stop_p,
            "stop_pct": stop_pct,
            "status": "HOLDING",
            "peak_price": float(buy_price),
            "reasons": ["用户自主确认买入/实盘建仓"],
            "exit_date": None,
            "exit_time": None,
            "exit_price": None,
            "exit_reason": None,
            "return_pct": None,
            "alerted_flags": []
        }
        self._trades.insert(0, trade_item)
        self._save()
        return trade_item

    def get_active_positions(self) -> List[Dict[str, Any]]:
        """只返回用户确认实际买入、且尚未人工确认平仓的记录。"""
        return [
            t for t in self._trades
            if t.get("source") == "manual_actual_fill"
            and t.get("status") in ["HOLDING", "ALERTED_TIMED", "ALERTED_EXIT"]
        ]

    def get_all_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        """获取所有 2.30 战法历史及活跃记录"""
        return self._trades[:limit]

    def close_trade(self, symbol: str, buy_date: str, status: str, exit_price: float, exit_reason: str):
        """标记某笔 2.30 战法交易完成平仓"""
        now = datetime.now()
        for t in self._trades:
            if t.get("source") == "manual_actual_fill" and t.get("symbol") == symbol and t.get("buy_date") == buy_date:
                t["status"] = status
                t["exit_date"] = now.strftime("%Y-%m-%d")
                t["exit_time"] = now.strftime("%H:%M:%S")
                t["exit_price"] = round(exit_price, 2)
                t["exit_reason"] = exit_reason
                buy_p = float(t.get("buy_price", exit_price))
                t["return_pct"] = round((exit_price - buy_p) / buy_p * 100.0, 2) if buy_p > 0 else 0.0
                t["exit_fill_confirmed"] = True
                t["return_method"] = "买卖成交价变化，未扣佣金、印花税及其他费用；不是账户净收益"
                self._save()
                return t
        return None

    def add_alert_flag(self, symbol: str, buy_date: str, flag: str):
        """记录该笔交易已发送过的预警标志，防止盘中重复狂刷"""
        for t in self._trades:
            if t.get("symbol") == symbol and t.get("buy_date") == buy_date:
                flags = t.setdefault("alerted_flags", [])
                if flag not in flags:
                    flags.append(flag)
                    self._save()
                return

    def reconcile_and_clean_expired_positions(self, current_dt: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """
        保留兼容入口但不再用日线最高/最低价推断实际成交或自动平仓。
        需要核对的记录由用户通过实际成交价人工结算。
        """
        return []

    def check_morning_plan(self, quotes: Optional[Dict[str, Any]] = None, current_dt: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """汇总用户确认买入记录的行情参考，不代表委托或实际成交。"""
        self.reconcile_and_clean_expired_positions(current_dt)
        now = current_dt or datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        t1_positions = [p for p in self.get_active_positions() if p.get("buy_date") < today_str]
        if not t1_positions:
            return []

        symbols = [p["symbol"] for p in t1_positions]
        if quotes is None:
            quotes = data_engine.get_realtime_quotes(symbols)

        plans = []
        for pos in t1_positions:
            sym = pos["symbol"]
            name = pos["name"]
            buy_p = float(pos.get("buy_price", 0.0))
            target_p = float(pos.get("target_sell_price", buy_p * 1.025))
            stop_p = float(pos.get("stop_loss_price", buy_p * 0.982))
            q = quotes.get(sym, {})
            open_p = float(q.get("price") or q.get("open") or buy_p)
            open_chg = round((open_p - buy_p) / buy_p * 100.0, 2) if buy_p > 0 else 0.0

            plans.append({
                "symbol": sym,
                "name": name,
                "buy_date": pos.get("buy_date"),
                "buy_price": buy_p,
                "open_price": open_p,
                "open_change_pct": open_chg,
                "target_sell_price": target_p,
                "stop_loss_price": stop_p
            })
        return plans

    def check_next_day_exits(self, quotes: Optional[Dict[str, Any]] = None, current_dt: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """
        监控用户确认的实际买入记录并生成行情提醒；不会下单或推断平仓成交。
        """
        self.reconcile_and_clean_expired_positions(current_dt)
        now = current_dt or datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        cur_hm = now.strftime("%H:%M")

        active_positions = self.get_active_positions()
        # 严格限定买入日期早于今天 (T+1 日)
        t1_positions = [p for p in active_positions if p.get("buy_date") < today_str]
        if not t1_positions:
            return []

        symbols = [p["symbol"] for p in t1_positions]
        if quotes is None:
            quotes = data_engine.get_realtime_quotes(symbols)

        exit_signals = []

        for pos in t1_positions:
            sym = pos["symbol"]
            name = pos["name"]
            buy_p = float(pos.get("buy_price", 0.0))
            target_p = float(pos.get("target_sell_price", buy_p * 1.025))
            stop_p = float(pos.get("stop_loss_price", buy_p * 0.982))
            peak_p = float(pos.get("peak_price", buy_p))
            buy_date = pos.get("buy_date", "")
            alerted_flags = pos.get("alerted_flags", [])

            q = quotes.get(sym)
            if not q:
                continue

            cur_p = float(q.get("price", 0.0))
            if cur_p <= 0:
                continue

            high_p = float(q.get("high", cur_p))

            if max(cur_p, high_p) > peak_p:
                peak_p = max(cur_p, high_p)
                pos["peak_price"] = peak_p
                self._save()

            cur_gain_pct = round((cur_p - buy_p) / buy_p * 100.0, 2) if buy_p > 0 else 0.0
            peak_gain_pct = round((peak_p - buy_p) / buy_p * 100.0, 2) if buy_p > 0 else 0.0

            # -------------------------------------------------------------
            # 规则 1: 次日冲高止盈达成 (TAIL_PROFIT_EXIT)
            # 条件：现价触达目标止盈线 (+2.5%)，或早盘高点已触达且现价维持在 +1.8% 以上
            # -------------------------------------------------------------
            if cur_p >= target_p or (high_p >= target_p and cur_p >= buy_p * 1.018):
                if "PROFIT_EXIT" not in alerted_flags:
                    sig = {
                        "symbol": sym,
                        "name": name,
                        "date": today_str,
                        "price": cur_p,
                        "signal_type": "TAIL_PROFIT_EXIT",
                        "signal_desc": "🎯【行情提醒】价格触及止盈观察位，请核对实际持仓与委托",
                        "buy_price": buy_p,
                        "buy_date": buy_date,
                        "target_sell_price": target_p,
                        "stop_loss_price": stop_p,
                        "pnl_pct": cur_gain_pct,
                        "peak_gain_pct": peak_gain_pct,
                        "reasons": [
                            f"记录的买入成交价为 {buy_p:.2f}元，当前行情触及观察价位 {target_p:.2f}元。",
                            f"当前价格变动 {cur_gain_pct:+.2f}%，盘中高点对应变动 {peak_gain_pct:+.2f}%；未包含交易费用。",
                            "此提醒不代表已成交或应立即卖出；请先核对实际持仓、可卖数量和委托状态。"
                        ],
                        "trade_levels": {
                            "entry_price": buy_p,
                            "suggested_sell_price": cur_p,
                            "target_price": target_p,
                            "initial_stop": stop_p,
                            "action_urgency": "行情提醒；不代表委托或成交"
                        }
                    }
                    exit_signals.append(sig)
                    self.add_alert_flag(sym, buy_date, "PROFIT_EXIT")
                    pos["status"] = "ALERTED_EXIT"
                    pos["last_alert_type"] = "PROFIT_EXIT"
                    self._save()
                continue

            # -------------------------------------------------------------
            # 规则 2: 次日破位止损离场 (TAIL_STOP_EXIT)
            # 条件：开盘或盘中击穿 -1.8% 硬止损底线
            # -------------------------------------------------------------
            if cur_p <= stop_p:
                if "STOP_EXIT" not in alerted_flags:
                    sig = {
                        "symbol": sym,
                        "name": name,
                        "date": today_str,
                        "price": cur_p,
                        "signal_type": "TAIL_STOP_EXIT",
                        "signal_desc": "🛑【行情提醒】价格触及止损观察位，请核对实际持仓与委托",
                        "buy_price": buy_p,
                        "buy_date": buy_date,
                        "target_sell_price": target_p,
                        "stop_loss_price": stop_p,
                        "pnl_pct": cur_gain_pct,
                        "peak_gain_pct": peak_gain_pct,
                        "reasons": [
                            f"记录的买入成交价为 {buy_p:.2f}元，当前行情触及止损观察位 {stop_p:.2f}元。",
                            f"当前价格变动 {cur_gain_pct:+.2f}%，该价格变动未包含交易费用。",
                            "此提醒不代表已成交或应立即卖出；请先核对实际持仓、可卖数量和委托状态。"
                        ],
                        "trade_levels": {
                            "entry_price": buy_p,
                            "suggested_sell_price": cur_p,
                            "target_price": target_p,
                            "initial_stop": stop_p,
                            "action_urgency": "行情提醒；不代表委托或成交"
                        }
                    }
                    exit_signals.append(sig)
                    self.add_alert_flag(sym, buy_date, "STOP_EXIT")
                    pos["status"] = "ALERTED_EXIT"
                    pos["last_alert_type"] = "STOP_EXIT"
                    self._save()
                continue

            # -------------------------------------------------------------
            # 规则 3: 早盘时限动能衰竭离场提醒 (TAIL_TIMED_EXIT)
            # 条件：时间到达 09:45~10:15，标的既未触及止盈也未破止损，主力脉冲窗口结束
            # -------------------------------------------------------------
            if "09:45" <= cur_hm <= "10:15":
                if "TIMED_EXIT" not in alerted_flags and pos.get("status") == "HOLDING":
                    sig = {
                        "symbol": sym,
                        "name": name,
                        "date": today_str,
                        "price": cur_p,
                        "signal_type": "TAIL_TIMED_EXIT",
                        "signal_desc": "⏳【2.30战法·早盘动能衰竭】冲高尾声·择机离场",
                        "score": 85,
                        "buy_price": buy_p,
                        "buy_date": buy_date,
                        "target_sell_price": target_p,
                        "stop_loss_price": stop_p,
                        "pnl_pct": cur_gain_pct,
                        "peak_gain_pct": peak_gain_pct,
                        "reasons": [
                            f"当前时间已到达 {cur_hm}，A股早盘惯性冲高黄金窗口 (09:30~09:45) 已经结束",
                            f"买入价 {buy_p:.2f}元，当前现价 {cur_p:.2f}元(盈亏: {cur_gain_pct:+.2f}%)，今早最高冲至 {peak_p:.2f}元(+{peak_gain_pct:+.2f}%)",
                            "该提示只反映时段规则，不预测后续涨跌，也不代表已成交或应立即卖出；请核对实际持仓和委托状态。"
                        ],
                        "trade_levels": {
                            "entry_price": buy_p,
                            "suggested_sell_price": cur_p,
                            "target_price": target_p,
                            "initial_stop": stop_p,
                            "action_urgency": "时段观察提醒；不代表委托或成交"
                        }
                    }
                    exit_signals.append(sig)
                    self.add_alert_flag(sym, buy_date, "TIMED_EXIT")
                    pos["status"] = "ALERTED_TIMED"
                    self._save()
                continue

            # -------------------------------------------------------------
            # 规则 4: 次日尾盘时段观察提醒 (TAIL_EOD_EXIT)
            # -------------------------------------------------------------
            if "14:30" <= cur_hm <= "14:55":
                if "EOD_EXIT" not in alerted_flags and pos.get("status") in ["HOLDING", "ALERTED_TIMED"]:
                    sig = {
                        "symbol": sym,
                        "name": name,
                        "date": today_str,
                        "price": cur_p,
                        "signal_type": "TAIL_EOD_EXIT",
                        "signal_desc": "🚨【行情提醒】达到模型持仓时限观察点，请核对实际持仓",
                        "buy_price": buy_p,
                        "buy_date": buy_date,
                        "target_sell_price": target_p,
                        "stop_loss_price": stop_p,
                        "pnl_pct": cur_gain_pct,
                        "peak_gain_pct": peak_gain_pct,
                        "reasons": [
                            f"当前时间为 {cur_hm}，模型的观察期限已到；买入价 {buy_p:.2f}元，现价 {cur_p:.2f}元。",
                            f"价格变动 {cur_gain_pct:+.2f}%，未包含交易费用，不等同于账户盈亏。",
                            "此提醒不代表必须卖出或已经成交；请按实际持仓、风险承受能力和委托状态自行处理。"
                        ],
                        "trade_levels": {
                            "entry_price": buy_p,
                            "suggested_sell_price": cur_p,
                            "target_price": target_p,
                            "initial_stop": stop_p,
                            "action_urgency": "时段观察提醒；不代表委托或成交"
                        }
                    }
                    exit_signals.append(sig)
                    self.add_alert_flag(sym, buy_date, "EOD_EXIT")
                    pos["status"] = "ALERTED_EXIT"
                    pos["last_alert_type"] = "EOD_EXIT"
                    self._save()
                continue

        return exit_signals

tail_trade_tracker = TailTradeTracker()
