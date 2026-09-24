/* Node built-in test runner (`node --test`) exercising the pure
 * decision logic in src/dashboard/static/operator_control.js directly
 * -- no DOM shim, no bundler, no new framework dependency (Step 22.9,
 * PAPER_TRADING_V1.4.8). Run standalone with `node --test tests/frontend/`
 * or via `pytest tests/unit/dashboard/test_frontend_operator_control.py`,
 * which shells out to exactly that command as part of the ordinary
 * Python test suite.
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const {
  CYCLE_STATE,
  deriveCycleState,
  runButtonState,
  systemHealthLabel,
  validationProgress,
  createRunGuard,
  CONFIRM_RUN_MESSAGE,
} = require(path.join(__dirname, "..", "..", "src", "dashboard", "static", "operator_control.js"));

function baseStatus(overrides = {}) {
  return {
    configured: true,
    cohort_id: "paper-trading-v1.4.3-validation-2026-09-22",
    nav: 100000,
    cash: 100000,
    open_position_count: 0,
    today_cycle_id: "validation-2026-09-24",
    today_cycle_ran: false,
    today_cycle_degraded: null,
    today_cycle_halted: null,
    today_cycle_errors: [],
    provider: { provider: "tradier", is_tradier_production: true, ready: true, detail: "ready" },
    awaiting_review_count: 0,
    awaiting_review_candidate_ids: [],
    validation_duration_days: 90,
    validation_days_recorded: 3,
    validation_minimum_completed_trades: 50,
    validation_preferred_completed_trades: 100,
    validation_completed_trades: 0,
    alerts: [],
    ...overrides,
  };
}

// ---------------------------------------------------------- deriveCycleState

test("deriveCycleState: not configured is ERROR", () => {
  assert.equal(deriveCycleState({ configured: false }, false), CYCLE_STATE.ERROR);
});

test("deriveCycleState: null status is ERROR", () => {
  assert.equal(deriveCycleState(null, false), CYCLE_STATE.ERROR);
});

test("deriveCycleState: not run yet + provider ready is READY", () => {
  assert.equal(deriveCycleState(baseStatus({ today_cycle_ran: false }), false), CYCLE_STATE.READY);
});

test("deriveCycleState: not run yet + provider not ready is NOT_RUN", () => {
  const status = baseStatus({ today_cycle_ran: false, provider: { provider: "mock", is_tradier_production: false, ready: false, detail: "not configured" } });
  assert.equal(deriveCycleState(status, false), CYCLE_STATE.NOT_RUN);
});

test("deriveCycleState: ran, not degraded/halted/errored is COMPLETE", () => {
  const status = baseStatus({ today_cycle_ran: true, today_cycle_degraded: false, today_cycle_halted: false });
  assert.equal(deriveCycleState(status, false), CYCLE_STATE.COMPLETE);
});

test("deriveCycleState: ran + degraded is DEGRADED", () => {
  const status = baseStatus({ today_cycle_ran: true, today_cycle_degraded: true, today_cycle_halted: false });
  assert.equal(deriveCycleState(status, false), CYCLE_STATE.DEGRADED);
});

test("deriveCycleState: ran + halted is HALTED (takes precedence over degraded)", () => {
  const status = baseStatus({ today_cycle_ran: true, today_cycle_degraded: true, today_cycle_halted: true });
  assert.equal(deriveCycleState(status, false), CYCLE_STATE.HALTED);
});

test("deriveCycleState: ran + errors present is ERROR", () => {
  const status = baseStatus({ today_cycle_ran: true, today_cycle_degraded: false, today_cycle_halted: false, today_cycle_errors: ["boom"] });
  assert.equal(deriveCycleState(status, false), CYCLE_STATE.ERROR);
});

test("deriveCycleState: clientRunning always wins, even over a completed cycle", () => {
  const status = baseStatus({ today_cycle_ran: true });
  assert.equal(deriveCycleState(status, true), CYCLE_STATE.RUNNING);
});

// ---------------------------------------------------------- runButtonState

test("runButtonState: enabled only when configured, not-yet-run, provider ready", () => {
  const state = runButtonState(baseStatus(), false);
  assert.equal(state.disabled, false);
  assert.equal(state.reason, "");
  assert.equal(state.cycleState, CYCLE_STATE.READY);
});

test("runButtonState: disabled when clientRunning regardless of status", () => {
  const state = runButtonState(baseStatus(), true);
  assert.equal(state.disabled, true);
  assert.match(state.reason, /running/i);
});

test("runButtonState: disabled when status missing/unconfigured (fail closed)", () => {
  assert.equal(runButtonState(null, false).disabled, true);
  assert.equal(runButtonState({ configured: false }, false).disabled, true);
});

test("runButtonState: disabled once today's cycle already ran", () => {
  const state = runButtonState(baseStatus({ today_cycle_ran: true }), false);
  assert.equal(state.disabled, true);
  assert.match(state.reason, /already completed/i);
});

test("runButtonState: disabled when provider not ready, surfaces provider detail", () => {
  const status = baseStatus({ provider: { provider: "mock", is_tradier_production: false, ready: false, detail: "Tradier production token is not configured." } });
  const state = runButtonState(status, false);
  assert.equal(state.disabled, true);
  assert.equal(state.reason, "Tradier production token is not configured.");
});

test("runButtonState: disabled when provider object itself is missing", () => {
  const status = baseStatus({ provider: undefined });
  const state = runButtonState(status, false);
  assert.equal(state.disabled, true);
});

// ---------------------------------------------------------- systemHealthLabel

test("systemHealthLabel: UNKNOWN when unconfigured", () => {
  assert.equal(systemHealthLabel({ configured: false }), "UNKNOWN");
  assert.equal(systemHealthLabel(null), "UNKNOWN");
});

test("systemHealthLabel: NORMAL, DEGRADED, HALTED in precedence order", () => {
  assert.equal(systemHealthLabel(baseStatus()), "NORMAL");
  assert.equal(systemHealthLabel(baseStatus({ today_cycle_degraded: true })), "DEGRADED");
  assert.equal(systemHealthLabel(baseStatus({ today_cycle_degraded: true, today_cycle_halted: true })), "HALTED");
});

// ---------------------------------------------------------- validationProgress

test("validationProgress: null when duration is unavailable", () => {
  assert.equal(validationProgress({ configured: false }), null);
  assert.equal(validationProgress(baseStatus({ validation_duration_days: null })), null);
});

test("validationProgress: computes day/trade fractions, clamped to 1", () => {
  const p = validationProgress(baseStatus({ validation_days_recorded: 3, validation_duration_days: 90, validation_completed_trades: 5, validation_minimum_completed_trades: 50 }));
  assert.equal(p.daysElapsed, 3);
  assert.equal(p.totalDays, 90);
  assert.ok(Math.abs(p.dayPct - 3 / 90) < 1e-9);
  assert.ok(Math.abs(p.minTradePct - 5 / 50) < 1e-9);
});

test("validationProgress: clamps a fraction that would exceed 1", () => {
  const p = validationProgress(baseStatus({ validation_days_recorded: 120, validation_duration_days: 90, validation_completed_trades: 200, validation_minimum_completed_trades: 50 }));
  assert.equal(p.dayPct, 1);
  assert.equal(p.minTradePct, 1);
});

// ---------------------------------------------------------- createRunGuard

test("createRunGuard: first beginRun succeeds, a second while in-flight is refused", () => {
  const guard = createRunGuard();
  assert.equal(guard.isInFlight(), false);
  assert.equal(guard.beginRun(), true);
  assert.equal(guard.isInFlight(), true);
  // The double-click / duplicate-submission case: a second call before
  // endRun() must be refused, not silently allowed through.
  assert.equal(guard.beginRun(), false);
  assert.equal(guard.isInFlight(), true);
});

test("createRunGuard: endRun releases the guard for a subsequent legitimate run", () => {
  const guard = createRunGuard();
  assert.equal(guard.beginRun(), true);
  guard.endRun();
  assert.equal(guard.isInFlight(), false);
  assert.equal(guard.beginRun(), true);
});

test("createRunGuard: two independent guards never share state", () => {
  const a = createRunGuard();
  const b = createRunGuard();
  assert.equal(a.beginRun(), true);
  assert.equal(b.beginRun(), true);
  assert.equal(b.isInFlight(), true);
});

// ---------------------------------------------------------- CONFIRM_RUN_MESSAGE

test("CONFIRM_RUN_MESSAGE: states no automatic new position and separate human review", () => {
  assert.match(CONFIRM_RUN_MESSAGE, /cannot automatically open a new position/i);
  assert.match(CONFIRM_RUN_MESSAGE, /separate human review/i);
});
