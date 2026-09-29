/* Daily Validation Control -- pure, DOM-free decision logic (Step 22.9,
 * PAPER_TRADING_V1.4.8).
 *
 * Everything in this file is a pure function of an `OperatorStatusView`
 * (the JSON `GET /api/operator-status` already returns, unmodified by
 * this step) plus a small amount of client-only UI state (whether a
 * click is currently in flight). There is no fetch, no DOM access, and
 * no business logic that duplicates or re-derives anything the backend
 * itself decides -- the Risk Engine, Quant, the provider preflight, and
 * cycle-level idempotency all remain exactly as authoritative as they
 * already were; this file only decides how the *button* should look and
 * whether it should be clickable, in a way that fails closed whenever
 * the backend's own state doesn't clearly allow a run.
 *
 * Loaded via a plain <script> tag in index.html BEFORE dashboard.js (no
 * bundler, no module system, matching this dashboard's existing
 * architecture). Every helper dashboard.js needs is ALSO assigned
 * explicitly onto `window` near the bottom of this file -- PAPER_TRADING
 * V1.5.2, Step 2A, added that after a real-browser acceptance run showed
 * dashboard.js could not resolve `marketSessionLabel` even though this
 * file loads first; see that block's own comment and
 * `tests/frontend/operator_control_browser_scope.test.js` for why an
 * explicit export is now the browser path's contract rather than an
 * implicit one. The separate `module.exports` guard at the bottom is
 * inert in a browser (`module` is undefined there) and lets
 * `tests/frontend/operator_control.test.js` `require()` this exact file
 * under Node with zero DOM/browser shims -- the same file, the same
 * logic, tested directly rather than re-implemented for the test.
 */

const CYCLE_STATE = Object.freeze({
  NOT_RUN: "NOT_RUN",
  READY: "READY",
  RUNNING: "RUNNING",
  COMPLETE: "COMPLETE",
  DEGRADED: "DEGRADED",
  HALTED: "HALTED",
  ERROR: "ERROR",
});

// A nontechnical-friendly label for each CYCLE_STATE value.
const CYCLE_STATE_LABEL = Object.freeze({
  NOT_RUN: "Not run yet today",
  READY: "Ready to run",
  RUNNING: "Running…",
  COMPLETE: "Today's validation complete",
  DEGRADED: "Completed with degraded data",
  HALTED: "Halted",
  ERROR: "Error",
});

/**
 * Derives today's cycle state for display. `clientRunning` is true only
 * for the brief window between a confirmed click and that request's
 * response landing -- it never substitutes for, or outlives, the
 * backend's own `today_cycle_ran`/`today_cycle_degraded`/
 * `today_cycle_halted` facts, which always win once available.
 */
function deriveCycleState(status, clientRunning) {
  if (clientRunning) return CYCLE_STATE.RUNNING;
  if (!status || !status.configured) return CYCLE_STATE.ERROR;
  if (!status.today_cycle_ran) {
    return status.provider && status.provider.ready ? CYCLE_STATE.READY : CYCLE_STATE.NOT_RUN;
  }
  if (status.today_cycle_halted) return CYCLE_STATE.HALTED;
  if (status.today_cycle_degraded) return CYCLE_STATE.DEGRADED;
  if (status.today_cycle_errors && status.today_cycle_errors.length > 0) return CYCLE_STATE.ERROR;
  return CYCLE_STATE.COMPLETE;
}

/**
 * Fail-closed run-button state: disabled unless the backend's own
 * status says configured, not-yet-run-today, market-session-allowed,
 * AND provider-ready. Any missing/unexpected shape in `status` disables
 * the button rather than guessing -- there is no path here that can
 * produce `disabled: false` from incomplete information.
 *
 * The market-session check (Step 2, PAPER_TRADING_V1.5.1) reads only
 * `status.validation_cycle_allowed`/`status.validation_cycle_block_reason`
 * -- both already decided by the backend's own
 * `src.portfolio.market_session.evaluate_validation_cycle_eligibility`.
 * This function never computes today's date, the current time, or
 * whether the market is open on its own -- the backend remains the
 * sole authority on market hours, exactly like it already is for the
 * provider/cycle-idempotency checks below.
 */
function runButtonState(status, clientRunning) {
  const cycleState = deriveCycleState(status, clientRunning);
  if (clientRunning) {
    return { disabled: true, reason: "Today's validation cycle is running.", cycleState };
  }
  if (!status || !status.configured) {
    return { disabled: true, reason: "Operator status is not available yet.", cycleState };
  }
  if (status.today_cycle_ran) {
    return { disabled: true, reason: "Today's validation cycle has already completed.", cycleState };
  }
  if (!status.validation_cycle_allowed) {
    const reason = status.validation_cycle_block_reason || "Market is not in the regular validation window.";
    return { disabled: true, reason, cycleState };
  }
  if (!status.provider || !status.provider.ready) {
    const detail = status.provider && status.provider.detail ? status.provider.detail : "Market data provider is not ready.";
    return { disabled: true, reason: detail, cycleState };
  }
  return { disabled: false, reason: "", cycleState };
}

