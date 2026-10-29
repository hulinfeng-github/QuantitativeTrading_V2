/**
 * A股智能量化系统 2.0 (QuantitativeTrading 2.0) 前端交互引擎
 * 支持：多因子选股雷达、实时做T监控、专业ECharts金融图表(K线/分时VWAP/筹码分布/组合净值对比)
 */

let klineChartInstance = null;
let t0ChartInstance = null;
let backtestChartInstance = null;

// 初始化入口
document.addEventListener("DOMContentLoaded", () => {
  initTabs();
  loadStatusAndSentiment();
  loadScreenerResults();
  loadWatchlist();
  loadT0Monitor();
  loadSettings();
  loadOptimizationLog();

  // 盘中自动轮询 (每 15 秒更新一次行情与监控)
  setInterval(() => {
    loadStatusAndSentiment();
    const activeTab = document.querySelector(".tab-btn.active")?.dataset.tab;
    if (activeTab === "watchlist") loadWatchlist();
    if (activeTab === "t0") loadT0Monitor();
    if (activeTab === "tail230") loadTailStatus();
  }, 15000);
});

// 标签切换
function initTabs() {
  const tabBtns = document.querySelectorAll(".tab-btn");
  tabBtns.forEach(btn => {
    btn.addEventListener("click", () => {
      tabBtns.forEach(b => b.classList.remove("active"));
      btn.classList.add("active");

      const targetTab = btn.dataset.tab;
      document.querySelectorAll(".tab-pane").forEach(pane => {
        pane.style.display = "none";
      });

      const activePane = document.getElementById(`pane-${targetTab}`);
      if (activePane) activePane.style.display = "block";

      // 切换到 2.30 战法或打板面板时加载数据
      if (targetTab === "tail230") loadTail230Data();
      if (targetTab === "limitup") loadLimitUpData();

      // 切换到带图表的面板时自适应尺寸
      if (targetTab === "diagnose" && klineChartInstance) klineChartInstance.resize();
      if (targetTab === "backtest" && backtestChartInstance) backtestChartInstance.resize();
    });
  });
}

// 格式化函数
function formatPct(val) {
  const n = parseFloat(val);
  if (isNaN(n)) return "0.00%";
  const sign = n > 0 ? "+" : "";
  const cls = n > 0 ? "text-up" : n < 0 ? "text-down" : "";
  return `<span class="${cls}">${sign}${n.toFixed(2)}%</span>`;
}

function formatPrice(val, prev) {
  const p = parseFloat(val);
  if (isNaN(p)) return "0.00";
  const pr = parseFloat(prev);
  const cls = (!isNaN(pr) && p > pr) ? "text-up" : (!isNaN(pr) && p < pr) ? "text-down" : "";
  return `<span class="${cls}">${p.toFixed(2)}</span>`;
}

// 1. 加载大盘情绪与宏观雷达
async function loadStatusAndSentiment() {
  try {
    const res = await fetch("/api/sentiment");
    if (!res.ok) return;
    const data = await res.json();

    document.getElementById("macroIndexPrice").innerHTML = `${data.index_price} ${formatPct(data.index_pct)}`;
    document.getElementById("macroSentimentScore").innerHTML = `<span class="text-gold">${data.temp_score} 分</span> (${data.cycle_phase})`;
    document.getElementById("macroExposure").innerHTML = `<span class="text-cyan">${(data.target_exposure * 100).toFixed(0)}%</span>`;
    document.getElementById("macroRegimeText").innerText = data.regime;
    document.getElementById("macroActionGuide").innerText = data.action_guide;
  } catch (e) {
    console.error("加载情绪雷达异常:", e);
  }
}

// 2. 选股雷达与全市场扫描
async function loadScreenerResults() {
  try {
    const res = await fetch("/api/scan/results");
    if (!res.ok) return;
    const data = await res.json();
    renderScreenerCards(data.results || []);

    const status = data.status || {};
    const notice = document.getElementById("scanNotice");
    if (status.is_scanning) {
      notice.innerHTML = `<span class="text-gold">⏳ ${status.message} (${status.progress}/${status.total})</span>`;
    } else if (status.last_scan_time) {
      notice.innerHTML = `<span class="text-muted">上次扫描: ${status.last_scan_time} · ${status.message}</span>`;
    }
  } catch (e) {
    console.error("获取选股结果异常:", e);
  }
}

function renderScreenerCards(candidates) {
  const container = document.getElementById("screenerContainer");
  if (!candidates || candidates.length === 0) {
    container.innerHTML = `
      <div style="grid-column: 1 / -1; text-align: center; padding: 60px 0; color: var(--text-dim);">
        <p style="font-size: 16px; margin-bottom: 8px;">💎 选股池暂未生成或正在扫描中</p>
        <p style="font-size: 13px;">点击右上角「🚀 立即全市场主板智能选股」按钮，系统将在后台全量计算多因子特征</p>
      </div>`;
    return;
  }

  let html = "";
  candidates.forEach(c => {
    const chip = c.chip_info || {};
    const levels = c.trade_levels || {};
    const isExplosion = c.control_status === "主力突破起爆";
    const statusPill = isExplosion ? `<span class="pill pill-gold">🚀 主力起爆</span>` :
                       c.control_status === "主力吸筹蓄势" ? `<span class="pill pill-purple">🛡️ 密集蓄势</span>` :
                       `<span class="pill pill-cyan">📈 多头共振</span>`;

    // 财报深度指标
    const fin = c.financial || {};
    const dupont = fin.dupont || {};
    const growth = fin.growth || {};
    const redFlags = fin.red_flags || {};
    const isSafe = redFlags.is_safe !== false;
    const persona = fin.persona || "";

    html += `
      <div class="stock-card" onclick="openStockDiagnose('${c.symbol}')">
        <div class="stock-header">
          <div class="stock-name-box">
            <h3>${c.name} ${statusPill}</h3>
            <span>${c.symbol} · 主板精选</span>
            ${persona ? `<div><span class="pill pill-purple" style="font-size: 10px; margin-top: 4px; display: inline-block;">${persona}</span></div>` : ''}
          </div>
          <div class="score-badge">
            ⭐ 评分: ${c.score}分
          </div>
        </div>

        <div style="display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px;">
          <div>
            <span style="font-size: 20px; font-weight: 700; font-family: var(--font-mono);">${c.price.toFixed(2)}</span>
            <span style="font-size: 13px; margin-left: 6px;">${formatPct(c.change_pct)}</span>
          </div>
          <span style="font-size: 12px; color: var(--c-cyan); font-weight: 600;">${c.signal_desc}</span>
        </div>

        <div class="stock-metric-row">
          <div class="metric-item">
            <div class="metric-label">修正 RSRS</div>
            <div class="metric-value text-cyan">${c.rsrs_norm}</div>
          </div>
          <div class="metric-item">
            <div class="metric-label">主力成本</div>
            <div class="metric-value text-purple">${c.main_cost ? c.main_cost.toFixed(2) : '-'}</div>
          </div>
          <div class="metric-item">
            <div class="metric-label">获利盘比例</div>
            <div class="metric-value text-up">${c.profit_chip_ratio ? c.profit_chip_ratio + '%' : '-'}</div>
          </div>
        </div>

        <!-- 深度财报体检速览 -->
        <div class="fin-mini-strip">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 5px;">
            <span style="font-weight: 600; color: var(--c-cyan);">📊 经营与财报体检</span>
            <span class="${isSafe ? 'pill pill-safe' : 'pill pill-danger'}">${redFlags.risk_level || '安全'} (排雷: ${redFlags.radar_score || 100}分)</span>
          </div>
          <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 4px; text-align: center;">
            <div>
              <div style="font-size: 10px; color: var(--text-dim);">杜邦 ROE</div>
              <div style="font-weight: 700; color: #fff;">${dupont.roe !== undefined ? dupont.roe.toFixed(1) + '%' : '-'}</div>
            </div>
            <div>
              <div style="font-size: 10px; color: var(--text-dim);">扣非净利增速</div>
              <div style="font-weight: 700;" class="${growth.deduct_profit_yoy >= 0 ? 'text-up' : 'text-down'}">${growth.deduct_profit_yoy !== undefined ? (growth.deduct_profit_yoy > 0 ? '+' : '') + growth.deduct_profit_yoy.toFixed(1) + '%' : '-'}</div>
            </div>
            <div>
              <div style="font-size: 10px; color: var(--text-dim);">皮氏 F-Score</div>
              <div style="font-weight: 700; color: var(--c-gold);">${fin.piotroski_f_score ? fin.piotroski_f_score.score + '/9分' : '-'}</div>
            </div>
          </div>
          ${redFlags.triggered_risks && redFlags.triggered_risks.length > 0 ? `
            <div style="margin-top: 5px; color: #f87171; font-size: 10px; background: rgba(239,68,68,0.1); padding: 2px 6px; border-radius: 3px;">
              ⚠️ 触雷: ${redFlags.triggered_risks.slice(0, 2).join("；")}
            </div>
          ` : ''}
        </div>

        <div style="font-size: 12px; color: var(--text-dim); margin-bottom: 8px;">
          建议止损: <span class="text-down">${levels.initial_stop || '-'}元 (-${levels.stop_pct || 0}%)</span> | 
          目标止盈: <span class="text-up">${levels.target_price || '-'}元 (+${levels.target_pct || 0}%)</span>
        </div>

        <ul class="reason-list">
          ${(c.reasons || []).slice(0, 3).map(r => `<li>${r}</li>`).join("")}
        </ul>
      </div>
    `;
  });

  container.innerHTML = html;
}

