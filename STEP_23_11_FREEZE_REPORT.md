# STEP 23.11 FREEZE REPORT — PAPER_TRADING_V1.5.11

## Status

**PAPER_TRADING_V1.5.11 / SOFTWARE FREEZE VERIFIED**

A narrow safety release: existing-position lifecycle monitoring may now
receive a manually-initiated recheck more than once per trading day,
bucketed by the market-local clock hour, while new-position scanning
remains at most once per trading day. No scheduler, background loop,
or automatic trigger was added. Cohort
`paper-trading-v1.4.3-validation-2026-09-22` remains ACTIVE, untouched,
and unreset by this step. No official validation cycle,
`confirm_candidate.py` invocation against an operational candidate, or
production Tradier call occurred during development or testing. No
risk limit, Quant threshold, liquidity threshold, candidate DTE policy,
ranking policy, candidate-generation behavior, NAV, universe, or
strategy set was changed. Alert behavior (including the pre-existing
non-auto-resolution of alerts) is unchanged.

## A. Root cause

Before this release, `run_validation_cycle()`'s own top-level
`cycle_id = f"validation-{date}"` existence check was an unconditional,
**function-wide early return**: the instant today's main new-position
scan had already run, the entire function stopped — including
existing-position lifecycle/Risk-kill-switch monitoring — regardless of
how many open positions needed a fresh look. An operator who manually
re-invoked the script later the same day (e.g. for a fresh
existing-position safety check after lunch) got a silent no-op: no
fresh lifecycle evaluation, no new audit snapshot, no Risk kill-switch
re-run against current data. Separately, even where V1.5.9/V1.5.10's
gate-closed `_run_lifecycle_only_safety_check` path WAS reached, its own
cycle id (`validation-{date}-lifecycle`) was idempotent **once per
calendar day**, so a second manual recheck later that same day, through
that path, was also a no-op.

## B. Old control flow

```
run_validation_cycle(now):
    cycle_id = f"validation-{date}"
    if control_loop_store.get_cycle_record(cycle_id) is not None:
        return True          # <-- unconditional, function-wide stop
    ...
    if not eligibility.validation_cycle_allowed:
        return await _run_lifecycle_only_safety_check(...)   # gate-closed only
    ...normal full scan-eligible cycle...
```

A manual recheck after the main cycle had already run that day reached
neither branch — the function returned before even checking whether
positions existed.

## C. New control flow

```
run_validation_cycle(now):
    cycle_id = f"validation-{date}"
    main_cycle_already_ran = control_loop_store.get_cycle_record(cycle_id) is not None  # boolean, not a return

    existing_portfolio = portfolio_store.get(ops.account_id)
    existing_positions = existing_portfolio.positions if existing_portfolio else []

    if not eligibility.validation_cycle_allowed:
        if not existing_positions:
            return False      # safe no-op, nothing persisted
        return await _run_lifecycle_only_safety_check(...)        # CASE 6/7

    if main_cycle_already_ran:
        if not existing_positions:
            return True        # safe no-op, nothing persisted       # CASE 1
        return await _run_lifecycle_only_safety_check(...)         # CASE 2/4

    ...normal full scan-eligible cycle, unchanged, using cycle_id...  # CASE 5
```

`main_cycle_already_ran` is now used ONLY to decide whether to skip the
*opportunity-scan continuation* — never to stop the function itself.
`_run_lifecycle_only_safety_check` is called from the gate-closed branch
exactly as before (unchanged in this respect) AND, newly, from the
gate-open-but-already-scanned branch.

## D. Main daily cycle-id semantics

`cycle_id = f"validation-{now.date().isoformat()}"` — **completely
unchanged**: still UTC-date-based (`now` is UTC in every real
invocation), still represents at most one normal new-position
validation cycle per trading date, still the same string format, still
checked via the same `control_loop_store.get_cycle_record(cycle_id)`
idempotency mechanism. This release never writes to this id's slot
more than once per day; it only changes what happens when that slot is
already occupied.

## E. Lifecycle hourly cycle-id semantics

`_run_lifecycle_only_safety_check`'s own cycle id changed from
`f"validation-{date}-lifecycle"` (once per calendar day) to:

```python
local_now = now.astimezone(EASTERN)
lifecycle_cycle_id = f"validation-{local_now.date().isoformat()}-lifecycle-{local_now.hour:02d}"
```

