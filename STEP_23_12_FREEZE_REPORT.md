# STEP 23.12 FREEZE REPORT — PAPER_TRADING_V1.5.12

## Status

**PAPER_TRADING_V1.5.12 / SOFTWARE FREEZE VERIFIED**

A narrowly scoped proposal-ID length bug fix. Cohort
`paper-trading-v1.4.3-validation-2026-09-22` remains ACTIVE, untouched,
and unreset by this step. No official validation cycle,
`confirm_candidate.py` invocation against an operational candidate, or
production Tradier call occurred during development or testing. No
risk limit, Quant threshold, liquidity threshold, candidate DTE
policy, universe, enabled strategies, candidate ranking, position
sizing, market-hours behavior, lifecycle behavior, Tradier/Fidelity
behavior, human-confirmation requirement, PaperBroker execution
behavior, or `TradeProposal.proposal_id`'s `max_length=64` was
changed. Starting NAV remains $100,000.

## A. Root cause

During the official 2026-10-06 validation cycle, `scripts/run_
validation_cycle.py` called the opportunity scan with:

```python
proposal_id_prefix=f"validation-scan-{now.date().isoformat()}"
```

but `src.workflows.candidate_generation._next_id()` INDEPENDENTLY
appends `now.date().isoformat()` to every `proposal_id` it builds
(its own, pre-existing SY-001 uniqueness fix):

```python
return f"{proposal_id_prefix}-{ticker}-{now.date().isoformat()}-{strategy_tag}-{expiration.isoformat()}-{strike_part}-{counter}"
```

The scan date was therefore encoded TWICE. A realistic multi-leg
PUT_CREDIT_SPREAD id (ticker + two strikes) reached 66 characters --
two over `TradeProposal.proposal_id`'s `max_length=64` -- so Pydantic
raised a `ValidationError` for both PCS candidates before they ever
reached Quant/Risk. The shorter, single-leg CASH_SECURED_PUT id (62
characters) happened to stay just under the limit, which is exactly
why only PUT_CREDIT_SPREAD failed that day while CASH_SECURED_PUT's
two candidates both passed Quant and were correctly rejected by Risk
(`reject_max_trade_risk`).

## B. Exact production code change

`scripts/run_validation_cycle.py`, the single `OpportunityScanConfig`
construction site inside `run_validation_cycle()`:

```python
# before
broker_capabilities=broker_capabilities, proposal_id_prefix=f"validation-scan-{now.date().isoformat()}",

# after
broker_capabilities=broker_capabilities, proposal_id_prefix="validation-scan",
```

`_next_id()` itself was not touched -- it still appends the date
exactly once, and now does so against a bare prefix that no longer
duplicates it. No other production call site shares this bug:
`src/portfolio/opportunity_scan.py`/`src/portfolio/orchestrator.py`'s
own `proposal_id_prefix: str = "control-loop-scan"` default never
carried a date in the first place (confirmed by grep across every
`proposal_id_prefix=` call site in `scripts/` and `src/`).

## C. Why SY-001 uniqueness/idempotency remains protected

`_next_id()`'s own uniqueness contract — date + ticker + strategy tag
+ expiration + strike(s) + in-call counter, all concatenated after the
prefix — is completely unmodified. Removing the redundant PREFIX-level
date changes only how many times the date appears in the final id
(twice -> once); it changes none of `_next_id()`'s own uniqueness
inputs. Consequently:
- the scan date is still present in every proposal_id (now exactly
  once, proven directly);
- two different scan dates for the same ticker/strike still produce
  different ids, because `_next_id()`'s own date component still
  changes day to day;
- multiple candidates in the same scan remain unique, because
  `_next_id()`'s own ticker/strategy-tag/expiration/strike(s)/counter
  components are unaffected by the prefix's own content.

## D. Before/after representative PCS proposal ID and character counts

