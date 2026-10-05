# STEP 23.9 FREEZE REPORT — PAPER_TRADING_V1.5.9

## Status

**PAPER_TRADING_V1.5.9 / SOFTWARE FREEZE VERIFIED**

A narrow safety release. Cohort `paper-trading-v1.4.3-validation-2026-09-22`
remains ACTIVE, untouched, and unreset by this step. No official
validation cycle, `confirm_candidate.py` invocation against an
operational candidate, or production Tradier call occurred during
development or testing. No risk limit, Quant threshold, liquidity
threshold, DTE policy, ranking policy, candidate-generation behavior,
NAV, universe, or strategy set was changed.

## A. Root cause and exact old control flow

**Root cause.** `scripts/run_validation_cycle.py::run_validation_cycle()`
enforced the V1.5.1 new-position market-hours gate
(`src.portfolio.market_session.evaluate_validation_cycle_eligibility`)
with:

```python
if not eligibility.validation_cycle_allowed:
    print(f"FAIL: market-hours gate -- {eligibility.block_reason}")
    return False
```

This `return False` happened **before** `src.portfolio.orchestrator
.run_outer_cycle` was ever constructed or called, for ANY reason the
gate was closed (pre-market, post-market, weekend, holiday, or a
calendar-evaluation failure) — regardless of whether
`Portfolio.positions` was empty or not. `run_outer_cycle` is the *only*
production caller of `src.portfolio.control_loop.run_control_cycle`,
which is in turn the only thing that invokes the Lifecycle Engine
(`src.lifecycle.engine.evaluate_position`) and the Risk kill-switch
(`src.risk.kill_switch.check_kill_switch`). So a closed gate silently
prevented existing-position monitoring from ever running too, on any
day the script wasn't invoked during the open scan window.

**The documentation/code mismatch.** Both `scripts/run_validation_cycle.py`'s
own module docstring and `src/portfolio/market_session.py`'s module
docstring already asserted "existing-position Lifecycle/Risk monitoring
remains fully intact and callable regardless of this gate." This was
true of the Lifecycle/Risk *code itself* (never modified, never gated
internally — confirmed directly: `grep "market_session"
src/portfolio/control_loop.py` finds nothing, and the pre-existing test
`TestExistingPositionSafetyNeverSuppressed.test_control_loop_module_never_imports_the_market_hours_gate`
already proved this) but false of whether that code was ever actually
*reached* when the gate closed — the two things the old docstrings
conflated.

**Old control flow** (exact, before this step):
```
run_validation_cycle()
  -> cohort-started check
  -> Tradier production preflight
  -> evaluate_validation_cycle_eligibility(now)
       -> validation_cycle_allowed == False?  -> return False  [EVERYTHING BELOW NEVER RUNS]
  -> (only reached when the gate is open)
  -> build stores, cycle_id idempotency check, _expire_stale_candidates
  -> load/bootstrap Portfolio, load PaperBroker
  -> construct market-data provider, fetch chains (universe + positions)
  -> build OuterCycleInputs(opportunity_scan=<configured>)
  -> run_outer_cycle(inputs)   <-- the ONLY path to run_control_cycle
  -> persist DailySnapshot, Portfolio, PaperAccountState
```

## B. Exact new control flow

```
run_validation_cycle()
  -> cohort-started check
  -> Tradier production preflight
  -> evaluate_validation_cycle_eligibility(now)        [no early return]
  -> build stores, cycle_id ("validation-{date}") idempotency check
  -> _expire_stale_candidates(...)                      [now unconditional]
  -> existing_portfolio = portfolio_store.get(...)       [read-only peek]
  -> existing_positions = existing_portfolio.positions or []

  if not eligibility.validation_cycle_allowed:
      if not existing_positions:
          print(...); return False        # BYTE-IDENTICAL to pre-V1.5.9:
                                            # no provider, no record, no bootstrap
      return await _run_lifecycle_only_safety_check(
          now, block_reason, limits, existing_portfolio,
          lifecycle_store, control_loop_store,
      )
      # -> lifecycle_cycle_id = "validation-{date}-lifecycle"  (OWN id)
      # -> idempotency check on THAT id
      # -> construct provider; fetch ONLY position tickers
      #    (never the scan universe, never DTE-windowed)
      # -> OuterCycleInputs(opportunity_scan=None, skip_opportunity_scan=True)
      # -> run_outer_cycle(inputs)   <-- SAME unmodified function
      # -> print summary; return True

  # gate open -- unchanged from pre-V1.5.9 below this point
  -> bootstrap Portfolio if this is a never-before-seen account
  -> load PaperBroker, construct provider, fetch universe+position chains
  -> build OuterCycleInputs(opportunity_scan=<configured>)
  -> run_outer_cycle(inputs)        # uses the MAIN "validation-{date}" id
  -> persist DailySnapshot, Portfolio, PaperAccountState
```

