"""
A股智能量化投研与实战交易系统 2.0 (QuantitativeTrading 2.0)
系统统一启动入口，支持 Web 控制台与命令行多模式快捷操作
"""
import os
import sys
import json
import time
import socket
import argparse
import subprocess

# 兼容 Windows GBK 控制台编码，防止 UnicodeEncodeError
if sys.platform.startswith('win'):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            try:
                stream.reconfigure(encoding='utf-8', errors='ignore')
            except Exception:
                pass

# config 为轻量模块 (仅 os/json/pathlib)，可安全地在重型量化模块之前导入，
# 以便 --stop / --restart 能在毫秒级完成，不必等待数据引擎与策略模块加载。
from config import load_config

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# =====================================================================================
# 服务生命周期管理：端口占用探测 / 残留进程识别与清理
#
# 背景（Windows 特有陷阱）：
#   uvicorn 绑定端口时会设置 SO_REUSEADDR，而 Windows 允许在端口已被监听的情况下
#   再次绑定成功。因此“bind 成功”不代表端口可用：上一次未退出的进程会继续接收所有
#   新连接，新进程虽然打印了 “Uvicorn running on ...”，却收不到任何请求，表现为卡住。
#   这里用 SO_EXCLUSIVEADDRUSE 做互斥探测，确保能真正发现“端口已被占用”。
# =====================================================================================

def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """精确判断端口是否已被占用（不会被 SO_REUSEADDR 的重复绑定欺骗）。"""
    if host in (None, "", "0.0.0.0"):
        bind_host = "0.0.0.0"
    else:
        bind_host = host

    if sys.platform.startswith("win"):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # SO_EXCLUSIVEADDRUSE 与任何已存在的绑定互斥，是 Windows 上唯一可靠的探测手段
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind((bind_host, port))
            return False
        except OSError:
            return True
        finally:
            probe.close()

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((bind_host, port))
        return False
    except OSError:
        return True
    finally:
        probe.close()


def _listening_pids(port: int):
    """返回正在监听指定端口的进程 PID 集合。"""
    pids = set()
    if not sys.platform.startswith("win"):
        return pids

    ps_cmd = (
        "$ErrorActionPreference='SilentlyContinue';"
        f"Get-NetTCPConnection -LocalPort {port} -State Listen | "
        "Select-Object -ExpandProperty OwningProcess"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=20
        ).stdout or ""
        for token in out.split():
            if token.isdigit():
                pids.add(int(token))
    except Exception:
        pass

    if not pids:
        try:
            out = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=20
            ).stdout or ""
            for line in out.splitlines():
                cols = line.split()
                if len(cols) >= 5 and cols[3].upper() == "LISTENING" and cols[1].endswith(f":{port}"):
                    if cols[4].isdigit():
                        pids.add(int(cols[4]))
        except Exception:
            pass
    return pids


def _process_info(pids):
    """批量获取 PID 对应的进程名与命令行，返回 {pid: {"Name":..., "CommandLine":...}}。"""
    infos = {}
    if not pids or not sys.platform.startswith("win"):
        return infos

    id_list = ",".join(str(p) for p in sorted(pids))
    ps_cmd = (
        "$ErrorActionPreference='SilentlyContinue';"
        f"Get-CimInstance Win32_Process | Where-Object {{ @({id_list}) -contains $_.ProcessId }} | "
        "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=20
        ).stdout or ""
        out = out.strip()
        if out:
            data = json.loads(out)
            if isinstance(data, dict):
                data = [data]
            for item in data:
                try:
                    pid = int(item.get("ProcessId"))
                except Exception:
                    continue
                infos[pid] = {
                    "Name": item.get("Name") or "",
                    "CommandLine": item.get("CommandLine") or ""
                }
    except Exception:
        pass
    return infos


def _looks_like_this_app(pid: int, info: dict) -> bool:
    """判断占用端口的进程是否为本量化系统的 Web 服务（python 运行 main.py）。"""
    name = (info.get("Name") or "").lower()
    cmd = (info.get("CommandLine") or "").lower()
    if not name.startswith("python"):
        return False
    return "main.py" in cmd


def _describe(pid: int, info: dict) -> str:
    name = info.get("Name") or "未知进程"
    cmd = (info.get("CommandLine") or "").strip()
    if len(cmd) > 150:
        cmd = cmd[:150] + "..."
    return f"PID {pid} | {name} | {cmd if cmd else '(无法读取命令行)'}"


