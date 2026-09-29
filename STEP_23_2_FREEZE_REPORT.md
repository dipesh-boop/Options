# STEP_23_2_FREEZE_REPORT.md

## New-Position Daily Validation Cycle Market-Hours Safety Gate — PAPER_TRADING_V1.5.1

This report documents Step 23-2 (V1.5's Step 2): the new-position daily
opportunity scan's market-hours safety gate, fixing the 2026-09-25
incident where the daily validation cycle could start scanning for
new-position candidates around 9:21 AM ET — before the regular market
session even opened. It follows the same two-commit freeze pattern
every prior step (V1.0 through V1.5.0) established.

**This step changes ONLY whether a new daily validation cycle's
new-position opportunity scan may start.** It does not touch ticker
universe, active strategies, candidate-generation logic, Quant logic
or thresholds, Risk logic or thresholds, position sizing, lifecycle
logic, PaperBroker fills, collateral rules, Tradier market-data
normalization, freshness/quality-gate thresholds, earnings logic,
correlation logic, sector logic, the Step 1 `ExperimentVersion`
architecture, transaction-cost/slippage assumptions, or human-
confirmation semantics.

## 1. Executive Summary

- **What was built:** `src.portfolio.market_session
  .evaluate_validation_cycle_eligibility` — a pure, deterministic
  function that decides whether today's automated new-position
  opportunity scan may start *right now*, built entirely on the
  existing, unmodified `src.data.market_calendar` calendar primitive
  (holidays, early closes, timezone-aware `[open, close)` session
  membership — all reused, none reimplemented) plus a new,
  operator-configurable post-open/pre-close buffer
  (`config/operations.yaml`'s new `market_hours` section: 5 minutes
  after the open, 15 minutes before the close, both documented and
  overridable via `_env` keys — no such buffer policy existed anywhere
  in the repository before this step, and this is explicitly a
  documented, minimal, conservative default, never silently invented).
  A new `MarketSessionState` enum (`MARKET_CLOSED`, `PRE_MARKET`,
  `REGULAR_MARKET`, `POST_MARKET`) gives the dashboard a small,
  backend-decided session vocabulary.
- **Wired in as the very next preflight check.** Inside
  `scripts/run_validation_cycle.py`'s `run_validation_cycle()` — the
  single production entry point both the CLI and the dashboard's
  `POST /api/validation-cycle/run` route call — the gate runs
  immediately after the existing Tradier-production preflight and
  strictly before any store beyond the cohort-check's own
  `validation_store`, before `cycle_id` is computed, before the
  market-data provider is touched, before `_expire_stale_candidates`
  (the script's first real mutation), and therefore before any
  candidate/cycle/snapshot record could ever be persisted. A blocked
  call prints `FAIL: market-hours gate -- <reason>` and returns
  `False` — zero provider calls, zero persistence, exactly matching
  every other preflight failure's existing shape.
- **Existing-position safety fully preserved, proven directly.** The
  gate lives *only* in this one script/dashboard-route layer. It was
  never added inside `src.portfolio.control_loop.run_control_cycle`,
  `src.lifecycle.engine.evaluate_position`, or
  `src.risk.kill_switch.check_kill_switch` — all three remain
  completely unmodified (confirmed: `run_control_cycle`'s own single
  production call site is this same script, and it was *already*
  market-agnostic before this step — `is_market_open` was always
  carried through only as descriptive metadata, never a skip
  condition). `tests/acceptance/test_market_hours_gate.py`'s
  `TestExistingPositionSafetyNeverSuppressed` proves this two ways:
  structurally (`src.portfolio.control_loop`'s own source never
  mentions `market_session`/`evaluate_validation_cycle_eligibility`)
  and behaviorally (`run_control_cycle` evaluates an open position
  identically whether `is_market_open` is `True` or `False`, on a
  Saturday with the market fully closed all day).
- **Why gating the *whole* daily invocation is the safe design**
  (not just the opportunity-scan sub-stage): `run_control_cycle`
  itself unconditionally persists a `ControlCycleRecord` the moment it
  runs, which would consume that day's cycle-level idempotency slot.
  If existing-position monitoring were allowed to run pre-market while
  the opportunity scan was merely skipped, a *second*, in-window
  invocation later the same day would see "today's cycle already ran"
  and never run the opportunity scan at all — silently losing that
  day's new-position scan entirely. Gating the whole invocation avoids
  this: a blocked call touches nothing, so a later, in-window retry
  the same day runs cleanly from scratch. No refactor of
  `run_control_cycle`'s persistence was required or attempted.
- **Dashboard reflects backend authority only.** `OperatorStatusView`
  gained six additive, read-only fields
  (`market_session_state`/`is_trading_day`/`regular_session_open`/
  `regular_session_close`/`validation_cycle_allowed`/
  `validation_cycle_block_reason`), computed via the same
  `evaluate_validation_cycle_eligibility` call. `operator_control.js`'s
  `runButtonState` now also requires `status.validation_cycle_allowed`
  — inserted after the existing `today_cycle_ran` check (so "already
  ran" still shows as the primary reason once true) and before the
  provider-readiness check. No JavaScript file computes market hours
  independently anywhere — `marketSessionLabel`/the new "Market
  Session" control-center card only format fields the backend already
  decided. `fmtEasternTime` in `dashboard.js` is display-only
  formatting of an already-backend-decided timestamp.
- **New freeze check, mirroring an exact existing precedent.**
  `market_hours_gate_precedes_mutation` was added following the
  identical structural-proof technique
  `official_cycle_requires_tradier_preflight` already established
  (same known-mutating-call list, same function-body-scoping
  precaution). No new whole-file/whole-directory hash was needed:
  `src/portfolio/market_session.py` (new) and the modified
  `src/portfolio/operations_config.py` are both automatically covered
  by the existing `portfolio_module_hash` whole-directory hash;
  `run_validation_cycle_script_hash`/`dashboard_validation_ops_module_hash`
  already existed and simply reflect the new content.
- **Tests:** `tests/unit/portfolio/test_market_session.py` (18 tests,
  covering the 25-scenario list's calendar/buffer/timezone-specific
  items 1-12, plus naive-datetime rejection and configurable-buffer
  edge cases) and `tests/acceptance/test_market_hours_gate.py` (8
  tests, covering backend POST/CLI inside/outside session, zero
  provider calls/persistence when blocked, historical-record
  readability, API backward compatibility, and the mandatory
  existing-position safety proof) are new. `tests/unit/portfolio
  /test_operations_config.py` gained 2 tests for the new config
  fields. `tests/unit/validation/test_freeze.py` gained a full new
  test class (3 tests) for `market_hours_gate_precedes_mutation`, plus
  the version-bump assertions. `tests/frontend
  /operator_control.test.js` gained 7 tests (5 for the market-hours
  branch of `runButtonState`, 2 for `marketSessionLabel`) — 31 Node
  tests total (up from 24). Every pre-existing test that reaches the
  real `POST /api/validation-cycle/run` route or calls
  `run_validation_cycle()` end-to-end
  (`tests/acceptance/test_review_only_daily_cycle.py`,
  `tests/acceptance/test_run_validation_cycle_cli.py`,
  `tests/unit/dashboard/test_operator_status.py`,
  `tests/unit/dashboard/test_frontend_control_center.py`) was updated
  to bypass the gate via monkeypatch (each file's own purpose is the
  Review-Only workflow or dashboard/CLI wiring, never the gate itself
  — dedicated gate tests are the two new files above) so no
  pre-existing test's pass/fail depends on the real wall-clock time
  the suite happens to run at.
- **90-day validation was NOT started, reset, altered, or touched from
  this sandbox.** The official cohort
  (`paper-trading-v1.4.3-validation-2026-09-22`) is exactly as it was
  before this step. `data/options_agent.db` does not exist in this
  sandbox, before or after this step.

## 2. Starting State

- Repository: `dipesh-boop/Options`, branch
  `claude/options-trading-agent-2b4yi8`.
- Prior freeze: `PAPER_TRADING_V1.5.0`, implementation commit
  `13510a4`, freeze-artifacts commit `8c11079` (`HEAD` at the start of
  this step). `git status --short` was clean; `git branch
  --show-current` confirmed `claude/options-trading-agent-2b4yi8`;
  `git log -1 --oneline` confirmed `8c11079 Step 23-1 freeze:
  PAPER_TRADING_V1.5.0 manifest + report`.
- Pre-implementation integrity baselines recorded (Section 12/13):
  `data/options_agent.db` absent; SHA-256 of all 4 protected config
  files (identical to every baseline recorded in every prior step).

## 3. Source-First Findings

Investigated directly before any edit, per the task's own required
12-item list:

1. **Existing market-calendar implementation:**
   `src/data/market_calendar.py` (Step 22 Part 7-9) — a stdlib-only
   (`datetime`/`zoneinfo`, deliberately no `pandas`) NYSE calendar
   already computing holidays (published observance rules), early
   closes, and `[open, close)` session membership, all timezone-aware
   via `zoneinfo`. Reused as-is; never modified, never reimplemented.
2. **Exchange calendar source:** derived deterministically from
   published federal-holiday observance rules and the
   Meeus/Jones/Butcher Easter algorithm (Good Friday) — no external
   data file or network dependency.
3. **Holidays representation:** `_nyse_holidays(year) -> set[date]`,
   computed per calendar year from closed-form rules.
4. **Early closes:** `_nyse_early_closes(year) -> set[date]` (day after
   Thanksgiving, Christmas Eve on a weekday, July 3rd Mon-Thu);
   `regular_close(d)` automatically returns 1:00pm ET on such a day.
5. **Timezone conversion:** every function requires a timezone-aware
   `datetime`, converts internally via `.astimezone(EASTERN)`; a naive
   datetime raises `NaiveDatetimeError` rather than being silently
   assumed to be any particular zone.
6. **Current `market_open` computation:** `is_market_open(dt)` — a
   strict `[open, close)` membership check against `regular_open`/
   `regular_close`.
7. **Where `ControlCycleRecord.market_open` is populated:**
   `src/portfolio/control_loop.py`'s `run_control_cycle` stamps it
   directly from `inputs.is_market_open`, itself computed by the
   caller (`scripts/run_validation_cycle.py`) via
   `is_market_open(now)` — purely descriptive metadata, never used
   internally to skip a position's evaluation.
8. **Where `POST /api/validation-cycle/run` reaches the runner:**
   `src/dashboard/app.py`'s route calls
   `src.dashboard.validation_ops.trigger_validation_cycle()`, which
   loads `scripts/run_validation_cycle.py` as a module and awaits its
   unmodified `run_validation_cycle()` coroutine directly — the *same*
   function object the CLI's `main()` calls. Confirmed: adding the
   gate inside `run_validation_cycle()` itself automatically protects
   both entry points with zero duplication.
9. **Where the dashboard decides the Run button state:**
   `src/dashboard/static/operator_control.js`'s pure `runButtonState`
   function, fed by `GET /api/operator-status`'s JSON — confirmed
   before any edit that no JS file computed market hours on its own.
10. **CLI independent entry path:** confirmed `run_outer_cycle`/
    `run_control_cycle` have exactly one production call site anywhere
    in `src/` — this same script — via a repository-wide grep.
11. **Deterministic/fake clocks in existing tests:**
    `tests/acceptance/test_review_only_daily_cycle.py`'s own comment
    confirmed `run_validation_cycle()` used the real wall clock with
    no injectable `now` before this step; `build_operator_status(*,
    now: datetime | None = None)` already established the exact
    optional-injectable-clock idiom this step reused for
    `run_validation_cycle`.
12. **Existing-position lifecycle evaluation order relative to
    opportunity scanning:** confirmed via
    `src/portfolio/orchestrator.py`'s `run_outer_cycle` docstring and
    body — ticket monitoring, then opportunity scanning (both
    rate-limit-gated), then the *unconditional*, never-rate-limited
    call to `run_control_cycle` for existing positions. Also confirmed
    `OuterCycleInputs.skip_opportunity_scan: bool = False` already
    existed as an established mechanism (its own comment names
    "don't scan outside market hours" as an example use) — informing,
    but not directly used by, this step's design (Section 5 explains
    why the whole-invocation gate was chosen over this finer-grained
    mechanism).

**No planning assumption was found to be wrong; no STOP condition was
triggered.**

## 4. Market-Session Design Implemented

```python
class MarketSessionState(str, Enum):
    MARKET_CLOSED = "MARKET_CLOSED"
    PRE_MARKET = "PRE_MARKET"
    REGULAR_MARKET = "REGULAR_MARKET"
    POST_MARKET = "POST_MARKET"

@dataclass(frozen=True)
class ValidationCycleEligibility:
    as_of: datetime
    market_session_state: MarketSessionState
    is_trading_day: bool
    regular_session_open: datetime | None
    regular_session_close: datetime | None
    validation_cycle_allowed: bool
    block_reason: str | None
```

`evaluate_validation_cycle_eligibility(now, *, scan_open_buffer_minutes,
scan_close_buffer_minutes)`: not a trading day → `MARKET_CLOSED`,
blocked. Otherwise, `market_session_state` reflects the *raw* exchange
session (`PRE_MARKET`/`REGULAR_MARKET`/`POST_MARKET`, purely for
display), while `validation_cycle_allowed` is a *separate*,
buffer-adjusted decision:
`scan_window_open = regular_open + open_buffer`,
`scan_window_close = regular_close - close_buffer`,
`allowed = scan_window_open <= now < scan_window_close`. This means a
time technically inside `REGULAR_MARKET` (e.g. exactly 9:30am, or
9:50pm-close-buffer territory near an early close) can still be
correctly blocked by the buffer — proven by
`test_2_exactly_at_exchange_open_respects_the_open_buffer_policy`.
No early-close-specific `MarketSessionState` member exists — an early
close is represented by `regular_session_close` itself already being
earlier (1:00pm ET), matching `market_calendar.MarketStatus
.is_early_close_session`'s own existing boolean-flag precedent instead
of inventing a 5th display state.

## 5. Open/Close Buffer Policy

No pre-existing "market open buffer"/"data stabilization buffer"
policy existed anywhere in this repository (confirmed by inspection:
`DEFAULT_MAX_QUOTE_AGE`/`MAX_MARKET_DATA_AGE`, both 15 minutes, are
QUOTE-STALENESS tolerances — "how old may a quote be" — a different
question). Implemented as a new, clearly-named, operator-configurable
`config/operations.yaml` `market_hours` section:

```yaml
market_hours:
  scan_open_buffer_minutes: 5
  scan_open_buffer_minutes_env: OPTIONS_AGENT_SCAN_OPEN_BUFFER_MINUTES
  scan_close_buffer_minutes: 15
  scan_close_buffer_minutes_env: OPTIONS_AGENT_SCAN_CLOSE_BUFFER_MINUTES
```

Documented reasoning (also in `src/portfolio/market_session.py`'s own
module docstring): 5 minutes post-open is a conservative, common rule
of thumb for letting opening-auction volatility settle before a new
automated scan trusts the data it sees; 15 minutes pre-close
deliberately matches this platform's own existing 15-minute
quote-staleness tolerance, so a scan is never started so close to the
bell that a quote it just fetched could already be stale by the time
it's used. Neither value is derived from anything else in the
codebase — both are documented, minimal, conservative policy defaults,
never silently invented as hardcoded logic, and fully
operator-overridable via the `_env` keys. `config/operations.yaml` is
**not** one of the 4 protected configs (`universe.yaml`/
`risk_limits.yaml`/`validation.yaml`/`brokers.yaml`) — extending it was
in-bounds without triggering the "STOP before touching a protected
config" rule.

## 6. Backend Gate

**Exact function/point of rejection:**
`scripts/run_validation_cycle.py::run_validation_cycle()`, immediately
after `verify_official_provider_is_tradier_production()` succeeds and
its `_line("provider preflight", ...)` log, and strictly before
`control_loop_store`/`lifecycle_store`/`review_store`/
`account_state_store`/`portfolio_store` are constructed, before
`cycle_id` is computed, and before `_expire_stale_candidates` (the
first real mutation). On block: `print(f"FAIL: market-hours gate --
{eligibility.block_reason}")`, `return False` — the exact same
response shape (`success=False`, reason in the printed log) every
other preflight failure in this script already uses, surfaced by the
dashboard's `ValidationCycleRunView(success=False, log=...)` exactly
as before, per the application's existing API convention (never a new
HTTP error status — `POST /api/validation-cycle/run` always returns
200 with a `success` boolean, matching the provider-preflight-failure
precedent).

## 7. CLI Gate

`run_validation_cycle()` gained an optional `now: datetime | None =
None` parameter (defaulting to the real wall clock exactly as before
this step) — the *same, single* coroutine both `scripts/run_validation
_cycle.py`'s `main()` (CLI) and `src/dashboard/validation_ops
.trigger_validation_cycle()` (dashboard route) call. Because the gate
lives inside this one shared function, the CLI receives the exact same
protection as the dashboard with zero duplicated logic — proven
directly by `tests/acceptance/test_market_hours_gate.py`'s
`TestCliOutsideSession`/`TestCliInsideSession` classes, which construct
the script module via the same `importlib`-based technique
`test_review_only_daily_cycle.py` already established and call
`run_validation_cycle()` directly.

## 8. Existing-Position Safety

Investigated, designed against, and proven directly — see Section 1's
third bullet and Section 3, item 12, above. Summary: the gate is added
**only** at the single daily-cycle script/dashboard-route entry point;
`run_control_cycle`, `evaluate_position`, and `check_kill_switch`
remain completely unmodified, structurally proven (source-scan) to
never mention the gate, and behaviorally proven to evaluate an existing
position identically regardless of `is_market_open`. This step never
added a simplistic global "market closed → return" anywhere inside the
shared library functions — the only new early-return is in the daily
script's own outer function, before any of those library calls happen
at all.

## 9. Operator-Status Changes

Six additive, read-only fields on `OperatorStatusView`, all
`None`/`False`-defaulted (never fabricated) when unconfigured:
`market_session_state: str | None`, `is_trading_day: bool | None`,
`regular_session_open: datetime | None`, `regular_session_close:
datetime | None`, `validation_cycle_allowed: bool = False`,
`validation_cycle_block_reason: str | None`. No secret is ever
exposed — these are purely calendar-derived facts.

## 10. Dashboard Changes

A new "Market Session" card in the Daily Validation Control section
shows the session-state badge (reusing `.cc-state-badge` with 4 new
color rules), the regular session's open/close time formatted in
Eastern time (display-only formatting of an already-backend-decided
timestamp, via `Intl.DateTimeFormat` in `dashboard.js`), and either
"New-position scan window is open." or the backend's own block reason.
The Run button is now disabled whenever `validation_cycle_allowed` is
`false`, in addition to every pre-existing prerequisite (configured,
not-yet-run-today, provider-ready) — checked immediately after
`today_cycle_ran` so "already completed" still wins as the displayed
reason once true, satisfying "the Run button should require BOTH
existing run eligibility AND backend market-session eligibility."

## 11. Files Changed

| File | Change |
|---|---|
| `src/portfolio/market_session.py` | **New.** `MarketSessionState`, `ValidationCycleEligibility`, `evaluate_validation_cycle_eligibility`. |
| `config/operations.yaml` | New `market_hours` section (`scan_open_buffer_minutes`/`scan_close_buffer_minutes` + `_env` overrides). Not a protected config. |
| `src/portfolio/operations_config.py` | `OperationsConfig` gains `scan_open_buffer_minutes`/`scan_close_buffer_minutes`; loader reads the new section. |
| `scripts/run_validation_cycle.py` | `run_validation_cycle(*, now: datetime | None = None)`; market-hours gate inserted after the provider preflight, before any mutation; docstring updated. |
| `src/dashboard/validation_ops.py` | `OperatorStatusView` gains 6 read-only fields; `build_operator_status()` computes them via `evaluate_validation_cycle_eligibility`. |
| `src/dashboard/static/operator_control.js` | `runButtonState` gains the market-hours check; new `marketSessionLabel`, exported. |
| `src/dashboard/static/dashboard.js` | New `renderCcMarketSession`/`fmtEasternTime`; wired into `renderControlCenter`. |
| `src/dashboard/static/index.html` | New "Market Session" `.control-block`. |
| `src/dashboard/static/dashboard.css` | 4 new `.cc-state-*` color rules for the market-session badge. |
| `src/validation/freeze.py` | Version bumped to `1.5.1`/`PAPER_TRADING_V1.5.1`; new `market_hours_gate_precedes_mutation` field/check/verification function; historical comment block extended; `official_cycle_requires_tradier_preflight`'s own extraction marker updated for the new `run_validation_cycle` signature. |
| `tests/unit/portfolio/test_market_session.py` | **New.** 18 tests. |
| `tests/acceptance/test_market_hours_gate.py` | **New.** 8 tests. |
| `tests/unit/portfolio/test_operations_config.py` | 2 new tests for the new config fields. |
| `tests/unit/validation/test_freeze.py` | New `TestMarketHoursGatePrecedesMutationCheck` class (3 tests); version-bump assertions. |
| `tests/frontend/operator_control.test.js` | 7 new tests; `baseStatus()` gains the 6 new fields (defaulted allowed). |
| `tests/acceptance/test_review_only_daily_cycle.py` | Gate bypassed via monkeypatch (this file tests the Review-Only workflow, not the gate). |
| `tests/acceptance/test_run_validation_cycle_cli.py` | Same. |
| `tests/unit/dashboard/test_operator_status.py` | Same. |
| `tests/unit/dashboard/test_frontend_control_center.py` | Same. |

No file under `src/strategies/`, `src/quant/`, `src/risk/`,
`src/lifecycle/`, `src/brokers/paper.py`, `src/brokers/fidelity.py`,
`src/llm/`, `src/data/market_calendar.py`, `src/portfolio
/control_loop.py`, `src/portfolio/orchestrator.py`, `src/review
/confirmation.py`, `src/orchestration/pipeline.py`,
`scripts/confirm_candidate.py`, or any of the 4 protected
`config/*.yaml` files was touched.

## 12. Test Results

```
pytest -q tests/unit/portfolio/test_market_session.py
18 passed

pytest -q tests/acceptance/test_market_hours_gate.py
8 passed

pytest -q tests/unit/validation/test_freeze.py
73 passed

pytest -q tests/unit/portfolio/ tests/unit/dashboard/ \
  tests/unit/data/test_market_calendar.py tests/unit/validation/
742 passed

pytest -q tests/acceptance/
335 passed, 2 skipped

node --test tests/frontend/operator_control.test.js
tests 31
pass 31
fail 0

pytest -q   # full suite
3583 passed, 6 skipped, 2 warnings
```

(V1.5.0 was 3552 passed, 6 skipped — net new: 31 tests.)

## 13. Freeze Verification

```
make freeze-manifest
Wrote /home/user/Options/VALIDATION_MANIFEST.json
(manifest_hash=7b7acdee375f696a86c18258c75a9c9b16f2fdba40ce00ee870f8f8b1defd1c0)

make verify-freeze
[... 66 checks ...]
PAPER_TRADING_V1.5.1 / FREEZE VERIFIED / VALIDATION NOT STARTED /
READY FOR FINAL PRE-VALIDATION ACCEPTANCE
```

**66 of 66 checks passing**, including the new
`market_hours_gate_precedes_mutation` check and every carried-forward
V1.5.0 check.

## 14. Protected Config Integrity

| File | Before (SHA-256) | After (SHA-256) | Match |
|---|---|---|---|
| `config/universe.yaml` | `b88aac45f5d0eb15a974f228f12257d16d70153611767cf63ae5e5f3f26dd30c` | `b88aac45f5d0eb15a974f228f12257d16d70153611767cf63ae5e5f3f26dd30c` | Y |
| `config/risk_limits.yaml` | `e502d64800785ffd9d21980f250f9822ac287850a798d6e935d26acd4894c8b9` | `e502d64800785ffd9d21980f250f9822ac287850a798d6e935d26acd4894c8b9` | Y |
| `config/validation.yaml` | `d5f3bfe9eb951362bd050d20a6f08301f5db7707689e72c932a0801732445f52` | `d5f3bfe9eb951362bd050d20a6f08301f5db7707689e72c932a0801732445f52` | Y |
| `config/brokers.yaml` | `99a3d9dddb0ca1c8a425ac0bee6f18cf4ee0c468e451f0689450720bb5b9fed9` | `99a3d9dddb0ca1c8a425ac0bee6f18cf4ee0c468e451f0689450720bb5b9fed9` | Y |

**All 4 protected configs byte-for-byte unchanged.** `config/operations.yaml`
(not protected) was intentionally extended — Section 5 explains why
this was in-bounds.

## 15. Operational DB Integrity

| | Before | After |
|---|---|---|
| `data/options_agent.db` exists | No | No |

**Exact match: Y (both absent).** This sandbox never contained the
operator's real database. No test in this step opens, copies, or
writes to a path named `data/options_agent.db`; every SQLite store
this step's tests use is `tmp_path`-scoped. `rm -rf data` was run
before/after every test batch and manual exercise this step.

## 16. Network / Execution Safety

- **No Tradier/Fidelity/IBKR call** anywhere in this step —
  `src/portfolio/market_session.py` never imports a provider module;
  every acceptance test uses `FakeMarketDataProvider`, never a real
  network call; this sandbox has no Tradier credentials configured.
- **No real validation cycle was run** — every exercise of
  `run_validation_cycle()`/`POST /api/validation-cycle/run` in this
  step used temporary `tmp_path`-scoped stores via the `environment`
  fixture, never `data/options_agent.db`.
- **No candidate was confirmed** — `scripts/confirm_candidate.py` and
  `src/review/confirmation.py` were not touched or executed against
  any real state.
- **No PaperBroker position was opened** — `src/brokers/paper.py` was
  not touched; `daily_cycle_never_calls_place_order`'s freeze check
  (unaffected by this step) still passes.
- **No operational cohort was modified, initialized, or reset.**

## 17. Historical Data Safety

Sep 22-onward history was not modified. `TestHistoricalRecordsRemainReadable`
(`tests/acceptance/test_market_hours_gate.py`) directly proves a
`ControlCycleRecord` shaped exactly like the real 2026-09-23
degraded/mock incident record (`market_open=False`, `degraded_mode=True`)
still deserializes correctly — `ControlCycleRecord` itself was not
touched by this step. No historical alert was modified. The gate
applies prospectively only, to future invocations of
`run_validation_cycle()`.

## 18. Experimental Behavior Change

**The only intended behavioral change:** the automated new-position
daily opportunity scan (`scripts/run_validation_cycle.py`, both CLI
and dashboard-triggered) now refuses to start outside the approved
regular market session (weekday, non-holiday, within the configured
post-open/pre-close buffer of the actual NYSE session, honestly
computed and fail-closed on any calendar uncertainty). Existing-
position Lifecycle Engine/Risk kill-switch monitoring's own decision
logic is completely unaffected — it produces identical recommendations
for identical inputs regardless of this step, since it was not touched
and never gated on market hours to begin with, before or after this
step. No Quant calculation, Risk threshold, position-sizing rule,
strategy definition, or human-confirmation requirement changed.

## FREEZE

`FREEZE_NAME`/`MANIFEST_VERSION`/`freeze_version` bumped in place from
`PAPER_TRADING_V1.5.0`/`1.5.0` to `PAPER_TRADING_V1.5.1`/`1.5.1`.

- **Manifest hash:** `7b7acdee375f696a86c18258c75a9c9b16f2fdba40ce00ee870f8f8b1defd1c0`
- **`git_commit` recorded in the manifest:** `cfab43d0611eca360fbe1f95c749bf2bfad4a6d1` (the implementation commit).

## Software Freeze State vs. Operational Cohort State

- **Software freeze state:** **PAPER_TRADING_V1.5.1: FROZEN.** All 66
  `make verify-freeze` checks pass against a clean working tree at the
  implementation commit.
- **Operational validation-cohort state (unaffected):**
  **90_DAY_VALIDATION: IN_PROGRESS / ACTIVE.** Cohort
  `paper-trading-v1.4.3-validation-2026-09-22` continues uninterrupted
  across this market-hours-safety step — nothing in this step started
  a new cohort, reset the existing one, or touched any of its
  historical records.

**LIVE_TRADING: DISABLED. FIDELITY_EXECUTION: MANUAL_ONLY. TRADIER:
MARKET_DATA_ONLY. NEW_POSITION_EXECUTION: HUMAN_CONFIRMED_REVIEW_ONLY.
NEW_POSITION_SCAN: REGULAR_MARKET_SESSION_ONLY (buffer-adjusted).**

## Git Commit and Tag

- **Implementation commit** (the market-hours gate, config/dashboard
  wiring, freeze-manifest module bump and new check, and every
  new/changed test file): `cfab43d0611eca360fbe1f95c749bf2bfad4a6d1`.
- **Freeze-report commit** (this freeze report + regenerated
  `VALIDATION_MANIFEST.json` + `progress.md`'s Step 23-2 entry
  together) immediately follows.
- Tag `paper-trading-v1.5.1` attempted after both commits.
- V1.0 through V1.5.0 tags and their underlying commits were not
  touched by this step.