async function triggerScan() {
  const btn = document.getElementById("btnStartScan");
  btn.disabled = true;
  btn.innerText = "⏳ 全市场扫描启动中...";
  try {
    const res = await fetch("/api/scan/start", { method: "POST" });
    const data = await res.json();
    alert(data.message || "全市场扫描已启动");
    pollScanStatus();
  } catch (e) {
    alert("启动扫描异常: " + e);
  } finally {
    btn.disabled = false;
    btn.innerText = "🚀 立即全市场主板智能选股";
  }
}

function pollScanStatus() {
  const timer = setInterval(async () => {
    const res = await fetch("/api/scan/results");
    if (!res.ok) { clearInterval(timer); return; }
    const data = await res.json();
    const status = data.status || {};
    document.getElementById("scanNotice").innerHTML = `<span class="text-gold">⏳ ${status.message} (${status.progress}/${status.total})</span>`;
    if (!status.is_scanning) {
      clearInterval(timer);
      renderScreenerCards(data.results || []);
    }
  }, 2000);
}

// 3. 盘中自选股实时监控
async function loadWatchlist() {
  try {
    const res = await fetch("/api/watchlist");
    if (!res.ok) return;
    const items = await res.json();
    const tbody = document.getElementById("watchlistTbody");

    if (!items || items.length === 0) {
      tbody.innerHTML = `<tr><td colspan="8" style="text-align: center; color: var(--text-dim); padding: 30px;">暂无自选监控标的，点击右上角「+ 添加自选股」录入</td></tr>`;
      return;
    }

function formatStockPrice(p) {
  if (p === null || p === undefined || isNaN(p) || p <= 0) return "-";
  const num = parseFloat(p);
  return num < 10 || (num % 1 !== 0 && num.toString().split(".")[1]?.length >= 3) ? num.toFixed(3) : num.toFixed(2);
}

    let html = "";
    items.forEach(it => {
      html += `
        <tr>
          <td><strong style="color: #fff; cursor: pointer;" onclick="openStockDiagnose('${it.symbol}')">${it.name}</strong> <span style="font-size: 11px; color: var(--text-dim);">${it.symbol}</span></td>
          <td>${formatStockPrice(it.current_price)}</td>
          <td>${formatPct(it.change_pct)}</td>
          <td>${it.holding_cost > 0 ? formatStockPrice(it.holding_cost) : '-'}</td>
          <td>${it.holding_shares > 0 ? it.holding_shares : '-'}</td>
          <td>${it.holding_cost > 0 ? formatPct(it.pnl_pct) : '-'}</td>
          <td>${it.holding_cost > 0 ? `<span class="${it.pnl_val >= 0 ? 'text-up' : 'text-down'}">${it.pnl_val > 0 ? '+' : ''}${it.pnl_val.toFixed(2)}元</span>` : '-'}</td>
          <td>
            <button class="btn btn-secondary btn-sm" onclick="openStockDiagnose('${it.symbol}')">诊断</button>
            <button class="btn btn-secondary btn-sm" style="color: #ef4444;" onclick="deleteWatchlist('${it.symbol}')">删除</button>
          </td>
        </tr>
      `;
    });
    tbody.innerHTML = html;
  } catch (e) {
    console.error("加载自选股失败:", e);
  }
}

async function deleteWatchlist(symbol) {
  if (!confirm(`确定将 ${symbol} 移出自选监控池吗？`)) return;
  try {
    const res = await fetch(`/api/watchlist/${symbol}`, { method: "DELETE" });
    if (res.ok) loadWatchlist();
  } catch (e) {
    alert("删除失败: " + e);
  }
}

function showAddWatchlistModal() {
  document.getElementById("addWatchModal").style.display = "flex";
}

function hideAddWatchlistModal() {
  document.getElementById("addWatchModal").style.display = "none";
}

async function submitAddWatchlist() {
  const code = document.getElementById("addCode").value.trim();
  const cost = parseFloat(document.getElementById("addCost").value) || 0.0;
  const shares = parseInt(document.getElementById("addShares").value) || 0;
  const notes = document.getElementById("addNotes").value.trim();

  if (!code) { alert("请输入股票代码或名称"); return; }

  try {
    const res = await fetch("/api/watchlist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol: code, holding_cost: cost, holding_shares: shares, notes })
    });
    const data = await res.json();
    if (res.ok) {
      hideAddWatchlistModal();
      loadWatchlist();
    } else {
      alert("添加失败: " + (data.detail || "未知错误"));
    }
  } catch (e) {
    alert("提交异常: " + e);
  }
}

// 4. A股 T+1 日内做T助手
async function loadT0Monitor() {
  try {
    const res = await fetch("/api/t0/monitor");
    if (!res.ok) return;
    const items = await res.json();
    const container = document.getElementById("t0CardsContainer");

    if (!items || items.length === 0) {
      container.innerHTML = `<div style="grid-column: 1 / -1; text-align: center; color: var(--text-dim); padding: 40px;">暂无持仓股票做T监控数据，请先在自选池中添加标的</div>`;
      return;
    }

    let html = "";
    items.forEach(t => {
      const pos = t.position || {};
      const hasOpenPos = pos.status === "OPEN";
      const isBuyT = t.signal_action === "BUY_T";
      const isSellT = t.signal_action === "SELL_T";

      const badge = isBuyT ? `<span class="pill pill-red">正T低吸触发</span>` :
                    isSellT ? `<span class="pill pill-green">倒T高抛触发</span>` :
                    hasOpenPos ? `<span class="pill pill-gold">做T持仓执行中</span>` :
                    `<span class="pill pill-cyan">通道内震荡</span>`;

      html += `
        <div class="stock-card" style="border-left: 4px solid ${isBuyT ? 'var(--c-up)' : isSellT ? 'var(--c-down)' : 'var(--border-subtle)'};">
          <div class="stock-header">
            <div>
              <h3 style="font-size: 15px;">${t.name} (${t.symbol}) ${badge}</h3>
              <span style="font-size: 11px; color: var(--text-dim);">趋势通道: ${t.trend_direction === 'BULL' ? '顺势多头' : '空头破位'}</span>
            </div>
            <span style="font-size: 16px; font-weight: 700; font-family: var(--font-mono);">${t.current_price.toFixed(2)}</span>
          </div>

          <div class="stock-metric-row">
            <div class="metric-item">
              <div class="metric-label">分时均价 VWAP</div>
              <div class="metric-value">${t.vwap.toFixed(2)}</div>
            </div>
            <div class="metric-item">
              <div class="metric-label">分时偏离度</div>
              <div class="metric-value ${t.bias_pct > 0 ? 'text-up' : 'text-down'}">${t.bias_pct > 0 ? '+' : ''}${t.bias_pct}%</div>
            </div>
            <div class="metric-item">
              <div class="metric-label">动态布林下/上轨</div>
              <div class="metric-value" style="font-size: 11px;">${t.dyn_lower} / ${t.dyn_upper}</div>
            </div>
          </div>

          <div style="font-size: 12px; margin-bottom: 8px;">
            <strong>研判指导:</strong> <span class="text-cyan">${t.signal_desc}</span>
          </div>

          ${t.reasons && t.reasons.length ? `<p style="font-size: 11px; color: var(--text-muted); line-height: 1.5;">${t.reasons.join("；")}</p>` : ''}
        </div>
      `;
    });
    container.innerHTML = html;
  } catch (e) {
    console.error("加载做T监控异常:", e);
  }
}

async function resetT0Today() {
  if (!confirm("确定要重置今日所有做T持仓状态吗？")) return;
  await fetch("/api/t0/reset", { method: "POST" });
  loadT0Monitor();
}

