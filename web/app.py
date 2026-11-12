"""
A股智能量化系统 2.0 - FastAPI RESTful 核心服务层 (App 2.0)
提供高并发行情监控、多因子波段选股、高精筹码分布、日内做T、因果回测与复盘自进化 API
"""
import os
import json
import numpy as np
from pathlib import Path
from typing import Dict, Any, List, Optional
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query, Body, Response
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
from core.t0_group_manager import t0_group_manager

@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config()
    if cfg.get("scheduler_enabled", True):
        quant_scheduler2.start()
    yield
    quant_scheduler2.stop()

app = FastAPI(title="A-Share Smart Quant System 2.0", version="2.0.0", lifespan=lifespan)

@app.middleware("http")
async def add_no_cache_headers(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static") or path == "/" or path.startswith("/api"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

# 挂载静态前端资源
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/favicon.ico", include_in_schema=False)
def get_favicon():
    fav = STATIC_DIR / "favicon.ico"
    if fav.exists():
        return FileResponse(str(fav), media_type="image/x-icon")
    return Response(status_code=204)

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

@app.get("/api/dingtalk/status")
def get_dingtalk_status():
    """获取钉钉推送通道的实时频控状态与配额监控数据"""
    return notification_manager.get_dingtalk_status()

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
    groups_list = t0_group_manager.get_groups(include_stocks=False)
    groups_map = {g.get("id"): g.get("name", "未命名组") for g in groups_list}

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

        gid = it.get('group_id', '')
        gname = groups_map.get(gid, "默认做T组" if gid == "group_default" else "未分组") if gid else "未分组"

        results.append({
            "symbol": sym,
            "code": it.get('code', data_engine.clean_code(sym)),
            "name": name,
            "holding_cost": cost,
            "holding_shares": shares,
            "available_shares": int(it.get('available_shares') or 0),
            "current_price": cur_p,
            "change_pct": chg_pct,
            "turnover": q.get('turnover', 0.0),
            "pnl_pct": pnl_pct,
            "pnl_val": pnl_val,
            "notes": it.get('notes', ''),
            "group_id": gid,
            "group_name": gname,
            "t0_enabled": it.get('t0_enabled', True)
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
    group_id = item.get("group_id", "")
    t0_enabled = bool(item.get("t0_enabled", True))

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
            it["available_shares"] = min(
                int(item.get("available_shares", it.get("available_shares", 0)) or 0),
                max(0, it["holding_shares"])
            )
            it["notes"] = item.get("notes", it.get("notes", ""))
            if "group_id" in item:
                it["group_id"] = group_id
            if "t0_enabled" in item:
                it["t0_enabled"] = t0_enabled
            with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False, indent=2)
            return {"success": True, "message": "标的已在自选池，已更新持仓信息", "item": it}

    new_item = {
        "symbol": sym,
        "code": code,
        "name": name,
        "holding_cost": float(item.get("holding_cost", 0.0)),
        "holding_shares": int(item.get("holding_shares", 0)),
        "available_shares": min(
            max(0, int(item.get("available_shares", 0) or 0)),
            max(0, int(item.get("holding_shares", 0) or 0))
        ),
        "notes": item.get("notes", ""),
        "group_id": group_id,
        "t0_enabled": t0_enabled
    }
    items.append(new_item)
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

    return {"success": True, "message": f"成功添加 {name} 至自选池", "item": new_item}

@app.put("/api/watchlist/{symbol}")
@app.post("/api/watchlist/{symbol}")
def update_watchlist_item(symbol: str, data: Dict[str, Any] = Body(...)):
    """更新自选股持仓成本、持股数量、所属做T分组及备注信息"""
    sym = data_engine.format_symbol(symbol)
    if not WATCHLIST_FILE.exists():
        raise HTTPException(status_code=404, detail="自选池为空")

    try:
        with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
            items = json.load(f)

        found = False
        target_item = None
        for it in items:
            if it.get("symbol") == sym:
                found = True
                if "holding_cost" in data:
                    it["holding_cost"] = max(0.0, float(data["holding_cost"]))
                if "holding_shares" in data:
                    it["holding_shares"] = max(0, int(data["holding_shares"]))
                if "available_shares" in data:
                    it["available_shares"] = max(0, int(data["available_shares"]))
                it["available_shares"] = min(
                    max(0, int(it.get("available_shares", 0) or 0)),
                    max(0, int(it.get("holding_shares", 0) or 0))
                )
                if "notes" in data:
                    it["notes"] = str(data["notes"]).strip()
                if "group_id" in data:
                    gid = str(data["group_id"]).strip()
                    it["group_id"] = gid
                    t0_group_manager.set_stock_group(sym, gid)
                if "t0_enabled" in data:
                    it["t0_enabled"] = bool(data["t0_enabled"])
                target_item = it
                break

        if not found:
            raise HTTPException(status_code=404, detail=f"自选池中未找到标的 {sym}")

        with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)

        return {
            "success": True,
            "message": f"已成功更新 {target_item.get('name', sym)} 的持仓信息",
            "item": target_item
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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
    if not load_config().get("t0_monitor_enabled", True):
        return []

    watchlist = []
    if WATCHLIST_FILE.exists():
        try:
            with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
                watchlist = json.load(f)
        except Exception:
            pass

    symbols = [
        x['symbol'] for x in watchlist
        if x.get('symbol') and x.get('t0_enabled', True)
        # 做T需同时支持主板个股与场内ETF（如 51x/56x/58x/15x/16x），不能用仅限个股的 is_main_board_equity
        and data_engine.is_main_board(x['symbol'])
    ]
    quotes = data_engine.get_realtime_quotes(symbols)
    market_state = sentiment_radar.analyze_market_regime()
    market_exposure = (
        float(market_state.get("target_exposure"))
        if market_state.get("data_available") and market_state.get("target_exposure") is not None
        else None
    )

    results = []
    groups_list = t0_group_manager.get_groups(include_stocks=False)
    groups_map = {g.get("id"): g.get("name", "未命名组") for g in groups_list}

    for it in watchlist:
        sym = it.get('symbol', '')
        if not it.get('t0_enabled', True) or not data_engine.is_main_board(sym):
            continue
        name = it.get('name', sym)
        cost = float(it.get('holding_cost') or it.get('entry_price') or 0.0)
        shares = int(it.get('holding_shares') or it.get('shares') or 0)
        available_shares = min(int(it.get('available_shares') or 0), shares)

        q = quotes.get(sym, {})
        cur_p = q.get('price', 0.0)
        if cur_p <= 0:
            continue

        df_kline = data_engine.get_daily_kline(sym, count=61)
        t0_res = t0_smart_engine.analyze_t0(
            symbol=sym,
            name=name,
            current_price=cur_p,
            daily_kline=df_kline,
            holding_cost=cost,
            holding_shares=shares,
            available_shares=available_shares,
            market_exposure=market_exposure
        )
        gid = it.get('group_id', '')
        gname = groups_map.get(gid, "默认做T组" if gid == "group_default" else "未分组") if gid else "未分组"

        t0_res["holding_cost"] = cost
        t0_res["holding_shares"] = shares
        t0_res["available_shares"] = available_shares
        t0_res["change_pct"] = q.get('change_pct', 0.0)
        t0_res["group_id"] = gid
        t0_res["group_name"] = gname
        t0_res["t0_enabled"] = it.get('t0_enabled', True)
        results.append(t0_res)

        # 实时自动同步推送做T开仓/平仓/止损指令 (去重机制由 notification_manager 兜底，绝不重复刷屏)
        act = t0_res.get("signal_action")
        if act in ["BUY_T", "SELL_T", "CLOSE_T", "STOP_T"]:
            sig_payload = t0_smart_engine.build_signal_payload(t0_res)
            sig_payload["group_id"] = gid
            sig_payload["group_name"] = gname
            notification_manager.send_signal(sig_payload)

    return results

@app.post("/api/t0/reset")
def reset_t0():
    t0_smart_engine.tracker.reset_today()
    return {"success": True, "message": "已重置今日全部做T持仓状态与流水"}

@app.get("/api/t0/stats")
def get_t0_stats():
    """获取日内做T历史已平仓样本的绩效统计 (信号代理口径，未含费用)"""
    return t0_smart_engine.get_stats()

# ==================== 做T分组与钉钉群管理专属 API ====================

@app.get("/api/t0/groups")
def get_t0_groups():
    """获取所有做T分组详情，包含各组关联股票列表与数量"""
    return t0_group_manager.get_groups(include_stocks=True)

@app.post("/api/t0/groups")
def save_t0_group(data: Dict[str, Any] = Body(...)):
    """创建或更新做T分组配置"""
    grp = t0_group_manager.save_group(data)
    return {"success": True, "group": grp}

@app.delete("/api/t0/groups/{group_id}")
def delete_t0_group(group_id: str):
    """删除做T分组"""
    success = t0_group_manager.delete_group(group_id)
    if not success:
        raise HTTPException(status_code=404, detail="未找到该做T分组")
    return {"success": True, "message": "做T分组已成功删除"}

@app.post("/api/t0/groups/test")
def test_t0_group_notify(data: Dict[str, Any] = Body(...)):
    """向指定做T分组的钉钉机器人发送测试联通卡片"""
    res = t0_group_manager.test_group_notification(
        group_id=data.get("group_id"),
        webhook=data.get("dingtalk_webhook"),
        secret=data.get("dingtalk_secret")
    )
    return res

@app.post("/api/watchlist/group")
def set_watchlist_stock_group(data: Dict[str, Any] = Body(...)):
    """为单只或多只标的批量划分做T分组"""
    symbols = data.get("symbols")
    symbol = data.get("symbol")
    group_id = data.get("group_id", "")
    if symbols and isinstance(symbols, list):
        ok = t0_group_manager.batch_set_stock_group(symbols, group_id)
    elif symbol:
        ok = t0_group_manager.set_stock_group(symbol, group_id)
    else:
        raise HTTPException(status_code=400, detail="必须提供 symbol 或 symbols")
    return {"success": ok}

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
    if not resolved or not data_engine.is_main_board_equity(resolved.get("symbol", symbol)):
        raise HTTPException(status_code=400, detail="未找到该标的或非合规主板股票")

    sym = resolved["symbol"]
    name = resolved["name"]
    df = data_engine.get_daily_kline(sym, count=650)
    if df is None or len(df) < 30:
        raise HTTPException(status_code=400, detail="历史K线数据不足")

    market = sentiment_radar.analyze_market_regime()
    res = multi_factor_strategy.analyze(
        sym, name, df,
        market_regime_bull=bool(market.get("data_available") and market.get("target_exposure", 0) >= 0.65)
    )
    if not res:
        raise HTTPException(status_code=500, detail="策略指标计算失败")

    # 附带计算仓位建议
    portfolio_state = portfolio_risk.get_watchlist_portfolio_state(portfolio_risk.initial_capital)
    eq_advice = portfolio_risk.calculate_position_size(
        symbol=sym,
        price=res["price"],
        atr=res["atr"],
        total_equity=portfolio_risk.initial_capital,
        current_holdings_count=portfolio_state["holdings_count"],
        market_regime=market.get("regime", "数据不可用·防守"),
        current_exposure_pct=portfolio_state["exposure_pct"]
    )
    eq_advice["portfolio_scope"] = "watchlist holdings only"
    eq_advice["portfolio_data_available"] = portfolio_state["available"]
    res["position_advice"] = eq_advice
    return res

@app.get("/api/financial/analyze")
def get_financial_analysis(symbol: str, force_refresh: bool = False):
    """获取标的上市公司深度财报、杜邦拆解与排雷报告"""
    resolved = data_engine.resolve_symbol(symbol)
    name = resolved["name"] if resolved else symbol
    sym = resolved["symbol"] if resolved else symbol
    from core.financial_engine import financial_engine
    return financial_engine.analyze(sym, name, force_refresh=force_refresh)

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
            "ma5": round(float(r['ma5']), 2) if 'ma5' in r and not np.isnan(r['ma5']) else None,
            "ma20": round(float(r['ma20']), 2) if 'ma20' in r and not np.isnan(r['ma20']) else None,
            "ma60": round(float(r['ma60']), 2) if 'ma60' in r and not np.isnan(r['ma60']) else None,
            "dif": round(float(r['dif']), 3) if 'dif' in r and not np.isnan(r['dif']) else 0.0,
            "dea": round(float(r['dea']), 3) if 'dea' in r and not np.isnan(r['dea']) else 0.0,
            "macd_bar": round(float(r['macd_bar']), 3) if 'macd_bar' in r and not np.isnan(r['macd_bar']) else 0.0,
            "rsrs_norm": round(float(r['rsrs_norm']), 3) if 'rsrs_norm' in r and not np.isnan(r['rsrs_norm']) else 0.0
        })

    return {
        "symbol": sym,
        "kline": records,
        "chip_info": chip_info
    }

