"""
A股智能量化系统 2.0 - FastAPI RESTful 核心服务层 (App 2.0)
提供高并发行情监控、多因子波段选股、高精筹码分布、日内做T、因果回测与复盘自进化 API
"""
import os
import json
import numpy as np
from pathlib import Path
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, HTTPException, Query, Body
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from config import (
    load_config, save_config, SIGNALS_FILE,
    WATCHLIST_FILE, REVIEW_FILE, OPTIMIZATION_FILE
)
from core.data_engine import data_engine
from core.sentiment_radar import sentiment_radar
from core.multi_factor_strategy import multi_factor_strategy
from core.t0_smart_engine import t0_smart_engine
from core.portfolio_risk import portfolio_risk
from core.backtest_portfolio import backtest_engine_v2
from core.review_engine_v2 import review_engine_v2
from core.strategy_tail_230 import tail_strategy_230, tail_trade_tracker
from notifier.manager import notification_manager
from scheduler.task_runner import quant_scheduler2

app = FastAPI(title="A-Share Smart Quant System 2.0", version="2.0.0")

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

# 挂载静态静态前端资源
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.on_event("startup")
async def startup_event():
    cfg = load_config()
    if cfg.get("scheduler_enabled", True):
        quant_scheduler2.start()

@app.get("/")
def read_root():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return {"message": "A股智能量化系统 2.0 已启动"}

@app.get("/api/status")
def get_status():
    return {
        "version": "2.0.0",
        "is_trading_time": quant_scheduler2.is_trading_time(),
        "scheduler_running": quant_scheduler2.is_running,
        "scan_status": quant_scheduler2.scan_status
    }

@app.get("/api/sentiment")
def get_market_sentiment():
    """获取宏观大盘多空情绪雷达与建议仓位敞口"""
    return sentiment_radar.analyze_market_regime()

@app.get("/api/config")
def get_config():
    return load_config()

@app.post("/api/config")
def update_config(cfg: Dict[str, Any]):
    success = save_config(cfg)
    if success:
        multi_factor_strategy.reload_config()
        portfolio_risk.reload_config()
        return {"success": True, "message": "配置保存并热加载生效成功"}
    raise HTTPException(status_code=500, detail="保存配置失败")

@app.post("/api/notify/test")
def test_notification():
    test_signal = {
        "symbol": "sh600519",
        "name": "贵州茅台",
        "signal_type": "BUY",
        "signal_desc": "2.0多因子共振突破买点(测试通道)",
        "price": 1680.00,
        "score": 92.5,
        "rsrs_norm": 1.25,
        "vol_ratio": 1.85,
        "control_status": "主力突破起爆",
        "main_cost": 1620.0,
        "trade_levels": {
            "initial_stop": 1590.0,
            "stop_pct": 5.36,
            "target_price": 1850.0,
            "target_pct": 10.12,
            "breakeven_trigger": 1780.0,
            "trailing_stop_pct": 6.0
        },
        "reasons": [
            "偏度修正RSRS强劲超标(1.25 > 0.70)，主力放量推升",
            "CYQ 筹码高密突破起爆，突破主力建仓成本(1620.0元)，获利盘超85%",
            "20日量价相关系数 +0.48，动能强劲"
        ]
    }
    res = notification_manager.send_signal(test_signal, force=True)
    return {"success": res["dingtalk"] or res["wechat"], "details": res}

@app.get("/api/watchlist")
def get_watchlist():
    """获取自选股清单与实时行情、多因子评分、持仓盈亏"""
    items = []
    if WATCHLIST_FILE.exists():
        try:
            with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
                items = json.load(f)
        except Exception:
            pass

    symbols = [x['symbol'] for x in items if x.get('symbol')]
    quotes = data_engine.get_realtime_quotes(symbols)

    results = []
    for it in items:
        sym = it.get('symbol', '')
        name = it.get('name', sym)
        cost = float(it.get('holding_cost') or it.get('entry_price') or 0.0)
        shares = int(it.get('holding_shares') or it.get('shares') or 0)

        q = quotes.get(sym, {})
        cur_p = q.get('price', 0.0)
        chg_pct = q.get('change_pct', 0.0)

        pnl_pct = round((cur_p - cost) / cost * 100.0, 2) if cost > 0 else 0.0
        pnl_val = round((cur_p - cost) * shares, 2) if cost > 0 else 0.0

        results.append({
            "symbol": sym,
            "code": it.get('code', data_engine.clean_code(sym)),
            "name": name,
            "holding_cost": cost,
            "holding_shares": shares,
            "current_price": cur_p,
            "change_pct": chg_pct,
            "turnover": q.get('turnover', 0.0),
            "pnl_pct": pnl_pct,
            "pnl_val": pnl_val,
            "notes": it.get('notes', '')
        })

    return results

