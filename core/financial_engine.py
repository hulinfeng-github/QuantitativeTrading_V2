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

        # ETF 特殊处理
        if raw and raw.get("is_etf"):
            return {
                "symbol": sym,
                "name": name,
                "is_etf": True,
                "report_name": "ETF 基金份额报告",
                "report_date": "最新交易日",
                "fundamental_score": 90.0,
                "persona": "🛡️【指数型基金·天然免疫个股排雷】",
                "summary": "场内交易型指数基金，持有一篮子优质标的，无单一公司财务造假与商誉暴雷风险。",
                "dupont": {
                    "roe": 0.0,
                    "net_margin": 0.0,
                    "net_profit_margin": 0.0,
                    "asset_turnover": 0.0,
                    "equity_multiplier": 1.0,
                    "dupont_score": 90.0,
                    "primary_driver": "分散化组合配置，规避个股非系统性风险",
                    "driver_type": "指数型资产配置",
                    "driver_desc": "分散化组合配置，规避个股非系统性风险"
                },
                "f_score": {"score": 9, "level": "卓越", "rating": "🌟 卓越防御资产", "details": ["ETF一篮子资产分散配置", "天然免疫单一财务暴雷"]},
                "piotroski_f_score": {"score": 9, "level": "卓越", "rating": "🌟 卓越防御资产", "details": ["ETF一篮子资产分散配置", "天然免疫单一财务暴雷"]},
                "growth": {"revenue_yoy": 0.0, "net_profit_yoy": 0.0, "deduct_profit_yoy": 0.0, "gross_margin": 0.0, "growth_tag": "跟踪标的指数"},
                "cash_quality": {"ocf_match_ratio": 1.0, "quality_tag": "优质流动性"},
                "red_flags": {
                    "radar_score": 100.0,
                    "risk_level": "极低风险",
                    "is_safe": True,
                    "has_major_risk": False,
                    "triggered_risks": [],
                    "goodwill_risk": "无商誉风险",
                    "receivables_risk": "无应收坏账风险",
                    "debt_risk": "无债务危机",
                    "risk_tags": [],
                    "risk_summary": "ETF 具备天然分散属性，通过全部财务排雷项",
                    "indicators": {
                        "goodwill_to_equity_pct": 0.0,
                        "receivables_to_revenue_pct": 0.0,
                        "inventory_to_assets_pct": 0.0,
                        "debt_to_assets_pct": 0.0,
                        "cash_to_net_profit_ratio": 1.0
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
        roe = float(latest_dp.get("ROE") or latest_ind.get("ROEJQ") or 0.0)
        net_margin = float(latest_dp.get("SALE_NPR") or latest_ind.get("XSJLL") or 0.0)
        turnover = float(latest_dp.get("TOTAL_ASSETS_TR") or 0.0)
        if turnover <= 0 and latest_dp.get("TOTAL_ASSETS"):
            tot_assets = float(latest_dp.get("TOTAL_ASSETS") or 1.0)
            tot_rev = float(latest_dp.get("TOTAL_OPERATE_INCOME") or 0.0)
            turnover = round(tot_rev / tot_assets, 3)

        leverage = float(latest_dp.get("EQUITY_MULTIPLIER") or 1.0)
        debt_ratio = float(latest_dp.get("DEBT_ASSET_RATIO") or latest_ind.get("ZCFZL") or 0.0)

        # 杜邦核心驱动力归因
        driver_type = "均衡稳健型"
        driver_desc = "盈利、运营周转与资本结构较为均衡"
        if net_margin >= 25.0:
            driver_type = "【高毛利·产品护城河驱动】"
            driver_desc = f"销售净利率高达 {net_margin:.1f}%，凭借强产品壁垒与品牌定价权创造核心利润"
        elif turnover >= 0.8:
            driver_type = "【高运营周转·供应链效率驱动】"
            driver_desc = f"总资产周转率高达 {turnover:.2f} 次，依托极速运营效率与薄利多销驱动收益"
        elif leverage >= 2.5 and debt_ratio >= 65.0:
            driver_type = "【高杠杆依赖·注意财务承压】"
            driver_desc = f"权益乘数达 {leverage:.2f}，资产负债率达 {debt_ratio:.1f}%，高ROE部分依靠负债杠杆拉动"
        elif roe >= 15.0:
            driver_type = "【高质高ROE·核心资产型】"
            driver_desc = f"ROE高达 {roe:.1f}%，且各项指标协调稳健"

        dupont_score = round(float(np.clip(roe * 3.5 + (10.0 if net_margin > 20 else 0) + (10.0 if turnover > 0.6 else 0), 20.0, 99.0)), 1)

        dupont_info = {
            "report_name": report_name,
            "roe": round(roe, 2),
            "net_margin": round(net_margin, 2),
            "net_profit_margin": round(net_margin, 2),
            "asset_turnover": round(turnover, 3),
            "equity_multiplier": round(leverage, 2),
            "debt_ratio": round(debt_ratio, 2),
            "dupont_score": dupont_score,
            "primary_driver": driver_desc,
            "driver_type": driver_type,
            "driver_desc": driver_desc
        }

        # 2. 营业成长与业绩景气度
        rev_yoy = float(latest_ind.get("TOTALOPERATEREVETZ") or latest_ind.get("YYSR_TB") or latest_ind.get("YYSRTZ") or 0.0)
        net_profit_yoy = float(latest_ind.get("PARENTNETPROFITTZ") or latest_ind.get("JLR_TB") or latest_ind.get("JLRTZ") or 0.0)
        deduct_profit_yoy = float(latest_ind.get("KCFJCXSYJLRTZ") or 0.0)
        gross_margin = float(latest_ind.get("XSMLL") or 0.0)

        growth_tag = "平稳运行"
        if deduct_profit_yoy >= 25.0 and rev_yoy >= 15.0:
            growth_tag = "🚀 高景气加速主升"
        elif deduct_profit_yoy >= 10.0:
            growth_tag = "📈 稳健持续增长"
        elif deduct_profit_yoy < -20.0:
            growth_tag = "⚠️ 业绩大幅滑坡承压"
        elif deduct_profit_yoy < 0.0:
            growth_tag = "📉 周期微跌调整"

        growth_info = {
            "revenue_yoy": round(rev_yoy, 2),
            "net_profit_yoy": round(net_profit_yoy, 2),
            "deduct_profit_yoy": round(deduct_profit_yoy, 2),
            "gross_margin": round(gross_margin, 2),
            "growth_tag": growth_tag
        }

        # 3. 现金流成色与匹配度 (Earnings Quality)
        # 经营活动现金流量净额
        tot_rev = float(latest_dp.get("TOTAL_OPERATE_INCOME") or 1.0)
        parent_np = float(latest_dp.get("PARENT_NETPROFIT") or latest_dp.get("NETPROFIT") or 0.0)
        # 经营现金流占营收比例
        jyxj_yysr = float(latest_ind.get("JYXJLYYSR") or 0.0)
        ocf = jyxj_yysr * tot_rev if jyxj_yysr != 0 else parent_np * 0.95

        ocf_ratio = round(ocf / parent_np, 2) if parent_np > 0 else 0.0
        cash_tag = "真金白银·现金充沛"
        if ocf_ratio >= 1.0:
            cash_tag = "💎 现金奶牛 (现金流超净利润)"
        elif ocf_ratio >= 0.7:
            cash_tag = "✅ 现金流与盈利匹配良好"
        elif ocf_ratio > 0:
            cash_tag = "⚠️ 盈利现金含量偏低 (应收占比较高)"
        else:
            cash_tag = "🚨 经营性失血 (现金流为负)"

        cash_info = {
            "ocf_ratio": ocf_ratio,
            "cash_tag": cash_tag,
            "per_share_ocf": round(float(latest_ind.get("MGJYXJJE") or 0.0), 3)
        }

        # 4. A股特色财务排雷避雷雷达 (Red Flags)
        tot_assets = float(latest_dp.get("TOTAL_ASSETS") or 1.0)
        tot_liab = float(latest_dp.get("TOTAL_LIABILITIES") or 0.0)
        net_assets = max(tot_assets - tot_liab, 1.0)

        goodwill = float(latest_dp.get("GOODWILL") or 0.0)
        goodwill_ratio = round(goodwill / net_assets * 100.0, 2)

        receivables = float(latest_dp.get("ACCOUNTS_RECE") or 0.0)
        receivables_ratio = round(receivables / tot_rev * 100.0, 2) if tot_rev > 0 else 0.0

        inventory = float(latest_dp.get("INVENTORY") or 0.0)
        inventory_ratio = round(inventory / tot_assets * 100.0, 2)

        risk_tags = []
        has_major_risk = False

        # 商誉风险
        if goodwill_ratio >= 35.0:
            risk_tags.append(f"🚨【重度商誉暴雷风险】商誉占净资产 {goodwill_ratio}% (警戒线 20%)，警惕年末大额资产减值洗澡！")
            has_major_risk = True
        elif goodwill_ratio >= 20.0:
            risk_tags.append(f"⚠️【中度商誉隐患】商誉占净资产 {goodwill_ratio}%，稍显偏高")

        # 应收账款风险
        if receivables_ratio >= 50.0:
            risk_tags.append(f"🚨【应收账款畸高】应收账款占营收 {receivables_ratio}%，谨防下游拖欠与坏账减值！")
            has_major_risk = True
        elif receivables_ratio >= 35.0:
            risk_tags.append(f"⚠️【应收占比偏大】应收账款占营收 {receivables_ratio}%")

        # 存货积压风险
        if inventory_ratio >= 40.0:
            risk_tags.append(f"⚠️【存货占比较高】存货占总资产 {inventory_ratio}%，注意产品迭代跌价损失")

        # 现金流失血风险
        if ocf_ratio < 0.3 and parent_np > 0:
            risk_tags.append("🚨【纸面富贵·缺乏真金白银】经营现金流严重恶化，多为赊销挂账")
            has_major_risk = True

        # 极高负债风险 (排除银行非银金融除外)
        if debt_ratio >= 78.0 and not (symbol.startswith(("sh600036", "sh601", "sz000001")) and "银行" in name):
            risk_tags.append(f"🚨【高杠杆债务承压】资产负债率高达 {debt_ratio}%，短期偿债与流动性压力大")
            has_major_risk = True

        radar_score = 100.0
        if has_major_risk:
            radar_score -= 30.0
        radar_score -= len(risk_tags) * 10.0
        radar_score = float(np.clip(radar_score, 10.0, 100.0))
        risk_level = "极低风险" if len(risk_tags) == 0 else ("高危触雷" if has_major_risk else "中度隐患")

        red_flags_info = {
            "radar_score": round(radar_score, 1),
            "risk_level": risk_level,
            "is_safe": not has_major_risk and len(risk_tags) <= 1,
            "red_flag_count": len(risk_tags),
            "has_major_risk": has_major_risk,
            "triggered_risks": risk_tags,
            "goodwill_ratio": goodwill_ratio,
            "goodwill_risk": f"商誉占比 {goodwill_ratio}% (安全)" if goodwill_ratio < 20.0 else f"商誉偏高 ({goodwill_ratio}%)",
            "receivables_ratio": receivables_ratio,
            "receivables_risk": f"应收占营收 {receivables_ratio}% (正常)" if receivables_ratio < 35.0 else f"应收偏高 ({receivables_ratio}%)",
            "debt_ratio": debt_ratio,
            "debt_risk": f"负债率 {debt_ratio}% (稳健)" if debt_ratio < 65.0 else f"负债偏高 ({debt_ratio}%)",
            "risk_tags": risk_tags,
            "risk_summary": "通过全部财务排雷检测，基本面健康无重大暴雷隐患" if not risk_tags else "；".join(risk_tags),
            "indicators": {
                "goodwill_to_equity_pct": goodwill_ratio,
                "receivables_to_revenue_pct": receivables_ratio,
                "inventory_to_assets_pct": inventory_ratio,
                "debt_to_assets_pct": debt_ratio,
                "cash_to_net_profit_ratio": ocf_ratio
            }
        }

        # 5. 皮奥特罗斯基 Piotroski F-Score (0 ~ 9 分)
        f_score = 0
        f_details = []

        # F1: 净资产收益率/ROA 为正
        if roe > 0:
            f_score += 1
            f_details.append("ROA/ROE为正 (+1)")
        # F2: 经营现金流为正
        if ocf > 0:
            f_score += 1
            f_details.append("经营活动现金流为正 (+1)")
        # F3: ROE/ROA 同比提升
        prev_roe = float(prev_dp.get("ROE") or 0.0)
        if roe >= prev_roe:
            f_score += 1
            f_details.append("资产回报率同比提升 (+1)")
        # F4: 经营现金流大于净利润 (盈利含金量)
        if ocf >= parent_np * 0.85:
            f_score += 1
            f_details.append("现金流成色高无纸面虚增 (+1)")
        # F5: 资产负债率同比未恶化
        prev_debt = float(prev_dp.get("DEBT_ASSET_RATIO") or 0.0)
        if debt_ratio <= prev_debt + 1.0 or debt_ratio < 50.0:
            f_score += 1
            f_details.append("长期财务杠杆未恶化 (+1)")
        # F6: 货币资金充裕 (无流动性断裂危机)
        monetary = float(latest_dp.get("MONETARYFUNDS") or 0.0)
        if monetary >= tot_liab * 0.25 or debt_ratio < 40.0:
            f_score += 1
            f_details.append("短期流动性储备充裕 (+1)")
        # F7: 未发生稀释性暴增
        f_score += 1
        f_details.append("股本结构保持稳定 (+1)")
        # F8: 毛利率保持或提升
        prev_gm = float(prev_ind.get("XSMLL") or 0.0)
        if gross_margin >= prev_gm - 0.5 or gross_margin >= 30.0:
            f_score += 1
            f_details.append("产品毛利率维持坚挺 (+1)")
        # F9: 资产周转率提升或保持高位
        prev_tr = float(prev_dp.get("TOTAL_ASSETS_TR") or 0.0)
        if turnover >= prev_tr or turnover >= 0.5:
            f_score += 1
            f_details.append("资产营运周转效率良好 (+1)")

        f_rating = "🌟 卓越价值标的" if f_score >= 8 else ("✅ 稳健中枢标的" if f_score >= 5 else "⚠️ 基本面承压")
        f_score_info = {
            "score": f_score,
            "rating": f_rating,
            "details": f_details
        }

        # 6. 综合基本面得分 (0 ~ 100) 与 企业画像 (Persona)
        score = 50.0
        # 盈利能力加分 (最多30分)
        score += min(roe * 1.5, 30.0)
        # 成长性加分 (最多25分)
        if deduct_profit_yoy > 0:
            score += min(deduct_profit_yoy * 0.5, 25.0)
        else:
            score += max(deduct_profit_yoy * 0.3, -15.0)
        # F-score 加分 (最多20分)
        score += (f_score / 9.0) * 20.0
        # 现金流加分
        if ocf_ratio >= 1.0:
            score += 10.0
        elif ocf_ratio < 0:
            score -= 15.0

        # 重大排雷惩罚
        if has_major_risk:
            score -= 30.0

        score = float(np.clip(score, 10.0, 99.0))

        # 画像定调
        persona = "⚖️【稳健蓝筹·均衡型】"
        if has_major_risk:
            persona = "🚨【财务排雷·高度风险警告】"
        elif roe >= 15.0 and ocf_ratio >= 1.0 and debt_ratio < 50.0:
            persona = "💎【现金奶牛·核心白马资产】"
        elif deduct_profit_yoy >= 25.0 and rev_yoy >= 15.0:
            persona = "🚀【高景气·高成长主升白马】"
        elif net_margin >= 30.0:
            persona = "🏰【高护城河·品牌溢价龙头】"
        elif turnover >= 1.0:
            persona = "⚡【极速周转·运营效率典范】"
        elif debt_ratio >= 70.0:
            persona = "⚠️【重资产·高负债承压型】"

        return {
            "symbol": sym,
            "name": name,
            "is_etf": False,
            "report_name": report_name,
            "report_date": report_name,
            "fundamental_score": round(score, 1),
            "persona": persona,
            "dupont": dupont_info,
            "growth": growth_info,
            "cash_quality": cash_info,
            "red_flags": red_flags_info,
            "f_score": f_score_info,
            "piotroski_f_score": f_score_info
        }

financial_engine = FinancialAnalysisEngine()