`EASTERN = ZoneInfo("America/New_York")`, the same module attribute
`src.data.market_calendar` already exports and the rest of this
codebase already uses for market-session semantics. A duplicate
invocation within the SAME market-local hour sees its own hourly
record already exists and no-ops (documented, not a scheduler — see
STEP S below); an invocation in a NEW hour — later the same day, or
after the main daily cycle has already run — gets its own,
still-untouched hourly id and runs a fresh evaluation. The two id
FAMILIES (`validation-{date}` and `validation-{date}-lifecycle-{hour}`)
are independently idempotent; neither can consume or block the other's
slot, by construction (they are different strings, checked against the
same `control_loop_store` but never cross-read).

## F. Timezone/bucket semantics

The hour is derived from `now.astimezone(EASTERN)`, never from `now`'s
own timezone directly (`now` is UTC in every real invocation — using
UTC's hour would silently misalign the bucket boundary from the market
session this whole module is about) and never from a fixed UTC offset
(which would silently break across the DST transition).
`zoneinfo`-based `astimezone` conversion handles DST entirely —
confirmed by `TestDstBoundaryAcrossSeasons`, which builds the same
11:30 ET instant on an EDT date (`2026-10-06`) and an EST date
(`2026-01-06`) and asserts both resolve to local hour 11 and bucket id
`...-lifecycle-11`, with no fixed-offset arithmetic anywhere in the
production code. `TestDistinctHourBuckets` proves the 13:59 ET -> 14:00
ET boundary specifically: two one-minute-apart instants straddling the
hour produce two distinct bucket ids and two distinct fresh fetches.

## G. Same-hour duplicate behavior

`TestSameHourSecondInvocationIsNoOp`: a main cycle at 10:00 ET followed
by two lifecycle rechecks within the 13:xx hour (13:05 and 13:47) —
the first performs a fresh provider fetch and persists a decision
snapshot; the second makes **zero** additional
`get_option_chain_for_dte_window` calls and produces no additional
snapshot, because `_run_lifecycle_only_safety_check` sees
`control_loop_store.get_cycle_record(lifecycle_cycle_id)` already
populated and returns immediately. This is the "duplicate/retry guard"
the task spec names explicitly, not a scheduler: nothing causes the
second call to happen — it is still a separate, manually-initiated
invocation that merely finds its hour's slot already used.

## H. Next-hour behavior

`TestFreshHourlyRecheckAfterMainCycle` and `TestDistinctHourBuckets`:
a recheck in a NEW hour bucket (whether the next hour the same day, or
crossing 13:59 -> 14:00 ET) always performs its own fresh
`get_option_chain`/`get_option_chain_for_dte_window` call(s) and
persists its own new decision snapshot under its own new hourly cycle
id — proven by asserting the provider's call count strictly increases
and a new `ControlCycleRecord` exists for the new id.

## I. Before-scan-window behavior

`TestGateClosedPreservesMainDailyId`: with the new-position scan gate
CLOSED (pre-market) and an open position, the lifecycle-only hourly
check runs and persists its own hourly record, while the main
`validation-{date}` id remains completely untouched
(`get_cycle_record(main_id) is None`); a LATER invocation once the gate
opens still runs the full, normal scan-eligible cycle against that
still-untouched `validation-{date}` slot — proving a gate-closed
lifecycle recheck can never consume or block the day's real
opportunity-scan slot.

## J. After-main-cycle behavior

`TestFreshHourlyRecheckAfterMainCycle` and
`TestMainCycleRanThenGateClosesAgain`: once the main daily cycle has
run, a later invocation — whether the scan gate is still open or has
since closed again — always routes to `_run_lifecycle_only_safety_check`
for any open position, never re-enters the opportunity-scan
continuation, and `review_store.all_candidates(...)` stays empty (still
at most one candidate per trading day, proven directly).

## K. No-position behavior

`TestMainCycleRanNoPositionsLaterNoOp` (CASE 1) and the zero-position
branch of `TestGateClosedPreservesMainDailyId`/CASE 7's own no-position
leg: with no open positions, a later invocation — gate open or closed —
performs no provider call, no fetch, no lifecycle evaluation, and
persists no cycle record at all; it returns immediately after checking
`existing_positions`.

## L. Exact held-expiration retrieval proof

