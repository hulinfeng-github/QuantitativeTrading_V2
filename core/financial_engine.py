"""
A股智能量化系统 2.0 - 上市公司财报与经营状况全景分析引擎 (FinancialAnalysisEngine 2.0)
吸收 GitHub 顶级 A 股基本面量化研报与开源项目 (tushare/akshare/Piotroski F-Score/DuPont-Analysis)：

核心模块：
1. 杜邦三因子分析 (DuPont Analysis): ROE = 净利润率 × 资产周转率 × 权益乘数，并智能识别驱动力核心；
2. 皮奥特罗斯基 9 分制价值评分 (Piotroski F-Score): 涵盖盈利能力、资本结构/偿债流动性、运营效率全方位体检；
3. A股专属财务排雷避雷雷达 (Financial Red Flags):
   - 商誉减值风险 (Goodwill Risk)
   - 应收账款虚构/坏账风险 (Receivables Risk)
   - 存货积压跌价风险 (Inventory Risk)
   - 经营现金流与净利润背离/失血风险 (Earnings Quality / OCF Mismatch)
   - 重资产高杠杆流动性断裂危机 (Solvency Risk)
4. 成长性与真实主业景气度 (Growth Momentum): 营收、归母净利润与扣非净利润同比增速 (YoY)；
5. 综合基本面得分 (0~100) 与企业经营画像 (Enterprise Persona).
"""
import re
import json
import time
import requests
import numpy as np
import pandas as pd
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime

from config import DATA_DIR, load_config
from core.data_engine import data_engine

FINANCIAL_CACHE_FILE = DATA_DIR / "financial_cache.json"