def _kill_pids(pids, force: bool = True):
    """结束进程（含子进程树），返回是否全部发出结束指令。"""
    current = os.getpid()
    ok = True
    for pid in sorted(pids):
        if pid == current:
            continue
        try:
            args = ["taskkill", "/PID", str(pid), "/T"]
            if force:
                args.append("/F")
            subprocess.run(args, capture_output=True, text=True, timeout=20)
        except Exception as exc:
            ok = False
            print(f"   ! 结束 PID {pid} 失败: {exc}")
    return ok


def stop_server(port: int, assume_ours: bool = False) -> bool:
    """停止监听指定端口的服务进程，并等待端口真正释放。"""
    pids = _listening_pids(port)
    if not pids:
        print(f"-> 端口 {port} 当前没有监听进程，无需停止。")
        return True

    infos = _process_info(pids)
    print(f"-> 端口 {port} 被以下进程占用：")
    for pid in sorted(pids):
        print(f"   {_describe(pid, infos.get(pid, {}))}")

    if assume_ours:
        foreign = [p for p in pids if not _looks_like_this_app(p, infos.get(p, {}))]
        if foreign:
            print("-> 其中包含非本系统进程，已取消自动清理，请手动确认后再操作。")
            return False

    print("-> 正在结束占用进程...")
    _kill_pids(pids)

    for _ in range(24):  # 最多等待 6 秒
        if not _port_in_use(port):
            print(f"-> 端口 {port} 已释放。")
            return True
        time.sleep(0.25)

    print(f"-> 端口 {port} 仍被占用，请重试或手动结束上述 PID。")
    return False


def ensure_port_available(port: int, host: str) -> bool:
    """启动前检查端口；若占用者为本系统的残留进程则自动清理，否则中止启动。"""
    if not _port_in_use(port, host):
        return True

    pids = _listening_pids(port)
    infos = _process_info(pids)

    print("\n" + "!" * 78)
    print(f"检测到端口 {port} 已被占用，新服务无法正常接管请求。")
    print("!" * 78)
    if pids:
        for pid in sorted(pids):
            print(f"   占用者: {_describe(pid, infos.get(pid, {}))}")
    else:
        print("   占用者: 无法读取进程信息（可能权限不足）")

    ours = bool(pids) and all(_looks_like_this_app(p, infos.get(p, {})) for p in pids)
    if not ours:
        print("\n-> 该端口被其他程序占用。请先关闭它，或运行: python main.py --stop 强制结束。\n")
        return False

    print(f"\n-> 这是本系统上一次未正常退出遗留的进程，正在自动清理端口 {port} ...")
    _kill_pids(pids)
    for _ in range(24):
        if not _port_in_use(port, host):
            print(f"-> 残留进程已结束，端口 {port} 已释放，继续启动。\n")
            return True
        time.sleep(0.25)

    print(f"-> 清理后端口 {port} 仍被占用，请手动执行: python main.py --stop\n")
    return False


def _port_from_argv(argv) -> int:
    """在不加载重型模块的前提下，从命令行解析 --port（缺省回落到配置）。"""
    for idx, item in enumerate(argv):
        if item == "--port" and idx + 1 < len(argv):
            try:
                return int(argv[idx + 1])
            except ValueError:
                pass
        if item.startswith("--port="):
            try:
                return int(item.split("=", 1)[1])
            except ValueError:
                pass
    try:
        return int(load_config().get("web_port", 8089))
    except Exception:
        return 8089


def _handle_early_stop(argv):
    """
    在导入数据引擎等重型模块之前处理 --stop / --restart，
    保证停止服务命令秒级响应，不需要等待策略模块初始化。
    """
    # 仅在主进程生效：uvicorn --reload 会在 Windows 上以 multiprocessing spawn
    # 方式重新导入本模块（此时 __name__ 为 "__mp_main__"），子进程绝不能重复执行停止逻辑。
    if __name__ != "__main__":
        return

    want_stop = "--stop" in argv
    want_restart = "--restart" in argv
    if not (want_stop or want_restart):
        return

    port = _port_from_argv(argv)
    print(f">>> 正在结束端口 {port} 上的量化服务进程...")
    ok = stop_server(port, assume_ours=not want_stop)
    if want_stop:
        sys.exit(0 if ok else 1)
    if not ok:
        print("-> 无法结束现有服务，已中止重启。")
        sys.exit(1)
    print("-> 即将重新启动服务...\n")


