/* Fidelity Human-Execution Dashboard — frontend (Step 18).
 *
 * This file only ever calls the five allowed-action endpoints
 * (refresh / copy / mark-order-entered / fill / cancel / reject) plus
 * read-only GETs. There is no code here, and no button anywhere in the
 * rendered page, for AUTO TRADE, EXECUTE, or SEND TO FIDELITY — see
 * README notes in src/dashboard/app.py for the server-side half of that
 * guarantee.
 */

const API = "";

function fmtMoney(v) {
  if (v === null || v === undefined) return "—";
  const sign = v < 0 ? "-" : "";
  return `${sign}$${Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}
function fmtPct(v) {
  if (v === null || v === undefined) return "—";
  return `${(v * 100).toFixed(1)}%`;
}
function fmtNum(v, digits = 2) {
  if (v === null || v === undefined) return "—";
  return Number(v).toFixed(digits);
}
function esc(s) {
  const div = document.createElement("div");
  div.textContent = s ?? "";
  return div.innerHTML;
}

async function api(path, options = {}) {
  const res = await fetch(API + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body.detail || `request failed (${res.status})`);
  }
  return body;
}

// ------------------------------------------------------------- top-level

async function loadAll() {
  document.getElementById("clock").textContent = new Date().toLocaleString();
  try {
    const [providerHealth, header, risk, wheels, lifecycle, opps, audit] = await Promise.all([
      api("/api/data-provider-health"),
      api("/api/portfolio-header"),
      api("/api/risk-panel"),
      api("/api/wheels"),
      api("/api/lifecycle"),
      api("/api/opportunities"),
      api("/api/audit"),
    ]);
    renderDataProviderHealth(providerHealth);
    renderPortfolioHeader(header);
    renderRiskPanel(risk);
    renderWheels(wheels);
    renderLifecycle(lifecycle);
    renderOpportunities(opps);
    renderAudit(audit);
  } catch (err) {
    console.error(err);
  }
}

// ------------------------------------------------- data provider health

function renderDataProviderHealth(p) {
  const badge = document.getElementById("data-provider-badge");
  badge.textContent = p.connection_status.replace(/_/g, " ");
  badge.className = `badge ${p.connection_status}`;

  const feedLabel = (feed) => {
    if (!feed) return "unavailable";
    if (feed.endsWith("_opra")) return "OPRA (real)";
    if (feed.endsWith("_indicative")) return "indicative (free/delayed)";
    if (feed === "mock") return "mock (synthetic)";
    return feed;
  };

  document.getElementById("data-provider-grid").innerHTML = [
    statTile("Provider", p.provider_selected.toUpperCase()),
    statTile("Authenticated", p.authenticated === null ? "n/a" : p.authenticated ? "yes" : "no", p.authenticated === false ? "neg" : ""),
    statTile("Equity Data", p.equity_data_available ? "available" : "unavailable", p.equity_data_available ? "" : "neg"),
    statTile("Options Data", p.options_data_available ? "available" : "unavailable", p.options_data_available ? "" : "neg"),
    statTile("Options Feed", feedLabel(p.options_feed_type), p.opra_entitled === false ? "neg" : ""),
    statTile("Market", p.market_open ? "OPEN" : "CLOSED", p.market_open ? "" : "na"),
    statTile("Last Fetch", p.last_successful_fetch_at ? new Date(p.last_successful_fetch_at).toLocaleTimeString() : "never"),
  ].join("");
}

// ------------------------------------------------------- portfolio header

function statTile(label, value, cls = "") {
  return `<div class="stat"><div class="label">${esc(label)}</div><div class="value ${cls}">${value}</div></div>`;
}

function renderPortfolioHeader(h) {
  const el = document.getElementById("portfolio-header-grid");
  const pnlCls = h.daily_pnl === null ? "na" : h.daily_pnl >= 0 ? "pos" : "neg";
  const ytdCls = h.ytd_return_pct === null ? "na" : h.ytd_return_pct >= 0 ? "pos" : "neg";
  el.innerHTML = [
    statTile("NAV", fmtMoney(h.nav)),
    statTile("Daily P&L", h.daily_pnl === null ? "not tracked" : fmtMoney(h.daily_pnl), pnlCls),
    statTile("YTD Return", h.ytd_return_pct === null ? "not tracked" : fmtPct(h.ytd_return_pct), ytdCls),
    statTile("Cash", fmtMoney(h.cash)),
    statTile("Capital Deployed", fmtPct(h.capital_deployed_pct)),
    statTile("Current Drawdown", fmtPct(h.current_drawdown_pct), h.current_drawdown_pct > 0 ? "neg" : ""),
    statTile("Portfolio Delta", h.portfolio_delta === null ? "not tracked" : fmtNum(h.portfolio_delta), h.portfolio_delta === null ? "na" : ""),
    statTile("Portfolio Theta", h.portfolio_theta === null ? "not tracked" : fmtNum(h.portfolio_theta), h.portfolio_theta === null ? "na" : ""),
    statTile("Portfolio Vega", h.portfolio_vega === null ? "not tracked" : fmtNum(h.portfolio_vega), h.portfolio_vega === null ? "na" : ""),
  ].join("");
}

// ------------------------------------------------------------------ wheels
// RESEARCH / PAPER only -- read-only. No button here submits, closes, or
// rolls anything; a Wheel's CSP/CC legs are opened/closed exclusively
// through the same Risk-Engine-gated PaperBroker/Fidelity paths every
// other strategy uses, never from this dashboard.

function renderWheels(wheels) {
  const section = document.getElementById("wheels-section");
  const container = document.getElementById("wheels-list");
  if (!wheels.length) {
    section.style.display = "none";
    return;
  }
  section.style.display = "";
  container.innerHTML = wheels.map(renderWheelCard).join("");
}

function renderWheelCard(w) {
  const pnlCls = w.total_net_pnl >= 0 ? "pos" : "neg";
  const activeCsp = w.active_csp
    ? `CSP $${fmtNum(w.active_csp.strike)} exp ${w.active_csp.expiration} x${w.active_csp.contracts} @ $${fmtNum(w.active_csp.premium_received_per_share)}`
    : "none";
  const activeCc = w.active_cc
    ? `CC $${fmtNum(w.active_cc.strike)} exp ${w.active_cc.expiration} x${w.active_cc.contracts} @ $${fmtNum(w.active_cc.premium_received_per_share)}`
    : "none";
  return `
    <div class="wheel-card">
      <div class="wheel-card-header">
        <span class="badge wheel-state">${esc(w.state.replace(/_/g, " "))}</span>
        <strong>${esc(w.ticker)}</strong>
        <span class="wheel-id">RESEARCH / PAPER &middot; wheel_id: ${esc(w.wheel_id)}</span>
      </div>
      <div class="stat-grid">
        ${statTile("Shares Owned", w.shares_owned)}
        ${statTile("Acquisition Basis", w.acquisition_basis_per_share === null ? "n/a" : fmtMoney(w.acquisition_basis_per_share))}
        ${statTile("Economic Basis", w.economic_basis_per_share === null ? "n/a" : fmtMoney(w.economic_basis_per_share))}
        ${statTile("Current Price", w.current_underlying_price === null ? "n/a" : fmtMoney(w.current_underlying_price))}
        ${statTile("Unrealized Stock P&L", fmtMoney(w.unrealized_stock_pnl), w.unrealized_stock_pnl >= 0 ? "pos" : "neg")}
        ${statTile("Premium Collected", fmtMoney(w.total_premium_collected))}
        ${statTile("Total Net P&L", fmtMoney(w.total_net_pnl), pnlCls)}
        ${statTile("Capital Committed", fmtMoney(w.capital_committed))}
        ${statTile("Return on Capital", w.return_on_committed_capital === null ? "n/a" : fmtPct(w.return_on_committed_capital))}
        ${statTile("Days Active", w.days_active)}
        ${statTile("CSP / CC Cycles", `${w.csp_cycle_count} / ${w.cc_cycle_count}`)}
        ${statTile("Risk Status", w.risk_status, w.risk_status === "normal" ? "" : "neg")}
      </div>
      <div class="wheel-active-legs">Active CSP: ${esc(activeCsp)} &nbsp;|&nbsp; Active CC: ${esc(activeCc)}</div>
      <div class="wheel-next-decision">${esc(w.next_decision)}</div>
    </div>`;
}

// ---------------------------------------------------- lifecycle positions
// RESEARCH / PAPER only -- read-only. No button here closes, rolls, or
// adjusts anything; a lifecycle action is generated only through the same
// Risk-Engine-gated PaperBroker/Fidelity paths every other strategy uses
// (src.lifecycle.paper_events/fidelity_events), never from this dashboard.

const LIFECYCLE_STATUS_LABELS = {
  hold: "HOLD",
  profit_target: "PROFIT TARGET",
  loss_review: "LOSS REVIEW",
  time_exit: "TIME EXIT",
  delta_review: "DELTA REVIEW",
  volatility_review: "VOLATILITY REVIEW",
  event_risk: "EVENT RISK",
  liquidity_warning: "LIQUIDITY WARNING",
  risk_exit: "RISK EXIT",
  data_insufficient: "DATA INSUFFICIENT",
  regime_review: "REGIME REVIEW",
  assignment_review: "ASSIGNMENT REVIEW",
};
const LIFECYCLE_STATUS_NEGATIVE = new Set([
  "loss_review", "risk_exit", "data_insufficient", "liquidity_warning", "event_risk",
]);

function renderLifecycle(positions) {
  const section = document.getElementById("lifecycle-section");
  const container = document.getElementById("lifecycle-list");
  if (!positions.length) {
    section.style.display = "none";
    return;
  }
  section.style.display = "";
  container.innerHTML = positions.map(renderLifecycleCard).join("");
}

function renderLifecycleCard(p) {
  const pnlCls = p.unrealized_pnl >= 0 ? "pos" : "neg";
  const statusLabel = LIFECYCLE_STATUS_LABELS[p.status_indicator] || p.status_indicator;
  const statusCls = LIFECYCLE_STATUS_NEGATIVE.has(p.status_indicator) ? "neg" : "";
  return `
    <div class="wheel-card">
      <div class="wheel-card-header">
        <span class="badge wheel-state ${statusCls}">${esc(statusLabel)}</span>
        <strong>${esc(p.ticker)}</strong>
        <span class="wheel-id">RESEARCH / PAPER &middot; ${esc(p.strategy.replace(/_/g, " "))} &middot; ${esc(p.management_policy)} &middot; trade_id: ${esc(p.trade_id)}</span>
      </div>
      <div class="stat-grid">
        ${statTile("Lifecycle State", p.current_state.replace(/_/g, " "))}
        ${statTile("Entry Date", p.entry_date)}
        ${statTile("Current DTE", p.current_dte === null ? "n/a" : p.current_dte)}
        ${statTile("Unrealized P&L", fmtMoney(p.unrealized_pnl), pnlCls)}
        ${statTile("Unrealized P&L %", p.unrealized_pnl_pct === null ? "n/a" : fmtPct(p.unrealized_pnl_pct))}
        ${statTile("MFE", fmtMoney(p.mfe))}
        ${statTile("MAE", fmtMoney(p.mae))}
        ${statTile("Delta", p.delta === null ? "n/a" : fmtNum(p.delta))}
        ${statTile("Next Review DTE", p.next_scheduled_review_dte === null ? "n/a" : p.next_scheduled_review_dte)}
        ${statTile("Risk Status", p.risk_status, p.risk_status === "ok" ? "" : "neg")}
        ${statTile("Data Freshness", p.data_is_fresh ? "fresh" : "STALE", p.data_is_fresh ? "" : "neg")}
      </div>
      <div class="wheel-next-decision">${esc(p.recommended_action)}</div>
    </div>`;
}

// -------------------------------------------------------------- risk panel

function renderRiskPanel(r) {
  const badge = document.getElementById("risk-state-badge");
  badge.textContent = r.state;
  badge.className = `badge ${r.state}`;
  document.getElementById("risk-state-reason").textContent = r.state_reason;

  document.getElementById("risk-panel-grid").innerHTML = [
    statTile("Capital Utilization", fmtPct(r.capital_utilization_pct)),
    statTile("Cash Reserve", fmtPct(r.cash_reserve_pct)),
    statTile("Drawdown Zone", r.drawdown_zone.toUpperCase()),
    statTile("Current Drawdown", fmtPct(r.current_drawdown_pct)),
  ].join("");

  const barList = (items, limitKey) =>
    items.length === 0
      ? '<li class="empty">no open positions</li>'
      : items.map((e) => `<li><span>${esc(e.label)}</span><span class="${e.breached ? "breached" : ""}">${fmtPct(e.exposure_pct)} / ${fmtPct(e.limit_pct)} limit</span></li>`).join("");

  document.getElementById("underlying-concentration").innerHTML = barList(r.underlying_concentration);
  document.getElementById("sector-concentration").innerHTML = barList(r.sector_concentration);

  const corrEl = document.getElementById("correlation-clusters");
  if (!r.correlation_tracked) {
    corrEl.innerHTML = '<li class="empty">not tracked (no price history for 2+ held tickers)</li>';
  } else if (r.correlation_clusters.length === 0) {
    corrEl.innerHTML = '<li class="empty">no highly-correlated pairs</li>';
  } else {
    corrEl.innerHTML = r.correlation_clusters
      .map((c) => `<li><span>${esc(c.tickers[0])} / ${esc(c.tickers[1])}</span><span>${fmtNum(c.correlation)}</span></li>`)
      .join("");
  }
}

// ------------------------------------------------------------ opportunities

function legLine(leg) {
  const action = leg.action.replace(/_/g, " ").toUpperCase();
  const right = leg.put_call === "P" ? "PUT" : "CALL";
  return `${action}: ${leg.contracts} × ${fmtNum(leg.strike, leg.strike % 1 === 0 ? 0 : 2)} ${right}`;
}

function statusLabel(status) {
  return status.replace(/_/g, " ").toUpperCase();
}

function renderOpportunities(opps) {
  const el = document.getElementById("opportunities-list");
  if (opps.length === 0) {
    el.innerHTML = '<div class="empty">No opportunities loaded. Cash is a valid position.</div>';
    return;
  }
  el.innerHTML = opps.map(renderOpportunityCard).join("");
}

function renderOpportunityCard(o) {
  const isReprice = o.status === "reprice_required";
  const da = o.devils_advocate;
  const rd = o.risk_engine_decision;

  const daBlock = da
    ? `<div class="da-box"><strong>Devil's Advocate — ${esc(da.verdict)}:</strong> ${esc(da.why_not_thesis)}</div>`
    : `<div class="da-box empty">no Devil's Advocate review on record</div>`;

  const rdBlock = rd
    ? `<div class="risk-box ${rd.decision === "reject" || rd.decision === "halt" ? "reject" : ""}"><strong>Risk Engine — ${esc(rd.decision.toUpperCase())}:</strong> ${esc(rd.message)}</div>`
    : `<div class="risk-box empty">no Risk Engine decision on record</div>`;

  const prob = o.probability_metrics;
  const probBlock = prob
    ? `POP ${fmtPct(prob.probability_of_profit)} · EV ${fmtMoney(prob.expected_value)} · Ann. ROC ${fmtPct(prob.annualized_roc)}`
    : "not available";

  return `
  <div class="opp-card ${isReprice ? "reprice" : ""}" data-trade-id="${esc(o.trade_id)}">
    <div class="opp-header">
      <div>
        <div class="opp-title">${esc(o.ticker)} — ${esc(o.strategy)}</div>
        <div class="opp-sub">Exp ${esc(o.expiration)} (${o.dte} DTE) · ${o.contracts} contract(s)</div>
      </div>
      <span class="status-pill ${o.status}">${statusLabel(o.status)}</span>
    </div>

    ${isReprice ? '<div class="reprice-banner">REPRICE REQUIRED — market data is stale. COPY FIDELITY ORDER is disabled until a refresh succeeds.</div>' : ""}

    <div class="opp-grid">
      <div><div class="k">Legs</div><div class="v">${o.legs.map(legLine).join("<br>")}</div></div>
      <div><div class="k">Underlying</div><div class="v">${fmtMoney(o.quote.underlying_price)}</div></div>
      <div><div class="k">Net Bid / Ask</div><div class="v">${fmtMoney(o.quote.net_bid)} / ${fmtMoney(o.quote.net_ask)}</div></div>
      <div><div class="k">Midpoint</div><div class="v">${fmtMoney(o.quote.net_mid)}</div></div>
      <div><div class="k">Target Limit</div><div class="v">${fmtMoney(o.target_limit)}</div></div>
      <div><div class="k">${esc(o.price_guard_label)}</div><div class="v">${fmtMoney(o.minimum_acceptable_price)}</div></div>
      <div><div class="k">Max Profit</div><div class="v">${fmtMoney(o.max_profit)}</div></div>
      <div><div class="k">Max Loss</div><div class="v">${fmtMoney(o.max_loss)}</div></div>
      <div><div class="k">${o.breakeven_upper != null ? "Breakeven (Lower)" : "Breakeven"}</div><div class="v">${fmtMoney(o.breakeven)}</div></div>
      ${o.breakeven_upper != null ? `<div><div class="k">Breakeven (Upper)</div><div class="v">${fmtMoney(o.breakeven_upper)}</div></div>` : ""}
      <div><div class="k">Capital Required</div><div class="v">${fmtMoney(o.capital_required)}</div></div>
      <div><div class="k">ROC</div><div class="v">${fmtPct(o.return_on_capital)}</div></div>
      <div><div class="k">Probability</div><div class="v">${probBlock}</div></div>
    </div>

    <div class="thesis-box"><strong>Thesis:</strong> ${esc(o.thesis)}</div>
    ${daBlock}
    ${rdBlock}

    <div class="detail hidden" id="detail-${esc(o.trade_id)}"></div>

    <div class="btn-row">
      <button class="btn" onclick="onRefresh('${esc(o.trade_id)}')">REFRESH PRICE</button>
      <button class="btn" onclick="onViewAnalysis('${esc(o.trade_id)}')">VIEW ANALYSIS</button>
      <button class="btn btn-primary" ${o.copy_enabled ? "" : "disabled"} onclick="onCopy('${esc(o.trade_id)}')">COPY FIDELITY ORDER</button>
      ${renderStatusButtons(o)}
    </div>
  </div>`;
}