// 5. 单股量化诊断与 ECharts 专业K线图 + 筹码分布
async function openStockDiagnose(symbol) {
  document.querySelector('.tab-btn[data-tab="diagnose"]').click();
  document.getElementById("diagnoseInput").value = symbol;
  runDiagnose();
}

async function runDiagnose() {
  const query = document.getElementById("diagnoseInput").value.trim();
  if (!query) { alert("请输入股票代码或名称"); return; }

  try {
    const res = await fetch(`/api/stock/diagnose?symbol=${encodeURIComponent(query)}`);
    if (!res.ok) {
      const err = await res.json();
      alert("诊断失败: " + (err.detail || "标的无效"));
      return;
    }
    const data = await res.json();
    renderDiagnoseSummary(data);

    // 加载专业K线图与筹码分布
    loadKlineChart(data.symbol);
  } catch (e) {
    alert("诊断异常: " + e);
  }
}

function renderDiagnoseSummary(data) {
  const box = document.getElementById("diagnoseSummaryBox");
  const levels = data.trade_levels || {};
  const chip = data.chip_info || {};
  const advice = data.position_advice || {};
  const fin = data.financial || {};
  const dupont = fin.dupont || {};
  const growth = fin.growth || {};
  const fScore = fin.piotroski_f_score || {};
  const redFlags = fin.red_flags || {};
  const inds = redFlags.indicators || {};
  const isSafe = redFlags.is_safe !== false;

  let finHtml = "";
  if (fin.symbol) {
    finHtml = `
      <div class="fin-report-card">
        <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid rgba(255,255,255,0.06); padding-bottom: 10px; margin-bottom: 12px; flex-wrap: wrap; gap: 8px;">
          <div>
            <h3 style="font-size: 15px; color: var(--c-cyan); display: inline-flex; align-items: center; gap: 6px;">
              📊 上市公司基本面经营与财报深度诊断
            </h3>
            <span style="font-size: 12px; color: var(--text-dim); margin-left: 8px;">
              最新报告期: <strong>${fin.report_date || '最新季度'}</strong>
            </span>
          </div>
          <div style="display: flex; gap: 8px; align-items: center;">
            <span class="pill pill-purple" style="font-size: 12px; padding: 4px 10px;">${fin.persona || '主板标的'}</span>
            <span class="${isSafe ? 'pill pill-safe' : 'pill pill-danger'}" style="font-size: 12px; padding: 4px 10px;">
              排雷状态: ${redFlags.risk_level || '安全'} (${redFlags.radar_score || 100}分)
            </span>
          </div>
        </div>

        <!-- 杜邦 3 因子核心分解 -->
        <div style="margin-bottom: 14px;">
          <div style="font-size: 12px; font-weight: 600; color: var(--text-muted); margin-bottom: 8px; display: flex; justify-content: space-between; flex-wrap: wrap; gap: 4px;">
            <span>🔬 杜邦分析 3 因子核心分解 (DuPont Analysis)</span>
            <span class="text-gold" style="font-size: 11px;">核心动力: ${dupont.primary_driver || '-'}</span>
          </div>
          <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px;">
            <div class="fin-dupont-box">
              <div style="font-size: 11px; color: var(--text-dim);">净资产收益率 (ROE)</div>
              <div style="font-size: 18px; font-weight: 700; color: var(--c-cyan); margin: 4px 0;">${dupont.roe !== undefined ? dupont.roe.toFixed(2) + '%' : '-'}</div>
              <div style="font-size: 10px; color: var(--text-muted);">杜邦评分: ${dupont.dupont_score || '-'}分</div>
            </div>
            <div class="fin-dupont-box">
              <div style="font-size: 11px; color: var(--text-dim);">销售净利率 (Net Margin)</div>
              <div style="font-size: 18px; font-weight: 700; color: #fff; margin: 4px 0;">${dupont.net_profit_margin !== undefined ? dupont.net_profit_margin.toFixed(2) + '%' : '-'}</div>
              <div style="font-size: 10px; color: var(--text-muted);">产品定价权与护城河</div>
            </div>
            <div class="fin-dupont-box">
              <div style="font-size: 11px; color: var(--text-dim);">总资产周转率 (Turnover)</div>
              <div style="font-size: 18px; font-weight: 700; color: #fff; margin: 4px 0;">${dupont.asset_turnover !== undefined ? dupont.asset_turnover.toFixed(2) + ' 次' : '-'}</div>
              <div style="font-size: 10px; color: var(--text-muted);">资金营运与周转效率</div>
            </div>
            <div class="fin-dupont-box">
              <div style="font-size: 11px; color: var(--text-dim);">权益乘数 (Leverage)</div>
              <div style="font-size: 18px; font-weight: 700; color: #fff; margin: 4px 0;">${dupont.equity_multiplier !== undefined ? dupont.equity_multiplier.toFixed(2) + ' 倍' : '-'}</div>
              <div style="font-size: 10px; color: var(--text-muted);">资产负债率: ${inds.debt_to_assets_pct !== undefined ? inds.debt_to_assets_pct.toFixed(1) + '%' : '-'}</div>
            </div>
          </div>
        </div>

        <!-- 经营成长与皮氏 F-Score 双栏网格 -->
        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; margin-bottom: 14px;">
          <!-- 成长与现金流质量 -->
          <div style="background: rgba(0,0,0,0.25); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); padding: 12px;">
            <div style="font-size: 12px; font-weight: 600; color: var(--text-muted); margin-bottom: 8px;">📈 经营成长性与盈利含金量</div>
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; font-size: 12px;">
              <div>营业收入增速: <strong class="${growth.revenue_yoy >= 0 ? 'text-up' : 'text-down'}">${growth.revenue_yoy !== undefined ? (growth.revenue_yoy > 0 ? '+' : '') + growth.revenue_yoy.toFixed(1) + '%' : '-'}</strong></div>
              <div>归母净利增速: <strong class="${growth.net_profit_yoy >= 0 ? 'text-up' : 'text-down'}">${growth.net_profit_yoy !== undefined ? (growth.net_profit_yoy > 0 ? '+' : '') + growth.net_profit_yoy.toFixed(1) + '%' : '-'}</strong></div>
              <div>扣非净利增速: <strong class="${growth.deduct_profit_yoy >= 0 ? 'text-up' : 'text-down'}">${growth.deduct_profit_yoy !== undefined ? (growth.deduct_profit_yoy > 0 ? '+' : '') + growth.deduct_profit_yoy.toFixed(1) + '%' : '-'}</strong></div>
              <div>综合销售毛利率: <strong style="color: #fff;">${growth.gross_margin !== undefined ? growth.gross_margin.toFixed(1) + '%' : '-'}</strong></div>
              <div style="grid-column: span 2; margin-top: 4px;">
                现金/净利匹配度: <strong class="${inds.cash_to_net_profit_ratio >= 1.0 ? 'text-cyan' : inds.cash_to_net_profit_ratio >= 0.7 ? 'text-gold' : 'text-down'}">${inds.cash_to_net_profit_ratio !== undefined ? inds.cash_to_net_profit_ratio.toFixed(2) + ' 倍' : '-'}</strong>
                <span style="font-size: 10px; color: var(--text-dim); margin-left: 4px;">(经营净现金流 / 归母净利润，>1.0为良性)</span>
              </div>
            </div>
          </div>

          <!-- 皮氏 9 维 F-Score 评分 -->
          <div style="background: rgba(0,0,0,0.25); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); padding: 12px;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
              <span style="font-size: 12px; font-weight: 600; color: var(--text-muted);">🏆 皮氏 Piotroski 9 维 F-Score</span>
              <span class="pill pill-gold">${fScore.score !== undefined ? fScore.score : 0} / 9 分 (${fScore.level || '正常'})</span>
            </div>
            <div style="font-size: 11px; color: var(--text-muted); line-height: 1.6;">
              ${(fScore.details || []).map(d => `<span class="fin-radar-tag" style="background: rgba(56, 189, 248, 0.1); color: var(--c-cyan);">✓ ${d}</span>`).join("")}
            </div>
          </div>
        </div>

        <!-- 财务排雷六维雷达指标 -->
        <div style="background: rgba(0,0,0,0.25); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); padding: 12px;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
            <span style="font-size: 12px; font-weight: 600; color: var(--text-muted);">🛡️ 六维财务排雷指标与雷达监控</span>
            <span style="font-size: 11px; color: var(--text-dim);">健康得分: <strong class="${redFlags.radar_score >= 80 ? 'text-cyan' : 'text-down'}">${redFlags.radar_score !== undefined ? redFlags.radar_score : 100} 分</strong></span>
          </div>

          <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; font-size: 11px;">
            <div style="background: rgba(255,255,255,0.02); padding: 6px 8px; border-radius: 4px;">
              <span style="color: var(--text-dim);">商誉/净资产:</span> <strong>${inds.goodwill_to_equity_pct !== undefined ? inds.goodwill_to_equity_pct.toFixed(1) + '%' : '0.0%'}</strong>
              <div style="font-size: 10px; color: var(--text-dim);">安全阈值 <25%</div>
            </div>
            <div style="background: rgba(255,255,255,0.02); padding: 6px 8px; border-radius: 4px;">
              <span style="color: var(--text-dim);">应收/总营收:</span> <strong>${inds.receivables_to_revenue_pct !== undefined ? inds.receivables_to_revenue_pct.toFixed(1) + '%' : '-'}</strong>
              <div style="font-size: 10px; color: var(--text-dim);">安全阈值 <60%</div>
            </div>
            <div style="background: rgba(255,255,255,0.02); padding: 6px 8px; border-radius: 4px;">
              <span style="color: var(--text-dim);">存货/总资产:</span> <strong>${inds.inventory_to_assets_pct !== undefined ? inds.inventory_to_assets_pct.toFixed(1) + '%' : '-'}</strong>
              <div style="font-size: 10px; color: var(--text-dim);">安全阈值 <30%</div>
            </div>
            <div style="background: rgba(255,255,255,0.02); padding: 6px 8px; border-radius: 4px;">
              <span style="color: var(--text-dim);">资产负债率:</span> <strong>${inds.debt_to_assets_pct !== undefined ? inds.debt_to_assets_pct.toFixed(1) + '%' : '-'}</strong>
              <div style="font-size: 10px; color: var(--text-dim);">安全阈值 <70%</div>
            </div>
          </div>

          ${redFlags.triggered_risks && redFlags.triggered_risks.length > 0 ? `
            <div style="margin-top: 10px; background: rgba(239, 68, 68, 0.15); border: 1px solid rgba(239, 68, 68, 0.4); border-radius: 4px; padding: 8px 12px;">
              <div style="font-size: 12px; font-weight: 700; color: #fca5a5; margin-bottom: 4px;">🚨 触发财务预警项清单:</div>
              <ul style="margin-left: 18px; font-size: 11px; color: #fecaca; line-height: 1.5;">
                ${redFlags.triggered_risks.map(r => `<li>${r}</li>`).join("")}
              </ul>
            </div>
          ` : `
            <div style="margin-top: 10px; color: #34d399; font-size: 11px; display: flex; align-items: center; gap: 4px;">
              <span>✅</span> 经全维度财务分析与财报核查，该标的资产负债表与现金流极为稳健，无重大排雷风险。
            </div>
          `}
        </div>
      </div>
    `;
  }

  box.innerHTML = `
    <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px;">
      <div style="background: rgba(0,0,0,0.25); padding: 14px; border-radius: var(--radius-sm);">
        <h4 style="font-size: 13px; color: var(--text-dim); margin-bottom: 6px;">基本面与多因子总览</h4>
        <div style="font-size: 18px; font-weight: 700; color: #fff; margin-bottom: 4px;">
          ${data.name} (${data.symbol}) - ${data.price.toFixed(2)}元 (${formatPct(data.change_pct)})
        </div>
        <div style="font-size: 13px; color: var(--c-cyan);">
          ⭐ 综合评分: <strong>${data.score} 分</strong> · ${data.signal_desc}
        </div>
      </div>

      <div style="background: rgba(0,0,0,0.25); padding: 14px; border-radius: var(--radius-sm);">
        <h4 style="font-size: 13px; color: var(--text-dim); margin-bottom: 6px;">CYQ 2.0 筹码控盘特征</h4>
        <div style="font-size: 14px; color: var(--c-purple); font-weight: 600;">
          ${data.control_status}
        </div>
        <div style="font-size: 12px; color: var(--text-muted); margin-top: 4px;">
          主力成本: <strong>${data.main_cost}元</strong> (溢价${data.bias_to_main > 0 ? '+' : ''}${data.bias_to_main}%) | 获利盘: <strong>${data.profit_chip_ratio}%</strong>
        </div>
      </div>

      <div style="background: rgba(0,0,0,0.25); padding: 14px; border-radius: var(--radius-sm);">
        <h4 style="font-size: 13px; color: var(--text-dim); margin-bottom: 6px;">动态 ATR 风控与仓位</h4>
        <div style="font-size: 12px; color: var(--text-muted); line-height: 1.6;">
          建议止损: <span class="text-down">${levels.initial_stop}元 (-${levels.stop_pct}%)</span><br>
          目标止盈: <span class="text-up">${levels.target_price}元 (+${levels.target_pct}%)</span><br>
          建议仓位: <strong class="text-cyan">${advice.position_pct || 0}%</strong> (${advice.suggested_shares || 0}股)
        </div>
      </div>
    </div>

    <!-- 插入深度财报诊断看板 -->
    ${finHtml}

    <div style="margin-top: 14px; font-size: 12px; color: var(--text-muted);">
      <strong>量化研判逻辑:</strong> ${data.reasons.join("； ")}
    </div>
  `;
}