Representative realistic SPY PUT_CREDIT_SPREAD id (ticker SPY, strikes
600/595, expiration 2026-10-30, scan date 2026-10-06):

| | ID | Length |
|---|---|---|
| Before | `validation-scan-2026-10-06-SPY-2026-10-06-pcs-2026-10-30-600-595-1` | 66 |
| After | `validation-scan-SPY-2026-10-06-pcs-2026-10-30-600-595-1` | 55 |

Equivalent single-leg CASH_SECURED_PUT id, for contrast (explains why
CSP candidates passed that day while PCS did not):

| | ID | Length |
|---|---|---|
| Before | `validation-scan-2026-10-06-SPY-2026-10-06-csp-2026-10-30-600-1` | 62 |
| After | `validation-scan-SPY-2026-10-06-csp-2026-10-30-600-1` | 51 |

`TradeProposal.proposal_id`'s `max_length=64` was not changed; the
fix brings every realistic candidate id back under it with margin to
spare.

## E. Files changed

- `scripts/run_validation_cycle.py` -- the one-line `proposal_id_prefix`
  fix described in §B, plus an explanatory comment at the call site.
- `Makefile` -- the `confirm-candidate` usage comment updated from the
  stale `ID=validation-scan-2026-09-22-SPY-...` example to one matching
  the corrected id layout (`ID=validation-scan-SPY-2026-09-22-csp-
  2026-10-15-600-1`). `confirm-candidate`'s own behavior is unchanged.
- `tests/unit/workflows/test_candidate_generation.py` -- new
  `TestProposalIdLengthRegressionV1512` class (5 tests; items A-E, plus
  a direct reproduction of the pre-fix failure for documentation).
- `tests/acceptance/test_proposal_id_length_regression_v1512.py` (new)
  -- behavior-level test (item G) against the real production entry
  point.
- `src/validation/freeze.py` -- `FREEZE_NAME`/`MANIFEST_VERSION` bumped
  from `PAPER_TRADING_V1.5.11`/`1.5.11` to `PAPER_TRADING_V1.5.12`/
  `1.5.12`, with a new changelog comment block.
- `tests/unit/validation/test_freeze.py` -- version assertions bumped
  to `1.5.12`.
- `VALIDATION_MANIFEST.json` -- regenerated via `make freeze-manifest`.
- `STEP_23_12_FREEZE_REPORT.md` (this file) -- new.
- `progress.md` -- new entry.

No changes to `src/risk/`, `src/quant/`, `src/portfolio/orchestrator.py`,
`src/portfolio/control_loop.py`, `src/portfolio/opportunity_scan.py`,
`src/workflows/candidate_generation.py`'s `_next_id()` itself,
`src/llm/schemas.py` (`TradeProposal.proposal_id`'s `max_length=64`
untouched), `src/lifecycle/`, `src/llm/` beyond test imports,
`src/brokers/fidelity.py`, `config/*.yaml`, or `data/options_agent.db`.

## F. New/updated tests

**`tests/unit/workflows/test_candidate_generation.py::TestProposalIdLengthRegressionV1512`**
(5 tests, all using the corrected bare `"validation-scan"` prefix and
realistic SPY-sized strikes, 600/595):
1. `test_realistic_pcs_with_corrected_prefix_produces_a_valid_proposal`
   -- items A, B, C: a valid `TradeProposal` is produced, its
   `proposal_id` is `<= 64` characters, and the scan date appears
   exactly once.
2. `test_same_ticker_strike_two_scan_dates_still_differ_under_corrected_prefix`
   -- item D.
3. `test_multiple_candidates_in_the_same_scan_remain_unique_under_corrected_prefix`
   -- item E.
4. `test_old_double_dated_prefix_reproduces_the_october_6_incident` --
   documents that the OLD, double-dated prefix format still fails this
   exact realistic scenario with a recorded `("PUT_CREDIT_SPREAD",
   "generation_exception", "ValidationError")` diagnostic event and
   zero candidates, proving this is a faithful reproduction of the
   real incident, not a synthetic edge case.

