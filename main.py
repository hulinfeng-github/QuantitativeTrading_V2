"""
A股智能量化投研与实战交易系统 2.0 (QuantitativeTrading 2.0)
系统统一启动入口，支持 Web 控制台与命令行多模式快捷操作
"""
import sys
import argparse
import uvicorn

# 兼容 Windows GBK 控制台编码，防止 UnicodeEncodeError
if sys.platform.startswith('win') and hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='ignore')
    except Exception:
        pass

from config import load_config
from core.data_engine import data_engine
from core.sentiment_radar import sentiment_radar
from core.multi_factor_strategy import multi_factor_strategy
from core.portfolio_risk import portfolio_risk
from core.backtest_portfolio import backtest_engine_v2
from core.review_engine_v2 import review_engine_v2
from notifier.manager import notification_manager
from scheduler.task_runner import quant_scheduler2

BANNER = """
========================================================================================
             [A股智能量化投研与实战交易系统 2.0 - QuantitativeTrading 2.0]
   - 因子：偏度修正 RSRS 2.0 + Qlib Alpha158 动量量价 + CYQ 2.0 金工研报高精筹码分布
   - 算法：大盘情绪周期雷达 + 动态波动率平价与半凯利仓位配比 + 宏观自适应风险敞口
   - 做T：A股 T+1 分时 VWAP 均价与动态自适应布林带 (%B) + 冲高回落动态对冲止盈
   - 回测：严格杜绝未来函数，完整考虑佣金/印花税/过户费/真实滑点，对标沪深300基准
   - 告警：支持钉钉机器人 (加签加密) + 微信公众号 (PushPlus / Server酱 / 企业微信)
========================================================================================
"""

def run_cli_scan(limit: int = 50):
    print(BANNER)
    print(">>> 正在启动宏观大盘与全市场主板多因子量化选股扫描...")
    macro = sentiment_radar.analyze_market_regime()
    print(f"-> 宏观大盘环境: {macro['regime']} | 情绪温度计: {macro['temp_score']}分 ({macro['cycle_phase']})")
    print(f"-> 建议总风险敞口: {int(macro['target_exposure']*100)}% | 指导: {macro['action_guide']}\n")

    stocks = data_engine.get_main_board_stock_list()
    print(f"-> 成功加载主板高流动性标的池: 共 {len(stocks)} 只有效标的")
    print(f"-> 正在计算多因子特征与筹码结构 (评估前 {limit} 只标的)...")

    candidates = []
    for s in stocks[:limit]:
        sym = s['symbol']
        name = s['name']
        try:
            df = data_engine.get_daily_kline(sym, count=180)
            if df is not None and len(df) >= 30:
                res = multi_factor_strategy.analyze(sym, name, df)
                if res and res.get("score", 0) >= 65:
                    candidates.append(res)
        except Exception:
            continue

    candidates.sort(key=lambda x: x.get("score", 0), reverse=True)
    print("\n" + "=" * 70)
    print(f"【A股主板 2.0 高潜波段精选结果】(精选出 {len(candidates)} 只符合条件标的)")
    print("=" * 70)

    for c in candidates:
        levels = c.get('trade_levels', {})
        print(f"[{c['signal_desc']}] {c['name']} ({c['symbol']}) - 现价: {c['price']:.2f}元 (涨跌: {c['change_pct']:+.2f}%)")
        print(f"   评分: {c['score']}分 | 偏度修正RSRS: {c['rsrs_norm']} | 筹码控盘: {c['control_status']} (主力成本: {c['main_cost']}元)")
        print(f"   风控建议: 止损 {levels.get('initial_stop')}元 (-{levels.get('stop_pct')}%) | 目标止盈 {levels.get('target_price')}元 (+{levels.get('target_pct')}%)")
        print(f"   决策理由: {'; '.join(c.get('reasons', []))}\n")

def run_cli_backtest(symbol: str = "sh601138", days: int = 600):
    print(BANNER)
    print(f">>> 正在对标的 [{symbol}] 执行因果历史回测 (回测天数: {days}，对比沪深300基准)...")
    res = backtest_engine_v2.run_single_stock_backtest(symbol=symbol, days=days)
    if "error" in res:
        print(f"回测失败: {res['error']}")
        return

    print("\n" + "=" * 65)
    print(f"【量化 2.0 回测报告: {res['name']} ({res['symbol']})】")
    print("=" * 65)
    print(f"回测周期: {res['start_date']} 至 {res['end_date']} (共 {res['days']} 个交易日)")
    print(f"初始本金: {res['initial_capital']:,.2f} 元  ->  期末资产: {res['final_equity']:,.2f} 元")
    print(f"策略总累计收益: {res['total_return_pct']:+.2f}%  | 年化复合增长率(CAGR): {res['cagr']:+.2f}%")
    print(f"沪深300基准收益: {res['benchmark_return_pct']:+.2f}%  | 策略超额收益(Alpha): {res['alpha_pct']:+.2f}%")
    print(f"最大回撤 (MaxDD): -{res['max_drawdown_pct']:.2f}%  | 夏普比率 (Sharpe): {res['sharpe_ratio']}")
    print(f"卡玛比率 (Calmar): {res['calmar_ratio']}  | 索提诺比率 (Sortino): {res['sortino_ratio']}")
    print(f"实盘胜率 (WinRate): {res['win_rate']}% ({res['total_trades']} 笔交易) | 盈亏比: {res['profit_loss_ratio']}:1")
    print(f"样本外盲测 (OOS) 收益: {res['oos_return_pct']:+.2f}%\n")