`src/portfolio/orchestrator.py`, `src/portfolio/control_loop.py`,
`src/portfolio/cycle_record.py`, `src/portfolio/persistence.py`, and
`src/portfolio/market_session.py` are **completely untouched** — every
behavior change lives in `scripts/run_validation_cycle.py`'s own control
flow, calling the exact same, already-existing building blocks
(`OuterCycleInputs.opportunity_scan`/`skip_opportunity_scan` already
existed and already let `run_outer_cycle` skip only its scan sub-stage
while always calling `run_control_cycle`).

## C. Files changed

- `scripts/run_validation_cycle.py` — the only production logic file
  touched: restructured `run_validation_cycle()`'s control flow (section
  B) and added one new helper, `_run_lifecycle_only_safety_check`.
- `src/validation/freeze.py` — `FREEZE_NAME`/`MANIFEST_VERSION` bump to
  `PAPER_TRADING_V1.5.9`/`1.5.9`.
- `tests/unit/validation/test_freeze.py` — version-string assertions
  bumped to `1.5.9`.
- `VALIDATION_MANIFEST.json` — regenerated (`run_validation_cycle_script_hash`
  legitimately reflects the script change).
- `tests/acceptance/test_lifecycle_market_hours_separation.py` (new) —
  11 regression tests, see section J.

**Not touched**: `src/portfolio/orchestrator.py`, `src/portfolio/control_loop.py`,
`src/portfolio/cycle_record.py`, `src/portfolio/persistence.py`,
`src/portfolio/market_session.py`, `src/lifecycle/*`, `src/risk/*`,
`src/quant/*`, `src/data/*`, `src/review/*`, `src/brokers/*`,
`src/dashboard/*` (the dashboard route needed no change — it already
calls `run_validation_cycle()` by loading the script as a module),
`config/*.yaml`, `data/options_agent.db` (does not exist in this
sandbox, confirmed via `git status --porcelain data/`).

## D. Why new-position scanning is still gated

`evaluate_validation_cycle_eligibility(now, ...)` is called in exactly
the same place, with exactly the same arguments, as before this step —
nothing about WHEN a new-position scan is allowed to start changed.
What changed is only what happens when it says no: instead of the whole
function returning immediately, the function now asks a second,
independent question (does `Portfolio.positions` need evaluation) before
deciding what to do. When the gate is closed, the `opportunity_scan`
field passed to `OuterCycleInputs` is always `None` (with
`skip_opportunity_scan=True` on top, belt-and-suspenders) — so
`src.portfolio.orchestrator._run_opportunity_scan_stage` returns its own
no-op path (`cfg is None or not cfg.universe`) for two independent
reasons, never constructing a scan universe, never calling
`scan_and_rank_opportunities`, never generating or persisting a
candidate, never touching `PaperBroker.place_order`. Proven directly:
`TestNoPositionOrOrderFromLifecycleOnlyPath` and every other lifecycle-
only test in the new suite asserts `review_store.all_candidates(...) == []`
and `idempotency_store.all() == []`.

## E. Why existing-position lifecycle is no longer incorrectly suppressed

Whenever the gate is closed **and** `Portfolio.positions` is non-empty,
`_run_lifecycle_only_safety_check` now calls the exact same, unmodified
`run_outer_cycle` the scan-eligible path already called — the Lifecycle
Engine and Risk kill-switch run through `run_control_cycle` exactly as
they always have, over every position in the portfolio, with all
existing freshness/quality-gate/kill-switch protections intact (none of
that code was touched). Proven directly: `TestScanWindowClosedOnePosition`
(one position, `positions_evaluated == 1`) and
`TestScanWindowClosedMultiplePositions` (two positions, both appear in
the persisted decision snapshots).