function renderStatusButtons(o) {
  if (o.status === "awaiting_human" || o.status === "reprice_required") {
    return `
      <button class="btn btn-success" onclick="onMarkOrderEntered('${esc(o.trade_id)}')">MARK ORDER ENTERED</button>
      <button class="btn btn-danger" onclick="onRejectTrade('${esc(o.trade_id)}')">REJECT TRADE</button>`;
  }
  if (o.status === "order_entered" || o.status === "partially_filled") {
    return `
      <button class="btn btn-success" onclick="onRecordFill('${esc(o.trade_id)}', 'FILLED')">FILLED</button>
      <button class="btn" onclick="onRecordFill('${esc(o.trade_id)}', 'PARTIALLY_FILLED')">PARTIALLY FILLED</button>
      <button class="btn btn-danger" onclick="onCancelOrder('${esc(o.trade_id)}')">CANCELLED</button>`;
  }
  return "";
}

function onViewAnalysis(tradeId) {
  const el = document.getElementById(`detail-${tradeId}`);
  const wasHidden = el.classList.contains("hidden");
  el.classList.toggle("hidden");
  if (!wasHidden) return;
  api(`/api/opportunities/${tradeId}`).then((o) => {
    const da = o.devils_advocate;
    let riskAssessmentRows = "";
    if (da) {
      riskAssessmentRows = da.risk_assessment
        .filter((r) => r.applicable)
        .map((r) => `<tr><td>${esc(r.category)}</td><td>${esc(r.note)}</td></tr>`)
        .join("");
      if (!riskAssessmentRows) riskAssessmentRows = '<tr><td colspan="2" class="empty">no applicable risk categories flagged</td></tr>';
    }
    const scenarios = da
      ? da.failure_scenarios.map((f) => `<li><strong>${esc(f.scenario)}</strong> — probability: ${esc(f.probability_category)}, severity: ${esc(f.severity)}</li>`).join("")
      : "";
    el.innerHTML = `
      <h3>Applicable risk categories</h3>
      <table><tbody>${riskAssessmentRows || '<tr><td class="empty">no Devil\'s Advocate review</td></tr>'}</tbody></table>
      <h3>Failure scenarios</h3>
      <ul class="bar-list">${scenarios || '<li class="empty">none on record</li>'}</ul>
      <h3>Reason codes</h3>
      <div class="muted">${o.risk_engine_decision ? o.risk_engine_decision.reason_codes.join(", ") : "none"}</div>
    `;
  });
}