def run_cli_diagnose(symbol: str):
    print(BANNER)
    resolved = data_engine.resolve_symbol(symbol)
    if not resolved:
        print(f"未找到代码或标的非主板: {symbol}")
        return
    sym = resolved["symbol"]
    name = resolved["name"]
    print(f">>> 正在对 [{name} ({sym})] 进行多因子与高精筹码深度量化诊断...")
    df = data_engine.get_daily_kline(sym, count=650)
    if df is None:
        print("拉取历史行情失败")
        return
    res = multi_factor_strategy.analyze(sym, name, df)
    if not res:
        print("计算诊断失败")
        return
    advice = portfolio_risk.calculate_position_size(
        symbol=sym,
        price=res["price"],
        atr=res["atr"],
        total_equity=100000.0,
        current_holdings_count=1
    )
    print("\n" + "=" * 60)
    print(f"【标的量化诊断: {res['name']} ({res['symbol']})】")
    print("=" * 60)
    print(f"最新收盘价: {res['price']:.2f}元 (今日涨跌: {res['change_pct']:+.2f}%)")
    print(f"综合多因子评分: {res['score']} 分  ->  信号判定: [{res['signal_desc']}]")
    print(f"偏度修正 RSRS: {res['rsrs_norm']} (Beta: {res['rsrs_beta']}, R²: {res['rsrs_r2']})")
    print(f"CYQ 2.0 筹码控盘: {res['control_status']}")
    print(f"  - 主力建仓加权成本: {res['main_cost']:.2f}元 (当前溢价: {res['bias_to_main']:+.1f}%)")
    print(f"  - 全市场获利盘比例: {res['profit_chip_ratio']}% | 90%筹码集中度: {res['concentration_90']}%")
    print(f"动态仓位建议: 配比 {advice.get('position_pct')}% ({advice.get('suggested_shares')}股，金额约 {advice.get('suggested_capital')}元)")
    levels = res['trade_levels']
    print(f"ATR 风控体系: 初始止损 {levels['initial_stop']}元 (-{levels['stop_pct']}%) | 目标止盈 {levels['target_price']}元 (+{levels['target_pct']}%)")
    print("量化理由:\n  " + "\n  ".join([f"• {r}" for r in res.get("reasons", [])]) + "\n")

def run_cli_review():
    print(BANNER)
    res = review_engine_v2.run_daily_review(auto_optimize=True)
    print(res.get("report_md", ""))

def run_cli_notify_test():
    print(BANNER)
    print(">>> 正在向配置的钉钉/微信通道发送 2.0 测试卡片...")
    test_sig = {
        "symbol": "sh600519",
        "name": "贵州茅台",
        "signal_type": "BUY",
        "signal_desc": "2.0多因子共振突破买点(测试)",
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
    r = notification_manager.send_signal(test_sig, force=True)
    print(f"推送结果: 钉钉={r['dingtalk']}, 微信={r['wechat']}")

def run_web_server(port: int = None, host: str = None):
    cfg = load_config()
    p = port or cfg.get("web_port", 8089)
    h = host or cfg.get("web_host", "127.0.0.1")

    print(BANNER)
    print(f"🚀 A股智能量化系统 2.0 Web 控制台已启动!")
    print(f"👉 本地访问地址: http://{h}:{p}")
    print(f"👉 自动化调度后台已就绪，按 Ctrl+C 可停止运行\n")

    uvicorn.run("web.app:app", host=h, port=p, reload=True, log_level="info")

def main():
    parser = argparse.ArgumentParser(description="QuantitativeTrading 2.0 启动入口")
    parser.add_argument("--scan", action="store_true", help="立即执行全市场多因子扫描并在终端输出")
    parser.add_argument("--backtest", action="store_true", help="运行严格因果回测")
    parser.add_argument("--symbol", type=str, default="sh601138", help="回测或诊断股票代码/名称")
    parser.add_argument("--days", type=int, default=600, help="回测天数")
    parser.add_argument("--diagnose", type=str, help="对指定标的进行单股深度诊断")
    parser.add_argument("--review", action="store_true", help="立即执行每日收盘全景复盘与参数自进化")
    parser.add_argument("--notify", action="store_true", help="测试消息推送通道")
    parser.add_argument("--port", type=int, default=8089, help="Web 控制台端口 (默认 8089)")

    args = parser.parse_args()

    if args.scan:
        run_cli_scan()
    elif args.backtest:
        run_cli_backtest(symbol=args.symbol, days=args.days)
    elif args.diagnose:
        run_cli_diagnose(symbol=args.diagnose)
    elif args.review:
        run_cli_review()
    elif args.notify:
        run_cli_notify_test()
    else:
        run_web_server(port=args.port)

if __name__ == "__main__":
    main()