async function loadKlineChart(symbol) {
  const chartDom = document.getElementById("klineChart");
  if (!window.echarts) {
    chartDom.innerHTML = `<p style="padding: 40px; text-align: center; color: var(--text-dim);">ECharts 库加载中或请检查网络连接...</p>`;
    return;
  }

  if (!klineChartInstance) {
    klineChartInstance = echarts.init(chartDom);
    window.addEventListener("resize", () => klineChartInstance.resize());
  }

  klineChartInstance.showLoading({ color: "#38bdf8", textColor: "#fff", maskColor: "rgba(0,0,0,0.5)" });

  try {
    const res = await fetch(`/api/stock/kline?symbol=${symbol}&count=120`);
    if (!res.ok) return;
    const json = await res.json();
    klineChartInstance.hideLoading();

    const klineData = json.kline || [];
    const dates = klineData.map(d => d.date);
    const ohlc = klineData.map(d => [d.open, d.close, d.low, d.high]);
    const ma5 = klineData.map(d => d.ma5);
    const ma20 = klineData.map(d => d.ma20);
    const ma60 = klineData.map(d => d.ma60);
    const volumes = klineData.map(d => d.volume);

    const option = {
      animation: false,
      backgroundColor: "transparent",
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross" },
        backgroundColor: "rgba(16, 24, 43, 0.9)",
        borderColor: "rgba(56, 189, 248, 0.3)",
        textStyle: { color: "#fff", fontSize: 12 }
      },
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      grid: [
        { left: "5%", right: "8%", top: "8%", height: "55%" },
        { left: "5%", right: "8%", top: "68%", height: "22%" }
      ],
      xAxis: [
        { type: "category", data: dates, gridIndex: 0, axisLine: { lineStyle: { color: "#334155" } } },
        { type: "category", data: dates, gridIndex: 1, axisLine: { lineStyle: { color: "#334155" } } }
      ],
      yAxis: [
        { scale: true, gridIndex: 0, axisLine: { lineStyle: { color: "#334155" } }, splitLine: { lineStyle: { color: "rgba(255,255,255,0.04)" } } },
        { scale: true, gridIndex: 1, axisLine: { lineStyle: { color: "#334155" } }, splitLine: { show: false } }
      ],
      series: [
        {
          name: "日K线",
          type: "candlestick",
          data: ohlc,
          itemStyle: {
            color: "#f43f5e",
            color0: "#10b981",
            borderColor: "#f43f5e",
            borderColor0: "#10b981"
          }
        },
        { name: "MA5", type: "line", data: ma5, smooth: true, lineStyle: { color: "#38bdf8", width: 1.5 } },
        { name: "MA20", type: "line", data: ma20, smooth: true, lineStyle: { color: "#fbbf24", width: 1.5 } },
        { name: "MA60", type: "line", data: ma60, smooth: true, lineStyle: { color: "#c084fc", width: 1.5 } },
        {
          name: "成交量",
          type: "bar",
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: volumes,
          itemStyle: { color: "rgba(56, 189, 248, 0.4)" }
        }
      ]
    };

    klineChartInstance.setOption(option, true);
  } catch (e) {
    klineChartInstance.hideLoading();
    console.error("绘制K线图异常:", e);
  }
}