@app.post("/api/scan/start")
def start_scan(limit: int = 100, notify: bool = False):
    quant_scheduler2.run_market_scan_async(limit=limit, notify=notify)
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
    return {"success": True, "message": "收盘量化复盘完成；自动调参默认开启，已结算样本不足30笔时只记录、不修改参数", "data": res}

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
    """获取尾盘规则的日线代理回放统计，不是实盘胜率"""
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
    """录入用户确认的主板实际买入成交，仅用于行情提醒。"""
    sym = data.get("symbol")
    name = data.get("name", sym)
    price = float(data.get("buy_price") or data.get("price") or 0.0)
    if not sym or price <= 0 or not data_engine.is_main_board_equity(sym):
        raise HTTPException(status_code=400, detail="请输入沪深主板普通股代码及有效的实际成交价")
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
    """录入用户确认的实际卖出成交价；行情提醒本身不会结算成交。"""
    sym = data.get("symbol")
    buy_date = data.get("buy_date")
    price = float(data.get("exit_price", 0.0))
    if price <= 0:
        raise HTTPException(status_code=400, detail="实际卖出成交价必须大于0")
    reason = data.get("exit_reason", "手动平仓离场")
    t = tail_trade_tracker.close_trade(sym, buy_date, "CLOSED_MANUAL", price, reason)
    if not t:
        raise HTTPException(status_code=404, detail="未找到对应的 2.30 战法持仓记录")
    return {"success": True, "trade": t}