@app.post("/api/watchlist")
def add_to_watchlist(item: Dict[str, Any]):
    sym_or_code = item.get("symbol") or item.get("code")
    if not sym_or_code:
        raise HTTPException(status_code=400, detail="缺少代码或代码无效")

    resolved = data_engine.resolve_symbol(sym_or_code)
    if not resolved:
        raise HTTPException(status_code=400, detail=f"无法解析代码或该股票非主板合规标的: {sym_or_code}")

    sym = resolved["symbol"]
    name = resolved["name"]
    code = resolved["code"]

    items = []
    if WATCHLIST_FILE.exists():
        try:
            with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
                items = json.load(f)
        except Exception:
            pass

    for it in items:
        if it.get("symbol") == sym:
            it["holding_cost"] = float(item.get("holding_cost", it.get("holding_cost", 0.0)))
            it["holding_shares"] = int(item.get("holding_shares", it.get("holding_shares", 0)))
            it["notes"] = item.get("notes", it.get("notes", ""))
            with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False, indent=2)
            return {"success": True, "message": "标的已在自选池，已更新持仓信息", "item": it}

    new_item = {
        "symbol": sym,
        "code": code,
        "name": name,
        "holding_cost": float(item.get("holding_cost", 0.0)),
        "holding_shares": int(item.get("holding_shares", 0)),
        "notes": item.get("notes", "")
    }
    items.append(new_item)
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

    return {"success": True, "message": f"成功添加 {name} 至自选池", "item": new_item}

