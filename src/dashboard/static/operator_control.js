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
 * Loaded via a plain <script> tag in index.html BEFORE dashboard.js, so
 * every function/const here becomes an ordinary global in the browser
 * (no bundler, no module system, matching this dashboard's existing
 * architecture). The `module.exports` guard at the bottom is inert in a
 * browser (`module` is undefined there) and lets
 * `tests/frontend/operator_control.test.mjs` `require()` this exact
 * file under Node with zero DOM/browser shims -- the same file, the
 * same logic, tested directly rather than re-implemented for the test.
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
 * status says configured, not-yet-run-today, AND provider-ready.
 * Any missing/unexpected shape in `status` disables the button rather
 * than guessing -- there is no path here that can produce
 * `disabled: false` from incomplete information.
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
  if (!status.provider || !status.provider.ready) {
    const detail = status.provider && status.provider.detail ? status.provider.detail : "Market data provider is not ready.";
    return { disabled: true, reason: detail, cycleState };
  }
  return { disabled: false, reason: "", cycleState };
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

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    CYCLE_STATE,
    CYCLE_STATE_LABEL,
    deriveCycleState,
    runButtonState,
    systemHealthLabel,
    validationProgress,
    createRunGuard,
    CONFIRM_RUN_MESSAGE,
  };
}