// ---------------------------------------------------------------- actions

function openModal(html) {
  document.getElementById("modal").innerHTML = html;
  document.getElementById("modal-backdrop").classList.remove("hidden");
}
function closeModal() {
  document.getElementById("modal-backdrop").classList.add("hidden");
  document.getElementById("modal").innerHTML = "";
}
function modalError(msg) {
  const el = document.getElementById("modal-error");
  if (el) el.textContent = msg;
}

async function onCopy(tradeId) {
  try {
    const { text } = await api(`/api/opportunities/${tradeId}/copy`, { method: "POST" });
    openModal(`
      <h3>Fidelity Order — ready to copy</h3>
      <div class="ticket-text" id="ticket-text">${esc(text)}</div>
      <div class="modal-actions">
        <button class="btn" onclick="closeModal()">Close</button>
        <button class="btn btn-primary" onclick="copyTicketText()">Copy to Clipboard</button>
      </div>
    `);
    window._ticketText = text;
    loadAll();
  } catch (err) {
    alert(err.message);
  }
}
function copyTicketText() {
  if (navigator.clipboard && window._ticketText) {
    navigator.clipboard.writeText(window._ticketText).catch(() => {});
  }
}

function onRefresh(tradeId) {
  openModal(`
    <h3>Refresh Price</h3>
    <p class="muted">Enter the current quote — this dashboard has no live market-data feed of its own; a human (or a future data connector) supplies it here.</p>
    <label>Underlying price</label><input id="rf-underlying" type="number" step="0.01">
    <label>Underlying bid</label><input id="rf-ubid" type="number" step="0.01">
    <label>Underlying ask</label><input id="rf-uask" type="number" step="0.01">
    <label>Leg quotes (strike,right,bid,ask,volume,oi[,iv]) — one per line</label>
    <textarea id="rf-legs" placeholder="620,P,1.28,1.34,500,1000,0.22&#10;615,P,0.58,0.64,400,800,0.20"></textarea>
    <div id="modal-error" class="error-text"></div>
    <div class="modal-actions">
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" onclick="submitRefresh('${esc(tradeId)}')">Refresh &amp; Re-run Risk Engine</button>
    </div>
  `);
}
async function submitRefresh(tradeId) {
  try {
    const legLines = document.getElementById("rf-legs").value.trim().split("\n").filter(Boolean);
    const leg_quotes = legLines.map((line) => {
      const [strike, right, bid, ask, volume, open_interest, iv] = line.split(",").map((s) => s.trim());
      return {
        strike: parseFloat(strike), right, bid: parseFloat(bid), ask: parseFloat(ask),
        volume: parseInt(volume, 10), open_interest: parseInt(open_interest, 10),
        iv: iv ? parseFloat(iv) : null,
      };
    });
    await api(`/api/opportunities/${tradeId}/refresh`, {
      method: "POST",
      body: JSON.stringify({
        underlying_price: parseFloat(document.getElementById("rf-underlying").value),
        underlying_bid: parseFloat(document.getElementById("rf-ubid").value),
        underlying_ask: parseFloat(document.getElementById("rf-uask").value),
        quote_timestamp: new Date().toISOString(),
        leg_quotes,
      }),
    });
    closeModal();
    loadAll();
  } catch (err) {
    modalError(err.message);
  }
}