@app.delete("/api/watchlist/{symbol}")
def delete_from_watchlist(symbol: str):
    sym = data_engine.format_symbol(symbol)
    if not WATCHLIST_FILE.exists():
        return {"success": True}

    try:
        with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
            items = json.load(f)
        filtered = [x for x in items if x.get("symbol") != sym]
        with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(filtered, f, ensure_ascii=False, indent=2)
        return {"success": True, "message": f"已将 {sym} 移出自选池"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/t0/monitor")
def get_t0_monitor():
    """获取所有自选持仓股的日内做T动态偏离与布林通道"""
    watchlist = []
    if WATCHLIST_FILE.exists():
        try:
            with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
                watchlist = json.load(f)
        except Exception:
            pass

    symbols = [x['symbol'] for x in watchlist if x.get('symbol')]
    quotes = data_engine.get_realtime_quotes(symbols)

    results = []
    for it in watchlist:
        sym = it.get('symbol', '')
        name = it.get('name', sym)
        cost = float(it.get('holding_cost') or it.get('entry_price') or 0.0)
        shares = int(it.get('holding_shares') or it.get('shares') or 100)

        q = quotes.get(sym, {})
        cur_p = q.get('price', 0.0)
        if cur_p <= 0:
            continue

        df_kline = data_engine.get_daily_kline(sym, count=60)
        t0_res = t0_smart_engine.analyze_t0(
            symbol=sym,
            name=name,
            current_price=cur_p,
            daily_kline=df_kline,
            holding_cost=cost,
            holding_shares=shares
        )
        t0_res["holding_cost"] = cost
        t0_res["holding_shares"] = shares
        t0_res["change_pct"] = q.get('change_pct', 0.0)
        results.append(t0_res)

    return results

@app.post("/api/t0/reset")
def reset_t0():
    t0_smart_engine.tracker.reset_today()
    return {"success": True, "message": "已重置今日全部做T持仓状态与流水"}

@app.get("/api/stock/minute")
def get_stock_minute(symbol: str):
    """获取个股分时走势与 VWAP 均价曲线"""
    df = data_engine.get_minute_data(symbol)
    if df is None:
        raise HTTPException(status_code=404, detail="无法获取该标的分时数据")
    return df.to_dict(orient="records")

@app.get("/api/stock/diagnose")
def diagnose_stock(symbol: str):
    """单只标的多因子深度量化诊断"""
    resolved = data_engine.resolve_symbol(symbol)
    if not resolved:
        raise HTTPException(status_code=400, detail="未找到该标的或非合规主板股票")

    sym = resolved["symbol"]
    name = resolved["name"]
    df = data_engine.get_daily_kline(sym, count=650)
    if df is None or len(df) < 30:
        raise HTTPException(status_code=400, detail="历史K线数据不足")

    res = multi_factor_strategy.analyze(sym, name, df)
    if not res:
        raise HTTPException(status_code=500, detail="策略指标计算失败")

    # 附带计算仓位建议
    eq_advice = portfolio_risk.calculate_position_size(
        symbol=sym,
        price=res["price"],
        atr=res["atr"],
        total_equity=100000.0,
        current_holdings_count=1
    )
    res["position_advice"] = eq_advice
    return res

@app.get("/api/financial/analyze")
def get_financial_analysis(symbol: str):
    """获取标的上市公司深度财报、杜邦拆解与排雷报告"""
    resolved = data_engine.resolve_symbol(symbol)
    name = resolved["name"] if resolved else symbol
    sym = resolved["symbol"] if resolved else symbol
    from core.financial_engine import financial_engine
    return financial_engine.analyze(sym, name)

@app.get("/api/stock/kline")
def get_stock_kline(symbol: str, count: int = 150):
    """获取K线序列（附带MA均线、MACD与主力筹码）供图表绘制"""
    sym = data_engine.format_symbol(symbol)
    df = data_engine.get_daily_kline(sym, count=count)
    if df is None:
        raise HTTPException(status_code=404, detail="获取K线数据失败")

    from core.alpha_factors import calculate_alpha_features
    from core.rsrs_skew import calculate_rsrs_skew
    from core.chip_engine import chip_engine

    df_feat = calculate_alpha_features(df)
    df_calc = calculate_rsrs_skew(df_feat, n=18, m=600, use_skew=True)
    chip_info = chip_engine.analyze_chips(df_calc)

    records = []
    for _, r in df_calc.iterrows():
        records.append({
            "date": str(r['date'])[:10],
            "open": float(r['open']),
            "close": float(r['close']),
            "high": float(r['high']),
            "low": float(r['low']),
            "volume": float(r['volume']),
            "ma5": round(float(r['ma5']), 2) if not np.isnan(r['ma5']) else None,
            "ma20": round(float(r['ma20']), 2) if not np.isnan(r['ma20']) else None,
            "ma60": round(float(r['ma60']), 2) if not np.isnan(r['ma60']) else None,
            "dif": round(float(r['dif']), 3) if not np.isnan(r['dif']) else 0.0,
            "dea": round(float(r['dea']), 3) if not np.isnan(r['dea']) else 0.0,
            "macd_bar": round(float(r['macd_bar']), 3) if not np.isnan(r['macd_bar']) else 0.0,
            "rsrs_norm": round(float(r['rsrs_norm']), 3) if not np.isnan(r['rsrs_norm']) else 0.0
        })

    return {
        "symbol": sym,
        "kline": records,
        "chip_info": chip_info
    }

@app.post("/api/scan/start")
def start_scan(limit: int = 100):
    quant_scheduler2.run_market_scan_async(limit=limit)
    return {"success": True, "message": "全市场主板智能选股已在后台启动"}

@app.get("/api/scan/results")
def get_scan_results():
    return {
        "status": quant_scheduler2.scan_status,
        "results": quant_scheduler2.latest_scan_results
    }

@app.get("/api/stock/search")
def search_stocks(query: str = Query(..., min_length=1)):
    return data_engine.search_stocks(query, limit=10)

@app.get("/api/signals/history")
def get_signals_history():
    if SIGNALS_FILE.exists():
        try:
            with open(SIGNALS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []

@app.get("/api/backtest")
def run_backtest(symbol: str = "sh601138", days: int = 600):
    """执行因果历史回测（对比沪深300基准）"""
    res = backtest_engine_v2.run_single_stock_backtest(symbol=symbol, days=days)
    if "error" in res:
        raise HTTPException(status_code=400, detail=res["error"])
    return res

@app.get("/api/review/latest")
def get_latest_review():
    if REVIEW_FILE.exists():
        try:
            with open(REVIEW_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
                if history:
                    return history[0]
        except Exception:
            pass
    return review_engine_v2.run_daily_review(auto_optimize=False)

@app.get("/api/review/history")
def get_review_history():
    if REVIEW_FILE.exists():
        try:
            with open(REVIEW_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []

@app.post("/api/review/run")
def run_review_now():
    res = review_engine_v2.run_daily_review(auto_optimize=True)
    return {"success": True, "message": "收盘量化复盘与参数自迭代执行完成", "data": res}

@app.get("/api/optimization/log")
def get_optimization_log():
    if OPTIMIZATION_FILE.exists():
        try:
            with open(OPTIMIZATION_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []

# ==================== 尾盘 2:30 突袭战法专用 API ====================

@app.get("/api/tail230/status")
def get_tail230_status():
    """获取当前是否处于尾盘 14:30 战法最佳决策窗口"""
    from datetime import datetime
    now = datetime.now()
    cur_hm = now.strftime("%H:%M")
    is_window = ("14:30" <= cur_hm <= "14:57") and (now.weekday() < 5)
    return {
        "current_time": cur_hm,
        "is_decision_window": is_window,
        "action_guide": "⚡ 当前正处于 14:30~14:57 尾盘量化抢筹决策黄金窗口！" if is_window else "💤 非尾盘决策时间 (最佳窗口为每个交易日 14:30 - 14:55)"
    }

@app.get("/api/tail230/candidates")
def get_tail230_candidates(force_refresh: bool = False):
    """获取今日 2.30 战法全市场主板扫描结果"""
    return tail_strategy_230.scan_today_candidates(limit=30, force_refresh=force_refresh)

@app.get("/api/tail230/performance")
def get_tail230_performance(force_refresh: bool = False):
    """获取 2.30 战法独立历史实战胜率与各项量化指标"""
    return tail_strategy_230.get_cached_or_run_backtest(force_refresh=force_refresh)

@app.post("/api/tail230/recalculate")
def recalculate_tail230_win_rate(days: int = Query(default=120, ge=30, le=500)):
    """独立重新计算 2.30 战法的胜率与回测表现"""
    res = tail_strategy_230.calculate_strategy_win_rate(days=days)
    return {"success": True, "data": res}

@app.get("/api/tail230/trades")
def get_tail230_trades():
    """获取 2.30 战法次日卖出监控池及历史持仓记录"""
    return tail_trade_tracker.get_all_trades()

@app.post("/api/tail230/trades/add")
def add_tail230_trade(data: Dict[str, Any] = Body(...)):
    """手动将某只标的加入 2.30 战法次日卖出盯盘"""
    sym = data.get("symbol")
    name = data.get("name", sym)
    price = float(data.get("buy_price") or data.get("price") or 0.0)
    if not sym or price <= 0:
        raise HTTPException(status_code=400, detail="代码与买入价不能为空且必须大于0")
    t = tail_trade_tracker.add_manual_trade(
        symbol=sym,
        name=name,
        buy_price=price,
        buy_date=data.get("buy_date"),
        target_pct=float(data.get("target_pct", 2.5)),
        stop_pct=float(data.get("stop_pct", 1.8))
    )
    return {"success": True, "trade": t}

@app.post("/api/tail230/trades/close")
def close_tail230_trade(data: Dict[str, Any] = Body(...)):
    """手动平仓/完成某笔 2.30 战法交易"""
    sym = data.get("symbol")
    buy_date = data.get("buy_date")
    price = float(data.get("exit_price", 0.0))
    reason = data.get("exit_reason", "手动平仓离场")
    status = data.get("status", "CLOSED_MANUAL")
    t = tail_trade_tracker.close_trade(sym, buy_date, status, price, reason)
    if not t:
        raise HTTPException(status_code=404, detail="未找到对应的 2.30 战法持仓记录")
    return {"success": True, "trade": t}