_handle_early_stop(sys.argv[1:])

# ==================== 以下为重型量化模块（--stop 已提前返回，不会加载） ====================
import uvicorn

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
   - 因子：日线动量、RSRS与筹码分布研究指标；不构成独立有效性的证明
   - 风控：指数数据缺失时暂停新仓；样本不足时不提供仓位建议；单笔风险预算默认0.5%
   - 跟踪：候选信号和人工录入成交分开；提醒不代表委托或实际成交
   - 回测：日线近似撮合、假设交易成本与基准对比；不等同实盘或独立盲测
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
                res = multi_factor_strategy.analyze(
                    sym, name, df,
                    market_regime_bull=bool(macro.get("data_available") and macro.get("target_exposure", 0) >= 0.65)
                )
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
    print(f">>> 正在对标的 [{symbol}] 执行日线代理回测 (回测天数: {days}，对比沪深300基准)...")
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
    benchmark_text = f"{res['benchmark_return_pct']:+.2f}%" if res['benchmark_return_pct'] is not None else "不可用"
    alpha_text = f"{res['alpha_pct']:+.2f}%" if res['alpha_pct'] is not None else "不可用"
    print(f"沪深300基准收益: {benchmark_text}  | 策略超额收益(Alpha): {alpha_text}")
    print(f"最大回撤 (MaxDD): -{res['max_drawdown_pct']:.2f}%  | 夏普比率 (Sharpe): {res['sharpe_ratio'] if res['sharpe_ratio'] is not None else '不可用'}")
    print(f"卡玛比率 (Calmar): {res['calmar_ratio'] if res['calmar_ratio'] is not None else '不可用'}  | 索提诺比率 (Sortino): {res['sortino_ratio'] if res['sortino_ratio'] is not None else '不可用'}")
    win_rate_text = f"{res['win_rate']}%" if res['win_rate'] is not None else "不可用"
    pl_ratio_text = f"{res['profit_loss_ratio']}:1" if res['profit_loss_ratio'] is not None else "不可用"
    print(f"回测样本胜率 (WinRate): {win_rate_text} ({res['total_trades']} 笔已平仓交易) | 盈亏比: {pl_ratio_text}")
    print(f"时间切分末段收益 (非独立盲测): {res['oos_return_pct']:+.2f}%\n")

def run_cli_diagnose(symbol: str):
    print(BANNER)
    resolved = data_engine.resolve_symbol(symbol)
    if not resolved or not data_engine.is_main_board_equity(resolved.get("symbol", symbol)):
        print(f"未找到代码或标的非主板: {symbol}")
        return
    sym = resolved["symbol"]
    name = resolved["name"]
    print(f">>> 正在对 [{name} ({sym})] 进行多因子与高精筹码深度量化诊断...")
    df = data_engine.get_daily_kline(sym, count=650)
    if df is None:
        print("拉取历史行情失败")
        return
    market = sentiment_radar.analyze_market_regime()
    market_regime_bull = bool(market.get("data_available") and market.get("target_exposure", 0) >= 0.65)
    res = multi_factor_strategy.analyze(sym, name, df, market_regime_bull=market_regime_bull)
    if not res:
        print("计算诊断失败")
        return
    portfolio_state = portfolio_risk.get_watchlist_portfolio_state(portfolio_risk.initial_capital)
    advice = portfolio_risk.calculate_position_size(
        symbol=sym,
        price=res["price"],
        atr=res["atr"],
        total_equity=portfolio_risk.initial_capital,
        current_holdings_count=portfolio_state["holdings_count"],
        market_regime=market.get("regime", "数据不可用·防守"),
        current_exposure_pct=portfolio_state["exposure_pct"]
    )
    advice["portfolio_scope"] = "watchlist holdings only"
    advice["portfolio_data_available"] = portfolio_state["available"]
    print("\n" + "=" * 60)
    print(f"【标的量化诊断: {res['name']} ({res['symbol']})】")
    print("=" * 60)
    print(f"最新收盘价: {res['price']:.2f}元 (今日涨跌: {res['change_pct']:+.2f}%)")
    print(f"综合多因子评分: {res['score']} 分  ->  信号判定: [{res['signal_desc']}]")
    print(f"偏度修正 RSRS: {res['rsrs_norm']} (Beta: {res['rsrs_beta']}, R²: {res['rsrs_r2']})")
    print(f"CYQ 2.0 筹码控盘: {res['control_status']}")
    print(f"  - 主力建仓加权成本: {res['main_cost']:.2f}元 (当前溢价: {res['bias_to_main']:+.1f}%)")
    print(f"  - 全市场获利盘比例: {res['profit_chip_ratio']}% | 90%筹码集中度: {res['concentration_90']}%")
    if advice.get("allowed"):
        print(f"动态仓位建议: 配比 {advice.get('position_pct')}% ({advice.get('suggested_shares')}股，金额约 {advice.get('suggested_capital')}元)")
    else:
        print(f"动态仓位评估: 暂不建议新增仓位 ({advice.get('reason', '风控条件未满足')})")
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