// 6. 组合与单股回测可视化 (对比沪深300基准)
async function triggerBacktest() {
  const sym = document.getElementById("backtestSymbol").value.trim() || "sh601138";
  const days = parseInt(document.getElementById("backtestDays").value) || 600;
  const btn = document.getElementById("btnRunBacktest");

  btn.disabled = true;
  btn.innerText = "⏳ 因果回测计算中...";

  try {
    const res = await fetch(`/api/backtest?symbol=${encodeURIComponent(sym)}&days=${days}`);
    if (!res.ok) {
      const err = await res.json();
      alert("回测失败: " + (err.detail || "未知错误"));
      return;
    }
    const data = await res.json();
    renderBacktestMetrics(data);
    renderBacktestChart(data);
  } catch (e) {
    alert("回测请求异常: " + e);
  } finally {
    btn.disabled = false;
    btn.innerText = "⚡ 运行严格防过拟合因果回测";
  }
}

function renderBacktestMetrics(data) {
  document.getElementById("btTotalReturn").innerHTML = formatPct(data.total_return_pct);
  document.getElementById("btCAGR").innerHTML = `${data.cagr}%`;
  document.getElementById("btBenchmark").innerHTML = formatPct(data.benchmark_return_pct);
  document.getElementById("btAlpha").innerHTML = `<span class="text-cyan">${data.alpha_pct > 0 ? '+' : ''}${data.alpha_pct}%</span>`;
  document.getElementById("btMaxDD").innerHTML = `<span class="text-down">${data.max_drawdown_pct}%</span>`;
  document.getElementById("btSharpe").innerHTML = `<span class="text-gold">${data.sharpe_ratio}</span>`;
  document.getElementById("btWinRate").innerHTML = `${data.win_rate}% (${data.total_trades}笔)`;
  document.getElementById("btPLRatio").innerHTML = `${data.profit_loss_ratio} : 1`;
  document.getElementById("btOOS").innerHTML = formatPct(data.oos_return_pct);
}

function renderBacktestChart(data) {
  const chartDom = document.getElementById("backtestChart");
  if (!window.echarts) return;

  if (!backtestChartInstance) {
    backtestChartInstance = echarts.init(chartDom);
    window.addEventListener("resize", () => backtestChartInstance.resize());
  }

  const curve = data.equity_curve || [];
  const dates = curve.map(c => c.date);
  const pRet = curve.map(c => c.portfolio_return_pct);
  const bRet = curve.map(c => c.benchmark_return_pct);

  const option = {
    backgroundColor: "transparent",
    tooltip: { trigger: "axis", backgroundColor: "rgba(16, 24, 43, 0.9)", textStyle: { color: "#fff" } },
    legend: { data: ["2.0策略组合累计收益", "沪深300基准"], textStyle: { color: "#94a3b8" } },
    grid: { left: "4%", right: "4%", top: "12%", bottom: "8%", containLabel: true },
    xAxis: { type: "category", data: dates, axisLine: { lineStyle: { color: "#334155" } } },
    yAxis: {
      type: "value",
      axisLabel: { formatter: "{value}%" },
      axisLine: { lineStyle: { color: "#334155" } },
      splitLine: { lineStyle: { color: "rgba(255,255,255,0.04)" } }
    },
    series: [
      {
        name: "2.0策略组合累计收益",
        type: "line",
        data: pRet,
        smooth: true,
        lineStyle: { color: "#38bdf8", width: 2.5 },
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: "rgba(56, 189, 248, 0.35)" },
            { offset: 1, color: "rgba(56, 189, 248, 0.0)" }
          ])
        }
      },
      {
        name: "沪深300基准",
        type: "line",
        data: bRet,
        smooth: true,
        lineStyle: { color: "#94a3b8", width: 1.5, type: "dashed" }
      }
    ]
  };

  backtestChartInstance.setOption(option, true);
}

// 7. 收盘复盘与自进化日志
async function triggerReviewNow() {
  const btn = document.getElementById("btnRunReview");
  btn.disabled = true;
  btn.innerText = "⏳ 全景复盘计算中...";
  try {
    const res = await fetch("/api/review/run", { method: "POST" });
    const data = await res.json();
    alert("收盘复盘完成！");
    loadLatestReview();
    loadOptimizationLog();
  } catch (e) {
    alert("复盘异常: " + e);
  } finally {
    btn.disabled = false;
    btn.innerText = "📊 立即执行今日收盘复盘与参数自进化";
  }
}

async function loadLatestReview() {
  try {
    const res = await fetch("/api/review/latest");
    if (!res.ok) return;
    const data = await res.json();
    document.getElementById("reviewReportContent").innerText = data.report_md || "暂无复盘报告";
  } catch (e) {
    console.error("加载复盘报告失败:", e);
  }
}

async function loadOptimizationLog() {
  try {
    const res = await fetch("/api/optimization/log");
    if (!res.ok) return;
    const logs = await res.json();
    const tbody = document.getElementById("optLogTbody");
    if (!logs || logs.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5" style="text-align: center; color: var(--text-dim); padding: 20px;">系统已开启自适应闭环迭代，收盘后自动生成演进流水</td></tr>`;
      return;
    }
    let html = "";
    logs.forEach(l => {
      html += `
        <tr>
          <td>${l.date}</td>
          <td>${l.market_regime}</td>
          <td>${l.win_rate}%</td>
          <td>RSRS买入阈值: ${l.old_rsrs_buy} -> <strong class="text-cyan">${l.new_rsrs_buy}</strong></td>
          <td style="font-size: 11px; color: var(--text-dim);">${(l.reasons || []).join("; ")}</td>
        </tr>
      `;
    });
    tbody.innerHTML = html;
  } catch (e) {
    console.error("获取演进日志异常:", e);
  }
}

// 8. 配置管理
async function loadSettings() {
  try {
    const res = await fetch("/api/config");
    if (!res.ok) return;
    const cfg = await res.json();

    document.getElementById("cfgRsrsBuy").value = cfg.rsrs_buy_threshold ?? 0.7;
    document.getElementById("cfgRsrsSell").value = cfg.rsrs_sell_threshold ?? -0.7;
    document.getElementById("cfgAtrStop").value = cfg.atr_stop_multiplier ?? 2.0;
    document.getElementById("cfgTrailing").value = cfg.trailing_stop_pct ? cfg.trailing_stop_pct * 100 : 6.0;

    document.getElementById("cfgT0Buy").value = cfg.t0_bias_buy ?? -1.5;
    document.getElementById("cfgT0Sell").value = cfg.t0_bias_sell ?? 2.0;
    const filterEl = document.getElementById("cfgT0TrendFilter");
    if (filterEl) filterEl.checked = cfg.t0_trend_filter_enabled !== false;

    document.getElementById("cfgDingEnabled").checked = !!cfg.dingtalk_enabled;
    document.getElementById("cfgDingWebhook").value = cfg.dingtalk_webhook ?? "";
    document.getElementById("cfgDingSecret").value = cfg.dingtalk_secret ?? "";

    document.getElementById("cfgWxEnabled").checked = !!cfg.wechat_enabled;
    document.getElementById("cfgWxProvider").value = cfg.wechat_provider ?? "pushplus";
    document.getElementById("cfgWxToken").value = cfg.wechat_token ?? "";
  } catch (e) {
    console.error("加载配置异常:", e);
  }
}

async function saveSettings() {
  const newCfg = {
    rsrs_buy_threshold: parseFloat(document.getElementById("cfgRsrsBuy").value),
    rsrs_sell_threshold: parseFloat(document.getElementById("cfgRsrsSell").value),
    atr_stop_multiplier: parseFloat(document.getElementById("cfgAtrStop").value),
    trailing_stop_pct: parseFloat(document.getElementById("cfgTrailing").value) / 100.0,

    t0_bias_buy: parseFloat(document.getElementById("cfgT0Buy").value),
    t0_bias_sell: parseFloat(document.getElementById("cfgT0Sell").value),
    t0_trend_filter_enabled: document.getElementById("cfgT0TrendFilter") ? document.getElementById("cfgT0TrendFilter").checked : true,

    dingtalk_enabled: document.getElementById("cfgDingEnabled").checked,
    dingtalk_webhook: document.getElementById("cfgDingWebhook").value.trim(),
    dingtalk_secret: document.getElementById("cfgDingSecret").value.trim(),

    wechat_enabled: document.getElementById("cfgWxEnabled").checked,
    wechat_provider: document.getElementById("cfgWxProvider").value,
    wechat_token: document.getElementById("cfgWxToken").value.trim()
  };

  try {
    const res = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(newCfg)
    });
    if (res.ok) {
      alert("✅ 策略与推送配置已成功保存并热重载！");
    } else {
      alert("保存失败");
    }
  } catch (e) {
    alert("保存异常: " + e);
  }
}

