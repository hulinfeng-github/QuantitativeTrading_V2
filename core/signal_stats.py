"""
A股智能量化系统 2.0 - 跨全市场信号前向收益统计 + 阈值网格搜索 (SignalStats 2.0)
目的：在不拟合样本外的前提下，诚实衡量多因子买入条件的后续行情表现，
并用「训练段网格搜索 + 样本外验证」标定 RSRS 上下限与评分阈值。
- 严格因果：T 日收盘产生信号，T+1 开盘撮合，未来 hold_days 日内按目标/止损/期末平仓估算；
- 计入双边佣金(含最低5元)、印花税(卖出)、滑点；
- 逐条记录每个买入候选bar的特征与前向收益，供离线网格搜索。
"""
import json
import time
import numpy as np
import pandas as pd
from typing import Dict, Any, List, Optional
from datetime import datetime

from config import DATA_DIR, load_config
from core.data_engine import data_engine
from core.alpha_factors import calculate_alpha_features
from core.rsrs_skew import calculate_rsrs_skew
from core.multi_factor_strategy import multi_factor_strategy

SIGNAL_STATS_FILE = DATA_DIR / "signal_stats.json"
SIGNAL_RECORDS_FILE = DATA_DIR / "signal_records.json"

VETO_STATUS = ("主力破位杀跌", "主力高位派发风险")


def _friction(entry_price: float, exit_price: float, shares: int = 100) -> float:
    cfg = load_config()
    comm_r = float(cfg.get("commission_rate", 0.00025))
    min_comm = float(cfg.get("min_commission", 5.0))
    stamp_r = float(cfg.get("stamp_duty_rate", 0.0005))
    slip_r = float(cfg.get("slippage_rate", 0.0005))
    entry_amt = entry_price * shares
    exit_amt = exit_price * shares
    comm_entry = max(min_comm, entry_amt * comm_r)
    comm_exit = max(min_comm, exit_amt * comm_r)
    stamp = exit_amt * stamp_r
    slip = (entry_amt + exit_amt) * slip_r
    fees = comm_entry + comm_exit + stamp + slip
    return fees / entry_amt * 100.0


def _simulate_exit(df: pd.DataFrame, start_idx: int, end_idx: int,
                   entry: float, target: float, stop: float) -> tuple:
    for j in range(start_idx, min(end_idx, len(df) - 1) + 1):
        bar = df.iloc[j]
        o = float(bar["open"]); h = float(bar["high"]); l = float(bar["low"])
        if o >= target:
            return o, "开盘止盈"
        if o <= stop:
            return o, "开盘止损"
        if h >= target and l <= stop:
            return (stop if (o - stop) <= (target - o) else target), "双触及保守"
        if h >= target:
            return target, "触目标"
        if l <= stop:
            return stop, "触止损"
    last = df.iloc[min(end_idx, len(df) - 1)]
    return float(last["close"]), "期末平仓"


def _agg(nets: List[float]) -> Dict[str, Any]:
    if not nets:
        return {"count": 0}
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n <= 0]
    pf = round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else None
    return {
        "count": len(nets),
        "win_rate": round(len(wins) / len(nets) * 100.0, 1),
        "avg_net_pct": round(float(np.mean(nets)), 2),
        "avg_win_pct": round(float(np.mean(wins)), 2) if wins else 0.0,
        "avg_loss_pct": round(float(np.mean(losses)), 2) if losses else 0.0,
        "profit_factor": pf,
    }


def grid_search(records: List[Dict], train_ratio: float = 0.7, min_samples: int = 80) -> Dict[str, Any]:
    """在训练段网格搜索 (rsrs_lo, rsrs_hi, score_min)，并在样本外验证。"""
    if len(records) < min_samples * 2:
        return {"available": False, "note": f"记录不足({len(records)})，无法划分训练/验证段"}

    records = sorted(records, key=lambda r: r["date"])
    cut = int(len(records) * train_ratio)
    train, test = records[:cut], records[cut:]

    rsrs_los = [0.4, 0.5, 0.6, 0.7]
    rsrs_his = [0.9, 1.0, 1.1, 1.3, 99.0]
    score_mins = [60.0, 65.0, 70.0, 75.0, 80.0]

    def evaluate(subset, lo, hi, smin):
        sel = [r for r in subset if lo <= r["rsrs"] < hi and r["score"] >= smin]
        if len(sel) < min_samples:
            return None
        nets = [r["net"] for r in sel]
        a = _agg(nets)
        a["rsrs_lo"] = lo
        a["rsrs_hi"] = hi
        a["score_min"] = smin
        return a

    results = []
    for lo in rsrs_los:
        for hi in rsrs_his:
            if hi <= lo:
                continue
            for smin in score_mins:
                a = evaluate(train, lo, hi, smin)
                if a:
                    results.append(a)

    if not results:
        return {"available": False, "note": "训练段无满足最小样本量的参数组合"}

    # 以训练段「平均净收益」排序，取前若干组合在样本外验证
    results.sort(key=lambda x: x["avg_net_pct"], reverse=True)
    top = results[:8]
    for a in top:
        oos = evaluate(test, a["rsrs_lo"], a["rsrs_hi"], a["score_min"])
        a["oos"] = oos

    return {
        "available": True,
        "train_size": len(train),
        "test_size": len(test),
        "rsrs_lo_grid": rsrs_los,
        "rsrs_hi_grid": rsrs_his,
        "score_min_grid": score_mins,
        "min_samples": min_samples,
        "top_on_train_with_oos": top,
        "note": "按训练段平均净收益排序；oos为样本外验证(同样含成本)。样本外为正才说明参数稳健。",
    }