function onMarkOrderEntered(tradeId) {
  openModal(`
    <h3>Mark Order Entered</h3>
    <label>Actual Fidelity limit entered</label><input id="oe-limit" type="number" step="0.01">
    <label>Contracts</label><input id="oe-contracts" type="number" step="1">
    <label>Entered by</label><input id="oe-by" type="text">
    <div id="modal-error" class="error-text"></div>
    <div class="modal-actions">
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" onclick="submitOrderEntered('${esc(tradeId)}')">Confirm</button>
    </div>
  `);
}
async function submitOrderEntered(tradeId) {
  try {
    await api(`/api/opportunities/${tradeId}/mark-order-entered`, {
      method: "POST",
      body: JSON.stringify({
        actual_limit_entered: parseFloat(document.getElementById("oe-limit").value),
        contracts: parseInt(document.getElementById("oe-contracts").value, 10),
        entered_by: document.getElementById("oe-by").value,
      }),
    });
    closeModal();
    loadAll();
  } catch (err) {
    modalError(err.message);
  }
}

function onRecordFill(tradeId, status) {
  openModal(`
    <h3>${status === "FILLED" ? "Filled" : "Partially Filled"}</h3>
    <label>Actual fill price</label><input id="fl-price" type="number" step="0.01">
    <label>Contracts filled</label><input id="fl-contracts" type="number" step="1">
    <label>Confirmed by</label><input id="fl-by" type="text">
    <div id="modal-error" class="error-text"></div>
    <div class="modal-actions">
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" onclick="submitFill('${esc(tradeId)}', '${status}')">Confirm</button>
    </div>
  `);
}
async function submitFill(tradeId, status) {
  try {
    await api(`/api/opportunities/${tradeId}/fill`, {
      method: "POST",
      body: JSON.stringify({
        status,
        fill_price: parseFloat(document.getElementById("fl-price").value),
        contracts_filled: parseInt(document.getElementById("fl-contracts").value, 10),
        confirmed_by: document.getElementById("fl-by").value,
      }),
    });
    closeModal();
    loadAll();
  } catch (err) {
    modalError(err.message);
  }
}

