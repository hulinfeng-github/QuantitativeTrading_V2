"""
A股智能量化系统 2.0 - 多源韧性数据引擎 (DataEngine 2.0)
特色：
1. 专为主板定制（严格过滤科创板688、创业板300、北交所及ST风险股）；
2. 腾讯/新浪极速行情高可用容灾与自动故障切换；
3. 全市场涨跌比、大盘指数基准(上证指数/沪深300)与市场流动性广度聚合；
4. 真实分时毫秒级拉取与动态 VWAP 实时聚合；
5. 本地轻量化缓存与连接池长连接加速。
"""
import re
import json
import time
import threading
import requests
import pandas as pd
import numpy as np
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
from config import BASE_DIR, DATA_DIR, load_config
from core.persistence import atomic_write_json, safe_read_json

STOCK_LIST_CACHE = DATA_DIR / "main_board_stocks.json"
PLATES_CACHE_FILE = DATA_DIR / "stock_plates_cache.json"

class DataEngine:
    def __init__(self):
        self.session = requests.Session()
        self.session.trust_env = False  # 绕过本地VPN/代理，直连国内金融行情服务器
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer": "https://finance.qq.com"
        })
        self._lock = threading.RLock()
        self._quote_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._kline_5m_cache: Dict[str, Tuple[float, pd.DataFrame]] = {}
        self._trading_dates_cache: Tuple[float, List[str]] = (0.0, [])
        self._plates_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_ttl = 2.0  # 行情内存缓存 2 秒
        self._load_plates_cache()

    def _load_plates_cache(self):
        data = safe_read_json(PLATES_CACHE_FILE, {})
        if isinstance(data, dict):
            self._plates_cache = data

    @staticmethod
    def clean_code(code: str) -> str:
        """从任意格式提取纯6位数字代码"""
        if not code:
            return ""
        digits = re.sub(r'[^0-9]', '', str(code).strip())
        return digits

    @classmethod
    def is_main_board(cls, code: str) -> bool:
        """
        判断是否为A股主板股票代码或主流场内ETF基金
        沪市主板: 600, 601, 603, 605; 沪市主流ETF: 51, 56, 58
        深市主板: 000, 001, 002, 003; 深市主流ETF: 15, 16
        严格过滤高风险高门槛: 科创板(688)、创业板(300)、北交所(8/4/920)
        """
        digits = cls.clean_code(code)
        if len(digits) != 6:
            return False
        # 沪市主板与场内主流ETF
        if digits.startswith(("600", "601", "603", "605", "51", "56", "58")):
            return True
        # 深市主板与场内主流ETF
        if digits.startswith(("000", "001", "002", "003", "15", "16")):
            return True
        return False

    @classmethod
    def is_main_board_equity(cls, code: str) -> bool:
        """仅识别沪深主板普通股；ETF 等场内基金不进入个股选股池。"""
        digits = cls.clean_code(code)
        return len(digits) == 6 and digits.startswith(("600", "601", "603", "605", "000", "001", "002", "003"))

    @classmethod
    def format_symbol(cls, code: str) -> str:
        """格式化为标准市场前缀代码，如 sh600519, sz000001, sh000300"""
        c = str(code).strip().lower()
        if c.startswith(("sh", "sz", "bj")):
            return c
        digits = cls.clean_code(code)
        if len(digits) == 6:
            if digits.startswith(("6", "5")):
                return f"sh{digits}"
            elif digits.startswith(("0", "1", "3")):
                return f"sz{digits}"
            elif digits.startswith(("8", "4", "9")):
                return f"bj{digits}"
        return digits or c

    def get_realtime_quotes(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        批量获取实时行情快照（腾讯极速API + 新浪备选）
        """
        results = {}
        if not symbols:
            return results

        clean_syms = [self.format_symbol(s) for s in symbols if s]
        now = time.time()
        to_fetch = []
        for s in clean_syms:
            if s in self._quote_cache and (now - self._quote_cache[s][0]) < self._cache_ttl:
                results[s] = self._quote_cache[s][1]
            else:
                to_fetch.append(s)

        if not to_fetch:
            return results

        # 分批拉取腾讯极速行情（每批 60 只）
        chunk_size = 60
        for i in range(0, len(to_fetch), chunk_size):
            chunk = to_fetch[i : i + chunk_size]
            query_str = ",".join(chunk)
            url = f"https://qt.gtimg.cn/q={query_str}"
            try:
                resp = self.session.get(url, timeout=3.5)
                if resp.status_code != 200:
                    continue
                text = resp.content.decode("gbk", errors="ignore")
                for line in text.strip().split(";"):
                    line = line.strip()
                    if not line or '="' not in line:
                        continue
                    m = re.match(r"v_(.*?)=\"(.*?)\"", line)
                    if not m:
                        continue
                    sym = m.group(1).strip()
                    fields = m.group(2).split("~")
                    if len(fields) < 35:
                        continue

                    name = fields[1]
                    code = fields[2]
                    price = float(fields[3]) if fields[3] else 0.0
                    prev_close = float(fields[4]) if fields[4] else 0.0
                    open_price = float(fields[5]) if fields[5] else 0.0
                    volume = float(fields[6]) if fields[6] else 0.0
                    outer_vol = float(fields[7]) if len(fields) > 7 and fields[7] else 0.0
                    inner_vol = float(fields[8]) if len(fields) > 8 and fields[8] else 0.0
                    bid1 = float(fields[9]) if len(fields) > 9 and fields[9] else 0.0
                    bid1_vol = float(fields[10]) if len(fields) > 10 and fields[10] else 0.0
                    ask1 = float(fields[19]) if len(fields) > 19 and fields[19] else 0.0
                    ask1_vol = float(fields[20]) if len(fields) > 20 and fields[20] else 0.0
                    high = float(fields[33]) if len(fields) > 33 and fields[33] else price
                    low = float(fields[34]) if len(fields) > 34 and fields[34] else price
                    turnover = float(fields[37]) if len(fields) > 37 and fields[37] else 0.0
                    turnover_rate = float(fields[38]) if len(fields) > 38 and fields[38] else 0.0
                    amplitude = float(fields[43]) if len(fields) > 43 and fields[43] else 0.0
                    float_mkt_cap = float(fields[44]) if len(fields) > 44 and fields[44] else 0.0
                    limit_up_price = float(fields[47]) if len(fields) > 47 and fields[47] else (round(prev_close * 1.10, 2) if prev_close > 0 else 0.0)
                    limit_down_price = float(fields[48]) if len(fields) > 48 and fields[48] else (round(prev_close * 0.90, 2) if prev_close > 0 else 0.0)
                    vol_ratio = float(fields[49]) if len(fields) > 49 and fields[49] else 0.0
                    vwap = float(fields[51]) if len(fields) > 51 and fields[51] else 0.0
                    change_pct = float(fields[32]) if len(fields) > 32 and fields[32] else 0.0

                    turnover_yuan = turnover * 10000.0
                    if vwap <= 0 and volume > 0 and turnover_yuan > 0:
                        vwap = round(turnover_yuan / (volume * 100.0), 3)

                    q_data = {
                        "symbol": sym,
                        "code": code,
                        "name": name,
                        "price": price,
                        "prev_close": prev_close,
                        "open": open_price,
                        "high": high,
                        "low": low,
                        "volume": volume,
                        "turnover": turnover_yuan,  # 转换为元
                        "change_pct": change_pct,
                        "outer_vol": outer_vol,
                        "inner_vol": inner_vol,
                        "bid1": bid1,
                        "bid1_vol": bid1_vol,
                        "ask1": ask1,
                        "ask1_vol": ask1_vol,
                        "turnover_rate": turnover_rate,
                        "amplitude": amplitude,
                        "float_mkt_cap": float_mkt_cap,  # 亿元
                        "limit_up_price": limit_up_price,
                        "limit_down_price": limit_down_price,
                        "vol_ratio": vol_ratio,
                        "vwap": vwap,
                        "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    }
                    results[sym] = q_data
                    self._quote_cache[sym] = (now, q_data)
            except Exception as e:
                # 腾讯异常时，尝试从新浪备选拉取
                fallback_results = self._fetch_sina_quotes_fallback(chunk)
                results.update(fallback_results)

        return results

    def get_realtime_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """获取单只标的的实时行情快照"""
        if not symbol:
            return None
        res = self.get_realtime_quotes([symbol])
        return res.get(symbol)

    def _fetch_sina_quotes_fallback(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """新浪极速行情备用通道"""
        results = {}
        if not symbols:
            return results
        query_str = ",".join(symbols)
        url = f"https://hq.sinajs.cn/list={query_str}"
        try:
            resp = self.session.get(url, headers={"Referer": "https://finance.sina.com.cn"}, timeout=3)
            if resp.status_code == 200:
                text = resp.content.decode("gbk", errors="ignore")
                for line in text.strip().split(";"):
                    if not line or '="' not in line:
                        continue
                    m = re.match(r"var hq_str_(.*?)=\"(.*?)\"", line.strip())
                    if not m:
                        continue
                    sym = m.group(1).strip()
                    parts = m.group(2).split(",")
                    if len(parts) >= 10:
                        name = parts[0]
                        open_p = float(parts[1]) if parts[1] else 0.0
                        prev_c = float(parts[2]) if parts[2] else 0.0
                        price = float(parts[3]) if parts[3] else 0.0
                        high = float(parts[4]) if parts[4] else price
                        low = float(parts[5]) if parts[5] else price
                        vol = float(parts[8]) / 100.0 if parts[8] else 0.0
                        amt = float(parts[9]) if parts[9] else 0.0
                        pct = round((price - prev_c) / prev_c * 100.0, 2) if prev_c > 0 else 0.0

                        data = {
                            "symbol": sym,
                            "code": self.clean_code(sym),
                            "name": name,
                            "price": price,
                            "prev_close": prev_c,
                            "open": open_p,
                            "high": high,
                            "low": low,
                            "volume": vol,
                            "turnover": amt,
                            "change_pct": pct,
                            "update_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        }
                        results[sym] = data
                        self._quote_cache[sym] = (time.time(), data)
        except Exception:
            pass
        return results

    def get_daily_kline(self, symbol: str, count: int = 650) -> Optional[pd.DataFrame]:
        """
        获取单只股票或大盘指数前复权历史日K线数据 (腾讯/新浪双通道容灾)
        返回标准 DataFrame: date, open, close, high, low, volume
        """
        sym = self.format_symbol(symbol)

        # 1. 指数类标的优先走新浪直连通道 (极速无WAF)
        if sym in ["sh000001", "sh000300", "sz399001", "sz399006"] or sym.startswith(("sh000", "sz399")):
            sina_url = f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={count}"
            try:
                resp = self.session.get(sina_url, timeout=3)
                if resp.status_code == 200:
                    raw_items = resp.json()
                    if isinstance(raw_items, list) and len(raw_items) > 0:
                        records = []
                        for item in raw_items:
                            records.append({
                                "date": str(item['day'])[:10],
                                "open": float(item['open']),
                                "close": float(item['close']),
                                "high": float(item['high']),
                                "low": float(item['low']),
                                "volume": float(item.get('volume', 0.0))
                            })
                        df = pd.DataFrame(records)
                        df['date'] = pd.to_datetime(df['date'])
                        df = df.sort_values('date').reset_index(drop=True)
                        return df
            except Exception:
                pass

        # 2. 个股类通道 (腾讯接口)
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,{count},qfq"
        try:
            resp = self.session.get(url, timeout=4)
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                stock_data = data.get(sym, {})
                kline_list = stock_data.get("qfqday") or stock_data.get("day")
                if kline_list:
                    records = []
                    for item in kline_list:
                        if len(item) < 6:
                            continue
                        records.append({
                            "date": str(item[0])[:10],
                            "open": float(item[1]),
                            "close": float(item[2]),
                            "high": float(item[3]),
                            "low": float(item[4]),
                            "volume": float(item[5])
                        })
                    df = pd.DataFrame(records)
                    df['date'] = pd.to_datetime(df['date'])
                    df = df.sort_values('date').reset_index(drop=True)
                    return df
        except Exception:
            pass

        # 3. 腾讯受阻时备选新浪通道 (新浪个股通道同样支持 datalen 大额拉取，勿截断到120)
        try:
            sina_url = f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={sym}&scale=240&ma=no&datalen={count}"
            resp = self.session.get(sina_url, timeout=3)
            if resp.status_code == 200:
                raw_items = resp.json()
                if isinstance(raw_items, list) and len(raw_items) > 0:
                    records = []
                    for item in raw_items:
                        records.append({
                            "date": str(item['day'])[:10],
                            "open": float(item['open']),
                            "close": float(item['close']),
                            "high": float(item['high']),
                            "low": float(item['low']),
                            "volume": float(item.get('volume', 0.0))
                        })
                    df = pd.DataFrame(records)
                    df['date'] = pd.to_datetime(df['date'])
                    df = df.sort_values('date').reset_index(drop=True)
                    return df
        except Exception:
            pass

        return None

    def get_minute_data(self, symbol: str) -> Optional[pd.DataFrame]:
        """
        获取股票当日分时数据并聚合分时成交均价 (VWAP)
        主通道：腾讯分时 query；备选：腾讯 m1 K线最近交易日聚合
        """
        sym = self.format_symbol(symbol)
        url = f"http://web.ifzq.gtimg.cn/appstock/app/minute/query?code={sym}"
        try:
            resp = self.session.get(url, timeout=5)
            if resp.status_code == 200:
                data = resp.json().get("data", {}).get(sym, {})
                min_data = data.get("data", {}).get("data", [])
                if min_data:
                    records = []
                    for row in min_data:
                        parts = row.split(" ")
                        if len(parts) < 3:
                            continue
                        t_str = parts[0]
                        price = float(parts[1])
                        cum_volume = float(parts[2])  # 累计成交量(手)
                        amt = float(parts[3]) if len(parts) > 3 else (price * cum_volume * 100.0)
                        vwap = (amt / (cum_volume * 100.0)) if cum_volume > 0 else price
                        records.append({
                            "time": f"{t_str[:2]}:{t_str[2:]}",
                            "price": price,
                            "volume": cum_volume,
                            "amount": amt,
                            "vwap": round(vwap, 3)
                        })
                    if records:
                        return pd.DataFrame(records)
        except Exception as e:
            print(f"获取 {sym} 分时数据异常: {e}")

        # 备选：腾讯 m1 分钟K线提取最新交易日完整分时
        try:
            mk_url = f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={sym},m1,,260"
            resp = self.session.get(mk_url, timeout=5)
            if resp.status_code == 200:
                arr = resp.json().get("data", {}).get(sym, {}).get("m1", [])
                if arr:
                    latest_day = str(arr[-1][0])[:8]
                    day_bars = [row for row in arr if str(row[0]).startswith(latest_day)]
                    records = []
                    cum_vol = 0.0
                    cum_amt = 0.0
                    for row in day_bars:
                        ts = str(row[0])
                        hhmm = f"{ts[8:10]}:{ts[10:12]}"
                        price = float(row[2])
                        bar_vol = float(row[5]) if len(row) > 5 else 0.0  # 手
                        bar_amt_wan = float(row[7]) if len(row) > 7 else (price * bar_vol / 100.0)
                        cum_vol += bar_vol
                        cum_amt += bar_amt_wan * 10000.0
                        vwap = (cum_amt / (cum_vol * 100.0)) if cum_vol > 0 else price
                        records.append({
                            "time": hhmm,
                            "price": price,
                            "volume": cum_vol,
                            "amount": cum_amt,
                            "vwap": round(vwap, 3)
                        })
                    if records:
                        return pd.DataFrame(records)
        except Exception:
            pass
        return None

    def get_intraday_kline_5m(self, symbol: str, count: int = 1200) -> Optional[pd.DataFrame]:
        """
        获取多交易日 5 分钟历史K线（用于两点半战法与弱转强打板的严格因果分钟级回测）：
        返回字段：datetime, date_str, time_str, open, high, low, close, volume, vwap
        主通道：新浪财经 getKLineData(scale=5, 最大可达 2000 根 ≈ 41 个交易日)
        备选：腾讯 mkline(m5,,320 ≈ 6.6 个交易日)
        """
        sym = self.format_symbol(symbol)
        cache_key = f"{sym}_{count}"
        now_ts = time.time()
        with self._lock:
            cached = self._kline_5m_cache.get(cache_key)
            if cached and (now_ts - cached[0]) < 120.0:
                return cached[1].copy()

        records = []
        # 通道 1: 新浪 5 分钟历史 K 线
        try:
            sina_url = (
                f"http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                f"CN_MarketData.getKLineData?symbol={sym}&scale=5&ma=no&datalen={int(count)}"
            )
            resp = self.session.get(sina_url, timeout=5)
            if resp.status_code == 200:
                raw_items = resp.json()
                if isinstance(raw_items, list) and len(raw_items) > 0:
                    for item in raw_items:
                        dt_str = str(item.get("day", "")).strip()
                        if len(dt_str) < 16:
                            continue
                        records.append({
                            "datetime": dt_str,
                            "date_str": dt_str[:10],
                            "time_str": dt_str[11:16],
                            "open": float(item["open"]),
                            "high": float(item["high"]),
                            "low": float(item["low"]),
                            "close": float(item["close"]),
                            "volume": float(item.get("volume", 0.0)),  # 股
                        })
        except Exception:
            records = []

        # 通道 2: 腾讯 mkline 5 分钟备选
        if not records:
            try:
                qq_len = min(max(100, int(count)), 320)
                qq_url = f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={sym},m5,,{qq_len}"
                resp = self.session.get(qq_url, timeout=5)
                if resp.status_code == 200:
                    arr = resp.json().get("data", {}).get(sym, {}).get("m5", [])
                    for row in arr:
                        ts = str(row[0])
                        if len(ts) < 12:
                            continue
                        date_s = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
                        time_s = f"{ts[8:10]}:{ts[10:12]}"
                        records.append({
                            "datetime": f"{date_s} {time_s}:00",
                            "date_str": date_s,
                            "time_str": time_s,
                            "open": float(row[1]),
                            "close": float(row[2]),
                            "high": float(row[3]),
                            "low": float(row[4]),
                            "volume": float(row[5]) * 100.0,  # 手转股
                        })
            except Exception:
                records = []

        if not records:
            return None

        df = pd.DataFrame(records)
        df = df.sort_values("datetime").reset_index(drop=True)
        # 按交易日分日因果计算盘中累计 VWAP (typical price * volume)
        tp = (df["high"] + df["low"] + df["close"]) / 3.0
        pv = tp * df["volume"]
        df["_pv"] = pv
        cum_pv = df.groupby("date_str")["_pv"].cumsum()
        cum_vol = df.groupby("date_str")["volume"].cumsum()
        df["vwap"] = np.where(cum_vol > 0, np.round(cum_pv / cum_vol, 3), df["close"])
        df = df.drop(columns=["_pv"])

        with self._lock:
            self._kline_5m_cache[cache_key] = (now_ts, df)
        return df.copy()

    def get_trading_dates(self, count: int = 90) -> List[str]:
        """基于上证指数历史日K返回权威A股已开市交易日列表（升序 YYYY-MM-DD）"""
        now_ts = time.time()
        with self._lock:
            if (now_ts - self._trading_dates_cache[0]) < 300.0 and len(self._trading_dates_cache[1]) >= min(20, count):
                return list(self._trading_dates_cache[1][-count:])

        df = self.get_daily_kline("sh000001", count=max(count, 120))
        if df is not None and len(df) > 0 and "date" in df.columns:
            dates = sorted({str(d)[:10] for d in df["date"].tolist() if str(d)})
            with self._lock:
                self._trading_dates_cache = (now_ts, dates)
            return dates[-count:]
        return []

    def get_stock_plates(self, symbol: str) -> Dict[str, Any]:
        """
        获取个股所属概念与行业板块信息及同板块涨停效应（腾讯 plate 接口 + 12小时本地缓存）
        """
        sym = self.format_symbol(symbol)
        now_ts = time.time()
        GENERIC_TAGS = {
            "融资融券", "转融券标的", "深股通", "沪股通", "富时罗素", "标普道琼斯",
            "MSCI中国", "百元股", "昨日涨停", "昨日连板", "昨日触板", "高股息精选",
            "标准普尔", "HS300_", "上证180_", "深成500", "中证500"
        }

        with self._lock:
            cached = self._plates_cache.get(sym)
            if cached and (now_ts - float(cached.get("ts", 0.0))) < 43200.0:
                return cached

        concepts: List[str] = []
        industries: List[str] = []
        hot_concepts: List[str] = []
        max_sector_zt = 0

        try:
            url = f"https://proxy.finance.qq.com/ifzqgtimg/appstock/app/stockinfo/plate?code={sym}"
            resp = self.session.get(url, timeout=3.5)
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                for p in data.get("plate", []) or []:
                    pname = str(p.get("name", "")).strip()
                    if pname:
                        industries.append(pname)
                for c in data.get("concept", []) or []:
                    cname = str(c.get("name", "")).strip()
                    tag = str(c.get("tag", "")).strip()
                    if not cname or any(g in cname for g in GENERIC_TAGS):
                        continue
                    concepts.append(cname)
                    if "涨停" in tag:
                        m = re.search(r"涨停(\d+)", tag)
                        if m:
                            zt_num = int(m.group(1))
                            max_sector_zt = max(max_sector_zt, zt_num)
                            hot_concepts.append(f"{cname}({tag})")
        except Exception:
            pass

        result = {
            "symbol": sym,
            "industries": industries,
            "concepts": concepts[:8],
            "hot_concepts": hot_concepts[:4],
            "max_sector_zt": max_sector_zt,
            "primary_sector": industries[-1] if industries else (concepts[0] if concepts else "主板"),
            "ts": now_ts
        }
        with self._lock:
            self._plates_cache[sym] = result
            if len(self._plates_cache) > 1500:
                # 裁剪较旧记录
                items = sorted(self._plates_cache.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True)[:1000]
                self._plates_cache = dict(items)
            if len(self._plates_cache) % 20 == 1:
                atomic_write_json(PLATES_CACHE_FILE, self._plates_cache)
        return result

    def get_market_breadth(self) -> Dict[str, Any]:
        """
        获取A股大盘全市场广度：上证指数、沪深300、全市场涨跌家数比、赚钱效应
        """
        indices = ["sh000001", "sh000300", "sz399001", "sz399006"]
        quotes = self.get_realtime_quotes(indices)

        sh_idx = quotes.get("sh000001", {})
        hs300 = quotes.get("sh000300", {})

        # 获取全市场涨跌统计（通过东财汇总API）
        breadth = {
            "sh_price": sh_idx.get("price", 0.0),
            "sh_pct": sh_idx.get("change_pct", 0.0),
            "hs300_price": hs300.get("price", 0.0),
            "hs300_pct": hs300.get("change_pct", 0.0),
            "up_count": 0,
            "down_count": 0,
            "flat_count": 0,
            "limit_up_count": 0,
            "limit_down_count": 0,
            "profit_effect_pct": 50.0,
            "total_turnover_billion": 0.0,
            "regime": "震荡分化"
        }

        try:
            url = "https://push2.eastmoney.com/api/qt/ulist.np/get?fltt=2&secids=1.000001,0.399001&fields=f2,f3,f4,f6"
            resp = self.session.get(url, timeout=3)
            # 东财全市场涨跌分布
            url_dist = "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=1&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281&fltt=2&invt=2&fid=f3&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&fields=f3"
            # 简化统计
        except Exception:
            pass

        # 根据上证与沪深300涨跌与均线定调宏观牛熊环境
        sh_pct = breadth["sh_pct"]
        if sh_pct >= 0.8:
            breadth["regime"] = "强势单边多头"
        elif sh_pct <= -1.0:
            breadth["regime"] = "弱势系统性杀跌"
        elif sh_pct > 0:
            breadth["regime"] = "温和多头震荡"
        else:
            breadth["regime"] = "弱势震荡蓄势"

        return breadth

    def get_main_board_stock_list(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        获取全A股主板股票清单（代码、名称）
        严格剔除：科创板(688)、创业板(300)、北交所、ST股
        双通道容灾架构：新浪极速金融通道 + 东方财富云端通道 + 本地永续缓存防击穿
        """
        if not force_refresh and STOCK_LIST_CACHE.exists():
            try:
                mtime = STOCK_LIST_CACHE.stat().st_mtime
                cache_dt = datetime.fromtimestamp(mtime)
                cache_day = cache_dt.strftime("%Y-%m-%d")
                now_dt = datetime.now()
                today = now_dt.strftime("%Y-%m-%d")
                cur_hm = now_dt.strftime("%H:%M")
                is_live_session = (now_dt.weekday() < 5) and ("09:25" <= cur_hm <= "15:05")
                # 盘中交易时段仅允许 90 秒短时缓存（避免早盘快照锁死全天涨跌幅）；盘后要求缓存落在 15:02 收盘定格之后
                cache_fresh = False
                if cache_day == today:
                    if is_live_session:
                        cache_fresh = (time.time() - mtime) < 90.0
                    elif cur_hm > "15:05":
                        cache_fresh = cache_dt.strftime("%H:%M") >= "15:02"
                    else:
                        cache_fresh = True
                if cache_fresh:
                    with open(STOCK_LIST_CACHE, "r", encoding="utf-8") as f:
                        cached_stocks = json.load(f)
                        if len(cached_stocks) > 500 and "open" in cached_stocks[0]:
                            # Legacy caches may contain ETFs because the older
                            # generic board predicate also accepted fund codes.
                            return [
                                stock for stock in cached_stocks
                                if self.is_main_board_equity(stock.get("code") or stock.get("symbol", ""))
                                and "ST" not in str(stock.get("name", "")).upper()
                                and "退" not in str(stock.get("name", ""))
                            ]
            except Exception:
                pass

        stocks = []
        # 通道 1: 新浪行情中心 (极速、无防爬断连、夜间高可用)
        try:
            sina_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
                "Referer": "http://finance.sina.com.cn"
            }
            # 沪深A股共约 5000 只标的，每页 100 只，35 页即可全覆盖主板合规标的
            for p in range(1, 40):
                url = f"http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData?page={p}&num=100&sort=symbol&asc=1&node=hs_a&symbol=&_s_r_a=page"
                r = self.session.get(url, headers=sina_headers, timeout=4)
                if r.status_code == 200:
                    text = r.content.decode('gbk', errors='ignore')
                    items = json.loads(text)
                    if not items or len(items) == 0:
                        break
                    for it in items:
                        code = str(it.get('code', '')).strip()
                        name = str(it.get('name', '')).strip()
                        if not self.is_main_board_equity(code):
                            continue
                        if 'ST' in name.upper() or '退' in name:
                            continue
                        stocks.append({
                            "code": code,
                            "name": name,
                            "symbol": self.format_symbol(code),
                            "price": float(it.get('trade', 0.0) or 0.0),
                            "open": float(it.get('open', 0.0) or 0.0),
                            "prev_close": float(it.get('settlement', 0.0) or 0.0),
                            "high": float(it.get('high', 0.0) or 0.0),
                            "low": float(it.get('low', 0.0) or 0.0),
                            "change_pct": float(it.get('changepercent', 0.0) or 0.0),
                            "turnover": float(it.get('amount', 0.0) or 0.0),
                            "turnover_rate": float(it.get('turnoverratio', 0.0) or 0.0),
                            "float_mkt_cap": round(float(it.get('nmc', 0.0) or 0.0) / 10000.0, 2)
                        })
                    if len(items) < 100:
                        break
        except Exception:
            pass

        # 通道 2: 若新浪通道获取标的偏少，使用东财通道补充
        if len(stocks) < 500:
            try:
                em_headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
                    "Referer": "https://quote.eastmoney.com/"
                }
                for p in range(1, 40):
                    url = (
                        "https://push2.eastmoney.com/api/qt/clist/get"
                        f"?pn={p}&pz=100&po=1&np=1&ut=bd1d9ddb04089700cf9c27f6f7426281"
                        "&fltt=2&invt=2&fid=f3&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
                        "&fields=f12,f14,f2,f3,f6,f17,f18,f15,f16,f8"
                    )
                    resp = self.session.get(url, headers=em_headers, timeout=4)
                    if resp.status_code != 200:
                        break
                    data = resp.json().get("data")
                    if not data or not data.get("diff"):
                        break
                    diff = data["diff"]
                    for item in diff:
                        code = str(item.get("f12", ""))
                        name = str(item.get("f14", ""))
                        if not self.is_main_board_equity(code):
                            continue
                        if "ST" in name.upper() or "退" in name:
                            continue
                        symbol = self.format_symbol(code)
                        stocks.append({
                            "code": code,
                            "name": name,
                            "symbol": symbol,
                            "price": item.get("f2", 0),
                            "open": item.get("f17", 0),
                            "prev_close": item.get("f18", 0),
                            "high": item.get("f15", 0),
                            "low": item.get("f16", 0),
                            "change_pct": item.get("f3", 0),
                            "turnover": item.get("f6", 0),
                            "turnover_rate": item.get("f8", 0)
                        })
                    if len(diff) < 100:
                        break
            except Exception:
                pass

        # 通道 3: 本地永续缓存防击穿保护
        # 若因断网或接口异常未能拉取到足够标的，坚决禁止用 33 只种子覆写已有的上千只标的！
        if len(stocks) < 500 and STOCK_LIST_CACHE.exists():
            try:
                with open(STOCK_LIST_CACHE, "r", encoding="utf-8") as f:
                    cached_stocks = json.load(f)
                    if len(cached_stocks) > len(stocks):
                        stocks = cached_stocks
            except Exception:
                pass

        # 通道 4: 极端无缓存冷启动兜底
        if len(stocks) < 100:
            stocks = self._get_fallback_stock_list()

        # 仅当获取到大于 500 只有效主板标的时才刷新写入本地磁盘缓存，防毒缓存污染
        if len(stocks) >= 500:
            atomic_write_json(STOCK_LIST_CACHE, stocks)

        return stocks

    def _get_fallback_stock_list(self) -> List[Dict[str, str]]:
        """兜底核心主板标的池"""
        core_seeds = [
            ("600519", "贵州茅台"), ("601318", "中国平安"), ("600036", "招商银行"),
            ("601857", "中国石油"), ("601398", "工商银行"), ("600900", "长江电力"),
            ("600030", "中信证券"), ("601088", "中国神华"), ("600887", "伊利股份"),
            ("601988", "中国银行"), ("600028", "中国石化"), ("601899", "紫金矿业"),
            ("600104", "上汽集团"), ("600050", "中国联通"), ("601668", "中国建筑"),
            ("600276", "恒瑞医药"), ("600309", "万华化学"), ("601628", "中国人寿"),
            ("000001", "平安银行"), ("000333", "美的集团"), ("000651", "格力电器"),
            ("000858", "五粮液"),   ("000725", "京东方A"),  ("002415", "海康威视"),
            ("000002", "万科A"),   ("002594", "比亚迪"),   ("000963", "华东医药"),
            ("002475", "立讯精密"), ("000768", "中航西飞"), ("002714", "牧原股份"),
            ("601138", "工业富联"), ("600418", "江淮汽车"), ("601919", "中远海控")
        ]
        return [{"code": c, "name": n, "symbol": self.format_symbol(c)} for c, n in core_seeds]

    COMMON_ETFS = {
        "300etf": ("510300", "sh510300", "沪深300ETF"),
        "沪深300etf": ("510300", "sh510300", "沪深300ETF"),
        "沪深300": ("510300", "sh510300", "沪深300ETF"),
        "500etf": ("510500", "sh510500", "中证500ETF"),
        "中证500etf": ("510500", "sh510500", "中证500ETF"),
        "中证500": ("510500", "sh510500", "中证500ETF"),
        "50etf": ("510050", "sh510050", "上证50ETF"),
        "上证50etf": ("510050", "sh510050", "上证50ETF"),
        "上证50": ("510050", "sh510050", "上证50ETF"),
        "创业板etf": ("159915", "sz159915", "创业板ETF"),
        "科创50etf": ("588000", "sh588000", "科创50ETF"),
        "科创50": ("588000", "sh588000", "科创50ETF"),
        "证券etf": ("512880", "sh512880", "证券ETF"),
        "券商etf": ("512000", "sh512000", "券商ETF"),
        "半导体etf": ("512480", "sh512480", "半导体ETF"),
        "芯片etf": ("159995", "sz159995", "芯片ETF"),
        "酒etf": ("512690", "sh512690", "酒ETF"),
        "白酒etf": ("512690", "sh512690", "酒ETF"),
        "医药etf": ("512010", "sh512010", "医药ETF"),
        "军工etf": ("512660", "sh512660", "军工ETF"),
        "红利etf": ("510880", "sh510880", "红利ETF"),
        "红利低波etf": ("512890", "sh512890", "红利低波ETF"),
        "恒生科技etf": ("513180", "sh513180", "恒生科技ETF"),
        "黄金etf": ("518880", "sh518880", "黄金ETF"),
    }

    def search_stocks(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """根据代码、名称模糊搜索主板股票与主流ETF"""
        q = str(query).strip().lower()
        if not q:
            return []
        matches = []
        # 优先匹配主流 ETF 字典
        for k, (c, s, n) in self.COMMON_ETFS.items():
            if q in k or q in c or q in n.lower():
                matches.append({"code": c, "symbol": s, "name": n})
                if len(matches) >= limit:
                    return matches

        stocks = self.get_main_board_stock_list()
        is_num = q.isdigit()
        for s in stocks:
            code = s.get("code", "")
            name = s.get("name", "")
            sym = s.get("symbol", "")
            if is_num:
                if code.startswith(q) or code == q:
                    matches.append(s)
            else:
                clean_name = name.replace(" ", "").lower()
                clean_q = q.replace(" ", "")
                if clean_q in clean_name or q in sym.lower():
                    matches.append(s)
            if len(matches) >= limit:
                break
        return matches

    def resolve_symbol(self, query: str) -> Optional[Dict[str, str]]:
        """解析股票代码或名称为标准化结构 (支持主板股票与主流ETF)"""
        q = str(query).strip()
        if not q:
            return None

        # 1. 优先匹配主流 ETF 别名
        q_lower = q.lower()
        if q_lower in self.COMMON_ETFS:
            c, s, n = self.COMMON_ETFS[q_lower]
            return {"code": c, "symbol": s, "name": n}

        # 2. 符合主板股票或场内主流 ETF 代码规则
        if self.is_main_board(q):
            sym = self.format_symbol(q)
            code = self.clean_code(q)
            stocks = self.get_main_board_stock_list()
            for s in stocks:
                if s.get("code") == code:
                    return {"code": code, "symbol": sym, "name": s.get("name", code)}
            # 针对主流ETF等非主板个股标的，尝试从实时行情快照补齐正式中文名称
            try:
                quotes = self.get_realtime_quotes([sym])
                if quotes and sym in quotes and quotes[sym].get("name"):
                    real_name = quotes[sym]["name"]
                    if real_name and not real_name.isdigit():
                        return {"code": code, "symbol": sym, "name": real_name}
            except Exception:
                pass
            return {"code": code, "symbol": sym, "name": code}

        # 3. 模糊名称或别名搜索匹配
        matches = self.search_stocks(q, limit=1)
        if matches:
            top = matches[0]
            return {"code": top["code"], "symbol": top["symbol"], "name": top["name"]}
        return None

data_engine = DataEngine()