class FinancialAnalysisEngine:
    def __init__(self):
        self.session = requests.Session()
        self.session.trust_env = False  # 绕过本地代理，直连行情源
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Referer": "https://emweb.securities.eastmoney.com"
        })
        self._cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._cache_ttl = 3600 * 24  # 财报为季报/年报数据，内存缓存24小时
        self._load_local_cache()

    @staticmethod
    def _has_finite_value(value: Any) -> bool:
        """Treat blanks, placeholders, NaN, and infinities as unavailable."""
        if value is None or (isinstance(value, str) and value.strip() in ("", "-")):
            return False
        try:
            return bool(np.isfinite(float(value)))
        except (TypeError, ValueError):
            return False

    @classmethod
    def _finite_float(cls, value: Any, default: float = 0.0) -> float:
        return float(value) if cls._has_finite_value(value) else default

    def _load_local_cache(self):
        try:
            if FINANCIAL_CACHE_FILE.exists():
                with open(FINANCIAL_CACHE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    now = time.time()
                    for k, v in data.items():
                        # 若缓存仍在 7 天有效期内，载入内存
                        t = v.get("cached_at", 0)
                        if now - t < 3600 * 24 * 7:
                            self._cache[k] = (t, v.get("data", {}))
        except Exception:
            self._cache = {}

    def _save_local_cache(self):
        try:
            FINANCIAL_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            export = {}
            for k, (t, d) in self._cache.items():
                export[k] = {"cached_at": t, "data": d}
            with open(FINANCIAL_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(export, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存财报缓存异常: {e}")

    def fetch_raw_financials(self, symbol: str, force_refresh: bool = False) -> Optional[Dict[str, Any]]:
        """
        拉取东方财富官方主要财务指标与杜邦拆解最新数据
        """
        code_clean = data_engine.clean_code(symbol)
        if not code_clean or len(code_clean) != 6:
            return None

        # 判断是否为 ETF (ETF 无个股财报，直接返回基金特权)
        sym_lower = str(symbol).lower()
        if any(sym_lower.startswith(p) or code_clean.startswith(p) for p in ["sh5", "sz15", "sz16", "sh51", "sh56", "sh58", "sh50", "sz18", "51", "56", "58", "15", "16"]):
            return {"is_etf": True, "code": code_clean}

        # 市场前缀：上交所 SH，深交所 SZ
        sec_prefix = "SH" if code_clean.startswith(("6", "5")) else "SZ"
        em_code = f"{sec_prefix}{code_clean}"

        # 检查缓存 (支持 force_refresh 强制绕过缓存拉取最新财报)
        now = time.time()
        if not force_refresh and em_code in self._cache and (now - self._cache[em_code][0]) < self._cache_ttl:
            return self._cache[em_code][1]

        result = {
            "is_etf": False,
            "code": code_clean,
            "em_code": em_code,
            "dupont_list": [],
            "indicator_list": []
        }

        # 1. 抓取杜邦拆解及资产负债表关键项
        url_dupont = f"https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/DBFXAjaxNew?code={em_code}"
        try:
            resp = self.session.get(url_dupont, timeout=6)
            if resp.status_code == 200:
                data = resp.json()
                result["dupont_list"] = data.get("bgq", []) or data.get("data", [])
        except Exception as e:
            pass

        # 2. 抓取主要财务指标（毛利、净利、扣非、每股现金流等）
        url_zyzb = f"https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/ZYZBAjaxNew?type=0&code={em_code}"
        try:
            resp = self.session.get(url_zyzb, timeout=6)
            if resp.status_code == 200:
                data = resp.json()
                result["indicator_list"] = data.get("data", []) or []
        except Exception as e:
            pass

        if result["dupont_list"] or result["indicator_list"]:
            self._cache[em_code] = (now, result)
            self._save_local_cache()
            return result

        return None

    def analyze(self, symbol: str, name: str = "", force_refresh: bool = False) -> Dict[str, Any]:
        """
        对指定标的进行全维度财报与经营质地深度量化分析
        """
        sym = data_engine.format_symbol(symbol)
        raw = self.fetch_raw_financials(sym, force_refresh=force_refresh)

        if not raw or (not raw.get("is_etf") and not (raw.get("dupont_list") or raw.get("indicator_list"))):
            return {
                "symbol": sym,
                "name": name,
                "data_available": False,
                "report_name": "财报数据不可用",
                "report_date": None,
                "fundamental_score": None,
                "persona": "⚠️【数据不足·基本面风险未知】",
                "summary": "未能获取可用财报数据；系统不会用估算值替代披露指标。",
                "red_flags": {
                    "risk_level": "未知",
                    "is_safe": False,
                    "has_major_risk": False,
                    "risk_summary": "财报数据不可用，无法完成风险排查",
                    "triggered_risks": []
                },
                "data_quality": {"available": False, "missing_reason": "数据源未返回财报记录"}
            }

        # ETF 特殊处理
        if raw and raw.get("is_etf"):
            return {
                "symbol": sym,
                "name": name,
                "is_etf": True,
                "data_available": False,
                "data_quality": {"available": False, "can_assess_risk": False, "missing_reason": "ETF不适用个股财务指标"},
                "report_name": "ETF 基金份额报告",
                "report_date": "最新交易日",
                "fundamental_score": None,
                "persona": "ETF · 本模块不评估基金净值风险",
                "summary": "ETF不适用个股财务排雷；基金跟踪误差、集中度、流动性与净值波动需另行评估。",
                "dupont": {
                    "roe": None,
                    "net_margin": None,
                    "net_profit_margin": None,
                    "asset_turnover": None,
                    "equity_multiplier": None,
                    "dupont_score": None,
                    "primary_driver": "不适用",
                    "driver_type": "不适用",
                    "driver_desc": "ETF不使用个股杜邦指标"
                },
                "f_score": {"score": None, "level": "不适用", "rating": "ETF不适用个股F-Score", "details": []},
                "piotroski_f_score": {"score": None, "level": "不适用", "rating": "ETF不适用个股F-Score", "details": []},
                "growth": {"revenue_yoy": None, "net_profit_yoy": None, "deduct_profit_yoy": None, "gross_margin": None, "growth_tag": "不适用"},
                "cash_quality": {"ocf_match_ratio": None, "quality_tag": "不适用·未评估基金现金流"},
                "red_flags": {
                    "radar_score": None,
                    "risk_level": "未知",
                    "is_safe": False,
                    "has_major_risk": False,
                    "triggered_risks": [],
                    "goodwill_risk": "不适用·未评估基金风险",
                    "receivables_risk": "不适用·未评估基金风险",
                    "debt_risk": "不适用·未评估基金风险",
                    "risk_tags": [],
                    "risk_summary": "ETF不适用个股财报排雷，基金风险未在此处评估",
                    "indicators": {
                        "goodwill_to_equity_pct": None,
                        "receivables_to_revenue_pct": None,
                        "inventory_to_assets_pct": None,
                        "debt_to_assets_pct": None,
                        "cash_to_net_profit_ratio": None
                    }
                }
            }

        # 个股财报数据解析
        dupont_list = raw.get("dupont_list", []) if raw else []
        ind_list = raw.get("indicator_list", []) if raw else []

        latest_dp = dupont_list[0] if dupont_list else {}
        prev_dp = dupont_list[1] if len(dupont_list) > 1 else {}
        latest_ind = ind_list[0] if ind_list else {}
        prev_ind = ind_list[1] if len(ind_list) > 1 else {}

        report_name = latest_dp.get("REPORT_TYPE") or latest_ind.get("REPORT_DATE_NAME") or "最新季度报告"

        # 1. 杜邦三因子提取与计算
        roe_raw = latest_dp.get("ROE")
        if not self._has_finite_value(roe_raw):
            roe_raw = latest_ind.get("ROEJQ")
        roe_known = self._has_finite_value(roe_raw)
        roe = self._finite_float(roe_raw)
        margin_raw = latest_dp.get("SALE_NPR")
        if not self._has_finite_value(margin_raw):
            margin_raw = latest_ind.get("XSJLL")
        net_margin_known = self._has_finite_value(margin_raw)
        net_margin = self._finite_float(margin_raw)
        turnover = self._finite_float(latest_dp.get("TOTAL_ASSETS_TR"))
        if turnover <= 0 and latest_dp.get("TOTAL_ASSETS"):
            tot_assets_for_turnover = self._finite_float(latest_dp.get("TOTAL_ASSETS"))
            tot_rev_for_turnover = self._finite_float(latest_dp.get("TOTAL_OPERATE_INCOME"))
            if tot_assets_for_turnover > 0:
                turnover = round(tot_rev_for_turnover / tot_assets_for_turnover, 3)

        turnover_known = self._has_finite_value(latest_dp.get("TOTAL_ASSETS_TR")) or (
            self._has_finite_value(latest_dp.get("TOTAL_ASSETS")) and
            self._has_finite_value(latest_dp.get("TOTAL_OPERATE_INCOME")) and
            self._finite_float(latest_dp.get("TOTAL_ASSETS")) > 0
        )
        debt_raw = latest_dp.get("DEBT_ASSET_RATIO")
        if not self._has_finite_value(debt_raw):
            debt_raw = latest_ind.get("ZCFZL")
        debt_known = self._has_finite_value(debt_raw)
        debt_ratio = float(debt_raw) if debt_known else 0.0
        leverage_raw = latest_dp.get("EQUITY_MULTIPLIER")
        leverage_known = self._has_finite_value(leverage_raw) or (debt_known and 0.0 <= debt_ratio < 100.0)
        leverage = (
            self._finite_float(leverage_raw)
            if self._has_finite_value(leverage_raw)
            else (round(100.0 / (100.0 - debt_ratio), 3) if debt_known and debt_ratio < 100.0 else 1.0)
        )

        # 杜邦核心驱动力归因
        driver_type = "数据不足·无法归因"
        driver_desc = "缺少完整的杜邦指标，无法可靠归因"
        if roe_known and net_margin_known and turnover_known and leverage_known and debt_known:
            driver_type = "均衡型"
            driver_desc = "杜邦指标未显示单一突出驱动因素"
        if roe_known and net_margin_known and turnover_known and leverage_known and debt_known and net_margin >= 25.0:
            driver_type = "【高毛利·产品护城河驱动】"
            driver_desc = f"销售净利率高达 {net_margin:.1f}%，凭借强产品壁垒与品牌定价权创造核心利润"
        elif roe_known and net_margin_known and turnover_known and leverage_known and debt_known and turnover >= 0.8:
            driver_type = "【高运营周转·供应链效率驱动】"
            driver_desc = f"总资产周转率高达 {turnover:.2f} 次，依托极速运营效率与薄利多销驱动收益"
        elif roe_known and net_margin_known and turnover_known and leverage_known and debt_known and leverage >= 2.5 and debt_ratio >= 65.0:
            driver_type = "【高杠杆依赖·注意财务承压】"
            driver_desc = f"权益乘数达 {leverage:.2f}，资产负债率达 {debt_ratio:.1f}%，高ROE部分依靠负债杠杆拉动"
        elif roe_known and net_margin_known and turnover_known and leverage_known and debt_known and roe >= 15.0:
            driver_type = "【高质高ROE·核心资产型】"
            driver_desc = f"ROE高达 {roe:.1f}%，且各项指标协调稳健"

        dupont_score = round(float(np.clip(roe * 3.5 + (10.0 if net_margin > 20 else 0) + (10.0 if turnover > 0.6 else 0), 20.0, 99.0)), 1) if roe_known and net_margin_known and turnover_known else None

        dupont_info = {
            "report_name": report_name,
            "roe": round(roe, 2) if roe_known else None,
            "net_margin": round(net_margin, 2) if net_margin_known else None,
            "net_profit_margin": round(net_margin, 2) if net_margin_known else None,
            "asset_turnover": round(turnover, 3) if turnover_known else None,
            "equity_multiplier": round(leverage, 2) if leverage_known else None,
            "debt_ratio": round(debt_ratio, 2) if debt_known else None,
            "dupont_score": dupont_score,
            "primary_driver": driver_desc,
            "driver_type": driver_type,
            "driver_desc": driver_desc
        }

        # 2. 营业成长与业绩景气度
        rev_yoy_raw = next((latest_ind.get(k) for k in ("TOTALOPERATEREVETZ", "YYSR_TB", "YYSRTZ") if self._has_finite_value(latest_ind.get(k))), None)
        net_profit_yoy_raw = next((latest_ind.get(k) for k in ("PARENTNETPROFITTZ", "JLR_TB", "JLRTZ") if self._has_finite_value(latest_ind.get(k))), None)
        rev_yoy_known = self._has_finite_value(rev_yoy_raw)
        net_profit_yoy_known = self._has_finite_value(net_profit_yoy_raw)
        deduct_profit_yoy_known = self._has_finite_value(latest_ind.get("KCFJCXSYJLRTZ"))
        gross_margin_known = self._has_finite_value(latest_ind.get("XSMLL"))
        rev_yoy = self._finite_float(rev_yoy_raw)
        net_profit_yoy = self._finite_float(net_profit_yoy_raw)
        deduct_profit_yoy = self._finite_float(latest_ind.get("KCFJCXSYJLRTZ"))
        gross_margin = self._finite_float(latest_ind.get("XSMLL"))

        growth_tag = "数据不足·关键增速字段缺失"
        if rev_yoy_known and deduct_profit_yoy_known:
            growth_tag = "平稳运行"
        if rev_yoy_known and deduct_profit_yoy_known and deduct_profit_yoy >= 25.0 and rev_yoy >= 15.0:
            growth_tag = "🚀 高景气加速主升"
        elif deduct_profit_yoy_known and deduct_profit_yoy >= 10.0:
            growth_tag = "📈 稳健持续增长"
        elif deduct_profit_yoy_known and deduct_profit_yoy < -20.0:
            growth_tag = "⚠️ 业绩大幅滑坡承压"
        elif deduct_profit_yoy_known and deduct_profit_yoy < 0.0:
            growth_tag = "📉 周期微跌调整"

        growth_info = {
            "revenue_yoy": round(rev_yoy, 2) if rev_yoy_known else None,
            "net_profit_yoy": round(net_profit_yoy, 2) if net_profit_yoy_known else None,
            "deduct_profit_yoy": round(deduct_profit_yoy, 2) if deduct_profit_yoy_known else None,
            "gross_margin": round(gross_margin, 2) if gross_margin_known else None,
            "growth_tag": growth_tag
        }

        # 3. 现金流成色与匹配度 (Earnings Quality)
        # 经营活动现金流量净额
        tot_rev = self._finite_float(latest_dp.get("TOTAL_OPERATE_INCOME"))
        parent_np_raw = latest_dp.get("PARENT_NETPROFIT")
        if not self._has_finite_value(parent_np_raw):
            parent_np_raw = latest_dp.get("NETPROFIT")
        parent_np = self._finite_float(parent_np_raw)
        # 经营现金流占营收比例。缺少披露值时保留为未知，禁止以净利润估算。
        ocf_ratio_raw = latest_ind.get("JYXJLYYSR")
        try:
            ocf_to_revenue = float(ocf_ratio_raw) if self._has_finite_value(ocf_ratio_raw) else None
        except (TypeError, ValueError):
            ocf_to_revenue = None
        ocf = ocf_to_revenue * tot_rev if ocf_to_revenue is not None and tot_rev > 0 else None
        ocf_ratio = round(ocf / parent_np, 2) if ocf is not None and parent_np > 0 else None
        cash_tag = "数据不足·无法判断现金流质量"
        if ocf_ratio is not None and ocf_ratio >= 1.0:
            cash_tag = "💎 现金奶牛 (现金流超净利润)"
        elif ocf_ratio is not None and ocf_ratio >= 0.7:
            cash_tag = "✅ 现金流与盈利匹配良好"
        elif ocf_ratio is not None and ocf_ratio > 0:
            cash_tag = "⚠️ 盈利现金含量偏低 (应收占比较高)"
        elif ocf_ratio is not None:
            cash_tag = "🚨 经营性失血 (现金流为负)"

        cash_info = {
            "ocf_ratio": ocf_ratio,
            "cash_tag": cash_tag,
            "per_share_ocf": round(self._finite_float(latest_ind.get("MGJYXJJE")), 3) if self._has_finite_value(latest_ind.get("MGJYXJJE")) else None,
            "data_available": ocf is not None
        }

        # 4. A股特色财务排雷避雷雷达 (Red Flags)
        tot_assets = self._finite_float(latest_dp.get("TOTAL_ASSETS"))
        tot_liab = self._finite_float(latest_dp.get("TOTAL_LIABILITIES"))
        net_assets = max(tot_assets - tot_liab, 1.0)

        goodwill_known = all(self._has_finite_value(latest_dp.get(k)) for k in ("GOODWILL", "TOTAL_ASSETS", "TOTAL_LIABILITIES"))
        receivables_known = all(self._has_finite_value(latest_dp.get(k)) for k in ("ACCOUNTS_RECE", "TOTAL_OPERATE_INCOME"))
        inventory_known = all(self._has_finite_value(latest_dp.get(k)) for k in ("INVENTORY", "TOTAL_ASSETS"))
        debt_known = self._has_finite_value(latest_dp.get("DEBT_ASSET_RATIO")) or self._has_finite_value(latest_ind.get("ZCFZL"))

        goodwill = self._finite_float(latest_dp.get("GOODWILL"))
        goodwill_ratio = round(goodwill / net_assets * 100.0, 2) if goodwill_known and net_assets > 0 else None

        receivables = self._finite_float(latest_dp.get("ACCOUNTS_RECE"))
        receivables_ratio = round(receivables / tot_rev * 100.0, 2) if receivables_known and tot_rev > 0 else None

        inventory = self._finite_float(latest_dp.get("INVENTORY"))
        inventory_ratio = round(inventory / tot_assets * 100.0, 2) if inventory_known and tot_assets > 0 else None

        missing_risk_metrics = [
            label for label, available in (
                ("商誉/净资产", goodwill_ratio is not None),
                ("应收账款/营收", receivables_ratio is not None),
                ("存货/总资产", inventory_ratio is not None),
                ("资产负债率", debt_known),
                ("经营现金流/净利润", ocf_ratio is not None)
            ) if not available
        ]
        can_assess_risk = not missing_risk_metrics

        risk_tags = []
        has_major_risk = False

        # 商誉风险
        if goodwill_ratio is not None and goodwill_ratio >= 35.0:
            risk_tags.append(f"🚨【重度商誉暴雷风险】商誉占净资产 {goodwill_ratio}% (警戒线 20%)，警惕年末大额资产减值洗澡！")
            has_major_risk = True
        elif goodwill_ratio is not None and goodwill_ratio >= 20.0:
            risk_tags.append(f"⚠️【中度商誉隐患】商誉占净资产 {goodwill_ratio}%，稍显偏高")

        # 应收账款风险
        if receivables_ratio is not None and receivables_ratio >= 50.0:
            risk_tags.append(f"🚨【应收账款畸高】应收账款占营收 {receivables_ratio}%，谨防下游拖欠与坏账减值！")
            has_major_risk = True
        elif receivables_ratio is not None and receivables_ratio >= 35.0:
            risk_tags.append(f"⚠️【应收占比偏大】应收账款占营收 {receivables_ratio}%")

        # 存货积压风险
        if inventory_ratio is not None and inventory_ratio >= 40.0:
            risk_tags.append(f"⚠️【存货占比较高】存货占总资产 {inventory_ratio}%，注意产品迭代跌价损失")

        # 现金流失血风险
        if ocf_ratio is not None and ocf_ratio < 0.3 and parent_np > 0:
            risk_tags.append("🚨【纸面富贵·缺乏真金白银】经营现金流严重恶化，多为赊销挂账")
            has_major_risk = True

        # 极高负债风险 (排除银行非银金融除外)
        if debt_known and debt_ratio >= 78.0 and not (symbol.startswith(("sh600036", "sh601", "sz000001")) and "银行" in name):
            risk_tags.append(f"🚨【高杠杆债务承压】资产负债率高达 {debt_ratio}%，短期偿债与流动性压力大")
            has_major_risk = True

        radar_score = 100.0
        if has_major_risk:
            radar_score -= 30.0
        radar_score -= len(risk_tags) * 10.0
        radar_score = float(np.clip(radar_score, 10.0, 100.0)) if can_assess_risk else None
        risk_level = ("高危触雷" if has_major_risk else "中度隐患") if risk_tags else ("未知" if not can_assess_risk else "极低风险")

        red_flags_info = {
            "radar_score": round(radar_score, 1) if radar_score is not None else None,
            "risk_level": risk_level,
            "is_safe": can_assess_risk and not has_major_risk and len(risk_tags) <= 1,
            "red_flag_count": len(risk_tags),
            "has_major_risk": has_major_risk,
            "triggered_risks": risk_tags,
            "goodwill_ratio": goodwill_ratio,
            "goodwill_risk": "数据缺失·无法判断" if goodwill_ratio is None else (f"商誉占比 {goodwill_ratio}% (安全)" if goodwill_ratio < 20.0 else f"商誉偏高 ({goodwill_ratio}%)"),
            "receivables_ratio": receivables_ratio,
            "receivables_risk": "数据缺失·无法判断" if receivables_ratio is None else (f"应收占营收 {receivables_ratio}% (正常)" if receivables_ratio < 35.0 else f"应收偏高 ({receivables_ratio}%)"),
            "debt_ratio": debt_ratio if debt_known else None,
            "debt_risk": "数据缺失·无法判断" if not debt_known else (f"负债率 {debt_ratio}% (稳健)" if debt_ratio < 65.0 else f"负债偏高 ({debt_ratio}%)"),
            "risk_tags": risk_tags,
            "risk_summary": "部分财报风险指标缺失：" + "、".join(missing_risk_metrics) if not can_assess_risk and not risk_tags else ("通过已计算的财务排雷项" if not risk_tags else "；".join(risk_tags)),
            "can_assess_risk": can_assess_risk,
            "unknown_risks": missing_risk_metrics,
            "indicators": {
                "goodwill_to_equity_pct": goodwill_ratio,
                "receivables_to_revenue_pct": receivables_ratio,
                "inventory_to_assets_pct": inventory_ratio,
            "debt_to_assets_pct": debt_ratio if debt_known else None,
                "cash_to_net_profit_ratio": ocf_ratio
            }
        }

        # 5. 皮奥特罗斯基 Piotroski F-Score (0 ~ 9 分)
        f_score = 0
        f_details = []

        # F1: 净资产收益率/ROA 为正
        roe_known = self._has_finite_value(latest_dp.get("ROE")) or self._has_finite_value(latest_ind.get("ROEJQ"))
        prev_roe_known = self._has_finite_value(prev_dp.get("ROE"))
        prev_debt_known = self._has_finite_value(prev_dp.get("DEBT_ASSET_RATIO"))
        gross_margin_known = self._has_finite_value(latest_ind.get("XSMLL"))
        prev_gross_margin_known = self._has_finite_value(prev_ind.get("XSMLL"))
        turnover_known = self._has_finite_value(latest_dp.get("TOTAL_ASSETS_TR")) or (
            self._has_finite_value(latest_dp.get("TOTAL_ASSETS")) and
            self._has_finite_value(latest_dp.get("TOTAL_OPERATE_INCOME"))
        )
        prev_turnover_known = self._has_finite_value(prev_dp.get("TOTAL_ASSETS_TR"))

        if roe_known and roe > 0:
            f_score += 1
            f_details.append("ROA/ROE为正 (+1)")
        # F2: 经营现金流为正
        if ocf is not None and ocf > 0:
            f_score += 1
            f_details.append("经营活动现金流为正 (+1)")
        # F3: ROE/ROA 同比提升
        prev_roe = self._finite_float(prev_dp.get("ROE"))
        if roe_known and prev_roe_known and roe >= prev_roe:
            f_score += 1
            f_details.append("资产回报率同比提升 (+1)")
        # F4: 经营现金流大于净利润 (盈利含金量)
        if ocf is not None and parent_np > 0 and ocf >= parent_np * 0.85:
            f_score += 1
            f_details.append("现金流成色高无纸面虚增 (+1)")
        # F5: 资产负债率同比未恶化
        prev_debt = self._finite_float(prev_dp.get("DEBT_ASSET_RATIO"))
        if debt_known and prev_debt_known and (debt_ratio <= prev_debt + 1.0 or debt_ratio < 50.0):
            f_score += 1
            f_details.append("长期财务杠杆未恶化 (+1)")
        # F6: 货币资金充裕 (无流动性断裂危机)
        monetary = self._finite_float(latest_dp.get("MONETARYFUNDS"))
        monetary_known = self._has_finite_value(latest_dp.get("MONETARYFUNDS"))
        liabilities_known = self._has_finite_value(latest_dp.get("TOTAL_LIABILITIES"))
        if (monetary_known and liabilities_known and monetary >= tot_liab * 0.25) or (debt_known and debt_ratio < 40.0):
            f_score += 1
            f_details.append("短期流动性储备充裕 (+1)")
        # F7 requires comparable share-count history, which this data source does
        # not provide reliably. Keep it unscored instead of awarding a free point.
        f_details.append("股本稀释情况：数据源缺少可比股本序列（未计分）")
        # F8: 毛利率保持或提升
        prev_gm = self._finite_float(prev_ind.get("XSMLL"))
        if gross_margin_known and prev_gross_margin_known and (gross_margin >= prev_gm - 0.5 or gross_margin >= 30.0):
            f_score += 1
            f_details.append("产品毛利率维持坚挺 (+1)")
        # F9: 资产周转率提升或保持高位
        prev_tr = self._finite_float(prev_dp.get("TOTAL_ASSETS_TR"))
        if turnover_known and prev_turnover_known and (turnover >= prev_tr or turnover >= 0.5):
            f_score += 1
            f_details.append("资产营运周转效率良好 (+1)")

        f_rating = "部分字段规则命中，非完整Piotroski F-Score"
        f_score_info = {
            "score": None,
            "partial_points": f_score,
            "complete_score_available": False,
            "rating": f_rating,
            "details": f_details
        }

        # 6. 企业画像仅在对应披露字段可用时生成；不把部分 F-Score 拼成综合评分。
        persona = "⚠️【数据不完整·基本面风险未知】" if not can_assess_risk else "基本面风险字段可用·未生成策略评级"
        if has_major_risk:
            persona = "🚨【财务排雷·高度风险警告】"
        elif can_assess_risk:
            if roe_known and roe >= 15.0 and ocf_ratio is not None and ocf_ratio >= 1.0 and debt_known and debt_ratio < 50.0:
                persona = "💎【现金奶牛·核心白马资产】"
            elif deduct_profit_yoy_known and rev_yoy_known and deduct_profit_yoy >= 25.0 and rev_yoy >= 15.0:
                persona = "🚀【高景气·高成长主升白马】"
            elif net_margin_known and net_margin >= 30.0:
                persona = "🏰【高护城河·品牌溢价龙头】"
            elif turnover_known and turnover >= 1.0:
                persona = "⚡【极速周转·运营效率典范】"
            elif debt_known and debt_ratio >= 70.0:
                persona = "⚠️【重资产·高负债承压型】"

        return {
            "symbol": sym,
            "name": name,
            "data_available": True,
            "data_quality": {
                "available": True,
                "can_assess_risk": can_assess_risk,
                "missing_risk_metrics": missing_risk_metrics,
                "can_generate_composite_score": False,
                "score_reason": "不使用未经验证的复合分；完整Piotroski F-Score所需的可比股本数据不可用"
            },
            "is_etf": False,
            "report_name": report_name,
            "report_date": report_name,
            "fundamental_score": None,
            "persona": persona,
            "dupont": dupont_info,
            "growth": growth_info,
            "cash_quality": cash_info,
            "red_flags": red_flags_info,
            "f_score": f_score_info,
            "piotroski_f_score": f_score_info
        }

financial_engine = FinancialAnalysisEngine()