function onCancelOrder(tradeId) {
  openModal(`
    <h3>Cancel Order</h3>
    <label>Reason</label><textarea id="cx-reason"></textarea>
    <label>Actor</label><input id="cx-actor" type="text">
    <div id="modal-error" class="error-text"></div>
    <div class="modal-actions">
      <button class="btn" onclick="closeModal()">Back</button>
      <button class="btn btn-danger" onclick="submitCancel('${esc(tradeId)}')">Cancel Order</button>
    </div>
  `);
}
async function submitCancel(tradeId) {
  try {
    await api(`/api/opportunities/${tradeId}/cancel`, {
      method: "POST",
      body: JSON.stringify({ reason: document.getElementById("cx-reason").value, actor: document.getElementById("cx-actor").value }),
    });
    closeModal();
    loadAll();
  } catch (err) {
    modalError(err.message);
  }
}

function onRejectTrade(tradeId) {
  openModal(`
    <h3>Reject Trade</h3>
    <label>Reason</label><textarea id="rj-reason"></textarea>
    <label>Actor</label><input id="rj-actor" type="text">
    <div id="modal-error" class="error-text"></div>
    <div class="modal-actions">
      <button class="btn" onclick="closeModal()">Back</button>
      <button class="btn btn-danger" onclick="submitReject('${esc(tradeId)}')">Reject Trade</button>
    </div>
  `);
}
async function submitReject(tradeId) {
  try {
    await api(`/api/opportunities/${tradeId}/reject`, {
      method: "POST",
      body: JSON.stringify({ reason: document.getElementById("rj-reason").value, actor: document.getElementById("rj-actor").value }),
    });
    closeModal();
    loadAll();
  } catch (err) {
    modalError(err.message);
  }
}

// ------------------------------------------------------------------ audit

function renderAudit(events) {
  const tbody = document.getElementById("audit-tbody");
  if (events.length === 0) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty">no events recorded yet</td></tr>';
    return;
  }
  tbody.innerHTML = events
    .slice()
    .reverse()
    .map(
      (e) => `<tr>
        <td>${new Date(e.at).toLocaleString()}</td>
        <td>${esc(e.trade_id ? e.trade_id.slice(0, 8) : "—")}</td>
        <td>${esc(e.event_type)}</td>
        <td>${esc(e.actor)}</td>
        <td>${esc(e.detail)}</td>
      </tr>`
    )
    .join("");
}

document.getElementById("refresh-all-btn").addEventListener("click", loadAll);
document.getElementById("modal-backdrop").addEventListener("click", (e) => {
  if (e.target.id === "modal-backdrop") closeModal();
});

loadAll();
setInterval(loadAll, 30000);