def run_web_server(port: int = None, host: str = None, reload: bool = False):
    cfg = load_config()
    p = int(port or cfg.get("web_port", 8089))
    h = host or cfg.get("web_host", "127.0.0.1")
    display_host = "127.0.0.1" if h in ("0.0.0.0", "", None) else h

    print(BANNER)
    if not ensure_port_available(p, h):
        sys.exit(2)

    print(f"🚀 A股智能量化系统 2.0 Web 控制台已启动!")
    print(f"👉 本地访问地址: http://{display_host}:{p}")
    print("👉 自动化调度后台已就绪，按 Ctrl+C 可停止运行 (单进程运行，退出即释放端口)\n")

    uvicorn_kwargs = {
        "app": "web.app:app",
        "host": h,
        "port": p,
        "reload": reload,
        "log_level": "info",
        # 限制优雅退出等待时长，避免连接或后台任务未释放时长期卡在 "Waiting for ..."
        "timeout_graceful_shutdown": 8,
    }
    if reload:
        # 仅在开发调试时开启：Windows 下 reload 会额外常驻一个监督进程，
        # 若 Ctrl+C 未完整回收，它会继续占用端口并导致下次启动“假启动”。
        uvicorn_kwargs["reload_dirs"] = [
            os.path.join(BASE_DIR, d) for d in ("core", "web", "notifier", "scheduler")
        ]
        try:
            import watchfiles  # noqa: F401
            uvicorn_kwargs["reload_excludes"] = ["storage/*", "*.json", "*.csv", "*.log", ".venv/*"]
        except ImportError:
            pass
        print("⚠️  已开启开发热重载模式 (多进程)，正式运行请去掉 --reload。\n")

    uvicorn.run(**uvicorn_kwargs)

def main():
    parser = argparse.ArgumentParser(description="QuantitativeTrading 2.0 启动入口")
    parser.add_argument("--scan", action="store_true", help="立即执行全市场多因子扫描并在终端输出")
    parser.add_argument("--backtest", action="store_true", help="运行严格因果回测")
    parser.add_argument("--symbol", type=str, default="sh601138", help="回测或诊断股票代码/名称")
    parser.add_argument("--days", type=int, default=600, help="回测天数")
    parser.add_argument("--diagnose", type=str, help="对指定标的进行单股深度诊断")
    parser.add_argument("--review", action="store_true", help="立即执行每日收盘全景复盘与参数自进化")
    parser.add_argument("--notify", action="store_true", help="测试消息推送通道")
    parser.add_argument("--port", type=int, default=None, help="Web 控制台端口 (默认读取配置，缺省 8089)")
    parser.add_argument("--host", type=str, default=None, help="Web 控制台监听地址 (默认读取配置，缺省 127.0.0.1)")
    parser.add_argument("--reload", action="store_true", help="开发模式：代码变更自动重载 (会额外常驻监督进程，不建议日常使用)")
    parser.add_argument("--stop", action="store_true", help="结束占用端口的残留服务进程后退出")
    parser.add_argument("--restart", action="store_true", help="先结束残留服务进程，再重新启动 Web 控制台")

    args = parser.parse_args()

    if args.stop:
        # 已在 main() 之前提前处理，此处仅为帮助信息完整性兜底
        sys.exit(0)
    elif args.scan:
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
        run_web_server(port=args.port, host=args.host, reload=args.reload)

if __name__ == "__main__":
    main()
