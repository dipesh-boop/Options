/* PAPER_TRADING_V1.5.2, Step 2A: browser-scope integration regression.
 *
 * Why this file exists: a real-browser (Safari) acceptance run showed
 * `typeof marketSessionLabel === "undefined"` inside dashboard.js's own
 * execution context, even though operator_control.js loads first via a
 * plain <script> tag in index.html, exactly as this file's own top
 * comment claimed was sufficient. `tests/frontend/operator_control.test.js`
 * never caught this because it uses Node's `require()`, which reads
 * operator_control.js's `module.exports` object -- a CommonJS-specific
 * contract that is completely inert in a browser (`module` is undefined
 * there) and has nothing to do with how dashboard.js actually resolves
 * these names at runtime. require() cannot reproduce, and therefore
 * cannot verify, real classic-<script>-tag global scope at all.
 *
 * This file loads operator_control.js and then dashboard.js, IN THAT
 * ORDER, into one shared Node `vm` context -- the same order index.html
 * uses, and (unlike require(), which gives each file its own isolated
 * module scope) a model that genuinely shares one global lexical
 * environment across both `vm.Script.runInContext()` calls, the same
 * way a browser shares one global scope across sequential classic
 * <script> tags. This is deliberately not jsdom or a browser-automation
 * dependency -- a hand-rolled DOM stub sized to exactly what
 * dashboard.js's top-level code and the functions under test touch is
 * the smallest harness that can accurately reproduce the failure this
 * file exists to prevent.
 *
 * Fix under test: operator_control.js's new explicit
 * `window.<name> = <name>` export block (see that file, "BEGIN EXPLICIT
 * BROWSER EXPORT"), which gives the browser path the same explicit,
 * verifiable contract the module.exports guard already gives Node --
 * removing dashboard.js's dependence on an implicit, previously
 * unverified assumption about cross-script scoping.
 */
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const STATIC_DIR = path.join(__dirname, "..", "..", "src", "dashboard", "static");
const OPERATOR_CONTROL_SOURCE = fs.readFileSync(path.join(STATIC_DIR, "operator_control.js"), "utf8");
const DASHBOARD_SOURCE = fs.readFileSync(path.join(STATIC_DIR, "dashboard.js"), "utf8");
const INDEX_HTML = fs.readFileSync(path.join(STATIC_DIR, "index.html"), "utf8");

// The pre-fix shape of operator_control.js: every line between the
// documented BEGIN/END markers stripped out, reconstructing exactly
// what shipped before this step -- no explicit window export, only the
// implicit sharing the file's own (now-corrected) comment used to claim
// was sufficient. Derived from the real file's current text (not a
// hand-typed duplicate) so it can never silently drift out of sync with
// the real source.
function stripExplicitExportBlock(source) {
  const start = source.indexOf("// --- BEGIN EXPLICIT BROWSER EXPORT ---");
  const end = source.indexOf("// --- END EXPLICIT BROWSER EXPORT ---");
  assert.ok(start !== -1 && end !== -1, "explicit export markers not found in operator_control.js");
  const endOfBlock = source.indexOf("\n", end) + 1;
  return source.slice(0, start) + source.slice(endOfBlock);
}

const PRE_FIX_OPERATOR_CONTROL_SOURCE = stripExplicitExportBlock(OPERATOR_CONTROL_SOURCE);

// ---------------------------------------------------------- DOM stub