async function testNotification() {
  try {
    const res = await fetch("/api/notify/test", { method: "POST" });
    const data = await res.json();
    if (data.success) {
      alert("🎉 测试推送发送成功！请检查手机微信或钉钉群通知。");
    } else {
      alert("⚠️ 推送未触发，请检查 Webhook 或 Token 是否填写正确并开启开关。");
    }
  } catch (e) {
    alert("测试推送请求异常: " + e);
  }
}

// ==================== 尾盘 2:30 突袭战法专属交互 ====================

let tailCandidatesLoaded = false;
let isTailScanning = false;
let tailPerformanceLoaded = false;

async function loadTail230Data() {
  loadTailStatus();
  loadTailTrades();
  if (!tailPerformanceLoaded) loadTailPerformance();
  if (!tailCandidatesLoaded) loadTailCandidates();
}

async function loadTailStatus() {
  try {
    const res = await fetch("/api/tail230/status");
    if (!res.ok) return;
    const data = await res.json();
    const badge = document.getElementById("tailWindowBadge");
    if (badge) {
      if (data.is_decision_window) {
        badge.className = "badge badge-up";
        badge.style.background = "rgba(244, 63, 94, 0.2)";
        badge.style.color = "#f43f5e";
        badge.textContent = "🔥 正在 14:30~14:57 抢筹黄金窗口！";
      } else {
        badge.className = "badge";
        badge.style.background = "rgba(245, 158, 11, 0.2)";
        badge.style.color = "#fbbf24";
        badge.textContent = `💤 盘中待机 (当前: ${data.current_time}，14:30 启动)`;
      }
    }
  } catch (e) {
    console.error("加载尾盘窗口状态失败:", e);
  }
}

async function loadTailPerformance(force = false) {
  try {
    const res = await fetch(`/api/tail230/performance?force_refresh=${force}`);
    if (!res.ok) return;
    const data = await res.json();
    tailPerformanceLoaded = true;
    renderTail230Performance(data);
  } catch (e) {
    console.error("加载2.30战法胜率失败:", e);
  }
}

function renderTail230Performance(data) {
  if (!data) return;

  const winRateEl = document.getElementById("tailWinRate");
  if (winRateEl) winRateEl.textContent = `${data.win_rate}%`;

  const countEl = document.getElementById("tailTradesCount");
  if (countEl) countEl.textContent = `样本共 ${data.total_trades} 笔 (胜 ${data.win_trades} / 负 ${data.loss_trades}，近${data.backtest_days}天)`;

  const enhEl = document.getElementById("tailEnhancedWinRate");
  if (enhEl) enhEl.textContent = `${data.enhanced_win_rate}%`;

  const plEl = document.getElementById("tailPLRatio");
  if (plEl) plEl.textContent = `${data.profit_loss_ratio}:1`;

  const winLossEl = document.getElementById("tailAvgWinLoss");
  if (winLossEl) winLossEl.textContent = `平均胜 +${data.avg_win_pct}% / 平均负 -${Math.abs(data.avg_loss_pct)}%`;

  const premEl = document.getElementById("tailMorningPrem");
  if (premEl) premEl.textContent = `+${data.avg_morning_premium_pct}%`;

  renderTail230Trades(data.recent_trades || []);
}

function renderTail230Trades(trades) {
  const tbody = document.getElementById("tailTradesTableBody");
  if (!tbody) return;

  if (!trades || trades.length === 0) {
    tbody.innerHTML = `<tr><td colspan="9" style="text-align:center; color: var(--text-dim); padding: 20px;">暂无历史交易样本</td></tr>`;
    return;
  }

  tbody.innerHTML = trades.map(t => {
    const isWin = t.is_win;
    const retCls = t.return_pct > 0 ? "text-up" : "text-down";
    const retSign = t.return_pct > 0 ? "+" : "";
    const badgeCls = isWin ? "badge-up" : "badge-down";
    const statusText = isWin ? "✅ 止盈获利" : "❌ 止损离场";
    const regimeCls = t.market_regime?.includes("多头") ? "text-cyan" : "text-dim";

    return `
      <tr>
        <td><strong>${t.name}</strong> <span style="font-size:11px; color:var(--text-dim);">(${t.symbol})</span></td>
        <td>${t.buy_date}</td>
        <td>${t.buy_price.toFixed(2)} 元</td>
        <td class="text-up">+${t.morning_high_pct.toFixed(2)}%</td>
        <td>${t.sell_price.toFixed(2)} 元</td>
        <td class="${retCls}"><strong>${retSign}${t.return_pct.toFixed(2)}%</strong></td>
        <td><span class="${regimeCls}">${t.market_regime || "平衡市"}</span></td>
        <td style="font-size:12px; color:var(--text-muted);">${t.reason}</td>
        <td><span class="badge ${badgeCls}">${statusText}</span></td>
      </tr>
    `;
  }).join("");
}

async function loadTailCandidates(force = false) {
  const container = document.getElementById("tailCandidatesContainer");
  if (!container) return;

  if (isTailScanning) return; // 正在扫描防抖

  if (!force && tailCandidatesLoaded && container.children.length > 0) {
    return; // 已有内容且非强制刷新，直接复用，杜绝重复刷新闪烁
  }

  // 仅在首次加载或强制刷新时显示占位符
  if (container.children.length === 0 || force) {
    container.innerHTML = `<div style="grid-column: 1/-1; text-align: center; padding: 30px; color: var(--text-dim);">🔍 正在全市场主板扫描 2.30 战法标的...</div>`;
  }

  isTailScanning = true;
  try {
    const res = await fetch(`/api/tail230/candidates?force_refresh=${force}`);
    if (!res.ok) return;
    const list = await res.json();
    tailCandidatesLoaded = true;

    if (!list || list.length === 0) {
      container.innerHTML = `
        <div style="grid-column: 1/-1; text-align: center; padding: 40px; color: var(--text-dim); background: var(--bg-card); border-radius: var(--radius-md); border: 1px dashed var(--border-subtle);">
          <div style="font-size: 28px; margin-bottom: 8px;">🛡️</div>
          <div style="font-weight: 600; color: var(--text-muted); margin-bottom: 4px;">今日暂无符合严格 2.30 战法的主板标的</div>
          <div style="font-size: 12px;">模型严格要求涨幅在 +2.0%~+5.5%、量比≥1.25且突破均线，弱市中严禁强行接盘。</div>
        </div>
      `;
      return;
    }

    container.innerHTML = list.map(c => {
      const reasonsHtml = (c.reasons || []).map(r => `<li>${r}</li>`).join("");
      return `
        <div class="stock-card" style="border-top: 3px solid var(--c-gold);">
          <div class="stock-header">
            <div>
              <div class="stock-name">${c.name}</div>
              <div class="stock-symbol">${c.symbol}</div>
            </div>
            <div style="text-align: right;">
              <div class="stock-price">${c.price.toFixed(2)} 元</div>
              <div class="stock-change text-up">+${c.change_pct.toFixed(2)}%</div>
            </div>
          </div>

          <div class="stock-meta-row" style="margin-top: 12px;">
            <div class="meta-item">
              <span class="meta-label">2.30战法评分</span>
              <span class="meta-val" style="color: var(--c-gold); font-size: 16px;">${c.score} 分</span>
            </div>
            <div class="meta-item">
              <span class="meta-label">放量量比</span>
              <span class="meta-val text-cyan">${c.vol_ratio} 倍</span>
            </div>
            <div class="meta-item">
              <span class="meta-label">次日冲高止盈</span>
              <span class="meta-val text-up">${c.target_sell_price} 元 (+${c.target_pct}%)</span>
            </div>
            <div class="meta-item">
              <span class="meta-label">防守止损价</span>
              <span class="meta-val text-down">${c.stop_loss_price} 元 (-${c.stop_pct}%)</span>
            </div>
          </div>

          <div style="margin-top: 12px; font-size: 12px; color: var(--text-muted);">
            <ul style="padding-left: 16px; margin: 0; line-height: 1.5;">
              ${reasonsHtml}
            </ul>
          </div>

          <div style="margin-top: 14px; display: flex; justify-content: flex-end; gap: 8px; flex-wrap: wrap;">
            <button class="btn btn-secondary btn-sm" onclick="trackTailCandidate('${c.symbol}', '${c.name}', ${c.price})">
              🎯 纳入次日卖出监控
            </button>
            <button class="btn btn-secondary btn-sm" onclick="quickAddWatchlist('${c.symbol}', '${c.name}', ${c.price})">
              + 加入自选
            </button>
            <button class="btn btn-primary btn-sm" onclick="showDiagnoseTab('${c.symbol}')">
              🔍 深度K线诊断
            </button>
          </div>
        </div>
      `;
    }).join("");
  } catch (e) {
    console.error("加载2.30候选标的失败:", e);
  } finally {
    isTailScanning = false;
  }
}