# ==================== 游资龙虎打板量化与自动台账 API ====================

@app.get("/api/limitup/radar")
def get_limitup_radar(limit: int = Query(default=20, ge=1, le=50)):
    """获取今日全市场主板涨停板雷达与硬度评分"""
    from core.limit_up_engine import limit_up_engine
    return limit_up_engine.scan_today_candidates(limit=limit)

@app.get("/api/limitup/stats")
def get_limitup_stats():
    """获取打板量化模型的全量胜率、连板率、隔日溢价与板型细分统计"""
    from core.limit_up_engine import limit_up_engine
    return limit_up_engine.calculate_model_stats()

@app.get("/api/limitup/ledger")
def get_limitup_ledger(limit: int = Query(default=100, ge=1, le=500)):
    """获取打板交易流水账本明细"""
    from core.limit_up_engine import limit_up_engine
    return limit_up_engine.get_all_trades(limit=limit)

@app.post("/api/limitup/refresh")
def refresh_limitup_settlement():
    """手动触发打板流水账本 T+1 真实行情结算与 CSV 重新导出"""
    from core.limit_up_engine import limit_up_engine
    res = limit_up_engine.review_and_update_trades()
    return {"success": True, "message": "打板账本 T+1 结算与表格更新完成", "stats": res}

@app.post("/api/limitup/record")
def record_limitup_trades(data: Dict[str, Any] = Body(default={})):
    """将今日精选打板候选股票一键录入实战跟踪账本"""
    from core.limit_up_engine import limit_up_engine
    top_n = int(data.get("top_n", 3))
    candidates = limit_up_engine.scan_today_candidates(limit=10)
    added = limit_up_engine.record_board_hitting_trades(candidates, top_n=top_n)
    return {"success": True, "added_count": len(added), "added": added}

@app.get("/api/limitup/csv")
def download_limitup_csv():
    """下载或查看带有 UTF-8 BOM 的打板实战流水表格 (Excel / WPS 即点即开)"""
    from config import LIMIT_UP_CSV_FILE
    from core.limit_up_engine import limit_up_engine
    if not LIMIT_UP_CSV_FILE.exists():
        limit_up_engine.export_csv()
    return FileResponse(
        str(LIMIT_UP_CSV_FILE),
        media_type="text/csv",
        filename="A股打板量化实战流水账本.csv"
    )

@app.post("/api/limitup/notify")
def send_limitup_notification(data: Dict[str, Any] = Body(...)):
    """向钉钉和微信推送单只股票详细的新手小白打板操盘指导卡片"""
    from core.limit_up_engine import limit_up_engine
    res = limit_up_engine.send_limit_up_signal(data)
    return res