def run_cross_sectional_stats(
    sample_size: int = 50,
    days: int = 600,
    hold_days: int = 5,
    min_turnover: float = 0.0
) -> Dict[str, Any]:
    stocks = data_engine.get_main_board_stock_list()
    stocks = sorted(stocks, key=lambda x: str(x.get("symbol", "")))
    step = max(1, len(stocks) // sample_size)
    symbols = [s["symbol"] for s in stocks[::step][:sample_size]]

    bull_dates = set()
    bench = data_engine.get_daily_kline("sh000300", count=days + 60)
    if bench is not None:
        b = bench.sort_values("date").reset_index(drop=True)
        b["ma20"] = b["close"].rolling(20, min_periods=20).mean()
        b["date_str"] = b["date"].astype(str).str[:10]
        for _, r in b.iterrows():
            if pd.notna(r["ma20"]) and float(r["close"]) >= float(r["ma20"]):
                bull_dates.add(r["date_str"])

    records: List[Dict[str, Any]] = []
    buy_signals = []
    sell_signals = []
    processed = 0

    for sym in symbols:
        try:
            df = data_engine.get_daily_kline(sym, count=days)
        except Exception:
            df = None
        if df is None or len(df) < 80:
            continue
        info = data_engine.resolve_symbol(sym)
        name = info.get("name", sym) if info else sym
        df = df.sort_values("date").reset_index(drop=True)
        df_feat = calculate_alpha_features(df)
        df_calc = calculate_rsrs_skew(df_feat, n=18, m=600, use_skew=True)
        processed += 1

        for i in range(60, len(df_calc) - hold_days - 1):
            d = str(df_calc.iloc[i]["date"])[:10]
            bull = d in bull_dates
            res = multi_factor_strategy.analyze_bar(sym, name, df_calc, idx=i, market_regime_bull=bull)
            if res is None:
                continue
            close_i = float(res["price"])
            if close_i <= float(res["ma20"]):
                continue  # 买入前置条件：站上MA20
            if res.get("control_status") in VETO_STATUS:
                continue
            entry = float(df_calc.iloc[i + 1]["open"])
            if entry <= 0:
                continue
            levels = res.get("trade_levels", {})
            target = float(levels.get("target_price") or close_i * 1.08)
            stop = float(levels.get("initial_stop") or close_i * 0.95)
            exit_price, reason = _simulate_exit(df_calc, i + 1, i + hold_days, entry, target, stop)
            raw = (exit_price - entry) / entry * 100.0
            net = round(raw - _friction(entry, exit_price), 2)

            rec = {
                "symbol": sym, "date": d,
                "score": float(res.get("score", 0)),
                "rsrs": float(res.get("rsrs_norm", 0)),
                "control": res.get("control_status", ""),
                "net": net, "bull": bull,
                "signal": res.get("signal_type", ""),
            }
            records.append(rec)
            if res.get("signal_type") == "BUY":
                buy_signals.append(rec)
            elif res.get("signal_type") == "SELL":
                sell_signals.append(rec)

    # 保存逐条记录
    try:
        with open(SIGNAL_RECORDS_FILE, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False)
    except Exception as e:
        print(f"保存信号记录失败: {e}")

    gs = grid_search(records)

    result = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "universe_size": processed,
        "hold_days": hold_days,
        "candidate_records": len(records),
        "current_buy": _agg([r["net"] for r in buy_signals]),
        "current_sell_count": len(sell_signals),
        "grid_search": gs,
        "note": "T日收盘信号、T+1开盘撮合、未来5日按目标/止损/期末收盘估算；含双边佣金/印花税/滑点；非实盘盈亏",
    }
    try:
        with open(SIGNAL_STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存信号统计失败: {e}")
    return result


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    t0 = time.time()
    r = run_cross_sectional_stats(sample_size=n)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print(f"\n耗时 {time.time()-t0:.0f}s, 样本 {r['universe_size']} 只, 记录 {r['candidate_records']} 条")