async function recalculateTail230() {
  const days = document.getElementById("tailBacktestDays")?.value || 120;
  const winRateEl = document.getElementById("tailWinRate");
  if (winRateEl) winRateEl.textContent = "测算中...";

  try {
    const res = await fetch(`/api/tail230/recalculate?days=${days}`, { method: "POST" });
    if (!res.ok) throw new Error("测算失败");
    const json = await res.json();
    if (json.data) {
      renderTail230Performance(json.data);
      alert(`🎉 2.30 战法历史因果胜率测算完成！近 ${days} 天全周期胜率: ${json.data.win_rate}% (多头增强胜率: ${json.data.enhanced_win_rate}%)`);
    }
  } catch (e) {
    alert("重新测算失败: " + e);
  }
}

async function loadTailTrades() {
  try {
    const res = await fetch("/api/tail230/trades");
    if (!res.ok) return;
    const trades = await res.json();
    renderTailHoldings(trades);
  } catch (e) {
    console.error("加载 2.30 战法次日卖出监控失败:", e);
  }
}

function renderTailHoldings(trades) {
  const tbody = document.getElementById("tailHoldingsTableBody");
  if (!tbody) return;

  if (!trades || trades.length === 0) {
    tbody.innerHTML = `<tr><td colspan="9" style="text-align:center; color: var(--text-dim); padding: 25px;">暂无 2.30 战法持仓监控记录（每日 14:32 推荐标的将自动纳入次日冲高卖出监控池）</td></tr>`;
    return;
  }

  tbody.innerHTML = trades.map(t => {
    let statusBadge = `<span class="badge" style="background: rgba(99, 102, 241, 0.2); color: #818cf8;">监控中 (次日早盘)</span>`;
    if (t.status === "CLOSED_PROFIT") {
      statusBadge = `<span class="badge badge-up">🎯 已冲高止盈</span>`;
    } else if (t.status === "CLOSED_STOP") {
      statusBadge = `<span class="badge badge-down">🛑 已破位止损</span>`;
    } else if (t.status === "ALERTED_TIMED") {
      statusBadge = `<span class="badge" style="background: rgba(245, 158, 11, 0.2); color: #fbbf24;">⏳ 动能衰竭离场</span>`;
    } else if (t.status === "CLOSED_EOD") {
      statusBadge = `<span class="badge" style="background: rgba(244, 63, 94, 0.2); color: #f43f5e;">🚨 次日尾盘清仓</span>`;
    }

    const retPct = (t.return_pct != null) ? `${t.return_pct > 0 ? '+' : ''}${t.return_pct}%` : "--";
    const retClass = (t.return_pct != null) ? (t.return_pct > 0 ? "text-up" : (t.return_pct < 0 ? "text-down" : "")) : "";
    const exitPriceStr = t.exit_price ? `${t.exit_price.toFixed(2)}元 (${retPct})` : `等待次日开盘兑现`;

    let actionBtn = "";
    if (t.status === "HOLDING" || t.status === "ALERTED_TIMED") {
      actionBtn = `<button class="btn btn-secondary btn-sm" onclick="manualCloseTailTrade('${t.symbol}', '${t.buy_date}', ${t.target_sell_price})" style="padding: 3px 8px; font-size: 11px;">手动平仓</button>`;
    } else {
      actionBtn = `<span style="font-size: 11px; color: var(--text-dim);">已完结</span>`;
    }

    return `
      <tr>
        <td>
          <strong>${t.name}</strong><br>
          <span style="font-size: 11px; color: var(--text-muted);">${t.symbol}</span>
        </td>
        <td>${t.buy_date} ${t.buy_time || '14:32'}</td>
        <td><strong>${t.buy_price.toFixed(2)}</strong> 元</td>
        <td class="text-up"><strong>${t.target_sell_price.toFixed(2)}</strong> 元 (+${t.target_pct}%)</td>
        <td class="text-down"><strong>${t.stop_loss_price.toFixed(2)}</strong> 元 (-${t.stop_pct}%)</td>
        <td>${statusBadge}</td>
        <td class="${retClass}"><strong>${exitPriceStr}</strong></td>
        <td style="font-size: 12px; max-width: 260px;">${t.exit_reason || '次日 09:30~09:45 冲高止盈盯盘中'}</td>
        <td>${actionBtn}</td>
      </tr>
    `;
  }).join("");
}

async function trackTailCandidate(sym, name, price) {
  try {
    const res = await fetch("/api/tail230/trades/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol: sym, name: name, buy_price: price })
    });
    if (res.ok) {
      alert(`标的 ${name} (${sym}) 已成功加入次日卖出监控池！明日早盘将实时推送卖出信号。`);
      loadTailTrades();
    }
  } catch (e) {
    alert("加入失败: " + e);
  }
}

async function manualCloseTailTrade(sym, buy_date, defaultPrice) {
  const pStr = prompt(`请输入 ${sym} 的平仓成交价格：`, defaultPrice);
  if (!pStr) return;
  const price = parseFloat(pStr);
  if (isNaN(price) || price <= 0) return alert("输入价格无效");

  try {
    const res = await fetch("/api/tail230/trades/close", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol: sym, buy_date: buy_date, exit_price: price, exit_reason: "用户手动平仓确认" })
    });
    if (res.ok) {
      alert("已记录平仓离场！");
      loadTailTrades();
    }
  } catch (e) {
    alert("平仓操作失败: " + e);
  }
}

// ==================== 游资龙虎打板模块前端交互 ====================

let isLuRadarLoading = false;
let isLuLedgerLoading = false;

async function loadLimitUpData() {
  await Promise.all([
    loadLimitUpStats(),
    loadLimitUpRadar(),
    loadLimitUpLedger()
  ]);
}

function refreshLimitUpData() {
  loadLimitUpData();
}

// 1. 加载打板量化统计指标
async function loadLimitUpStats() {
  try {
    const res = await fetch("/api/limitup/stats");
    if (!res.ok) return;
    const stats = await res.json();

    document.getElementById("luTotalCount").textContent = `${stats.total_trades || 0} 笔`;
    document.getElementById("luSettledSub").textContent = `已结算 ${stats.settled_trades || stats.closed_trades || 0} 笔 · 待决 ${stats.pending_trades || 0} 笔`;
    document.getElementById("luOpenWinRate").textContent = `${stats.open_premium_win_rate || 0}%`;
    document.getElementById("luPeakWinRate").textContent = `${stats.peak_profit_win_rate || 0}%`;
    document.getElementById("luConsecutiveRate").textContent = `${stats.consecutive_rate || 0}%`;
    document.getElementById("luPlRatio").textContent = `${stats.profit_loss_ratio || 1.0}:1`;
    document.getElementById("luBigLossSub").textContent = `大面率 (跌幅≥4%): ${stats.big_loss_rate || 0}%`;

    document.getElementById("luAvgOpenPnl").textContent = `${stats.avg_open_pnl > 0 ? '+' : ''}${stats.avg_open_pnl || 0}%`;
    document.getElementById("luAvgPeakPnl").textContent = `${stats.avg_peak_pnl > 0 ? '+' : ''}${stats.avg_peak_pnl || 0}%`;
    document.getElementById("luAvgClosePnl").innerHTML = formatPct(stats.avg_close_pnl || 0);

    // 板型分布横幅渲染
    const typeObj = stats.by_board_type || stats.board_types || {};
    const typeItems = [];
    for (const [k, v] of Object.entries(typeObj)) {
      typeItems.push(`<span><strong style="color: #fff;">${k}</strong> (${v.count}笔 · 冲高胜率 <span class="text-up">${v.win_rate}%</span> · 均益 ${v.avg_return > 0 ? '+' : ''}${v.avg_return}%)</span>`);
    }
    const typeEl = document.getElementById("luTypeBreakdown");
    if (typeEl) {
      typeEl.innerHTML = typeItems.length > 0 ? typeItems.join(" &nbsp;|&nbsp; ") : "样本积累中...";
    }
  } catch (e) {
    console.error("加载打板统计异常:", e);
  }
}