**The idempotency-slot problem this design specifically avoids.**
`run_control_cycle` unconditionally calls `control_loop_store.save_cycle_record(...)`,
keyed by whatever `cycle_id` it's given, the moment it runs — this is
the mechanism `run_validation_cycle`'s own top-of-function idempotency
check (`get_cycle_record(cycle_id) is not None -> return True`) relies
on. If the lifecycle-only safety check shared the scan-eligible cycle's
own `cycle_id` (`f"validation-{date}"`), an early, gate-closed lifecycle
check would consume that slot — and the REAL scan, invoked later the
same day once the window opened, would see the slot already filled and
silently skip, PERMANENTLY losing that day's actual new-position
opportunity. This is why `_run_lifecycle_only_safety_check` uses its own,
distinct id, `f"validation-{date}-lifecycle"` — proven directly by
`TestScanWindowOpenWithExistingPosition`, which runs the lifecycle-only
path implicitly absent (gate open from the start) and confirms the main
cycle id alone carries `positions_evaluated == 1`, and by the fact that
every closed-gate test in the new suite asserts
`control_loop_store.get_cycle_record(_main_cycle_id()) is None` after the
lifecycle-only path runs.

## F. Weekend/non-trading-day semantics

Deliberately **not** given a separate code path. `evaluate_validation_cycle_eligibility`
already returns `validation_cycle_allowed=False` (with `is_trading_day=False`)
for a weekend or holiday exactly like it does for an ordinary pre/post-market
closure on a trading day — both route through the same gate-closed branch
in `run_validation_cycle`. Existing positions are still fetched and
evaluated on a weekend; the EXISTING, unmodified freshness quality gate
(`src.data.quality_gate.validate_option_chain`, `max_quote_age` default
15 minutes) is what correctly fails a genuinely stale weekend quote
closed, exactly as it already does for stale data on an ordinary trading
day. This is a deliberate decision, not an oversight: capital
preservation never loses by evaluating lifecycle MORE often (a weekend
check can only ever come back `DATA_INSUFFICIENT` or a genuine, safe
decision — nothing in this architecture lets a lifecycle evaluation
automatically execute a trade), so there is no safety reason to special-
case it. Proven and documented explicitly by
`TestWeekendBehaviorExplicit`, which asserts the lifecycle-only cycle
record IS created on a simulated Saturday, with `degraded_mode=True`
and `"SPY" in symbols_failed` from the (realistically stale) weekend
quote, and that no candidate is ever created.

## G. Provider/freshness/failure semantics