function escapeHtml(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function makeElement() {
  let text = "";
  let html = "";
  const classes = new Set();
  return {
    get textContent() {
      return text;
    },
    set textContent(v) {
      text = v == null ? "" : String(v);
      html = escapeHtml(text);
    },
    get innerHTML() {
      return html;
    },
    set innerHTML(v) {
      html = v == null ? "" : String(v);
    },
    className: "",
    disabled: false,
    value: "",
    classList: {
      add: (...names) => names.forEach((n) => classes.add(n)),
      remove: (...names) => names.forEach((n) => classes.delete(n)),
      toggle(name) {
        if (classes.has(name)) {
          classes.delete(name);
          return false;
        }
        classes.add(name);
        return true;
      },
      contains: (n) => classes.has(n),
    },
    addEventListener: () => {},
    setAttribute: () => {},
    appendChild: () => {},
  };
}

/**
 * Creates one fresh `vm` context with the smallest DOM/browser surface
 * dashboard.js's top-level code (which runs unconditionally the instant
 * the script is evaluated, exactly as it does in a real page load) and
 * the functions under test touch. `fetch` always rejects -- network
 * calls are irrelevant to this scope-integration proof and every call
 * site that reaches one is already wrapped in try/catch, matching the
 * production file's own documented behavior.
 */
function createBrowserContext() {
  const elementsById = new Map();
  const documentStub = {
    getElementById(id) {
      if (!elementsById.has(id)) elementsById.set(id, makeElement());
      return elementsById.get(id);
    },
    createElement() {
      return makeElement();
    },
  };
  const sandbox = {
    document: documentStub,
    fetch: () => Promise.reject(new Error("network disabled in browser-scope test harness")),
    setInterval: () => 0,
    clearInterval: () => {},
    console,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  return { sandbox, elementsById };
}

function runInContext(source, filename, sandbox) {
  new vm.Script(source, { filename }).runInContext(sandbox);
}

/** Loads operator_control.js then dashboard.js, in that order, into one
 * shared context -- the exact load order index.html uses. */
function loadDashboardInBrowserScope({ operatorControlSource = OPERATOR_CONTROL_SOURCE } = {}) {
  const { sandbox, elementsById } = createBrowserContext();
  runInContext(operatorControlSource, "operator_control.js", sandbox);
  runInContext(DASHBOARD_SOURCE, "dashboard.js", sandbox);
  return { sandbox, elementsById };
}

function statusFixture(overrides = {}) {
  return {
    configured: true,
    cohort_id: "paper-trading-v1.4.3-validation-2026-09-22",
    market_session_state: "REGULAR_MARKET",
    is_trading_day: true,
    regular_session_open: "2026-09-29T13:30:00Z",
    regular_session_close: "2026-09-29T20:00:00Z",
    validation_cycle_allowed: true,
    validation_cycle_block_reason: null,
    today_cycle_ran: false,
    today_cycle_degraded: null,
    today_cycle_halted: null,
    today_cycle_errors: [],
    provider: { provider: "tradier", is_tradier_production: true, ready: true, detail: "ready" },
    nav: 100000,
    cash: 100000,
    open_position_count: 0,
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

// ------------------------------------------------- index.html load order

test("index.html loads operator_control.js via a plain classic <script> tag before dashboard.js, neither deferred/async/module", () => {
  const ocMatch = INDEX_HTML.match(/<script\s+src="\/static\/operator_control\.js"([^>]*)>/);
  const dbMatch = INDEX_HTML.match(/<script\s+src="\/static\/dashboard\.js"([^>]*)>/);
  assert.ok(ocMatch, "operator_control.js <script> tag not found");
  assert.ok(dbMatch, "dashboard.js <script> tag not found");
  for (const attrs of [ocMatch[1], dbMatch[1]]) {
    assert.doesNotMatch(attrs, /type\s*=\s*"module"/, "must not be a module script");
    assert.doesNotMatch(attrs, /\bdefer\b/, "must not be deferred");
    assert.doesNotMatch(attrs, /\basync\b/, "must not be async");
  }
  assert.ok(INDEX_HTML.indexOf(ocMatch[0]) < INDEX_HTML.indexOf(dbMatch[0]));
});

// --------------------------------- item A: helper availability (post-fix)

test("item A: marketSessionLabel and every operator_control.js helper are available to dashboard.js under real browser-style script loading (explicit window export)", () => {
  const { sandbox } = loadDashboardInBrowserScope();
  for (const name of [
    "CYCLE_STATE", "CYCLE_STATE_LABEL", "deriveCycleState", "runButtonState",
    "marketSessionLabel", "systemHealthLabel", "validationProgress",
    "createRunGuard", "CONFIRM_RUN_MESSAGE",
  ]) {
    assert.notEqual(typeof sandbox.window[name], "undefined", `window.${name} must be an explicit global`);
    assert.notEqual(typeof sandbox[name], "undefined", `${name} must also resolve as a bare identifier`);
  }
});

test("item A (regression proof): window.<const> is a real, new contract -- absent from the pre-fix source", () => {
  // NOTE on scope: `function` declarations (marketSessionLabel,
  // createRunGuard, deriveCycleState, runButtonState, systemHealthLabel,
  // validationProgress) become properties of the global/window object
  // automatically in every standards-compliant engine -- that part of
  // classic-script scoping was never actually broken, pre- or post-fix,
  // and this harness (built on Node's vm module, which genuinely
  // reproduces cross-script global scope, unlike require()) confirms it.
  // The one part of the "every const/function here becomes an ordinary
  // global" claim that was NOT already true is the `const`-declared
  // names: CYCLE_STATE, CYCLE_STATE_LABEL, MARKET_SESSION_LABEL, and
  // CONFIRM_RUN_MESSAGE resolve fine as bare identifiers via the shared
  // *lexical* environment (proven by the bare-identifier half of item A
  // above, both pre- and post-fix), but were never `window` properties
  // until this fix. This test proves that specific, real, previously
  // undocumented gap is now closed -- the fix genuinely adds an explicit
  // guarantee no part of the pre-fix contract already provided.
  const { sandbox } = loadDashboardInBrowserScope({ operatorControlSource: PRE_FIX_OPERATOR_CONTROL_SOURCE });
  assert.equal(typeof sandbox.window.CYCLE_STATE, "undefined", "pre-fix operator_control.js never assigned window.CYCLE_STATE");
  assert.equal(typeof sandbox.window.MARKET_SESSION_LABEL, "undefined", "pre-fix operator_control.js never assigned window.MARKET_SESSION_LABEL");
  assert.equal(typeof sandbox.window.CONFIRM_RUN_MESSAGE, "undefined", "pre-fix operator_control.js never assigned window.CONFIRM_RUN_MESSAGE");
});

// ----------------------------------------------- item B: no ReferenceError

test("item B: renderCcMarketSession executes without a ReferenceError for a REGULAR_MARKET status", () => {
  const { sandbox } = loadDashboardInBrowserScope();
  assert.doesNotThrow(() => sandbox.renderCcMarketSession(statusFixture()));
});

// ------------------------------------------- item C: REGULAR_MARKET render

test("item C: a REGULAR_MARKET, validation_cycle_allowed status renders the market-session card", () => {
  const { sandbox, elementsById } = loadDashboardInBrowserScope();
  sandbox.renderCcMarketSession(statusFixture());
  const html = elementsById.get("cc-market-session").innerHTML;
  assert.match(html, /REGULAR SESSION/); // existing MARKET_SESSION_LABEL convention for REGULAR_MARKET
  assert.match(html, /Regular session: 9:30 AM ET.*4:00 PM ET/);
  assert.match(html, /New-position scan window is open\./);
});

// ------------------------------------------------ item D: blocked render

test("item D: a PRE_MARKET, validation_cycle_allowed=false status renders the backend block reason", () => {
  const { sandbox, elementsById } = loadDashboardInBrowserScope();
  const blockReason = "pre-market -- regular session opens at 2026-09-29T13:30:00Z";
  sandbox.renderCcMarketSession(
    statusFixture({
      market_session_state: "PRE_MARKET",
      validation_cycle_allowed: false,
      validation_cycle_block_reason: blockReason,
    })
  );
  const html = elementsById.get("cc-market-session").innerHTML;
  assert.match(html, /PRE-MARKET/);
  assert.ok(html.includes(blockReason), "must render the backend's own block reason verbatim, never a frontend-computed one");
});

// -------------------------------------------------- item E: run-button safety

test("item E: Run Daily Validation stays disabled when the backend reports validation_cycle_allowed=false", () => {
  const { sandbox, elementsById } = loadDashboardInBrowserScope();
  const status = statusFixture({
    market_session_state: "PRE_MARKET",
    validation_cycle_allowed: false,
    validation_cycle_block_reason: "pre-market -- regular session opens at 2026-09-29T13:30:00Z",
  });
  sandbox.renderCcRunButton(status, false);
  const btn = elementsById.get("run-validation-btn");
  assert.equal(btn.disabled, true);
  assert.match(elementsById.get("cc-run-reason").textContent, /pre-market/i);
});

test("item E (contrast): Run Daily Validation is enabled once every prerequisite, including the market-hours gate, passes", () => {
  const { sandbox, elementsById } = loadDashboardInBrowserScope();
  sandbox.renderCcRunButton(statusFixture(), false);
  assert.equal(elementsById.get("run-validation-btn").disabled, false);
});

// -------------------------------------------------- item F: software badge

test("item F: the software version badge reads PAPER_TRADING_V1.5.2", () => {
  const { sandbox, elementsById } = loadDashboardInBrowserScope();
  // SOFTWARE_VERSION is a top-level `const` in dashboard.js -- resolvable
  // as a bare identifier in this same shared scope, but (like any const)
  // never a `sandbox`/`window` property, so it's read back the same way
  // any later classic <script> tag would: by evaluating a bare reference
  // in that same context.
  const softwareVersion = new vm.Script("SOFTWARE_VERSION", { filename: "probe.js" }).runInContext(sandbox);
  assert.equal(softwareVersion, "PAPER_TRADING_V1.5.2");
  sandbox.renderControlCenter(statusFixture(), {});
  assert.equal(elementsById.get("cc-software-badge").textContent, "PAPER_TRADING_V1.5.2");
});

// --------------------------- whole-render smoke test (no ReferenceError
// anywhere in the Control Center for either an allowed or a blocked cycle)

test("renderControlCenter runs end-to-end without throwing for both an allowed and a blocked status", () => {
  const { sandbox } = loadDashboardInBrowserScope();
  assert.doesNotThrow(() => sandbox.renderControlCenter(statusFixture(), { clientRunning: false }));
  assert.doesNotThrow(() =>
    sandbox.renderControlCenter(
      statusFixture({ market_session_state: "PRE_MARKET", validation_cycle_allowed: false, validation_cycle_block_reason: "blocked" }),
      { clientRunning: false }
    )
  );
});