// 2. 加载今日主板涨停板雷达
async function loadLimitUpRadar() {
  if (isLuRadarLoading) return;
  isLuRadarLoading = true;
  const container = document.getElementById("limitupRadarContainer");
  if (!container) return;

  container.innerHTML = `
    <div style="grid-column: 1/-1; text-align: center; padding: 30px; color: var(--text-dim);">
      <div style="font-size: 24px; margin-bottom: 8px;">⚡</div>
      <div>正在全市场扫描主板涨停板与封板硬度数据...</div>
    </div>
  `;

  try {
    const res = await fetch("/api/limitup/radar?limit=20");
    if (!res.ok) throw new Error("获取涨停雷达失败");
    const list = await res.json();

    if (!list || list.length === 0) {
      container.innerHTML = `
        <div style="grid-column: 1/-1; text-align: center; padding: 40px; color: var(--text-dim); background: var(--bg-card); border-radius: var(--radius-md); border: 1px dashed var(--border-subtle);">
          <div style="font-size: 28px; margin-bottom: 8px;">🛡️</div>
          <div style="font-weight: 600; color: var(--text-muted); margin-bottom: 4px;">今日暂无符合严格涨停封死标准的主板标的</div>
          <div style="font-size: 12px;">模型严格要求涨幅≥9.8%且触达涨停线，已自动过滤科创板/创业板/ST股。</div>
        </div>
      `;
      return;
    }

    container.innerHTML = list.map(c => {
      const reasonsHtml = (c.reasons || []).map(r => `<li>${r}</li>`).join("");
      const hardnessColor = c.hardness_score >= 85 ? "var(--c-red)" : c.hardness_score >= 75 ? "var(--c-gold)" : "var(--c-cyan)";

      return `
        <div class="stock-card" style="border-top: 3px solid ${hardnessColor};">
          <div class="stock-header">
            <div>
              <div class="stock-name" style="display: flex; align-items: center; gap: 6px;">
                <span>${c.name}</span>
                <span class="badge" style="background: rgba(239, 68, 68, 0.2); color: #f87171; font-size: 10px;">${c.board_type}</span>
              </div>
              <div class="stock-symbol">${c.symbol} · 连板数: ${c.ladder_count}</div>
            </div>
            <div style="text-align: right;">
              <div class="stock-price">${c.price.toFixed(2)} 元</div>
              <div class="stock-change text-up">涨停封板</div>
            </div>
          </div>

          <div class="stock-meta-row" style="margin-top: 12px;">
            <div class="meta-item">
              <span class="meta-label">封板硬度得分</span>
              <span class="meta-val" style="color: ${hardnessColor}; font-size: 16px;">${c.hardness_score} 分</span>
            </div>
            <div class="meta-item">
              <span class="meta-label">封成比</span>
              <span class="meta-val text-gold">${c.seal_turnover_ratio}%</span>
            </div>
            <div class="meta-item">
              <span class="meta-label">封板时间</span>
              <span class="meta-val text-cyan">${c.seal_time || '早盘'}</span>
            </div>
            <div class="meta-item">
              <span class="meta-label">成交额</span>
              <span class="meta-val">${(c.turnover_amount / 100000000.0).toFixed(2)} 亿元</span>
            </div>
          </div>

          <div style="margin-top: 12px; font-size: 12px; color: var(--text-muted);">
            <ul style="padding-left: 16px; margin: 0; line-height: 1.5;">
              ${reasonsHtml}
            </ul>
          </div>

          <div style="margin-top: 14px; display: flex; justify-content: flex-end; gap: 8px; flex-wrap: wrap;">
            <button class="btn btn-secondary btn-sm" onclick="quickAddWatchlist('${c.symbol}', '${c.name}', ${c.price})">
              + 加入自选
            </button>
            <button class="btn btn-primary btn-sm" onclick="showDiagnoseTab('${c.symbol}')">
              🔍 深度K线诊断
            </button>
          </div>
        </div>
      `;
    }).join("");
  } catch (e) {
    console.error("加载打板雷达失败:", e);
  } finally {
    isLuRadarLoading = false;
  }
}

// 3. 加载打板实战流水账本表格
async function loadLimitUpLedger() {
  if (isLuLedgerLoading) return;
  isLuLedgerLoading = true;
  const tbody = document.getElementById("limitupLedgerTableBody");
  if (!tbody) return;

  try {
    const res = await fetch("/api/limitup/ledger?limit=100");
    if (!res.ok) throw new Error("获取打板账本失败");
    const trades = await res.json();

    if (!trades || trades.length === 0) {
      tbody.innerHTML = `<tr><td colspan="14" style="text-align:center; color: var(--text-dim); padding: 25px;">暂无打板流水记录（每日收盘全自动维护录入）</td></tr>`;
      return;
    }

    tbody.innerHTML = trades.map(t => {
      let statusBadge = `<span class="badge" style="background: rgba(99, 102, 241, 0.2); color: #818cf8;">⏳ 持仓待结算</span>`;
      if (t.status === "WIN") {
        statusBadge = `<span class="badge badge-up">🎉 胜·溢价兑现</span>`;
      } else if (t.status === "LOSS") {
        statusBadge = `<span class="badge badge-down">🛑 负·破位止损</span>`;
      } else if (t.status === "FLAT") {
        statusBadge = `<span class="badge" style="background: rgba(148, 163, 184, 0.2); color: #94a3b8;">⚖️ 平·微利对冲</span>`;
      }

      const openPnlStr = t.next_open_pnl != null ? `${t.next_open_pnl > 0 ? '+' : ''}${t.next_open_pnl}%` : "--";
      const openPnlCls = t.next_open_pnl != null ? (t.next_open_pnl > 0 ? "text-up" : (t.next_open_pnl < 0 ? "text-down" : "")) : "";

      const peakPnlStr = t.next_high_pnl != null ? `+${t.next_high_pnl}%` : "--";
      const peakPnlCls = t.next_high_pnl != null ? (t.next_high_pnl >= 2.0 ? "text-up" : "") : "";

      const closePnlStr = t.next_close_pnl != null ? `${t.next_close_pnl > 0 ? '+' : ''}${t.next_close_pnl}%` : "--";
      const closePnlCls = t.next_close_pnl != null ? (t.next_close_pnl > 0 ? "text-up" : (t.next_close_pnl < 0 ? "text-down" : "")) : "";

      const consecutiveStr = t.is_consecutive ? `<span class="text-up" style="font-weight:700;">🚀 连板成功</span>` : `<span style="color:var(--text-dim);">否</span>`;
      const reasonsStr = (t.reasons || []).slice(0, 2).join("；") || "-";

      return `
        <tr>
          <td>${t.date}</td>
          <td>
            <strong>${t.name}</strong><br>
            <span style="font-size: 11px; color: var(--text-muted);">${t.symbol}</span>
          </td>
          <td><span class="badge" style="background: rgba(239, 68, 68, 0.15); color: #f87171;">${t.board_type || '首板'}</span></td>
          <td><strong>${t.ladder_count || 1}</strong> 板</td>
          <td><strong>${Number(t.seal_price || 0).toFixed(2)}</strong> 元</td>
          <td style="font-size: 11px; color: var(--text-dim);">${t.seal_time || '-'}</td>
          <td><strong style="color: var(--c-gold);">${t.hardness_score || 0}</strong> 分</td>
          <td>${t.seal_turnover_ratio || 0}%</td>
          <td class="${openPnlCls}"><strong>${openPnlStr}</strong></td>
          <td class="${peakPnlCls}"><strong>${peakPnlStr}</strong></td>
          <td class="${closePnlCls}"><strong>${closePnlStr}</strong></td>
          <td>${consecutiveStr}</td>
          <td>${statusBadge}</td>
          <td style="font-size: 12px; max-width: 280px; color: var(--text-muted);">${reasonsStr}</td>
        </tr>
      `;
    }).join("");
  } catch (e) {
    console.error("加载打板流水表格失败:", e);
  } finally {
    isLuLedgerLoading = false;
  }
}

// 4. 一键录入前3只精选打板标的至账本
async function autoRecordTopLimitUp() {
  try {
    const res = await fetch("/api/limitup/record", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ top_n: 3 })
    });
    if (!res.ok) throw new Error("录入失败");
    const json = await res.json();
    alert(`🎉 已成功将 ${json.added_count} 只今日精选涨停标的录入流水账本！表格已同步刷新。`);
    await loadLimitUpData();
  } catch (e) {
    alert("一键录入异常: " + e);
  }
}

// 5. 立即执行 T+1 真实复盘结算
async function triggerManualSettlement() {
  try {
    const res = await fetch("/api/limitup/refresh", { method: "POST" });
    if (!res.ok) throw new Error("结算失败");
    const json = await res.json();
    alert(`✅ T+1 真实行情结算完成！已更新台账数据，CSV 表格已自动同步重构。`);
    await loadLimitUpData();
  } catch (e) {
    alert("结算失败: " + e);
  }
}

// 6. 导出 / 下载 CSV 电子表格
function exportLimitUpCsv() {
  window.open("/api/limitup/csv", "_blank");
}


