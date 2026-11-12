"""
A股智能量化系统 2.0 - 涨停候选日线筛选与信号跟踪 (LimitUpEngine)

核心模块：
1. 基于日线涨幅和成交量的涨停候选筛选与形态分类；
2. 日线技术规则分；不评估盘口队列、封板时间、炸板回封或真实资金流；
3. 涨停候选信号跟踪台账 (记录推荐日期和后续日线行情，不是实际成交记录)；
4. 每日收盘复核候选信号后续行情并更新跟踪字段；
5. 汇总信号跟踪统计，不代表成交胜率、盈亏比或未来收益。
"""
import os
import csv
import json
import time
import math
import shutil
import threading
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple

from config import DATA_DIR, LIMIT_UP_TRADES_FILE, LIMIT_UP_CSV_FILE, load_config
from core.data_engine import data_engine

class LimitUpEngine:
    # Historical demo trades shipped in the prior version. They are excluded
    # from live statistics and removed from the ledger on a backed-up migration.
    SYNTHETIC_SEED_IDS = {
        "sh600501_20261028",
        "sh603103_20261028",
        "sz002535_20261027",
        "sh600592_20261027",
        "sz002842_20261026"
    }

    def __init__(self):
        self._trades: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._load_trades()

    def _load_trades(self):
        """载入本地信号跟踪记录；新台账为空，不填充演示样本。"""
        if LIMIT_UP_TRADES_FILE.exists():
            try:
                with open(LIMIT_UP_TRADES_FILE, "r", encoding="utf-8") as f:
                    loaded_trades = json.load(f)
                    self._trades = loaded_trades if isinstance(loaded_trades, list) else []
                    filtered_trades = [
                        trade for trade in self._trades
                        if str(trade.get("id", "")) not in self.SYNTHETIC_SEED_IDS
                    ]
                    needs_legacy_migration = any(
                        trade.get("data_provenance_version") != 2
                        for trade in filtered_trades
                    )
                    if len(filtered_trades) != len(self._trades) or needs_legacy_migration:
                        backup_path = LIMIT_UP_TRADES_FILE.with_suffix(".json.pre_seed_cleanup.bak")
                        if backup_path.exists():
                            backup_path = LIMIT_UP_TRADES_FILE.with_name(
                                f"{LIMIT_UP_TRADES_FILE.stem}.pre_seed_cleanup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak"
                            )
                        shutil.copy2(LIMIT_UP_TRADES_FILE, backup_path)
                        if LIMIT_UP_CSV_FILE.exists():
                            csv_backup_path = LIMIT_UP_CSV_FILE.with_name(
                                LIMIT_UP_CSV_FILE.name + ".pre_seed_cleanup.bak"
                            )
                            if csv_backup_path.exists():
                                csv_backup_path = LIMIT_UP_CSV_FILE.with_name(
                                    f"{LIMIT_UP_CSV_FILE.stem}.pre_seed_cleanup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv.bak"
                                )
                            shutil.copy2(LIMIT_UP_CSV_FILE, csv_backup_path)
                        self._trades = filtered_trades
                        for trade in self._trades:
                            if trade.get("data_provenance_version") != 2:
                                # Older versions fabricated intraday book fields and
                                # presented a daily close as a trade with target/stop.
                                trade["seal_time"] = None
                                trade["seal_turnover_ratio"] = None
                                trade["open_count"] = None
                                trade["trade_levels"] = {}
                                trade["score_type"] = "日线技术规则分（非封板硬度）"
                                trade["data_limitations"] = "旧版本记录不含盘口队列、封板时间、炸板次数或实际成交；历史规则归因已清空"
                                trade["data_provenance_version"] = 2
                        self._save_trades()
                        self.export_csv()
                    if self._trades:
                        return
            except Exception:
                self._trades = []
                # Do not overwrite an existing ledger if parsing or migration
                # fails; leave the source file intact for recovery.
                return

        self._trades = []
        self._save_trades()
        self.export_csv()

    def _save_trades(self):
        """保存账本至 JSON"""
        try:
            LIMIT_UP_TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(LIMIT_UP_TRADES_FILE, "w", encoding="utf-8") as f:
                json.dump(self._trades, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[LimitUpEngine] 保存打板JSON异常: {e}")

    def export_csv(self) -> str:
        """
        导出/自动更新 CSV 电子表格文件 (带 UTF-8 BOM，Excel与WPS完美秒开不乱码)
        """
        try:
            LIMIT_UP_CSV_FILE.parent.mkdir(parents=True, exist_ok=True)
            headers = [
                "信号编号", "信号日期", "股票代码", "股票名称", "日线形态分类", "连续涨停数估算",
                "信号日收盘参考价(元)", "封板时间(无日线数据)", "日线技术规则分", "封成比(无盘口数据)", "炸板次数(无分时数据)", "成交额(缺失则留空，万元)",
                "次日开盘价(元)", "信号至次日开盘变动(%)", "次日最高价(元)", "信号至次日最高变动(%)",
                "次日收盘价(元)", "信号至次日收盘变动(%)", "次日继续涨停(日线估算)", "行情跟踪状态", "日线规则依据"
            ]

            with open(LIMIT_UP_CSV_FILE, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(headers)
                for t in self._trades:
                    writer.writerow([
                        t.get("id", ""),
                        t.get("date", ""),
                        t.get("symbol", ""),
                        t.get("name", ""),
                        t.get("board_type", ""),
                        t.get("ladder_count", 1),
                        t.get("seal_price", 0.0),
                        t.get("seal_time") or "",
                        t.get("hardness_score", 0.0),
                        t.get("seal_turnover_ratio") if t.get("seal_turnover_ratio") is not None else "",
                        t.get("open_count") if t.get("open_count") is not None else "",
                        round(float(t["turnover_amount"]) / 10000.0, 1) if t.get("turnover_amount") is not None else "",
                        t.get("next_open_price", "-"),
                        t.get("next_open_pnl", "-"),
                        t.get("next_high_price", "-"),
                        t.get("next_high_pnl", "-"),
                        t.get("next_close_price", "-"),
                        t.get("next_close_pnl", "-"),
                        "是" if t.get("is_consecutive") else "否",
                        t.get("status", "HOLDING"),
                        "; ".join(t.get("reasons", []))
                    ])
            return str(LIMIT_UP_CSV_FILE)
        except Exception as e:
            print(f"[LimitUpEngine] 导出CSV表格异常: {e}")
            return ""

    def evaluate_limit_up_bar(self, symbol: str, name: str, df: pd.DataFrame) -> Optional[Dict[str, Any]]:
        """
        用日线数据判断涨停候选并计算技术规则分；不含分时盘口数据
        """
        if df is None or len(df) < 20:
            return None

        curr = df.iloc[-1]
        prev = df.iloc[-2]
        close = float(curr['close'])
        prev_close = float(prev['close'])
        open_price = float(curr['open'])
        high = float(curr['high'])
        low = float(curr['low'])
        turnover_raw = pd.to_numeric(pd.Series([curr.get('turnover')]), errors='coerce').iloc[0]
        turnover = float(turnover_raw) if pd.notna(turnover_raw) and np.isfinite(turnover_raw) and turnover_raw > 0 else None
        pct_change = round((close - prev_close) / prev_close * 100.0, 2) if prev_close > 0 else 0.0

        # 主板涨停判定：涨幅 >= 9.8% 且收盘在最高点附近
        limit_target = round(prev_close * 1.10, 2)
        is_limit_up = (pct_change >= 9.80) and (abs(close - limit_target) <= 0.03 or abs(close - high) <= 0.02)
        if not is_limit_up:
            return None

        # 一字板判定：开=高=低=收 (无盘中成交机会，仅能跟踪次日溢价)
        is_one_price_board = (
            abs(open_price - high) <= 0.01
            and abs(open_price - low) <= 0.01
            and abs(open_price - close) <= 0.01
        )

        # 1. 计算历史连板数与板型结构
        ladder_count = 1
        for i in range(2, min(10, len(df))):
            p_curr = df.iloc[-i]
            p_prev = df.iloc[-i - 1]
            p_chg = (p_curr['close'] - p_prev['close']) / p_prev['close'] * 100.0
            if p_chg >= 9.8:
                ladder_count += 1
            else:
                break

        board_type = "首板突破"
        if is_one_price_board:
            board_type = "一字板" if ladder_count == 1 else f"一字连板({ladder_count}板)"
        elif ladder_count == 2:
            board_type = "1进2接力"
        elif ladder_count == 3:
            board_type = "2进3接力"
        elif ladder_count > 3:
            board_type = f"高位连板({ladder_count}板)"
        elif ladder_count == 1:
            # 检查前一日是否冲高回落形成弱转强
            prev2 = df.iloc[-3] if len(df) >= 3 else prev
            prev_upper = (prev['high'] - max(prev['open'], prev['close'])) / prev_close * 100.0
            if prev_upper >= 4.0:
                board_type = "弱转强反包"

        # 2. Daily-bar technical rules score. This is not an order-book or
        # seal-quality score because no intraday book data is available.
        hardness_score = 60.0
        reasons = []

        # 板型加分
        if is_one_price_board:
            hardness_score += 5.0
            reasons.append("一字板无量封死，盘中无法买入，仅跟踪次日溢价")
        elif board_type == "首板突破":
            hardness_score += 15.0
            reasons.append("日线涨停形态符合首板分类规则；未评估监管或成交风险")
        elif board_type == "1进2接力":
            hardness_score += 15.0
            reasons.append("日线涨停形态符合1进2分类规则")
        elif board_type == "2进3接力":
            hardness_score += 10.0
            reasons.append("日线涨停形态符合2进3分类规则；高位波动风险较大")
        elif board_type == "弱转强反包":
            hardness_score += 12.0
            reasons.append("按前一交易日上影线和当日涨停估算为反包形态")

        # 放量与流动性充沛度
        volume_history = pd.to_numeric(df['volume'].iloc[-6:-1], errors='coerce').replace([np.inf, -np.inf], np.nan).dropna()
        vol_5 = float(volume_history.mean()) if len(volume_history) == 5 else None
        cur_vol_raw = pd.to_numeric(pd.Series([curr.get('volume')]), errors='coerce').iloc[0]
        cur_vol = float(cur_vol_raw) if pd.notna(cur_vol_raw) and np.isfinite(cur_vol_raw) else None
        vol_ratio = round(cur_vol / vol_5, 2) if vol_5 and vol_5 > 0 and cur_vol is not None else None
        if vol_ratio is None:
            reasons.append("前5日或当日成交量数据不完整；跳过量能评分")
        elif vol_ratio >= 1.5:
            hardness_score += 10.0
            reasons.append(f"日成交量较前5日均值放大({vol_ratio:.1f}倍)")
        elif vol_ratio < 0.6 and not is_one_price_board:
            # 换手板但缩量 (无法验证封单强度)
            hardness_score += 8.0
            reasons.append("日成交量低于前5日均值；日线无法验证封单强度")

        # 均线多头形态共振
        ma5 = float(df['close'].iloc[-5:].mean())
        ma20 = float(df['close'].iloc[-20:].mean())
        if close > ma5 > ma20:
            hardness_score += 8.0
            reasons.append("均线多头排列发散，顺势攻击动能强劲")

        hardness_score = round(float(np.clip(hardness_score, 45.0, 98.0)), 1)
        beginner_guide = {
            "data_limitations": "日线规则评分；缺少分时成交、封单队列、封板时间、炸板次数和真实资金流数据，不构成交易建议"
        }

        return {
            "symbol": symbol,
            "name": name,
            "date": str(curr['date'])[:10],
            "price": close,
            "seal_price": close,
            "board_type": board_type,
            "ladder_count": ladder_count,
            "is_one_price_board": is_one_price_board,
            "seal_time": None,
            "hardness_score": hardness_score,
            "score_type": "日线技术规则分（非封板硬度）",
            "seal_turnover_ratio": None,
            "open_count": None,
            "turnover_amount": turnover,
            "reasons": reasons,
            "beginner_guide": beginner_guide,
            "trade_levels": {}
        }

    def send_limit_up_signal(self, candidate: Dict[str, Any], force: bool = False) -> Dict[str, Any]:
        """
        向钉钉与微信推送日线候选研究摘要；不生成订单指令。
        """
        from notifier.manager import notification_manager
        sym = candidate.get("symbol")
        name = candidate.get("name")
        price = float(candidate.get("price") or candidate.get("seal_price") or 0.0)

        signal_payload = {
            "symbol": sym,
            "name": name,
            "signal_type": "LIMIT_UP",
            "signal_desc": f"涨停候选日线规则筛选({candidate.get('board_type', '首板')})；仅供研究，不是交易指令",
            "price": price,
            "seal_price": price,
            "board_type": candidate.get("board_type", "首板突破"),
            "ladder_count": candidate.get("ladder_count", 1),
            "hardness_score": candidate.get("hardness_score"),
            "score_type": candidate.get("score_type", "日线技术规则分（非封板硬度）"),
            "seal_turnover_ratio": None,
            "open_count": None,
            "trade_levels": {},
            "reasons": candidate.get("reasons", [])
        }
        res = notification_manager.send_signal(signal_payload, force=force)
        return {"success": True, "res": res, "signal": signal_payload}

    def scan_today_candidates(self, limit: int = 15) -> List[Dict[str, Any]]:
        """
        全市场扫描今日涨停板与打板精选标的
        """
        stocks = data_engine.get_main_board_stock_list()
        candidates = []

        # 筛选涨幅 >= 9.8% 的主板个股
        zt_stocks = [s for s in stocks if float(s.get("change_pct", 0)) >= 9.80][:80]
        if not zt_stocks:
            return []

        for s in zt_stocks:
            sym = s.get("symbol")
            name = s.get("name")
            try:
                df = data_engine.get_daily_kline(sym, count=60)
                if df is not None and len(df) >= 20:
                    eval_res = self.evaluate_limit_up_bar(sym, name, df)
                    if eval_res:
                        candidates.append(eval_res)
            except Exception:
                continue

        candidates.sort(key=lambda x: x.get("hardness_score", 0), reverse=True)
        return candidates[:limit]

    def record_board_hitting_trades(self, candidates: List[Dict[str, Any]], top_n: int = 3) -> List[Dict[str, Any]]:
        """
        将今日候选信号写入本地跟踪台账；不代表实际买入或成交
        """
        today_str = datetime.now().strftime("%Y-%m-%d")
        added = []

        with self._lock:
            existing_ids = {t["id"] for t in self._trades}
            for c in candidates[:top_n]:
                trade_id = f"{c['symbol']}_{today_str.replace('-', '')}"
                if trade_id in existing_ids:
                    continue

                item = {
                    "id": trade_id,
                    "date": today_str,
                    "symbol": c["symbol"],
                    "name": c["name"],
                    "board_type": c["board_type"],
                    "ladder_count": c.get("ladder_count", 1),
                    "seal_price": c["seal_price"],
                    "seal_time": c.get("seal_time"),
                    "hardness_score": c.get("hardness_score"),
                    "score_type": c.get("score_type", "日线技术规则分（非封板硬度）"),
                    "seal_turnover_ratio": None,
                    "open_count": None,
                    "turnover_amount": c.get("turnover_amount"),
                    "reasons": c.get("reasons", []),
                    "status": "HOLDING",
                    "next_open_price": None,
                    "next_open_pnl": None,
                    "next_high_price": None,
                    "next_high_pnl": None,
                    "next_close_price": None,
                    "next_close_pnl": None,
                    "is_consecutive": False,
                    "trade_levels": {},
                    "data_limitations": "日线信号参考价和后续行情变动不代表实际成交",
                    "data_provenance_version": 2
                }
                self._trades.insert(0, item)
                existing_ids.add(trade_id)
                added.append(item)

            if added:
                self._save_trades()
                self.export_csv()

        return added

    def review_and_update_trades(self, review_date: Optional[str] = None) -> Dict[str, Any]:
        """
        更新候选信号之后首个交易日的日线行情字段；这不是实际成交结算。
        """
        today_str = review_date or datetime.now().strftime("%Y-%m-%d")
        updated_count = 0

        with self._lock:
            for t in self._trades:
                # Only update prior candidate signals that do not yet have a following bar.
                if t.get("status") == "HOLDING" and t.get("date") < today_str:
                    sym = t.get("symbol")
                    buy_price = float(t.get("seal_price", 0.0))
                    if buy_price <= 0:
                        continue

                    # 获取历史或实时日K (足够覆盖多日停机跨度)
                    df = data_engine.get_daily_kline(sym, count=40)
                    if df is not None and len(df) >= 2:
                        df['date_str'] = df['date'].astype(str).str[:10]
                        trade_date = str(t.get("date", ""))[:10]
                        # 严格查找 trade_date 之后的交易日
                        next_bars = df[df['date_str'] > trade_date]
                        if len(next_bars) == 0:
                            # No following daily bar yet (for example, the candidate is from today).
                            continue

                        # Use the first daily bar after the signal date, not the newest bar.
                        t1_bar = next_bars.iloc[0]
                        t1_date = str(t1_bar['date_str'])

                        next_open = float(t1_bar['open'])
                        next_high = float(t1_bar['high'])
                        next_close = float(t1_bar['close'])

                        open_pnl = round((next_open - buy_price) / buy_price * 100.0, 2)
                        high_pnl = round((next_high - buy_price) / buy_price * 100.0, 2)
                        close_pnl = round((next_close - buy_price) / buy_price * 100.0, 2)

                        # 是否成功连板
                        limit_target = round(buy_price * 1.10, 2)
                        is_consecutive = (next_close >= limit_target * 0.995) or (close_pnl >= 9.80)

                        # Status describes only the signal-price to next-close
                        # move. It is not a trade win/loss or executable return.
                        status = "WIN" if close_pnl > 0 else ("LOSS" if close_pnl < 0 else "FLAT")

                        t["settled_date"] = t1_date
                        t["next_open_price"] = next_open
                        t["next_open_pnl"] = open_pnl
                        t["next_high_price"] = next_high
                        t["next_high_pnl"] = high_pnl
                        t["next_close_price"] = next_close
                        t["next_close_pnl"] = close_pnl
                        t["is_consecutive"] = is_consecutive
                        t["status"] = status
                        updated_count += 1

            if updated_count > 0:
                self._save_trades()
                self.export_csv()

        stats = self.calculate_model_stats()
        stats["updated_trades_count"] = updated_count
        stats["settled_count"] = updated_count
        return stats

    def calculate_model_stats(self) -> Dict[str, Any]:
        """
        汇总候选信号后续日线变动；这些指标不代表真实成交统计
        """
        closed_trades = [t for t in self._trades if t.get("status") in ["WIN", "LOSS", "FLAT"]]
        total = len(closed_trades)

        if total == 0:
            return {
                "total_trades": len(self._trades),
                "closed_trades": 0,
                "open_premium_win_rate": None,
                "peak_profit_win_rate": None,
                "consecutive_rate": None,
                "avg_open_pnl": None,
                "avg_peak_pnl": None,
                "avg_close_pnl": None,
                "profit_loss_ratio": None,
                "big_loss_rate": None,
                "board_types": {},
                "data_status": "暂无已结算信号样本，胜率和盈亏比不可用"
            }

        # 1. Share of signal close to next open changes above zero.
        open_wins = sum(1 for t in closed_trades if float(t.get("next_open_pnl") or 0) > 0)
        open_win_rate = round(open_wins / total * 100.0, 1)

        # 2. Share of next-session highs at least 2% above the signal close.
        peak_wins = sum(1 for t in closed_trades if float(t.get("next_high_pnl") or 0) >= 2.0)
        peak_win_rate = round(peak_wins / total * 100.0, 1)

        # 3. Approximate next-day limit-up continuation share from daily bars.
        consecutives = sum(1 for t in closed_trades if t.get("is_consecutive"))
        consecutive_rate = round(consecutives / total * 100.0, 1)

        # 4. 平均收益指标
        avg_open = round(float(np.mean([float(t.get("next_open_pnl") or 0) for t in closed_trades])), 2)
        avg_peak = round(float(np.mean([float(t.get("next_high_pnl") or 0) for t in closed_trades])), 2)
        avg_close = round(float(np.mean([float(t.get("next_close_pnl") or 0) for t in closed_trades])), 2)

        # 5. 盈亏比
        profits = [float(t.get("next_close_pnl") or 0) for t in closed_trades if float(t.get("next_close_pnl") or 0) > 0]
        losses = [abs(float(t.get("next_close_pnl") or 0)) for t in closed_trades if float(t.get("next_close_pnl") or 0) < 0]
        mean_p = float(np.mean(profits)) if profits else None
        mean_l = float(np.mean(losses)) if losses else None
        pl_ratio = round(mean_p / mean_l, 2) if mean_p is not None and mean_l is not None and mean_l > 0 else None

        # 6. 大面率 (收盘亏损 >= 4%)
        big_losses = sum(1 for t in closed_trades if float(t.get("next_close_pnl") or 0) <= -4.0)
        big_loss_rate = round(big_losses / total * 100.0, 1)

        # 7. 板型分化统计 (首板 vs 连板)
        board_stats = {}
        for b_type in ["首板突破", "1进2接力", "弱转强反包"]:
            sub = [t for t in closed_trades if t.get("board_type") == b_type]
            if sub:
                sub_win = sum(1 for t in sub if float(t.get("next_high_pnl") or 0) >= 2.0)
                board_stats[b_type] = {
                    "count": len(sub),
                    "win_rate": round(sub_win / len(sub) * 100.0, 1),
                    "avg_return": round(float(np.mean([float(t.get("next_close_pnl") or 0) for t in sub])), 2)
                }

        pending_count = sum(1 for t in self._trades if t.get("status") == "HOLDING")

        return {
            "total_trades": len(self._trades),
            "closed_trades": total,
            "settled_trades": total,
            "pending_trades": pending_count,
            "open_premium_win_rate": open_win_rate,
            "peak_profit_win_rate": peak_win_rate,
            "consecutive_rate": consecutive_rate,
            "avg_open_pnl": avg_open,
            "avg_peak_pnl": avg_peak,
            "avg_close_pnl": avg_close,
            "profit_loss_ratio": pl_ratio,
            "big_loss_rate": big_loss_rate,
            "board_types": board_stats,
            "by_board_type": board_stats,
            "data_status": "统计本地信号跟踪记录的未扣成本日线变动；不代表真实成交、净收益或未来表现"
        }

    def get_all_trades(self, limit: int = 100) -> List[Dict[str, Any]]:
        """获取流水账本列表"""
        with self._lock:
            return self._trades[:limit]

limit_up_engine = LimitUpEngine()