`TestTwoExpirationsPreservedAcrossHourlyChecks` and
`TestBelowMinDtePreservedAcrossHourlyChecks`: V1.5.10's
`_fetch_existing_position_chain` (grouped by ticker, one
`get_option_chain_for_dte_window(min_dte=max_dte=that position's own
real DTE, ...)` call per distinct held expiration, merged with the
provider's own near-term default) is called **verbatim, unmodified** by
the new hourly path — proven by asserting the exact `(30, 30)` and
`(10, 10)` DTE windows are both requested across a two-expiration,
two-position SPY test, and that a QQQ position held 10 DTE below the
`[20, 45]` candidate-entry window still gets its own exact-DTE request
on a LATER hourly recheck, not just on the first invocation.

## M. Stale/missing/provider-failure behavior

`TestMissingAndStaleContractsFailClosedOnRecheck` (both sub-tests) and
`TestProviderFailureIsolationOnRecheck`: a held contract that is
missing entirely, or present but stale, still fails that position
closed to `DATA_INSUFFICIENT` (no decision snapshot persisted) on an
HOURLY recheck exactly as it would on the main cycle — unchanged
matching/freshness logic, now exercised again by the hourly path; one
ticker's simulated provider exception (`RuntimeError`) is isolated from
a second, healthy ticker's position, which is still fully evaluated in
the same hourly cycle — the pre-existing per-ticker isolation doctrine,
unchanged.

## N. Alert dedup proof

`TestUnresolvedAlertDedupAcrossHourlyChecks`: the same still-true DTE
forced-exit condition on one position, evaluated across two separate
hourly rechecks, raises exactly ONE unresolved `ControlLoopAlertType
.DTE_EXIT` alert (`raise_alert_if_new` dedupes by `(scope=position_id,
alert_type)` against `all_unresolved_alerts()`, independent of cycle
id — a pre-existing, zero-new-code mechanism), while BOTH hourly checks
each persist their own `decision_snapshots_for_cycle` audit row under
their own distinct hourly cycle id. (Note: the test deliberately uses a
DTE outside `[0, 7]` so the pre-existing, separate
`EXPIRATION_APPROACHING` alert type — raised independently whenever
`0 <= dte <= 7`, regardless of what other alert type already fired for
the same position — does not also fire and change the expected count;
this is documented, unmodified, pre-existing alert behavior, not
something this release touches.)

## O. Known alert non-resolution behavior

`TestAlertDoesNotAutoResolve`: a trigger condition true at the first
hourly check (raising one unresolved alert), then cleared by the second
check (position replaced by one well above the forced-exit DTE
threshold) — the earlier alert remains unresolved; nothing in this
release calls `resolve_alert()` or adds auto-resolution. This is the
SAME documented, pre-existing limitation the V1.5.11 audit flagged —
explicitly out of scope per the task's own instruction, left
unresolved and recorded again here (see AB).

## P. Proof lifecycle remains observation-only

`TestNoCandidatesFromHourlyPath`: across repeated hourly invocations
(gate closed, two positions, multiple hours), `review_store
.all_candidates(cohort_id=...)` stays empty at every point — zero
candidates ever generated through this path, exactly as before this
release (nothing in `_run_lifecycle_only_safety_check` changed in this
respect; only its cycle-id string and its caller's reachability
changed).

## Q. Proof no PaperBroker/order/fill/confirmation

`TestNoExecutionPathFromHourlyRecheck`: asserts, via direct source
inspection of `scripts/run_validation_cycle.py`, that `PaperBroker`,
`place_order`, `confirm_fill`, and `confirm_candidate` are never
imported or referenced anywhere in the module — the same structural
guarantee the V1.5.11 audit already established (`run_control_cycle`/
`run_outer_cycle` never construct a `PaperBroker`;
`OuterCycleInputs.opportunity_scan=None, skip_opportunity_scan=True`
makes `_run_opportunity_scan_stage` a pure no-op for two independent
reasons) now re-verified against the actual, modified file.

## R. Proof no automatic scheduler/background POST

No cron, launchd, APScheduler, asyncio background loop, browser timer,
automatic dashboard trigger, broker callback, or webhook monitoring was
added anywhere in this release — confirmed by `grep` across the diff
(no new import of `sched`, `apscheduler`, `asyncio.create_task` outside
existing test-harness code, `cron`, or any new dashboard route beyond
the pre-existing single `POST /api/validation-cycle/run`).
`TestDashboardHourlyRecheck` exercises that one existing route directly
(not a new one) and confirms a second POST within the same hour bucket
dedupes exactly like the CLI path, and that all OTHER dashboard routes
remain read-only `GET`s. Every lifecycle-only invocation in this
release remains exactly as manually-initiated (CLI `python
scripts/run_validation_cycle.py`, or that one existing dashboard POST)
as it already was before V1.5.11 — the hourly bucket changes only what
a manual recheck is ALLOWED to do, never what causes one to happen.

## S. Tradier request/rate-limit implications

No change to `RateLimitState`/rate-limit wiring in this release. As
the V1.5.11 audit already documented: the real operational
`run_validation_cycle` does not currently populate `RateLimitState`
from live Tradier response headers — a known, pre-existing gap, out of
scope here (see AB). The one-hourly-recheck-per-clock-hour policy this
release adds is intentionally bounded and conservative: at most one
additional existing-position fetch cycle per America/New_York clock
hour, strictly less frequent than even a plausible future automated
poll, and still gated entirely behind manual operator action. No
Tradier production request was made during development or testing —
every test in this release uses an in-memory fake provider
(`_DenseExpirationFakeProvider`) and injects an explicit, simulated
`now`.

## T. Active-cohort compatibility

No change to `config/operations.yaml`, `config/validation.yaml`,
`config/risk_limits.yaml`, `config/brokers.yaml`, `config/universe.yaml`,
NAV, the SPY/QQQ universe, the enabled strategy set, candidate-entry
DTE, liquidity/Quant/Risk thresholds, ranking policy, Tradier/Fidelity
configuration, human-confirmation requirements, or the PaperBroker
architecture. `data/options_agent.db` was never opened, read, or
written by this work. No historical cycle record was rewritten, reset,
or deleted.

## U. Files changed

- `scripts/run_validation_cycle.py` — `run_validation_cycle`'s
  top-level cycle-id check changed from an unconditional early `return`
  to a captured boolean (`main_cycle_already_ran`) used only to route to
  `_run_lifecycle_only_safety_check` when positions exist;
  `_run_lifecycle_only_safety_check`'s own cycle-id construction changed
  from date-only to Eastern-local-date-and-hour; module/function
  docstrings updated to document both. `_fetch_existing_position_chain`
  (V1.5.10) is untouched.
- `tests/acceptance/test_lifecycle_market_hours_separation.py` — the
  pre-existing `_lifecycle_cycle_id()` test helper updated to the new
  hourly format (using the same `EASTERN` zoneinfo), matching the
  production change; no test behavior/assertions otherwise changed.
- `tests/acceptance/test_lifecycle_exact_expiration_retrieval.py` —
  additive, backward-compatible: `_DenseExpirationFakeProvider` gained
  an optional `reference_now`/`set_reference_now()` so a test can reuse
  one provider instance across multiple simulated `now` instants
  (defaults to the real wall clock, preserving every existing caller's
  behavior exactly); `get_option_chain`/`get_option_chain_for_dte_window`
  now re-stamp every returned contract's `.timestamp` to that call's own
  `as_of` via `model_copy`, so a fake chain's quotes are always "fresh as
  of whenever it was fetched" relative to the test's own simulated
  clock, matching how a real provider's quotes behave, rather than
  carrying whichever real wall-clock instant the contract objects
  happened to be constructed at.
- `tests/acceptance/test_lifecycle_hourly_recheck.py` (new, 18 test
  classes covering the 22 required items).
- `src/validation/freeze.py` — `FREEZE_NAME`/`MANIFEST_VERSION` bumped
  from `PAPER_TRADING_V1.5.10`/`1.5.10` to `PAPER_TRADING_V1.5.11`/
  `1.5.11`, with a new changelog comment block.
- `tests/unit/validation/test_freeze.py` — version assertions bumped to
  `1.5.11`.
- `VALIDATION_MANIFEST.json` — regenerated via `make freeze-manifest`.
- `STEP_23_11_FREEZE_REPORT.md` (this file) — new.
- `progress.md` — new entry.

No changes to `src/risk/`, `src/quant/`, `src/portfolio/orchestrator.py`,
`src/portfolio/control_loop.py`, `src/portfolio/revaluation.py`,
`src/portfolio/market_session.py`, `src/lifecycle/`, `src/llm/`,
`src/brokers/fidelity.py`, `src/dashboard/` (the existing single
validation-cycle POST route and its underlying call are unchanged —
only what that call now does inside `run_validation_cycle` changed),
`config/*.yaml`, or `data/options_agent.db`.

## V. Focused tests

All 18 test classes / 18 test methods in the new
`tests/acceptance/test_lifecycle_hourly_recheck.py` passed. The broader
focused run (`test_lifecycle_market_hours_separation.py` +
`test_lifecycle_exact_expiration_retrieval.py` +
`test_lifecycle_hourly_recheck.py` + `test_review_only_daily_cycle.py` +
`test_market_hours_gate.py` + `test_dte_window_chain_retrieval.py`, 58
tests total) passed except the 4 known pre-existing date-rot failures
within that subset (see X).

## W. Full-suite result

3806 passed, 6 skipped, 9 failed, in ~62 seconds.

## X. Exact known failures + pristine V1.5.10 comparison

The same 9 node IDs known since V1.5.7/V1.5.8/V1.5.9/V1.5.10, unchanged
in count, identity, and root cause (the fixed-calendar-date
`tests/unit/review/conftest.py::EXPIRATION` fixture drifting outside
the `[20, 45]` DTE window as real wall-clock time advances):

1. `tests/acceptance/test_market_hours_gate.py::TestBackendPostInsideSession::test_reaches_the_mocked_runner_path`
2. `tests/acceptance/test_market_hours_gate.py::TestCliInsideSession::test_reaches_the_mocked_normal_path`
3. `tests/acceptance/test_opportunity_evaluation_timestamp.py::TestOpportunityEvaluationTimestampFixV157::test_a_chain_timestamped_after_the_cycle_start_no_longer_silently_loses_the_candidate`
4. `tests/acceptance/test_opportunity_evaluation_timestamp.py::TestOpportunityEvaluationTimestampFixV157::test_control_cycle_record_shows_zero_silent_generation_exceptions`
5. `tests/acceptance/test_review_only_daily_cycle.py::TestReviewOnlyDailyCycleEndToEnd::test_daily_cycle_never_auto_fills_and_surfaces_exactly_one_candidate`
6. `tests/acceptance/test_review_only_daily_cycle.py::TestReviewOnlyDailyCycleEndToEnd::test_confirm_candidate_places_exactly_one_order_even_when_invoked_twice`
7. `tests/acceptance/test_run_validation_cycle_cli.py::TestTradierProductionProviderPasses::test_tradier_production_reaches_the_mutating_cycle`
8. `tests/unit/dashboard/test_frontend_control_center.py::TestValidationEndpointIdempotencyUnaffected::test_dashboard_route_running_twice_the_same_day_is_a_documented_no_op`
9. `tests/unit/dashboard/test_operator_status.py::TestValidationCycleRunRoute::test_end_to_end_dashboard_trigger_matches_the_cli_safety_outcome`

Re-ran all 9 against pristine V1.5.10 (the branch's own HEAD,
`5699d78`, via `git stash` rather than a worktree, since this branch's
HEAD already IS the V1.5.10 freeze commit) at the same wall clock —
identical 9 failures, identical root cause. Working tree restored
cleanly afterward (`git stash pop`, verified clean diff against the
pre-stash state). Confirmed not V1.5.11 regressions. Zero additional
failures beyond these 9 anywhere in the full suite.

## Y. Freeze result

`make verify-freeze` passed cleanly against the regenerated V1.5.11
manifest: `PAPER_TRADING_V1.5.11 / SOFTWARE FREEZE VERIFIED`.

## Z. Implementation commit / freeze commit / push / tag results

Pending — created immediately after this report (see final chat report
for hashes and push/tag results).

## AA. Explicit confirmation — unchanged items

The active validation cohort (`paper-trading-v1.4.3-validation-2026-09-22`),
the operational DB `data/options_agent.db`, $100,000 NAV, the SPY/QQQ
universe, the enabled strategy set, candidate-entry DTE 20-45, all
liquidity/Quant/Risk thresholds, risk-per-trade/capital-allocation
settings, ranking policy, Tradier/Fidelity configuration, the
PaperBroker-only architecture, the human-confirmation requirement, the
`TradeProposal` validator, V1.5.10's exact held-expiration retrieval
semantics, and all canonical timestamp handling were **not changed**.
No historical record was rewritten. No official validation cycle was
run. No candidate was confirmed. No operational position was placed or
simulated. Tradier production was never contacted. No credential was
printed. No scheduler, background loop, or automatic trigger of any
kind exists anywhere in this repository after this release.

## AB. Remaining known issues

- The 9 known date-rot test failures remain open by design (explicitly
  out of scope per instruction) and will keep drifting further out of
  the DTE window as wall-clock time advances until someone updates
  `tests/unit/review/conftest.py`'s fixture anchor.
- **Alerts do not automatically resolve when the trigger condition
  clears** (confirmed again by `TestAlertDoesNotAutoResolve`) — a
  known, pre-existing issue, explicitly out of scope for this release
  per the task's own instruction. No auto-resolution behavior was
  silently added.
- The real operational `run_validation_cycle` does not currently
  populate `RateLimitState` from live Tradier response headers — a
  known, pre-existing gap, not redesigned in this release per
  instruction.
- The one-hourly-recheck-per-clock-hour policy is intentionally
  conservative and bounded; it does not attempt to model or approximate
  genuine real-time position monitoring, and remains entirely
  dependent on an operator remembering to manually re-invoke the
  script or dashboard button within a given hour.