- **Provider unavailable** (a `get_option_chain` call raises): isolated
  per-symbol by the existing, unmodified `run_validation_cycle`-style
  try/except inside `_run_lifecycle_only_safety_check`'s own fetch loop
  (mirroring the main path's identical pattern) — one bad symbol is
  recorded in `fetch_results[ticker] = exc`, never aborts the cycle.
  `_quality_gate_market_data` (unmodified) then adds it to
  `symbols_failed`/`errors`, setting `degraded_mode=True`. Proven by
  `TestScanWindowClosedProviderFailure`.
- **Stale/malformed data**: isolated by the existing, unmodified
  `validate_option_chain` quality gate — a stale chain is rejected
  before the position is valued, producing a `DATA_INSUFFICIENT`
  decision for that position (never a fabricated "safely evaluated"
  claim) and the same `symbols_failed`/`degraded_mode` signal as a
  provider exception. Proven by `TestScanWindowClosedStaleData`.
  **Distinguishing note discovered while writing these tests**: a
  `DATA_INSUFFICIENT` decision's `PortfolioControlDecisionSnapshot` is
  appended only to `run_control_cycle`'s own in-memory return list, never
  durably persisted via `control_loop_store.append_decision_snapshot`
  (only a fully-evaluated position's snapshot is persisted) — this is
  pre-existing, unmodified `src/portfolio/control_loop.py` behavior, not
  something this step introduced or needed to change; the durable,
  observable signal for this case is the cycle record's own
  `symbols_failed`/`degraded_mode`/`positions_evaluated` fields, which
  this report's tests use directly.
- **Halted state**: `check_kill_switch` runs unconditionally inside
  `run_control_cycle` exactly as before, regardless of market hours or
  which cycle id is in use — untouched by this step.

## H. Idempotency proof

`TestIdempotency::test_second_same_day_invocation_with_gate_closed_is_a_documented_no_op`
calls `run_validation_cycle()` twice in direct succession with the gate
closed and one existing position: the first call fetches the provider
and persists exactly one decision snapshot under the lifecycle-only
cycle id; the second call's own idempotency check
(`get_cycle_record(lifecycle_cycle_id) is not None`) short-circuits
before touching the provider again — asserted directly
(`provider.requested_tickers == ["SPY"]`, i.e. only ONE fetch across
both calls) and `len(snapshots) == 1` (never duplicated). `idempotency_store.all() == []`
throughout, proving no order is ever placed by either call.

## I. Dashboard vs CLI equivalence

Proven by construction, not duplicated logic: `src.dashboard.validation_ops
.trigger_validation_cycle` loads `scripts/run_validation_cycle.py` as a
module (`importlib.util.spec_from_file_location`) and calls
`module.run_validation_cycle()` — the exact same function this report's
other tests call directly. `TestDashboardCliEquivalence` exercises the
real `POST /api/validation-cycle/run` FastAPI route with a gate-closed,
one-position environment and confirms `success=True`,
`"place_order(" not in log`, and the SAME lifecycle-only cycle record
(`positions_evaluated == 1`) the CLI-direct tests produce. No dashboard
file needed any change.

## J. Focused test results

- `tests/acceptance/test_lifecycle_market_hours_separation.py` (new): **11
  passed** — covering all 12 task-spec edge cases (item 12 is covered by
  the untouched, still-passing V1.5.8-era tests referenced below).
- `tests/unit/validation/test_freeze.py`: **94 passed** (version
  assertions bumped).
- `tests/acceptance/test_market_hours_gate.py`: **16 passed, 2 failed**
  — the 2 failures are the pre-existing, known date-rot failures (see
  section L), not regressions; every other test in this file, including
  `TestBackendPostOutsideSession`, `TestCliOutsideSession`, and the
  mandatory `TestExistingPositionSafetyNeverSuppressed` class, passes
  unmodified.
- `tests/unit/portfolio/test_market_session.py`,
  `tests/unit/portfolio/test_orchestrator.py`,
  `tests/unit/portfolio/test_control_loop.py`: all pass unmodified
  (these modules were not touched).
- Combined focused run: **167 passed, 2 failed** (the same 2 known
  date-rot failures).

## K. Full-suite results

`python -m pytest -q`: **3773 passed, 6 skipped, 9 failed** (up from
3762 passed before this step — the 11 new tests). The 9 failures are
byte-for-byte the same 9 node IDs already known and accepted as
pre-existing on frozen V1.5.8 (see section L) — **zero new failures
introduced by this step**.

## L. Comparison of failures against pristine V1.5.8

Exact 9 failing node IDs, both before and after this step:

1. `tests/acceptance/test_market_hours_gate.py::TestBackendPostInsideSession::test_reaches_the_mocked_runner_path`
2. `tests/acceptance/test_market_hours_gate.py::TestCliInsideSession::test_reaches_the_mocked_normal_path`
3. `tests/acceptance/test_opportunity_evaluation_timestamp.py::TestOpportunityEvaluationTimestampFixV157::test_a_chain_timestamped_after_the_cycle_start_no_longer_silently_loses_the_candidate`
4. `tests/acceptance/test_opportunity_evaluation_timestamp.py::TestOpportunityEvaluationTimestampFixV157::test_control_cycle_record_shows_zero_silent_generation_exceptions`
5. `tests/acceptance/test_review_only_daily_cycle.py::TestReviewOnlyDailyCycleEndToEnd::test_daily_cycle_never_auto_fills_and_surfaces_exactly_one_candidate`
6. `tests/acceptance/test_review_only_daily_cycle.py::TestReviewOnlyDailyCycleEndToEnd::test_confirm_candidate_places_exactly_one_order_even_when_invoked_twice`
7. `tests/acceptance/test_run_validation_cycle_cli.py::TestTradierProductionProviderPasses::test_tradier_production_reaches_the_mutating_cycle`
8. `tests/unit/dashboard/test_frontend_control_center.py::TestValidationEndpointIdempotencyUnaffected::test_dashboard_route_running_twice_the_same_day_is_a_documented_no_op`
9. `tests/unit/dashboard/test_operator_status.py::TestValidationCycleRunRoute::test_end_to_end_dashboard_trigger_matches_the_cli_safety_outcome`

**Verified directly, not assumed**: created a temporary `git worktree`
at the pristine `PAPER_TRADING_V1.5.8` freeze commit (`904ee5c`), ran
these exact 9 node IDs there at the same wall clock — all 9 fail
identically. Worktree removed immediately after; `git status --short`
on the primary tree confirmed clean before and after. Root cause
(carried forward from the prior release's own acceptance review, not
rederived here): a fixed-calendar-date test fixture
(`tests/unit/review/conftest.py`'s `EXPIRATION`, anchored to
`NOW + 30 days` at authoring time) has drifted outside the
`QuantFilterConfig` DTE window (`[20, 45]`) as real wall-clock time has
advanced since these fixtures were written — unrelated to
`scripts/run_validation_cycle.py`'s control flow, `src/portfolio/orchestrator.py`,
or any file this step touches. **No new failure was introduced; the
known set is unchanged.**

## M. Freeze verification result

`make verify-freeze` → **PAPER_TRADING_V1.5.9 / SOFTWARE FREEZE VERIFIED**,
every check `[OK]`, including `market_hours_gate_precedes_mutation`,
`official_cycle_requires_tradier_preflight`, `daily_cycle_never_calls_place_order`,
`help_cannot_execute_validation`, and every other CLAUDE.md-invariant-shaped
structural check. `validation_cohort_started: False`, confirming this
freeze process itself never starts, resets, or modifies a cohort.
`run_validation_cycle_script_hash` legitimately reflects the genuine
script change in this step (verified by regenerating the manifest via
`make freeze-manifest` before this verification run).

## N. Implementation commit hash

See repository log — the commit immediately preceding the freeze-artifacts
commit for this step.

## O. Freeze commit hash

See repository log — this report, `VALIDATION_MANIFEST.json`,
`progress.md`, and the `src/validation/freeze.py`/
`tests/unit/validation/test_freeze.py` version bumps.

## P. Push result

Reported in the final chat summary, with the actual outcome stated
honestly.

## Q. Tag result

`paper-trading-v1.5.9` created locally after verification. Tag push
historically fails with HTTP 403 in this environment — reported
honestly if it recurs, never worked around, never force-pushed.

## R. Explicit statement

The active validation cohort (`paper-trading-v1.4.3-validation-2026-09-22`),
its operational database (`data/options_agent.db`, which does not exist
in this sandbox), NAV ($100,000), universe (SPY/QQQ), enabled
candidate-generation strategies, DTE 20–45 policy, liquidity thresholds,
Quant thresholds, Risk thresholds, risk-per-trade/capital-allocation
settings, candidate ranking policy, Tradier production configuration,
Fidelity manual-only configuration, PaperBroker-only execution
architecture, the human-confirmation requirement, V1.5.8's exact-
expiration confirmation safety behavior, `TradeProposal`'s future-data
validator, and canonical market-data timestamps were **not changed** by
this step. No code in this diff calls `start_new_cohort` or any reset
path; no historical record was rewritten.

## S. Remaining known risks/issues

1. The 2 (of the 9) date-rot test failures that live in
   `tests/acceptance/test_market_hours_gate.py` are now co-located with
   this step's own new, passing test file — a future fixture-maintenance
   pass should update `tests/unit/review/conftest.py`'s `EXPIRATION`
   anchor (not done here, per this step's own explicit instruction not
   to silently repair that known issue).
2. `src/dashboard/validation_ops.py`'s `GET /api/operator-status` route
   still only looks up the main `f"validation-{date}"` cycle id to decide
   "did today's cycle run" — it does not yet separately surface whether
   a lifecycle-only safety check (`f"validation-{date}-lifecycle"`) ran
   today when the main cycle hasn't. This is a dashboard-display
   enhancement opportunity, not a safety gap (the underlying record is
   fully durable and queryable), deliberately left out of this narrow
   release's scope.
3. A lifecycle-only safety check currently runs once per calendar day
   (its own idempotency slot), mirroring the main cycle's cadence
   philosophy. If the operator wants intraday (e.g. hourly) lifecycle
   re-checks while the scan window stays closed for an extended period,
   that would require a different idempotency key granularity — not
   requested by this step's task spec, not built here.
