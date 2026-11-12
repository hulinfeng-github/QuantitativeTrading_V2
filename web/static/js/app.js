/**
 * A股智能量化系统 2.0 (QuantitativeTrading 2.0) 前端交互引擎
 * 支持：多因子选股雷达、实时做T监控、专业ECharts金融图表(K线/分时VWAP/筹码分布/组合净值对比)
 */

let klineChartInstance = null;
let t0ChartInstance = null;
let backtestChartInstance = null;
let lastDiagnosedSymbol = "";

// 做T标的分组与多钉钉群分流推送状态
let cachedGroups = [];
let selectedWatchlistGroupFilter = "ALL";
let selectedT0GroupFilter = "ALL";
let cachedWatchlistRaw = [];
let cachedT0Raw = [];

// 初始化入口
document.addEventListener("DOMContentLoaded", () => {
  initTheme();
  initTabs();
  loadStatusAndSentiment();
  loadScreenerResults();
  loadGroups();
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

// 主题皮肤管理系统 (深空极光 / 华尔街黑金 / 赛博霓虹 / 翡翠墨绿 / 机构白昼)
function initTheme() {
  const savedTheme = localStorage.getItem("qt_theme") || "aurora";
  applyTheme(savedTheme);
}

function changeTheme(themeName) {
  applyTheme(themeName);
  localStorage.setItem("qt_theme", themeName);
}

function applyTheme(themeName) {
  document.documentElement.setAttribute("data-theme", themeName);
  document.body.setAttribute("data-theme", themeName);

  // 同步所有皮肤选择下拉菜单
  const selectors = document.querySelectorAll("#themeSelector, #settingsThemeSelector");
  selectors.forEach(sel => { if (sel) sel.value = themeName; });

  // 同步设置面板的高亮状态
  document.querySelectorAll(".theme-card-option").forEach(card => {
    if (card.getAttribute("onclick") && card.getAttribute("onclick").includes(`'${themeName}'`)) {
      card.classList.add("active-theme");
    } else {
      card.classList.remove("active-theme");
    }
  });

  // 如果已有K线图，重新载入以适配主题坐标轴与背景色
  if (klineChartInstance && lastDiagnosedSymbol) {
    loadKlineChart(lastDiagnosedSymbol);
  } else if (klineChartInstance) {
    klineChartInstance.resize();
  }
}
window.changeTheme = changeTheme;
window.applyTheme = applyTheme;

// 显式标签切换控制函数
function switchTab(targetTab) {
  if (!targetTab) return;
  const tabBtns = document.querySelectorAll(".tab-btn");
  tabBtns.forEach(btn => {
    if (btn.dataset.tab === targetTab) {
      btn.classList.add("active");
    } else {
      btn.classList.remove("active");
    }
  });

  document.querySelectorAll(".tab-pane").forEach(pane => {
    pane.style.display = "none";
  });

  const activePane = document.getElementById(`pane-${targetTab}`);
  if (activePane) {
    activePane.style.display = "block";
  }

  // 切换到对应面板时加载数据
  if (targetTab === "tail230") loadTail230Data();
  if (targetTab === "limitup") loadLimitUpData();
  if (targetTab === "t0") loadT0Monitor();
  if (targetTab === "watchlist") loadWatchlist();

  // 切换到带图表的面板时自适应尺寸
  if (targetTab === "diagnose" && klineChartInstance) {
    setTimeout(() => { if (klineChartInstance) klineChartInstance.resize(); }, 60);
  }
  if (targetTab === "backtest" && backtestChartInstance) {
    setTimeout(() => { if (backtestChartInstance) backtestChartInstance.resize(); }, 60);
  }
}
window.switchTab = switchTab;

// 标签导航初始化
function initTabs() {
  const tabBtns = document.querySelectorAll(".tab-btn");
  tabBtns.forEach(btn => {
    btn.addEventListener("click", () => {
      switchTab(btn.dataset.tab);
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
    const isSafe = redFlags.is_safe === true && fin.data_quality?.can_assess_risk === true;
    const radarScore = redFlags.radar_score;
    const fScore = fin.piotroski_f_score || {};
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
            <span class="${isSafe ? 'pill pill-safe' : 'pill pill-danger'}">${redFlags.risk_level || '未知'} (排雷: ${radarScore != null ? radarScore + '分' : '评分不可用'})</span>
          </div>
          <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 4px; text-align: center;">
            <div>
              <div style="font-size: 10px; color: var(--text-dim);">杜邦 ROE</div>
              <div style="font-weight: 700; color: #fff;">${dupont.roe != null ? dupont.roe.toFixed(1) + '%' : '未知'}</div>
            </div>
            <div>
              <div style="font-size: 10px; color: var(--text-dim);">扣非净利增速</div>
              <div style="font-weight: 700;" class="${growth.deduct_profit_yoy == null ? 'text-dim' : (growth.deduct_profit_yoy >= 0 ? 'text-up' : 'text-down')}">${growth.deduct_profit_yoy != null ? (growth.deduct_profit_yoy > 0 ? '+' : '') + growth.deduct_profit_yoy.toFixed(1) + '%' : '未知'}</div>
            </div>
            <div>
              <div style="font-size: 10px; color: var(--text-dim);">皮氏 F-Score</div>
              <div style="font-weight: 700; color: var(--c-gold);">${fScore.complete_score_available && fScore.score != null ? fScore.score + '/9分' : '不可用'}</div>
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
    cachedWatchlistRaw = items || [];
    renderGroupFilterBars();
    renderWatchlistTable(cachedWatchlistRaw);
  } catch (e) {
    console.error("加载自选股失败:", e);
  }
}

function renderWatchlistTable(items) {
  const tbody = document.getElementById("watchlistTbody");
  if (!tbody) return;

  if (!items || items.length === 0) {
    tbody.innerHTML = `<tr><td colspan="10" style="text-align: center; color: var(--text-dim); padding: 30px;">暂无自选监控标的，点击右上角「+ 添加自选股」录入</td></tr>`;
    return;
  }

  let filtered = items;
  if (selectedWatchlistGroupFilter !== "ALL") {
    if (selectedWatchlistGroupFilter === "UNASSIGNED") {
      filtered = items.filter(it => {
        const sym = (it.symbol || "").toLowerCase();
        const inAny = cachedGroups && cachedGroups.some(g => {
          const sSet = (g.stock_symbols || []).map(s => (s||"").toLowerCase());
          const sList = (g.stocks || []).map(s => (s.symbol||"").toLowerCase());
          return sSet.includes(sym) || sList.includes(sym) || (it.group_ids && it.group_ids.includes(g.id)) || it.group_id === g.id;
        });
        return !inAny;
      });
    } else {
      const targetGroup = cachedGroups ? cachedGroups.find(g => g.id === selectedWatchlistGroupFilter) : null;
      const targetSymbols = new Set([
        ...((targetGroup && targetGroup.stock_symbols) || []),
        ...(((targetGroup && targetGroup.stocks) || []).map(s => s.symbol))
      ].map(s => (s || "").toLowerCase()));

      filtered = items.filter(it => {
        const sym = (it.symbol || "").toLowerCase();
        const inGids = it.group_ids && it.group_ids.includes(selectedWatchlistGroupFilter);
        const inGid = it.group_id === selectedWatchlistGroupFilter;
        return inGids || inGid || targetSymbols.has(sym);
      });
    }
  }

  if (filtered.length === 0) {
    tbody.innerHTML = `<tr><td colspan="9" style="text-align: center; color: var(--text-dim); padding: 30px;">当前所选做T分组暂无标的</td></tr>`;
    return;
  }

  function formatStockPrice(p) {
    if (p === null || p === undefined || isNaN(p) || p <= 0) return "-";
    const num = parseFloat(p);
    return num < 10 || (num % 1 !== 0 && num.toString().split(".")[1]?.length >= 3) ? num.toFixed(3) : num.toFixed(2);
  }

  let html = "";
  filtered.forEach(it => {
    const symLower = (it.symbol || "").toLowerCase();
    const assignedGroups = cachedGroups ? cachedGroups.filter(g => {
      const gStockSymbols = (g.stock_symbols || []).map(s => (s || "").toLowerCase());
      const gStocks = (g.stocks || []).map(s => (s.symbol || "").toLowerCase());
      const inGids = it.group_ids && it.group_ids.includes(g.id);
      const inGid = it.group_id === g.id;
      return gStockSymbols.includes(symLower) || gStocks.includes(symLower) || inGids || inGid;
    }) : [];

    let groupBadgesHtml = "";
    if (assignedGroups.length > 0) {
      groupBadgesHtml = assignedGroups.map(g => `<span class="pill pill-group" onclick="showGroupModal('${g.id}')" title="点击查看【${g.name}】">${g.name}</span>`).join(" ");
    } else {
      groupBadgesHtml = `<span class="pill pill-group pill-group-default" onclick="showGroupModal()" title="点击加入做T分组">未分组</span>`;
    }

    const groupDisplay = `
      <div style="display: flex; align-items: center; gap: 4px; flex-wrap: wrap;">
        ${groupBadgesHtml}
        <button class="btn btn-secondary btn-sm" onclick="showGroupModal()" title="管理分组" style="padding: 1px 5px; font-size: 10px; border-radius: 4px; opacity: 0.7;">⚙️</button>
      </div>
    `;

    html += `
      <tr>
        <td><strong style="color: #fff; cursor: pointer;" onclick="openStockDiagnose('${it.symbol}')">${it.name}</strong> <span style="font-size: 11px; color: var(--text-dim);">${it.symbol}</span></td>
        <td>${groupDisplay}</td>
        <td>${formatStockPrice(it.current_price)}</td>
        <td>${formatPct(it.change_pct)}</td>
        <td style="cursor: pointer;" onclick="openEditWatchlistModal('${it.symbol}')" title="点击快速修改持仓成本与股数">${it.holding_cost > 0 ? formatStockPrice(it.holding_cost) : '<span style="color: var(--text-dim); font-size: 12px;">未设 ✏️</span>'}</td>
        <td style="cursor: pointer;" onclick="openEditWatchlistModal('${it.symbol}')" title="点击快速修改持仓成本与股数">${it.holding_shares > 0 ? it.holding_shares : '<span style="color: var(--text-dim); font-size: 12px;">0 ✏️</span>'}</td>
        <td style="cursor: pointer;" onclick="openEditWatchlistModal('${it.symbol}')" title="按券商账户确认当日可卖数量">${it.available_shares > 0 ? it.available_shares : '<span style="color: var(--text-dim); font-size: 12px;">0 ✏️</span>'}</td>
        <td>${it.holding_cost > 0 ? formatPct(it.pnl_pct) : '-'}</td>
        <td>${it.holding_cost > 0 ? `<span class="${it.pnl_val >= 0 ? 'text-up' : 'text-down'}">${it.pnl_val > 0 ? '+' : ''}${it.pnl_val.toFixed(2)}元</span>` : '-'}</td>
        <td>
          <button class="btn btn-secondary btn-sm" onclick="openStockDiagnose('${it.symbol}')">诊断</button>
          <button class="btn btn-primary btn-sm" onclick="openEditWatchlistModal('${it.symbol}')" title="修改持仓成本与持股数">✏️ 修改</button>
          <button class="btn btn-secondary btn-sm" style="color: #ef4444;" onclick="deleteWatchlist('${it.symbol}')">删除</button>
        </td>
      </tr>
    `;
  });
  tbody.innerHTML = html;
}

async function deleteWatchlist(symbol) {
  if (!confirm(`确定将 ${symbol} 移出自选监控池吗？`)) return;
  try {
    const res = await fetch(`/api/watchlist/${symbol}`, { method: "DELETE" });
    if (res.ok) {
      await loadGroups();
      await loadWatchlist();
      await loadT0Monitor();
    }
  } catch (e) {
    alert("删除失败: " + e);
  }
}

function showAddWatchlistModal() {
  updateGroupSelectDropdowns();
  document.getElementById("addWatchModal").style.display = "flex";
}

function hideAddWatchlistModal() {
  document.getElementById("addWatchModal").style.display = "none";
}

async function submitAddWatchlist() {
  const code = document.getElementById("addCode").value.trim();
  const cost = parseFloat(document.getElementById("addCost").value) || 0.0;
  const shares = parseInt(document.getElementById("addShares").value) || 0;
  const availableShares = parseInt(document.getElementById("addAvailableShares").value) || 0;
  const groupId = document.getElementById("addStockGroup") ? document.getElementById("addStockGroup").value : "";
  const notes = document.getElementById("addNotes").value.trim();

  if (!code) { alert("请输入股票代码或名称"); return; }

  try {
    const res = await fetch("/api/watchlist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        symbol: code,
        holding_cost: cost,
        holding_shares: shares,
        available_shares: availableShares,
        group_id: groupId,
        notes: notes
      })
    });
    const data = await res.json();
    if (res.ok) {
      hideAddWatchlistModal();
      document.getElementById("addCode").value = "";
      document.getElementById("addCost").value = "0";
      document.getElementById("addShares").value = "0";
      document.getElementById("addAvailableShares").value = "0";
      document.getElementById("addNotes").value = "";
      await loadGroups();
      await loadWatchlist();
      await loadT0Monitor();
    } else {
      alert("添加失败: " + (data.detail || "未知错误"));
    }
  } catch (e) {
    alert("提交异常: " + e);
  }
}

function openEditWatchlistModal(symbol) {
  const item = (cachedWatchlistRaw || []).find(x => (x.symbol || "").toLowerCase() === (symbol || "").toLowerCase());
  if (!item) return;

  document.getElementById("editWatchSymbol").value = item.symbol;
  document.getElementById("editWatchTitle").innerHTML = `✏️ 修改持仓 - <strong>${item.name}</strong> <span style="font-size: 13px; color: var(--text-dim);">(${item.symbol})</span>`;
  document.getElementById("editWatchCost").value = (item.holding_cost && item.holding_cost > 0) ? item.holding_cost : "";
  document.getElementById("editWatchShares").value = (item.holding_shares && item.holding_shares > 0) ? item.holding_shares : "";
  document.getElementById("editAvailableShares").value = (item.available_shares && item.available_shares > 0) ? item.available_shares : "0";
  document.getElementById("editWatchNotes").value = item.notes || "";

  updateGroupSelectDropdowns(item.group_id);
  document.getElementById("editWatchModal").style.display = "flex";
}

function hideEditWatchlistModal() {
  document.getElementById("editWatchModal").style.display = "none";
}

async function submitEditWatchlist() {
  const symbol = document.getElementById("editWatchSymbol").value;
  const cost = parseFloat(document.getElementById("editWatchCost").value) || 0.0;
  const shares = parseInt(document.getElementById("editWatchShares").value) || 0;
  const availableShares = parseInt(document.getElementById("editAvailableShares").value) || 0;
  const groupId = document.getElementById("editWatchGroup") ? document.getElementById("editWatchGroup").value : "";
  const notes = document.getElementById("editWatchNotes").value.trim();

  try {
    const res = await fetch(`/api/watchlist/${symbol}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        holding_cost: cost,
        holding_shares: shares,
        available_shares: availableShares,
        group_id: groupId,
        notes: notes
      })
    });
    const data = await res.json();
    if (res.ok) {
      hideEditWatchlistModal();
      await loadGroups();
      await loadWatchlist();
      await loadT0Monitor();
    } else {
      alert("保存持仓失败: " + (data.detail || "未知错误"));
    }
  } catch (e) {
    alert("请求异常: " + e);
  }
}

// 4. A股 T+1 日内做T助手
async function loadT0Monitor() {
  try {
    const res = await fetch("/api/t0/monitor");
    if (!res.ok) return;
    const items = await res.json();
    cachedT0Raw = items || [];
    renderGroupFilterBars();
    renderT0Cards(cachedT0Raw);
  } catch (e) {
    console.error("加载做T监控异常:", e);
  }
}

function renderT0Cards(items) {
  const container = document.getElementById("t0CardsContainer");
  if (!container) return;

  if (!items || items.length === 0) {
    container.innerHTML = `
      <div style="grid-column: 1 / -1; text-align: center; color: var(--text-dim); padding: 50px 20px; background: rgba(0,0,0,0.15); border: 1px dashed var(--border-subtle); border-radius: var(--radius-md);">
        <div style="font-size: 28px; margin-bottom: 10px;">🎯</div>
        <p style="font-size: 14px; color: var(--text-muted); margin-bottom: 12px;">T0 自动监控已开启。请添加主板股票或场内ETF并填写总持仓；当日可卖股数未填写时默认等于总持仓（当日新买入的 T+1 锁定部分请自行改小）。未满足数据、趋势或可卖数量条件时不会发出入场信号。</p>
        <button class="btn btn-secondary btn-sm" onclick="showAddWatchlistModal()">管理自选标的</button>
      </div>`;
    return;
  }

  let filtered = items;
  if (selectedT0GroupFilter !== "ALL") {
    if (selectedT0GroupFilter === "UNASSIGNED") {
      filtered = items.filter(t => {
        const sym = (t.symbol || t.code || "").toLowerCase();
        const inAny = cachedGroups && cachedGroups.some(g => {
          const sSet = (g.stock_symbols || []).map(s => (s||"").toLowerCase());
          const sList = (g.stocks || []).map(s => (s.symbol||"").toLowerCase());
          return sSet.includes(sym) || sList.includes(sym) || (t.group_ids && t.group_ids.includes(g.id)) || t.group_id === g.id;
        });
        return !inAny;
      });
    } else {
      const targetGroup = cachedGroups ? cachedGroups.find(g => g.id === selectedT0GroupFilter) : null;
      const targetSymbols = new Set([
        ...((targetGroup && targetGroup.stock_symbols) || []),
        ...(((targetGroup && targetGroup.stocks) || []).map(s => s.symbol))
      ].map(s => (s || "").toLowerCase()));

      filtered = items.filter(t => {
        const sym = (t.symbol || t.code || "").toLowerCase();
        const inGids = t.group_ids && t.group_ids.includes(selectedT0GroupFilter);
        const inGid = t.group_id === selectedT0GroupFilter;
        return inGids || inGid || targetSymbols.has(sym);
      });
    }
  }

  if (filtered.length === 0) {
    container.innerHTML = `
      <div style="grid-column: 1 / -1; text-align: center; color: var(--text-dim); padding: 40px 20px; background: rgba(0,0,0,0.15); border-radius: var(--radius-md);">
        <p style="font-size: 14px; color: var(--text-muted);">当前所选做T分组暂无监控标的</p>
      </div>`;
    return;
  }

  let html = "";
  filtered.forEach(t => {
    const sym = t.symbol || t.code || "";
    const symLower = (sym || "").toLowerCase();
    const pos = t.position || {};
    const hasOpenPos = pos.status === "OPEN";
    const isBuyT = t.signal_action === "BUY_T";
    const isSellT = t.signal_action === "SELL_T";

    const badge = isBuyT ? `<span class="pill pill-red">正T低吸触发</span>` :
                  isSellT ? `<span class="pill pill-green">倒T高抛触发</span>` :
                  hasOpenPos ? `<span class="pill pill-gold">模型信号观察中</span>` :
                  `<span class="pill pill-cyan">通道内震荡</span>`;

    const assignedGroups = cachedGroups ? cachedGroups.filter(g => {
      const gStockSymbols = (g.stock_symbols || []).map(s => (s || "").toLowerCase());
      const gStocks = (g.stocks || []).map(s => (s.symbol || "").toLowerCase());
      const inGids = t.group_ids && t.group_ids.includes(g.id);
      const inGid = t.group_id === g.id;
      return gStockSymbols.includes(symLower) || gStocks.includes(symLower) || inGids || inGid;
    }) : [];

    let groupTag = "";
    if (assignedGroups.length > 0) {
      groupTag = assignedGroups.map(g => `<span class="pill pill-group" onclick="event.stopPropagation(); showGroupModal('${g.id}')" title="所属【${g.name}】，点击配置">🤖 ${g.name}</span>`).join(" ");
    } else {
      groupTag = `<span class="pill pill-group pill-group-default" onclick="event.stopPropagation(); showGroupModal()" title="未指定做T专属分组，点击划分">未指定组</span>`;
    }

    html += `
      <div class="stock-card" data-symbol="${sym}" onclick="openStockDiagnose('${sym}')" title="点击直接跳转单股深度诊断与专业K线分析" style="border-left: 4px solid ${isBuyT ? 'var(--c-up)' : isSellT ? 'var(--c-down)' : 'var(--border-subtle)'}; cursor: pointer;">
        <div class="stock-header">
          <div>
            <h3 style="font-size: 15px; display: flex; align-items: center; gap: 6px; flex-wrap: wrap;">
              <span class="stock-name-link" style="color: var(--text-main); font-weight: 700; text-decoration: underline; text-decoration-color: var(--c-cyan); text-underline-offset: 4px;">${t.name}</span>
              <span style="font-size: 12px; color: var(--text-dim); font-weight: normal;">(${sym})</span>
              ${groupTag}
              ${badge}
            </h3>
            <div style="display: flex; align-items: center; gap: 8px; margin-top: 4px;">
              <span style="font-size: 11px; color: var(--text-dim);">趋势通道: ${t.trend_direction === 'BULL' ? '多头排列且上行' : t.trend_direction === 'BEAR' ? '空头或走弱' : '趋势数据不足/震荡'}</span>
              <span style="font-size: 11px; color: var(--c-cyan); font-weight: 600;">🔍 点击进入深度诊断</span>
            </div>
          </div>
          <div style="text-align: right;">
            <span style="font-size: 16px; font-weight: 700; font-family: var(--font-mono);">${(t.current_price || 0).toFixed(2)}</span>
            <div style="font-size: 11px;" class="${(t.change_pct || 0) >= 0 ? 'text-up' : 'text-down'}">${(t.change_pct || 0) >= 0 ? '+' : ''}${(t.change_pct || 0).toFixed(2)}%</div>
          </div>
        </div>

        <div class="stock-metric-row">
          <div class="metric-item">
            <div class="metric-label">分时均价 VWAP</div>
            <div class="metric-value">${(t.vwap || 0).toFixed(2)}</div>
          </div>
          <div class="metric-item">
            <div class="metric-label">分时偏离度</div>
            <div class="metric-value ${(t.bias_pct || 0) > 0 ? 'text-up' : 'text-down'}">${(t.bias_pct || 0) > 0 ? '+' : ''}${t.bias_pct || 0}%</div>
          </div>
          <div class="metric-item">
            <div class="metric-label">动态布林下/上轨</div>
            <div class="metric-value" style="font-size: 11px;">${t.dyn_lower || '-'} / ${t.dyn_upper || '-'}</div>
          </div>
        </div>

        <div style="font-size: 12px; margin-bottom: 8px;">
          <strong>研判指导:</strong> <span class="text-cyan">${t.signal_desc || '-'}</span>
        </div>

        ${t.reasons && t.reasons.length ? `<p style="font-size: 11px; color: var(--text-muted); line-height: 1.5; margin-bottom: 8px;">${t.reasons.join("；")}</p>` : ''}

        <div style="display: flex; justify-content: space-between; align-items: center; margin-top: 10px; padding-top: 8px; border-top: 1px dashed var(--border-subtle);">
          <span style="font-size: 11px; color: var(--text-dim);">总持仓: ${(t.holding_shares || 0) > 0 ? t.holding_shares + '股' : '未持股'} · 当日可卖: ${(t.available_shares || 0)}股 · 信号价不代表成交</span>
          <button type="button" class="btn btn-primary btn-xs" style="padding: 4px 12px; font-size: 11px;" onclick="event.stopPropagation(); openStockDiagnose('${sym}')">🔍 深度量化诊断与专业K线 →</button>
        </div>
      </div>
    `;
  });
  container.innerHTML = html;

  container.querySelectorAll(".stock-card").forEach(card => {
    card.addEventListener("click", () => {
      const sym = card.getAttribute("data-symbol");
      if (sym) openStockDiagnose(sym);
    });
  });
}

async function resetT0Today() {
  if (!confirm("确定要重置今日所有做T持仓状态吗？")) return;
  await fetch("/api/t0/reset", { method: "POST" });
  loadT0Monitor();
}

// ==================== 做T标的分组与多钉钉群推送前端管理 ====================

async function loadGroups() {
  try {
    const res = await fetch("/api/t0/groups");
    if (!res.ok) return;
    cachedGroups = await res.json();
    renderGroupFilterBars();
    renderGroupManageList();
    updateGroupSelectDropdowns();
    updateSettingsGroupSummary();
  } catch (e) {
    console.error("加载做T分组失败:", e);
  }
}

function updateSettingsGroupSummary() {
  const el = document.getElementById("settingsGroupSummary");
  if (!el) return;
  if (!cachedGroups || cachedGroups.length === 0) {
    el.innerHTML = "尚未配置做T专属分组，所有标的默认推送到全局钉钉机器人。";
    return;
  }
  const enabledCount = cachedGroups.filter(g => g.enabled !== false).length;
  const totalStocks = cachedGroups.reduce((acc, g) => acc + (g.stock_count || 0), 0);
  el.innerHTML = `已配置 <strong>${cachedGroups.length}</strong> 个做T分组（<strong>${enabledCount}</strong> 个启用），共覆盖 <strong>${totalStocks}</strong> 只标的。`;
}

function updateGroupSelectDropdowns(selectedEditGid = "") {
  const selAdd = document.getElementById("addStockGroup");
  if (selAdd) {
    let html = `<option value="">未指定 (使用默认钉钉群)</option>`;
    (cachedGroups || []).forEach(g => {
      html += `<option value="${g.id}">${g.name} (${g.stock_count || 0}只标的)</option>`;
    });
    selAdd.innerHTML = html;
  }
  const selEdit = document.getElementById("editWatchGroup");
  if (selEdit) {
    let html = `<option value="">未指定 (使用默认钉钉群)</option>`;
    (cachedGroups || []).forEach(g => {
      const selected = (g.id === selectedEditGid) ? "selected" : "";
      html += `<option value="${g.id}" ${selected}>${g.name} (${g.stock_count || 0}只标的)</option>`;
    });
    selEdit.innerHTML = html;
  }
}

function renderGroupFilterBars() {
  const totalCount = cachedWatchlistRaw ? cachedWatchlistRaw.length : 0;
  let unassignedCount = 0;
  if (cachedWatchlistRaw) {
    unassignedCount = cachedWatchlistRaw.filter(it => {
      const sym = (it.symbol || "").toLowerCase();
      const inAny = cachedGroups && cachedGroups.some(g => {
        const sSet = (g.stock_symbols || []).map(s => (s||"").toLowerCase());
        const sList = (g.stocks || []).map(s => (s.symbol||"").toLowerCase());
        return sSet.includes(sym) || sList.includes(sym) || (it.group_ids && it.group_ids.includes(g.id)) || it.group_id === g.id;
      });
      return !inAny;
    }).length;
  }

  // 1. 盘中自选分组筛选栏
  const wBar = document.getElementById("watchlistGroupFilterBar");
  if (wBar) {
    let wHtml = `
      <span style="font-size: 12px; color: var(--text-dim); margin-right: 4px;">🏷️ 做T分组筛选:</span>
      <button class="group-filter-btn ${selectedWatchlistGroupFilter === 'ALL' ? 'active' : ''}" onclick="filterWatchlistGroup('ALL')">全部标的 (${totalCount})</button>
    `;
    cachedGroups.forEach(g => {
      const active = selectedWatchlistGroupFilter === g.id ? 'active' : '';
      wHtml += `<button class="group-filter-btn ${active}" onclick="filterWatchlistGroup('${g.id}')">${g.name} (${g.stock_count || 0})</button>`;
    });
    if (unassignedCount > 0) {
      const active = selectedWatchlistGroupFilter === 'UNASSIGNED' ? 'active' : '';
      wHtml += `<button class="group-filter-btn ${active}" onclick="filterWatchlistGroup('UNASSIGNED')">未分组 (${unassignedCount})</button>`;
    }
    wBar.innerHTML = wHtml;
  }

  // 2. T0 做T助手分组筛选栏
  const tBar = document.getElementById("t0GroupFilterBar");
  if (tBar) {
    let tHtml = `
      <span style="font-size: 12px; color: var(--text-dim); margin-right: 4px;">🏷️ 做T标的分组分流:</span>
      <button class="group-filter-btn ${selectedT0GroupFilter === 'ALL' ? 'active' : ''}" onclick="filterT0Group('ALL')">全部做T标的 (${totalCount})</button>
    `;
    cachedGroups.forEach(g => {
      const active = selectedT0GroupFilter === g.id ? 'active' : '';
      tHtml += `<button class="group-filter-btn ${active}" onclick="filterT0Group('${g.id}')">${g.name} (${g.stock_count || 0})</button>`;
    });
    if (unassignedCount > 0) {
      const active = selectedT0GroupFilter === 'UNASSIGNED' ? 'active' : '';
      tHtml += `<button class="group-filter-btn ${active}" onclick="filterT0Group('UNASSIGNED')">未分组 (${unassignedCount})</button>`;
    }
    tBar.innerHTML = tHtml;
  }
}

function filterWatchlistGroup(gid) {
  selectedWatchlistGroupFilter = gid;
  renderGroupFilterBars();
  renderWatchlistTable(cachedWatchlistRaw);
}

function filterT0Group(gid) {
  selectedT0GroupFilter = gid;
  renderGroupFilterBars();
  renderT0Cards(cachedT0Raw);
}

let activeManageGroupId = null;

function renderGroupManageList() {
  const container = document.getElementById("groupCardsList");
  if (!container) return;
  if (!cachedGroups || cachedGroups.length === 0) {
    container.innerHTML = `<div style="text-align: center; color: var(--text-dim); padding: 30px;">暂无做T分组，请点击上方按钮新建</div>`;
    return;
  }

  // 如果当前选中的分组不在列表中或尚未初始化，默认高亮第一个
  if (!activeManageGroupId || !cachedGroups.some(g => g.id === activeManageGroupId)) {
    activeManageGroupId = cachedGroups[0].id;
  }

  let html = "";
  cachedGroups.forEach(g => {
    const isSelected = (g.id === activeManageGroupId);
    const webhookMasked = g.dingtalk_webhook ? (g.dingtalk_webhook.substring(0, 36) + "...") : "<span style='color:#ef4444;'>未配置 Webhook</span>";
    const hasSecret = !!g.dingtalk_secret;
    const stocks = g.stocks || [];

    let stockChipsHtml = "";
    if (stocks.length > 0) {
      stockChipsHtml = stocks.map(s => `<span class="group-stock-chip" title="${s.symbol}">${s.name} (${s.symbol})</span>`).join("");
    } else {
      stockChipsHtml = `<span style="font-size: 11px; color: var(--text-dim);">暂无标的归属该组</span>`;
    }

    // 模块使能状态胶囊
    const modT0 = g.push_t0 !== false;
    const modScreener = g.push_screener !== false;
    const modTail = g.push_tail230 !== false;
    const modLimitup = g.push_limitup !== false;
    const modReview = g.push_review !== false;

    const modulesHtml = `
      <div style="display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px;">
        <span class="module-sub-pill ${modT0 ? 'active' : 'inactive'}" title="${modT0 ? '已订阅日内做T' : '未订阅日内做T'}">🎯 做T</span>
        <span class="module-sub-pill ${modScreener ? 'active' : 'inactive'}" title="${modScreener ? '已订阅多因子选股' : '未订阅多因子选股'}">💎 选股</span>
        <span class="module-sub-pill ${modTail ? 'active' : 'inactive'}" title="${modTail ? '已订阅尾盘2:30' : '未订阅尾盘2:30'}">⚡ 尾盘</span>
        <span class="module-sub-pill ${modLimitup ? 'active' : 'inactive'}" title="${modLimitup ? '已订阅龙虎打板' : '未订阅龙虎打板'}">🚀 打板</span>
        <span class="module-sub-pill ${modReview ? 'active' : 'inactive'}" title="${modReview ? '已订阅收盘复盘' : '未订阅收盘复盘'}">📊 复盘</span>
      </div>
    `;

    html += `
      <div id="group-card-${g.id}" class="group-item-card ${isSelected ? 'active' : ''}" onclick="selectGroupForViewAndEdit('${g.id}')" title="点击直接在右侧查看/编辑本组配置及股票分配">
        <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 8px;">
          <div>
            <div style="display: flex; align-items: center; gap: 8px;">
              <strong style="font-size: 14px; color: #fff;">${g.name}</strong>
              <span class="pill ${g.enabled !== false ? 'pill-green' : 'pill-gold'}">${g.enabled !== false ? '已开启' : '已暂停'}</span>
              <span class="pill pill-cyan">${stocks.length} 只标的</span>
            </div>
            <div style="font-size: 11px; color: var(--text-dim); margin-top: 4px; font-family: var(--font-mono);">
              🤖 ${webhookMasked}
            </div>
            ${hasSecret ? `<div style="font-size: 11px; color: var(--c-purple);">🔒 已配置加签 Secret</div>` : ''}
            ${g.notes ? `<div style="font-size: 11px; color: var(--text-muted); margin-top: 2px;">📝 ${g.notes}</div>` : ''}
          </div>
        </div>

        <div style="margin: 6px 0; border-top: 1px dashed var(--border-subtle); padding-top: 6px;">
          <div style="font-size: 11px; color: var(--text-dim); margin-bottom: 2px;">📡 已订阅消息模块:</div>
          ${modulesHtml}
        </div>

        <div style="margin: 8px 0; border-top: 1px dashed var(--border-subtle); padding-top: 6px;">
          <div style="font-size: 11px; color: var(--text-dim); margin-bottom: 4px;">🎯 归属本组的股票:</div>
          <div style="display: flex; flex-wrap: wrap;">${stockChipsHtml}</div>
        </div>

        <div style="display: flex; gap: 8px; justify-content: flex-end; margin-top: 10px;">
          <button class="btn btn-secondary btn-sm" onclick="event.stopPropagation(); testGroupNotify('${g.id}')">🔔 测试推送</button>
          <button class="btn btn-secondary btn-sm" onclick="event.stopPropagation(); selectGroupForViewAndEdit('${g.id}')">👁️ 查看/编辑</button>
          ${g.id !== 'group_default' ? `<button class="btn btn-secondary btn-sm" style="color: #ef4444;" onclick="event.stopPropagation(); deleteGroup('${g.id}')">🗑️ 删除</button>` : ''}
        </div>
      </div>
    `;
  });
  container.innerHTML = html;
}

function selectGroupForViewAndEdit(groupId) {
  activeManageGroupId = groupId;
  const grp = cachedGroups.find(g => g.id === groupId);
  if (!grp) return;

  // 更新卡片激活态高亮样式
  document.querySelectorAll(".group-item-card").forEach(el => el.classList.remove("active"));
  const currentCard = document.getElementById(`group-card-${groupId}`);
  if (currentCard) {
    currentCard.classList.add("active");
  }

  document.getElementById("groupFormTitle").innerText = `✏️ 编辑分组: ${grp.name}`;
  document.getElementById("editGroupId").value = grp.id;
  document.getElementById("editGroupName").value = grp.name || "";
  document.getElementById("editGroupWebhook").value = grp.dingtalk_webhook || "";
  document.getElementById("editGroupSecret").value = grp.dingtalk_secret || "";
  document.getElementById("editGroupEnabled").checked = grp.enabled !== false;
  document.getElementById("editGroupNotes").value = grp.notes || "";

  // 5 个模块开关
  document.getElementById("editPushT0").checked = grp.push_t0 !== false;
  document.getElementById("editPushScreener").checked = grp.push_screener !== false;
  document.getElementById("editPushTail230").checked = grp.push_tail230 !== false;
  document.getElementById("editPushLimitup").checked = grp.push_limitup !== false;
  document.getElementById("editPushReview").checked = grp.push_review !== false;

  // 刷新并勾选属于本组的标的
  populateStockCheckboxesForGroup(grp.id);
}

// 别名保留兼容
function editGroup(groupId) {
  selectGroupForViewAndEdit(groupId);
}

function showGroupModal(targetGroupId = null) {
  document.getElementById("t0GroupModal").style.display = "flex";
  
  const targetId = targetGroupId || (cachedGroups && cachedGroups.length > 0 ? cachedGroups[0].id : null);
  activeManageGroupId = targetId;
  
  renderGroupManageList();
  
  if (targetId) {
    selectGroupForViewAndEdit(targetId);
  } else {
    resetGroupForm();
  }
}

function hideGroupModal() {
  document.getElementById("t0GroupModal").style.display = "none";
}

function populateStockCheckboxesForGroup(selectedGroupId = null) {
  const container = document.getElementById("groupStockCheckboxes");
  if (!container) return;
  if (!cachedWatchlistRaw || cachedWatchlistRaw.length === 0) {
    container.innerHTML = `<span style="font-size: 12px; color: var(--text-dim);">自选池暂无股票，请先添加自选股</span>`;
    return;
  }

  const grp = cachedGroups ? cachedGroups.find(g => g.id === selectedGroupId) : null;
  const grpSymbols = new Set();
  if (grp) {
    if (grp.stock_symbols && Array.isArray(grp.stock_symbols)) {
      grp.stock_symbols.forEach(s => grpSymbols.add((s || "").toLowerCase()));
    }
    if (grp.stocks && Array.isArray(grp.stocks)) {
      grp.stocks.forEach(s => grpSymbols.add((s.symbol || "").toLowerCase()));
    }
  }

  let html = "";
  cachedWatchlistRaw.forEach(it => {
    const symLower = (it.symbol || "").toLowerCase();

    // 判断该股票是否属于当前正在查看/编辑的分组 (支持跨组重合)
    const hasAnyGroup = (it.group_ids && it.group_ids.length > 0) || !!it.group_id;
    const isChecked = selectedGroupId && (
      grpSymbols.has(symLower) ||
      (it.group_ids && it.group_ids.includes(selectedGroupId)) ||
      it.group_id === selectedGroupId ||
      (!hasAnyGroup && selectedGroupId === 'group_default')
    );

    // 计算该股票所属的所有分组名称 (展示多重归属标签)
    const assignedNames = [];
    if (cachedGroups && cachedGroups.length > 0) {
      cachedGroups.forEach(g => {
        const inGrpSymbols = g.stock_symbols && g.stock_symbols.map(s => (s||"").toLowerCase()).includes(symLower);
        const inGrpStocks = g.stocks && g.stocks.map(s => (s.symbol||"").toLowerCase()).includes(symLower);
        const inGids = it.group_ids && it.group_ids.includes(g.id);
        const inGid = it.group_id === g.id;
        if (inGrpSymbols || inGrpStocks || inGids || inGid) {
          if (!assignedNames.includes(g.name)) {
            assignedNames.push(g.name);
          }
        }
      });
    }

    const grpTag = assignedNames.length > 0 
      ? `<span class="pill pill-cyan" style="font-size:10px; padding:1px 6px;">${assignedNames.join(", ")}</span>`
      : `<span style="font-size: 10px; color: var(--text-dim);">[未分组]</span>`;

    html += `
      <label style="display: flex; align-items: center; justify-content: space-between; gap: 8px; font-size: 12px; color: var(--text-main); cursor: pointer; padding: 4px 6px; border-radius: 4px; background: rgba(255,255,255,0.02); margin-bottom: 2px;">
        <div style="display: flex; align-items: center; gap: 6px;">
          <input type="checkbox" class="group-stock-chk" value="${it.symbol}" ${isChecked ? 'checked' : ''}>
          <span><strong>${it.name}</strong> (${it.symbol})</span>
        </div>
        <div>
          ${grpTag}
        </div>
      </label>
    `;
  });
  container.innerHTML = html;
}

function toggleSelectAllGroupStocks() {
  const chks = document.querySelectorAll(".group-stock-chk");
  const anyUnchecked = Array.from(chks).some(c => !c.checked);
  chks.forEach(c => c.checked = anyUnchecked);
}

function resetGroupForm() {
  activeManageGroupId = null;
  document.querySelectorAll(".group-item-card").forEach(el => el.classList.remove("active"));
  document.getElementById("groupFormTitle").innerText = "✨ 新增做T分组";
  document.getElementById("editGroupId").value = "";
  document.getElementById("editGroupName").value = "";
  document.getElementById("editGroupWebhook").value = "";
  document.getElementById("editGroupSecret").value = "";
  document.getElementById("editGroupEnabled").checked = true;
  document.getElementById("editGroupNotes").value = "";

  document.getElementById("editPushT0").checked = true;
  document.getElementById("editPushScreener").checked = true;
  document.getElementById("editPushTail230").checked = true;
  document.getElementById("editPushLimitup").checked = true;
  document.getElementById("editPushReview").checked = true;

  populateStockCheckboxesForGroup(null);
}

async function submitSaveGroup() {
  const gid = document.getElementById("editGroupId").value.trim();
  const name = document.getElementById("editGroupName").value.trim();
  const webhook = document.getElementById("editGroupWebhook").value.trim();
  const secret = document.getElementById("editGroupSecret").value.trim();
  const enabled = document.getElementById("editGroupEnabled").checked;
  const notes = document.getElementById("editGroupNotes").value.trim();

  const pushT0 = document.getElementById("editPushT0").checked;
  const pushScreener = document.getElementById("editPushScreener").checked;
  const pushTail230 = document.getElementById("editPushTail230").checked;
  const pushLimitup = document.getElementById("editPushLimitup").checked;
  const pushReview = document.getElementById("editPushReview").checked;

  if (!name) { alert("请输入分组名称"); return; }

  const checkedSymbols = Array.from(document.querySelectorAll(".group-stock-chk:checked")).map(c => c.value);

  const payload = {
    id: gid,
    name: name,
    dingtalk_webhook: webhook,
    dingtalk_secret: secret,
    enabled: enabled,
    notes: notes,
    push_t0: pushT0,
    push_screener: pushScreener,
    push_tail230: pushTail230,
    push_limitup: pushLimitup,
    push_review: pushReview,
    stock_symbols: checkedSymbols
  };

  try {
    const res = await fetch("/api/t0/groups", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (res.ok) {
      alert("✅ 做T分组与各策略订阅配置保存成功！");
      await loadGroups();
      await loadWatchlist();
      await loadT0Monitor();
      const savedId = (data.group && data.group.id) ? data.group.id : (gid || "group_default");
      renderGroupManageList();
      selectGroupForViewAndEdit(savedId);
    } else {
      alert("保存失败: " + (data.detail || "未知错误"));
    }
  } catch (e) {
    alert("保存分组异常: " + e);
  }
}

async function deleteGroup(groupId) {
  const grp = cachedGroups.find(g => g.id === groupId);
  const name = grp ? grp.name : groupId;
  if (!confirm(`确定要删除做T分组【${name}】吗？删除后该组内股票将恢复为未分组状态。`)) return;

  try {
    const res = await fetch(`/api/t0/groups/${groupId}`, { method: "DELETE" });
    if (res.ok) {
      alert("✅ 分组已删除");
      await loadGroups();
      await loadWatchlist();
      await loadT0Monitor();
      if (cachedGroups && cachedGroups.length > 0) {
        renderGroupManageList();
        selectGroupForViewAndEdit(cachedGroups[0].id);
      } else {
        resetGroupForm();
      }
    } else {
      alert("删除失败");
    }
  } catch (e) {
    alert("删除异常: " + e);
  }
}

async function testGroupNotify(groupId) {
  try {
    const res = await fetch("/api/t0/groups/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ group_id: groupId })
    });
    const data = await res.json();
    if (data.success) {
      alert(`🎉 【${data.group_name}】钉钉推送测试成功！已向该群发送联通卡片。`);
    } else {
      alert(`⚠️ 推送测试未成功: ${data.message || '请检查 Webhook 与 Secret'}`);
    }
  } catch (e) {
    alert("测试异常: " + e);
  }
}

async function quickChangeStockGroup(symbol, newGroupId) {
  try {
    const res = await fetch("/api/watchlist/group", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol: symbol, group_id: newGroupId })
    });
    if (res.ok) {
      await loadGroups();
      await loadWatchlist();
      await loadT0Monitor();
    }
  } catch (e) {
    console.error("切换做T分组失败:", e);
  }
}

// 5. 单股量化诊断与 ECharts 专业K线图 + 筹码分布
async function openStockDiagnose(symbol) {
  console.log("[Quant2.0] 触发单股深度诊断联动:", symbol);
  if (!symbol) return;
  const cleanSym = String(symbol).trim();
  lastDiagnosedSymbol = cleanSym;

  // 1. 显式切换至单股诊断面板
  switchTab("diagnose");

  // 2. 填写输入框
  const input = document.getElementById("diagnoseInput");
  if (input) {
    input.value = cleanSym;
  }

  // 3. 滚动到页面诊断区域顶部
  const activePane = document.getElementById("pane-diagnose");
  if (activePane) {
    activePane.scrollIntoView({ behavior: "smooth", block: "start" });
  } else {
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  // 4. 立即执行诊断并加载K线图
  await runDiagnose();
}
window.openStockDiagnose = openStockDiagnose;

async function runDiagnose() {
  const query = document.getElementById("diagnoseInput").value.trim();
  if (!query) { alert("请输入股票代码或名称"); return; }
  lastDiagnosedSymbol = query;

  const box = document.getElementById("diagnoseSummaryBox");
  if (box) {
    box.innerHTML = `
      <div style="text-align: center; padding: 40px; color: var(--text-muted);">
        <div style="font-size: 28px; margin-bottom: 8px;">🔍</div>
        <div style="font-size: 15px; color: var(--c-cyan); font-weight: 600;">正在加载并分析【${query}】量化多因子与专业K线...</div>
        <div style="font-size: 12px; color: var(--text-dim); margin-top: 6px;">正在拉取历史K线、主力筹码分布、RSRS 偏度动量与财报/ETF画像...</div>
      </div>
    `;
  }

  try {
    const res = await fetch(`/api/stock/diagnose?symbol=${encodeURIComponent(query)}`);
    if (!res.ok) {
      const err = await res.json();
      if (box) {
        box.innerHTML = `
          <div style="text-align: center; padding: 30px; color: #f87171; background: rgba(239, 68, 68, 0.08); border-radius: var(--radius-sm);">
            <strong>❌ 诊断失败:</strong> ${err.detail || "标的无效或历史K线数据不足"}
          </div>
        `;
      }
      return;
    }
    const data = await res.json();
    renderDiagnoseSummary(data);

    // 加载专业K线图与筹码分布
    await loadKlineChart(data.symbol);
    if (klineChartInstance) {
      setTimeout(() => klineChartInstance.resize(), 50);
    }
  } catch (e) {
    if (box) {
      box.innerHTML = `
        <div style="text-align: center; padding: 30px; color: #f87171; background: rgba(239, 68, 68, 0.08); border-radius: var(--radius-sm);">
          <strong>❌ 诊断异常:</strong> ${e}
        </div>
      `;
    }
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
  const riskDataComplete = fin.data_quality?.can_assess_risk === true;
  const isSafe = redFlags.is_safe === true && riskDataComplete;

  let finHtml = "";
  if (fin.symbol && !fin.data_available) {
    finHtml = `
      <div class="fin-report-card" style="border-color: rgba(239, 68, 68, 0.35);">
        <h3 style="font-size: 15px; color: #fca5a5;">⚠️ 财报数据不可用</h3>
        <p style="font-size: 12px; color: var(--text-muted); margin-top: 8px;">
          当前无法获取可核验的财务报告字段，风险状态为未知。系统未用估算数值代替披露数据，因此不生成基本面安全评分。
        </p>
      </div>`;
  } else if (fin.symbol) {
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
              排雷状态: ${redFlags.risk_level || '未知'} (${redFlags.radar_score != null ? redFlags.radar_score + '分' : '评分不可用'})
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
              <div style="font-size: 18px; font-weight: 700; color: var(--c-cyan); margin: 4px 0;">${dupont.roe != null ? dupont.roe.toFixed(2) + '%' : '未知'}</div>
              <div style="font-size: 10px; color: var(--text-muted);">杜邦评分: ${dupont.dupont_score ?? '-'}分</div>
            </div>
            <div class="fin-dupont-box">
              <div style="font-size: 11px; color: var(--text-dim);">销售净利率 (Net Margin)</div>
              <div style="font-size: 18px; font-weight: 700; color: #fff; margin: 4px 0;">${dupont.net_profit_margin != null ? dupont.net_profit_margin.toFixed(2) + '%' : '未知'}</div>
              <div style="font-size: 10px; color: var(--text-muted);">产品定价权与护城河</div>
            </div>
            <div class="fin-dupont-box">
              <div style="font-size: 11px; color: var(--text-dim);">总资产周转率 (Turnover)</div>
              <div style="font-size: 18px; font-weight: 700; color: #fff; margin: 4px 0;">${dupont.asset_turnover != null ? dupont.asset_turnover.toFixed(2) + ' 次' : '未知'}</div>
              <div style="font-size: 10px; color: var(--text-muted);">资金营运与周转效率</div>
            </div>
            <div class="fin-dupont-box">
              <div style="font-size: 11px; color: var(--text-dim);">权益乘数 (Leverage)</div>
              <div style="font-size: 18px; font-weight: 700; color: #fff; margin: 4px 0;">${dupont.equity_multiplier != null ? dupont.equity_multiplier.toFixed(2) + ' 倍' : '未知'}</div>
              <div style="font-size: 10px; color: var(--text-muted);">资产负债率: ${inds.debt_to_assets_pct != null ? inds.debt_to_assets_pct.toFixed(1) + '%' : '未知'}</div>
            </div>
          </div>
        </div>

        <!-- 经营成长与皮氏 F-Score 双栏网格 -->
        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; margin-bottom: 14px;">
          <!-- 成长与现金流质量 -->
          <div style="background: rgba(0,0,0,0.25); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); padding: 12px;">
            <div style="font-size: 12px; font-weight: 600; color: var(--text-muted); margin-bottom: 8px;">📈 经营成长性与盈利含金量</div>
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; font-size: 12px;">
              <div>营业收入增速: <strong class="${growth.revenue_yoy == null ? 'text-dim' : (growth.revenue_yoy >= 0 ? 'text-up' : 'text-down')}">${growth.revenue_yoy != null ? (growth.revenue_yoy > 0 ? '+' : '') + growth.revenue_yoy.toFixed(1) + '%' : '未知'}</strong></div>
              <div>归母净利增速: <strong class="${growth.net_profit_yoy == null ? 'text-dim' : (growth.net_profit_yoy >= 0 ? 'text-up' : 'text-down')}">${growth.net_profit_yoy != null ? (growth.net_profit_yoy > 0 ? '+' : '') + growth.net_profit_yoy.toFixed(1) + '%' : '未知'}</strong></div>
              <div>扣非净利增速: <strong class="${growth.deduct_profit_yoy == null ? 'text-dim' : (growth.deduct_profit_yoy >= 0 ? 'text-up' : 'text-down')}">${growth.deduct_profit_yoy != null ? (growth.deduct_profit_yoy > 0 ? '+' : '') + growth.deduct_profit_yoy.toFixed(1) + '%' : '未知'}</strong></div>
              <div>综合销售毛利率: <strong style="color: #fff;">${growth.gross_margin != null ? growth.gross_margin.toFixed(1) + '%' : '未知'}</strong></div>
              <div style="grid-column: span 2; margin-top: 4px;">
                现金/净利匹配度: <strong class="${inds.cash_to_net_profit_ratio == null ? 'text-dim' : (inds.cash_to_net_profit_ratio >= 1.0 ? 'text-cyan' : inds.cash_to_net_profit_ratio >= 0.7 ? 'text-gold' : 'text-down')}">${inds.cash_to_net_profit_ratio != null ? inds.cash_to_net_profit_ratio.toFixed(2) + ' 倍' : '未知'}</strong>
                <span style="font-size: 10px; color: var(--text-dim); margin-left: 4px;">(经营净现金流 / 归母净利润，>1.0为良性)</span>
              </div>
            </div>
          </div>

          <!-- 皮氏 9 维 F-Score 评分 -->
          <div style="background: rgba(0,0,0,0.25); border: 1px solid var(--border-subtle); border-radius: var(--radius-sm); padding: 12px;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
              <span style="font-size: 12px; font-weight: 600; color: var(--text-muted);">🏆 可用财务字段规则提示</span>
              <span class="pill pill-gold">不提供完整 F-Score</span>
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
            <span style="font-size: 11px; color: var(--text-dim);">排雷分: <strong class="${redFlags.radar_score == null ? 'text-dim' : (redFlags.radar_score >= 80 ? 'text-cyan' : 'text-down')}">${redFlags.radar_score != null ? redFlags.radar_score + '分' : '不可用'}</strong></span>
          </div>

          <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; font-size: 11px;">
            <div style="background: rgba(255,255,255,0.02); padding: 6px 8px; border-radius: 4px;">
              <span style="color: var(--text-dim);">商誉/净资产:</span> <strong>${inds.goodwill_to_equity_pct != null ? inds.goodwill_to_equity_pct.toFixed(1) + '%' : '未知'}</strong>
              <div style="font-size: 10px; color: var(--text-dim);">安全阈值 <25%</div>
            </div>
            <div style="background: rgba(255,255,255,0.02); padding: 6px 8px; border-radius: 4px;">
              <span style="color: var(--text-dim);">应收/总营收:</span> <strong>${inds.receivables_to_revenue_pct != null ? inds.receivables_to_revenue_pct.toFixed(1) + '%' : '未知'}</strong>
              <div style="font-size: 10px; color: var(--text-dim);">安全阈值 <60%</div>
            </div>
            <div style="background: rgba(255,255,255,0.02); padding: 6px 8px; border-radius: 4px;">
              <span style="color: var(--text-dim);">存货/总资产:</span> <strong>${inds.inventory_to_assets_pct != null ? inds.inventory_to_assets_pct.toFixed(1) + '%' : '未知'}</strong>
              <div style="font-size: 10px; color: var(--text-dim);">安全阈值 <30%</div>
            </div>
            <div style="background: rgba(255,255,255,0.02); padding: 6px 8px; border-radius: 4px;">
              <span style="color: var(--text-dim);">资产负债率:</span> <strong>${inds.debt_to_assets_pct != null ? inds.debt_to_assets_pct.toFixed(1) + '%' : '未知'}</strong>
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
          ` : !riskDataComplete ? `
            <div style="margin-top: 10px; color: #fbbf24; font-size: 11px;">
              ⚠️ 排雷字段不完整 (${(fin.data_quality?.missing_risk_metrics || []).join('、') || '关键字段缺失'})；未知项不代表安全。
            </div>
          ` : `
            <div style="margin-top: 10px; color: #34d399; font-size: 11px; display: flex; align-items: center; gap: 4px;">
              <span>✅</span> 已检查当前可用的财务排雷指标，未发现触发的预警项；这不构成安全或收益保证。
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
          仓位评估: <strong class="text-cyan">${advice.allowed ? `${advice.position_pct}% (${advice.suggested_shares}股)` : '暂不建议新增仓位'}</strong><br>
          ${advice.reason ? `<span style="color: var(--text-dim);">${advice.reason}</span>` : ''}
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

    const isLight = document.documentElement.getAttribute("data-theme") === "light";
    const axisColor = isLight ? "#94a3b8" : "#334155";
    const splitColor = isLight ? "rgba(0,0,0,0.06)" : "rgba(255,255,255,0.04)";
    const tooltipBg = isLight ? "rgba(255, 255, 255, 0.96)" : "rgba(16, 24, 43, 0.9)";
    const tooltipBorder = isLight ? "rgba(2, 132, 199, 0.35)" : "rgba(56, 189, 248, 0.3)";
    const tooltipText = isLight ? "#0f172a" : "#ffffff";

    const option = {
      animation: false,
      backgroundColor: "transparent",
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross" },
        backgroundColor: tooltipBg,
        borderColor: tooltipBorder,
        textStyle: { color: tooltipText, fontSize: 12 }
      },
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      grid: [
        { left: "5%", right: "8%", top: "8%", height: "55%" },
        { left: "5%", right: "8%", top: "68%", height: "22%" }
      ],
      xAxis: [
        { type: "category", data: dates, gridIndex: 0, axisLine: { lineStyle: { color: axisColor } } },
        { type: "category", data: dates, gridIndex: 1, axisLine: { lineStyle: { color: axisColor } } }
      ],
      yAxis: [
        { scale: true, gridIndex: 0, axisLine: { lineStyle: { color: axisColor } }, splitLine: { lineStyle: { color: splitColor } } },
        { scale: true, gridIndex: 1, axisLine: { lineStyle: { color: axisColor } }, splitLine: { show: false } }
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
  document.getElementById("btBenchmark").innerHTML = data.benchmark_available ? formatPct(data.benchmark_return_pct) : "不可用";
  document.getElementById("btAlpha").innerHTML = data.alpha_pct === null ? "不可用" : `<span class="text-cyan">${data.alpha_pct > 0 ? '+' : ''}${data.alpha_pct}%</span>`;
  document.getElementById("btMaxDD").innerHTML = `<span class="text-down">${data.max_drawdown_pct}%</span>`;
  document.getElementById("btSharpe").innerHTML = data.sharpe_ratio == null ? "不可用" : `<span class="text-gold">${data.sharpe_ratio}</span>`;
  document.getElementById("btWinRate").innerHTML = data.win_rate == null ? `不可用 (${data.total_trades}笔已平仓)` : `${data.win_rate}% (${data.total_trades}笔已平仓)`;
  document.getElementById("btPLRatio").innerHTML = data.profit_loss_ratio == null ? "不可用" : `${data.profit_loss_ratio} : 1`;
  document.getElementById("btOOS").innerHTML = `${formatPct(data.oos_return_pct)} <span class="text-dim">(时间切分)</span>`;
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
  if (winRateEl) winRateEl.textContent = data.win_rate == null ? "不可用" : `${data.win_rate}%`;

  const countEl = document.getElementById("tailTradesCount");
  if (countEl) countEl.textContent = `日线代理样本 ${data.total_trades} 笔 (胜 ${data.win_trades} / 负 ${data.loss_trades}，近${data.backtest_days}天)`;

  const enhEl = document.getElementById("tailEnhancedWinRate");
  if (enhEl) enhEl.textContent = data.enhanced_win_rate == null ? "不可用" : `${data.enhanced_win_rate}%`;

  const plEl = document.getElementById("tailPLRatio");
  if (plEl) plEl.textContent = data.profit_loss_ratio == null ? "不可用" : `${data.profit_loss_ratio}:1`;

  const winLossEl = document.getElementById("tailAvgWinLoss");
  if (winLossEl) winLossEl.textContent = data.avg_win_pct == null || data.avg_loss_pct == null
    ? "胜负样本不足，均值不可用"
    : `平均胜 +${data.avg_win_pct}% / 平均负 ${data.avg_loss_pct}%`;

  const premEl = document.getElementById("tailMorningPrem");
  const nextHighPremium = data.avg_exit_session_high_premium_pct ?? data.avg_next_session_high_premium_pct ?? data.avg_morning_premium_pct;
  if (premEl) premEl.textContent = nextHighPremium == null ? "不可用" : `${nextHighPremium > 0 ? '+' : ''}${nextHighPremium}%`;

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
        <td class="text-up">+${(t.exit_session_high_pct ?? t.next_session_high_pct ?? t.morning_high_pct).toFixed(2)}%</td>
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
              <span class="meta-label">模型目标观察价（非成交价）</span>
              <span class="meta-val text-up">${c.target_sell_price} 元 (+${c.target_pct}%)</span>
            </div>
            <div class="meta-item">
              <span class="meta-label">模型止损观察价（非成交价）</span>
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
              🧾 录入实际买入成交
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
      const sampleWinRate = json.data.win_rate == null ? "不可用" : `${json.data.win_rate}%`;
      const bullWinRate = json.data.enhanced_win_rate == null ? "不可用" : `${json.data.enhanced_win_rate}%`;
      alert(`日线代理回放完成。近 ${days} 天样本胜率: ${sampleWinRate} (多头样本: ${bullWinRate})。T日收盘信号，T+1开盘代理入场，T+2日线模拟退出；不代表真实成交或未来收益。`);
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
    tbody.innerHTML = `<tr><td colspan="9" style="text-align:center; color: var(--text-dim); padding: 25px;">暂无交易记录。研究候选不会自动登记为持仓；实际成交后可录入进行行情提醒。</td></tr>`;
    return;
  }

  tbody.innerHTML = trades.map(t => {
    let statusBadge = `<span class="badge" style="background: rgba(99, 102, 241, 0.2); color: #818cf8;">来源待核验</span>`;
    if (t.source === "research_signal" || t.status === "SIGNAL_ONLY") {
      statusBadge = `<span class="badge" style="background: rgba(99, 102, 241, 0.2); color: #a5b4fc;">研究信号·非持仓</span>`;
    } else if (t.source === "manual_actual_fill" && t.status === "ALERTED_EXIT") {
      statusBadge = `<span class="badge badge-down">行情触发·待人工确认成交</span>`;
    } else if (t.source === "manual_actual_fill" && t.status === "ALERTED_TIMED") {
      statusBadge = `<span class="badge" style="background: rgba(245, 158, 11, 0.2); color: #fbbf24;">⏳ 动能衰竭离场</span>`;
    } else if (t.source === "manual_actual_fill" && t.status === "HOLDING") {
      statusBadge = `<span class="badge" style="background: rgba(99, 102, 241, 0.2); color: #818cf8;">实际成交记录·提醒中</span>`;
    } else if (t.source === "manual_actual_fill" && String(t.status || "").startsWith("CLOSED_")) {
      statusBadge = `<span class="badge badge-up">人工记录已平仓</span>`;
    } else if (String(t.status || "").startsWith("CLOSED_")) {
      statusBadge = `<span class="badge" style="background: rgba(148, 163, 184, 0.2); color: #94a3b8;">旧版模型记录·成交未核验</span>`;
    }

    const actualEntryRecorded = t.source === "manual_actual_fill" && t.entry_fill_confirmed === true;
    const actualExitRecorded = actualEntryRecorded && t.exit_fill_confirmed === true && t.exit_price != null;
    const retPct = actualExitRecorded && t.return_pct != null ? `${t.return_pct > 0 ? '+' : ''}${t.return_pct}%（未扣费用）` : "--";
    const retClass = actualExitRecorded && t.return_pct != null ? (t.return_pct > 0 ? "text-up" : (t.return_pct < 0 ? "text-down" : "")) : "";
    const exitPriceStr = actualExitRecorded ? `${t.exit_price.toFixed(2)}元 (${retPct})` : (actualEntryRecorded ? "未录入实际卖出成交" : "历史价格记录·成交未核验");
    const buyPriceStr = actualEntryRecorded ? `${t.buy_price.toFixed(2)} 元` : `来源未核验 (${Number(t.buy_price || 0).toFixed(2)} 元)`;

    let actionBtn = "";
    if (t.source === "manual_actual_fill" && ["HOLDING", "ALERTED_TIMED", "ALERTED_EXIT"].includes(t.status)) {
      actionBtn = `<button class="btn btn-secondary btn-sm" onclick="manualCloseTailTrade('${t.symbol}', '${t.buy_date}', ${t.target_sell_price})" style="padding: 3px 8px; font-size: 11px;">手动平仓</button>`;
    } else {
      actionBtn = `<span style="font-size: 11px; color: var(--text-dim);">无实际持仓动作</span>`;
    }

    return `
      <tr>
        <td>
          <strong>${t.name}</strong><br>
          <span style="font-size: 11px; color: var(--text-muted);">${t.symbol}</span>
        </td>
        <td>${actualEntryRecorded ? `${t.buy_date} ${t.buy_time || ''}` : `旧记录·${t.buy_date}`}</td>
        <td><strong>${buyPriceStr}</strong></td>
        <td class="text-up"><strong>${t.target_sell_price.toFixed(2)}</strong> 元 (+${t.target_pct}%)</td>
        <td class="text-down"><strong>${t.stop_loss_price.toFixed(2)}</strong> 元 (-${t.stop_pct}%)</td>
        <td>${statusBadge}</td>
        <td class="${retClass}"><strong>${exitPriceStr}</strong></td>
        <td style="font-size: 12px; max-width: 260px;">${t.exit_reason || t.return_method || '行情提醒不等于成交；以人工录入成交价为准'}</td>
        <td>${actionBtn}</td>
      </tr>
    `;
  }).join("");
}

async function trackTailCandidate(sym, name, price) {
  const fillStr = prompt(`仅在已实际买入后录入。请输入 ${name} (${sym}) 的实际成交价格：`, Number(price).toFixed(2));
  if (fillStr === null) return;
  const fillPrice = parseFloat(fillStr);
  if (!Number.isFinite(fillPrice) || fillPrice <= 0) return alert("实际成交价无效");

  try {
    const res = await fetch("/api/tail230/trades/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol: sym, name: name, buy_price: fillPrice })
    });
    if (res.ok) {
      alert(`已记录 ${name} (${sym}) 的买入成交价格。系统只发送行情提醒，不会下单或自动记录卖出成交。`);
      loadTailTrades();
    }
  } catch (e) {
    alert("加入失败: " + e);
  }
}

async function manualCloseTailTrade(sym, buy_date, defaultPrice) {
  const pStr = prompt(`仅在已实际卖出后录入。请输入 ${sym} 的实际卖出成交价格：`);
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
      alert("已记录实际卖出成交价。价格变化未扣交易费用，也不等同于账户净收益。");
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

    document.getElementById("luTotalCount").textContent = `${stats.total_trades || 0} 条信号`;
    document.getElementById("luSettledSub").textContent = `已跟踪 ${stats.settled_trades || stats.closed_trades || 0} 条 · 待后续行情 ${stats.pending_trades || 0} 条`;
    const hasSettledSample = Number(stats.closed_trades || 0) > 0;
    const unavailable = "暂无已结算信号样本";
    document.getElementById("luOpenWinRate").textContent = hasSettledSample ? `${stats.open_premium_win_rate}%` : unavailable;
    document.getElementById("luPeakWinRate").textContent = hasSettledSample ? `${stats.peak_profit_win_rate}%` : unavailable;
    document.getElementById("luConsecutiveRate").textContent = hasSettledSample ? `${stats.consecutive_rate}%` : unavailable;
    document.getElementById("luPlRatio").textContent = hasSettledSample && stats.profit_loss_ratio != null ? `${stats.profit_loss_ratio}:1` : unavailable;
    document.getElementById("luBigLossSub").textContent = hasSettledSample ? `大面率 (跌幅≥4%): ${stats.big_loss_rate}%` : unavailable;

    document.getElementById("luAvgOpenPnl").textContent = hasSettledSample ? `${stats.avg_open_pnl > 0 ? '+' : ''}${stats.avg_open_pnl}%` : unavailable;
    document.getElementById("luAvgPeakPnl").textContent = hasSettledSample ? `${stats.avg_peak_pnl > 0 ? '+' : ''}${stats.avg_peak_pnl}%` : unavailable;
    document.getElementById("luAvgClosePnl").innerHTML = hasSettledSample ? formatPct(stats.avg_close_pnl) : unavailable;

    // 板型分布横幅渲染
    const typeObj = stats.by_board_type || stats.board_types || {};
    const typeItems = [];
    for (const [k, v] of Object.entries(typeObj)) {
      typeItems.push(`<span><strong style="color: #fff;">${k}</strong> (${v.count}条信号 · 次日最高涨幅≥2%占比 ${v.win_rate}% · 次日收盘均值 ${v.avg_return > 0 ? '+' : ''}${v.avg_return}%)</span>`);
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
      <div>正在按日线规则筛选主板涨停候选...</div>
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
          <div style="font-weight: 600; color: var(--text-muted); margin-bottom: 4px;">当前没有符合日线规则的候选</div>
          <div style="font-size: 12px;">此筛选无法验证封单、排队、封板时间或实际成交。</div>
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
              <span class="meta-label">日线技术规则分</span>
              <span class="meta-val" style="color: ${hardnessColor}; font-size: 16px;">${c.hardness_score} 分</span>
            </div>
            <div class="meta-item">
              <span class="meta-label">封成比 (无盘口数据)</span>
              <span class="meta-val text-gold">不可用</span>
            </div>
            <div class="meta-item">
              <span class="meta-label">封板时间</span>
              <span class="meta-val text-cyan">不可用</span>
            </div>
            <div class="meta-item">
              <span class="meta-label">成交额</span>
              <span class="meta-val">${c.turnover_amount != null ? (c.turnover_amount / 100000000.0).toFixed(2) + ' 亿元' : '不可用'}</span>
            </div>
          </div>

          <div style="margin-top: 12px; font-size: 12px; color: var(--text-muted);">
            <ul style="padding-left: 16px; margin: 0; line-height: 1.5;">
              ${reasonsHtml}
            </ul>
          </div>

          <!-- Signal limitations -->
          <div style="margin-top: 12px; background: rgba(15, 23, 42, 0.55); padding: 10px 12px; border-radius: 6px; border-left: 3px solid var(--c-gold); font-size: 12px;">
            <div style="font-weight: 600; color: var(--c-gold); margin-bottom: 5px; display: flex; justify-content: space-between; align-items: center;">
              <span>📖 数据限制</span>
              <span class="badge" style="background: rgba(245, 158, 11, 0.15); color: #fbbf24; font-size: 10px;">非交易指令</span>
            </div>
            <div style="color: var(--text-muted); line-height: 1.6;">
              日线规则分不是封板硬度。数据不含盘口队列、封板时间、炸板次数或可成交价格；次日行情回看不代表真实账户成交与收益。
            </div>
          </div>

          <div style="margin-top: 14px; display: flex; justify-content: flex-end; gap: 8px; flex-wrap: wrap;">
            <button class="btn btn-secondary btn-sm" onclick="pushLimitUpSignal('${c.symbol}', '${c.name}', ${c.price}, '${c.board_type}', ${c.hardness_score})">
              📢 推送候选信息
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
    tbody.innerHTML = `<tr><td colspan="14" style="text-align:center; color: var(--text-dim); padding: 25px;">暂无涨停候选信号记录</td></tr>`;
      return;
    }

    tbody.innerHTML = trades.map(t => {
      let statusBadge = `<span class="badge" style="background: rgba(99, 102, 241, 0.2); color: #818cf8;">⏳ 等待后续行情</span>`;
      if (t.status === "WIN") {
        statusBadge = `<span class="badge badge-up">📈 次日收盘上涨</span>`;
      } else if (t.status === "LOSS") {
        statusBadge = `<span class="badge badge-down">📉 次日收盘下跌</span>`;
      } else if (t.status === "FLAT") {
        statusBadge = `<span class="badge" style="background: rgba(148, 163, 184, 0.2); color: #94a3b8;">➖ 次日收盘持平</span>`;
      }

      const openPnlStr = t.next_open_pnl != null ? `${t.next_open_pnl > 0 ? '+' : ''}${t.next_open_pnl}%` : "--";
      const openPnlCls = t.next_open_pnl != null ? (t.next_open_pnl > 0 ? "text-up" : (t.next_open_pnl < 0 ? "text-down" : "")) : "";

      const peakPnlStr = t.next_high_pnl != null ? `+${t.next_high_pnl}%` : "--";
      const peakPnlCls = t.next_high_pnl != null ? (t.next_high_pnl >= 2.0 ? "text-up" : "") : "";

      const closePnlStr = t.next_close_pnl != null ? `${t.next_close_pnl > 0 ? '+' : ''}${t.next_close_pnl}%` : "--";
      const closePnlCls = t.next_close_pnl != null ? (t.next_close_pnl > 0 ? "text-up" : (t.next_close_pnl < 0 ? "text-down" : "")) : "";

      const consecutiveStr = t.is_consecutive ? `<span class="text-up" style="font-weight:700;">次日涨停</span>` : `<span style="color:var(--text-dim);">未继续涨停</span>`;
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
          <td><strong style="color: var(--c-gold);">${t.hardness_score ?? '不可用'}</strong> 分</td>
          <td>不可用</td>
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
    alert(`已将 ${json.added_count} 条候选信号写入跟踪台账。它们不代表真实成交。`);
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
    alert(`后续日线行情跟踪字段已更新。结果不代表真实成交或账户收益。`);
    await loadLimitUpData();
  } catch (e) {
    alert("结算失败: " + e);
  }
}

// 6. 导出 / 下载 CSV 电子表格
function exportLimitUpCsv() {
  window.open("/api/limitup/csv", "_blank");
}

// 7. 折叠 / 展开新手小白操盘指南
function toggleBeginnerGuide() {
  const body = document.getElementById("beginnerGuideBody");
  const btn = document.getElementById("btnToggleGuide");
  if (!body) return;
  if (body.style.display === "none") {
    body.style.display = "block";
    if (btn) btn.textContent = "▲ 收起风险说明";
  } else {
    body.style.display = "none";
    if (btn) btn.textContent = "▼ 展开风险说明";
  }
}

// 8. 推送单只候选信号摘要到钉钉/微信
async function pushLimitUpSignal(symbol, name, price, boardType, score) {
  try {
    const res = await fetch("/api/limitup/notify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        symbol: symbol,
        name: name,
        price: price,
        seal_price: price,
        board_type: boardType,
        hardness_score: score
      })
    });
    if (!res.ok) throw new Error("推送接口网络异常");
    alert(`已向钉钉/微信推送【${name} (${symbol})】候选信号摘要。请勿将其视为交易指令。`);
  } catch (e) {
    alert("推送失败: " + e);
  }
}