**`tests/acceptance/test_proposal_id_length_regression_v1512.py`** (1
test, item G): intercepts the REAL `src.portfolio.orchestrator
.scan_and_rank_opportunities` call the production `run_validation_
cycle()` entry point makes (never a source-text/string match against
the script file) and asserts the `proposal_id_prefix` it actually
receives at runtime does not already end with the scan date `_next_id()`
is about to append. Verified to fail against the pristine, pre-fix
`scripts/run_validation_cycle.py` (via a temporary `git stash` of just
that file) and pass against the fixed version.

**Item F** (existing proposal-ID tests continue to pass): confirmed --
all 29 pre-existing tests in `tests/unit/workflows/test_candidate_
generation.py`, including `TestMultipleStrategiesRequested
::test_proposal_ids_are_unique` and the full
`TestProposalIdsAreUniqueAcrossScanRunsRegressionSY001` class, pass
unchanged.

## G. Targeted test results

`tests/unit/workflows/test_candidate_generation.py`: 42 passed (29
pre-existing + 5 new in `TestProposalIdLengthRegressionV1512`, plus 8
others already present in the file).
`tests/acceptance/test_proposal_id_length_regression_v1512.py`: 1
passed.
`tests/unit/workflows/` + the above two files +
`tests/acceptance/test_review_only_daily_cycle.py` together: 181
passed, 2 failed -- both failures are within the known 9 pre-existing
date-rot set (see H).

## H. Full-suite results

**3811 passed, 6 skipped, 9 failed**, in ~83 seconds.

## I. Confirmation that failures are only the known pre-existing date-rot set

The same 9 node IDs known since V1.5.7/V1.5.8/V1.5.9/V1.5.10/V1.5.11,
unchanged in count, identity, and root cause (the fixed-calendar-date
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

Re-ran all 9 against this branch's own pristine V1.5.11 HEAD
(`a08820e`) via `git stash` (rather than a worktree, since the
branch's own HEAD already IS the V1.5.11 freeze commit) at the same
wall clock -- identical 9 failures, identical root cause. Working tree
restored cleanly afterward (`git stash pop`, verified clean diff
against the pre-stash state). **Zero new failures anywhere in the full
suite.**

## J. make verify-freeze result

Passed cleanly against the regenerated V1.5.12 manifest:
`PAPER_TRADING_V1.5.12 / SOFTWARE FREEZE VERIFIED`.

## K. Implementation commit / freeze commit / push / tag

Pending -- created immediately after this report (see final chat
report for hashes and push/tag results).

## L. Explicit confirmation -- did NOT

- run an official validation cycle
- call Tradier production
- mutate the active cohort/database
- change NAV (remains $100,000)
- alter Risk/Quant/liquidity/DTE thresholds
- change universe or enabled strategies
- confirm a candidate
- place or fill a PaperBroker order
- change `TradeProposal.proposal_id`'s `max_length=64`
- change `_next_id()`'s own date/ticker/strategy-tag/expiration/
  strike(s)/counter uniqueness scheme
- weaken Pydantic validation or swallow the `ValidationError`
  differently than before

## M. Remaining risks / recommended follow-up

- Diagnostics for a schema-validation generation exception still
  report only the bounded exception-class name (`"ValidationError"`),
  not which field or what length was exceeded -- left unchanged in
  this release per the task's own explicit scope guard ("if there is
  any doubt, leave diagnostics unchanged"); flagged here as a future
  observability improvement, not addressed now.
- The 9 known date-rot test failures remain open by design, unrelated
  to this release.
- No other proposal_id-length-sensitive code path was found during
  this investigation, but a future strategy with more legs (e.g. an
  iron condor, 4 legs) could in principle approach `max_length=64`
  again at extreme strike/ticker-length combinations; this release
  does not attempt to bound that generally, only to fix the specific,
  confirmed double-date defect.