// A nontechnical-friendly label for each backend-reported
// `market_session_state` value (Step 2). Reads only the backend's own
// string -- never independently computed.
const MARKET_SESSION_LABEL = Object.freeze({
  MARKET_CLOSED: "MARKET CLOSED",
  PRE_MARKET: "PRE-MARKET",
  REGULAR_MARKET: "REGULAR SESSION",
  POST_MARKET: "POST-MARKET",
});

function marketSessionLabel(status) {
  if (!status || !status.market_session_state) return "UNKNOWN";
  return MARKET_SESSION_LABEL[status.market_session_state] || status.market_session_state;
}

function systemHealthLabel(status) {
  if (!status || !status.configured) return "UNKNOWN";
  if (status.today_cycle_halted) return "HALTED";
  if (status.today_cycle_degraded) return "DEGRADED";
  return "NORMAL";
}

/**
 * Validation-progress math for the days-elapsed/completed-trades bars.
 * Returns null when the backend hasn't reported a duration at all
 * (unconfigured) -- callers should render an empty state, never a 0%
 * bar that implies data exists when it doesn't.
 */
function validationProgress(status) {
  if (!status || status.validation_duration_days == null) return null;
  const daysElapsed = status.validation_days_recorded || 0;
  const totalDays = status.validation_duration_days;
  const minTrades = status.validation_minimum_completed_trades;
  const preferredTrades = status.validation_preferred_completed_trades;
  const trades = status.validation_completed_trades || 0;
  return {
    daysElapsed,
    totalDays,
    dayPct: totalDays > 0 ? Math.min(1, daysElapsed / totalDays) : 0,
    trades,
    minTrades,
    preferredTrades,
    minTradePct: minTrades > 0 ? Math.min(1, trades / minTrades) : 0,
  };
}

/**
 * A tiny, explicit in-flight guard: exactly one validation-cycle POST
 * may be outstanding at a time from this page. `beginRun()` refuses
 * (returns false) if one is already in flight -- this is the
 * structural double-click / duplicate-submission guard, independent of
 * any real network call or DOM state, so it can be exercised directly
 * under Node.
 */
function createRunGuard() {
  let inFlight = false;
  return {
    beginRun() {
      if (inFlight) return false;
      inFlight = true;
      return true;
    },
    endRun() {
      inFlight = false;
    },
    isInFlight() {
      return inFlight;
    },
  };
}

const CONFIRM_RUN_MESSAGE =
  "Run today's PAPER validation cycle?\n\n" +
  "This scans Tradier production market data and evaluates existing " +
  "PaperBroker positions/opportunities.\n\n" +
  "It cannot automatically open a new position.\n\n" +
  "Any new-position candidate requires separate human review.";

// PAPER_TRADING_V1.5.2, Step 2A: explicit browser-global export.
//
// The comment above claimed classic-script global-scope sharing alone
// makes every const/function here an ordinary global dashboard.js can
// reference -- a real-browser (Safari) acceptance run showed
// `marketSessionLabel` undefined in dashboard.js's execution context
// despite that, and `tests/frontend/operator_control.test.js`'s
// require()-based tests could not have caught it: require() exercises
// only the module.exports object below, a completely separate,
// CommonJS-specific contract that has nothing to do with how a real
// `<script>`-loaded dashboard.js actually resolves these names. There
// was no explicit, verified contract for the browser path at all --
// only an implicit one.
//
// This block gives the browser path the same explicit guarantee the
// module.exports guard already gives Node: every helper dashboard.js
// depends on becomes a literal property of the global object, so
// resolving it never depends on an unverified assumption about
// cross-script lexical scoping in any particular engine or version.
// See tests/frontend/operator_control_browser_scope.test.js for the
// regression proof (loads this file then dashboard.js into one shared
// browser-like global scope, the same order index.html uses).
// --- BEGIN EXPLICIT BROWSER EXPORT ---
if (typeof window !== "undefined") {
  window.CYCLE_STATE = CYCLE_STATE;
  window.CYCLE_STATE_LABEL = CYCLE_STATE_LABEL;
  window.deriveCycleState = deriveCycleState;
  window.runButtonState = runButtonState;
  window.MARKET_SESSION_LABEL = MARKET_SESSION_LABEL;
  window.marketSessionLabel = marketSessionLabel;
  window.systemHealthLabel = systemHealthLabel;
  window.validationProgress = validationProgress;
  window.createRunGuard = createRunGuard;
  window.CONFIRM_RUN_MESSAGE = CONFIRM_RUN_MESSAGE;
}
// --- END EXPLICIT BROWSER EXPORT ---

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    CYCLE_STATE,
    CYCLE_STATE_LABEL,
    deriveCycleState,
    runButtonState,
    marketSessionLabel,
    systemHealthLabel,
    validationProgress,
    createRunGuard,
    CONFIRM_RUN_MESSAGE,
  };
}
