# STEP 23.10 FREEZE REPORT — PAPER_TRADING_V1.5.10

## Status

**PAPER_TRADING_V1.5.10 / SOFTWARE FREEZE VERIFIED**

A narrow safety release. Cohort `paper-trading-v1.4.3-validation-2026-09-22`
remains ACTIVE, untouched, and unreset by this step. No official
validation cycle, `confirm_candidate.py` invocation against an
operational candidate, or production Tradier call occurred during
development or testing. No risk limit, Quant threshold, liquidity
threshold, candidate DTE policy, ranking policy, candidate-generation
behavior, NAV, universe, or strategy set was changed. Lifecycle
cadence/idempotency (the once-daily behavior) is unchanged and
explicitly out of scope.

## A. Root cause

The V1.5.9 acceptance audit (read-only, no changes made) identified one
narrow blocker before the first paper position: V1.5.9's
`_run_lifecycle_only_safety_check` fetched existing-position market
data for a ticker via `provider.get_option_chain(ticker)` alone.
`TradierMarketDataProvider.get_option_chain` fetches only its nearest
`max_expirations` (default 6) calendar expirations, **regardless of
DTE** (confirmed directly from that method's own docstring and
implementation). For a position opened at this platform's own 20-45
DTE policy on a dense-expiration underlying (SPY/QQQ, the active
cohort's universe), the position's own held expiration is highly
likely to be entirely absent from that default fetch. Unlike the
analogous V1.5.8 confirmation-retrieval defect, the risk here is
**highest right after a position is opened** (its far-dated expiration
sits well behind the provider's nearest-N cutoff) and **falls** as the
position's DTE naturally shrinks toward the front of that list over
time — the opposite of "aging into the problem." A position can also
legitimately remain open **below** the candidate-entry DTE floor of 20,
which the global `[20, 45]` scan window could never cover either way.

The practical effect: `src.portfolio.revaluation.revalue_position`
correctly, honestly reports `PositionValuationStatus.DATA_INSUFFICIENT`
for any leg it can't match in the fetched chain — never a fabricated
price (CLAUDE.md invariants 2/5 held throughout) — but this meant the
Lifecycle Engine/Risk kill-switch monitoring V1.5.9 worked to
guarantee gets *called* on gate-closed days frequently had no usable
data to evaluate the position *with*.

## B. Old retrieval flow

```
_run_lifecycle_only_safety_check:
    for ticker in position_tickers:
        fetch_results[ticker] = await provider.get_option_chain(ticker)
        # plain nearest-max_expirations call, no DTE awareness at all
```

The gate-open main cycle's existing-position coverage was **better but
coincidental**, not guaranteed: a ticker that was both an existing
position AND in the opportunity-scan universe got a DTE-windowed
`get_option_chain_for_dte_window(min_dte=20, max_dte=45, ...)` call
merged in (because that ticker was being scanned for candidates
anyway) — but a position-only ticker, or one whose real DTE had
dropped below 20, got no such protection even in the gate-open path.

## C. New retrieval flow

```python
async def _fetch_existing_position_chain(provider, positions, *, now):
    ticker = positions[0].ticker
    chain = await provider.get_option_chain(ticker)          # unchanged baseline
    if isinstance(provider, DteWindowOptionChainProvider):
        for expiration in sorted({p.expiration for p in positions}):
            dte = (expiration - now.date()).days
            exact_chain = await provider.get_option_chain_for_dte_window(
                ticker, min_dte=dte, max_dte=dte, as_of=now.date(),
            )
            chain = merge_option_chains(chain, exact_chain)
    return chain
```

Used by BOTH:
- `_run_lifecycle_only_safety_check` (replacing its bare `get_option_chain`
  call per ticker), and
- the main scan-eligible cycle's own existing-position fetch (replacing
  the ad hoc `lifecycle_chain = await provider.get_option_chain(ticker)`
  sub-call in the merge branch, and the bare `get_option_chain(ticker)`
  fallback for a position-only ticker not in the universe).

A provider that does **not** implement `DteWindowOptionChainProvider`
(e.g. Alpaca) gets byte-identical behavior to pre-V1.5.10 — the
`isinstance` check means the helper only ever *adds* coverage on top
of the unchanged baseline call, never replaces or removes it.

## D. How actual held expirations are determined

Directly from the canonical domain model, never inferred: each
`src.risk.portfolio_risk.PortfolioPosition` already carries its own
`expiration: date` field (one expiration per position — this
platform's 16-strategy library has no calendar-spread structure, so
every position's legs share a single expiration by construction). The
helper groups a ticker's open positions, takes the **distinct set** of
their `.expiration` values, and requests each one **exactly**
(`min_dte = max_dte = (expiration - as_of).days`) — never the
candidate-entry `[20, 45]` window, and never a min..max range spanning
multiple distinct expirations (which could still silently drop one of
them behind `max_expirations`'s own bound if there were more than 6
distinct intervening provider expirations). This mirrors
`src.review.confirmation._fetch_exact_expiration_chain` (V1.5.8's own
fix for the analogous confirmation-retrieval gap), generalized from "one
review candidate" to "every distinct expiration a ticker's open
positions actually hold."

## E. Exact contract-identity guarantees

Unchanged, because this release never touches matching logic — only
retrieval. `src.portfolio.revaluation.build_contract_index` indexes
fetched contracts by `(expiration, strike, right)`; `revalue_position`
looks up each leg via `contract_index.get((position.expiration,
leg.strike, leg.right))`. There is no fuzzy/nearest matching anywhere
in this path. Underlying identity is enforced structurally: contracts
are already bucketed per-ticker in `fetch_results`/`contracts_by_ticker`
before this lookup ever runs. `PortfolioPositionLeg` has no
`option_symbol` field (confirmed by reading `src.risk.portfolio_risk`),
so option-symbol identity was never part of this platform's existing
matching contract and this release does not add one — the
`(underlying, expiration, strike, right)` quadruple is, and remains,
authoritative. Consequently: no nearest-expiration substitution, no
nearest-strike substitution, no wrong-right substitution — proven, not
merely asserted, by Items 7 and 8 of the new test suite (a provider
that returns contracts at the wrong expiration, or the wrong
strike/right, is rejected exactly like a provider that returns nothing
at all).

## F. Multi-leg behavior

All legs of one `PortfolioPosition` share its single `.expiration`
(§D), so one exact-DTE-window request per distinct expiration covers
every leg of every position at that expiration. `revalue_position`
already required (unchanged) that **every** leg match a fresh,
non-stale contract before reporting `status=OK`; one unmatched leg
fails the **whole position** closed to `DATA_INSUFFICIENT` — never a
partial valuation using only the matched legs. Proven by Item 6 of the
new test suite (a two-leg spread with one leg's strike entirely absent
from the provider's data) and Item 5 (both legs present and matched).

## G. Missing/stale/provider-failure behavior

- **Missing contract** (leg genuinely absent from the provider, even
  at the exact requested expiration): `revalue_position` reports
  `DATA_INSUFFICIENT`; `run_control_cycle`'s per-position loop reaches
  a decision (`recommendations_created` increments) but never persists
  a decision snapshot for it (a pre-existing, unmodified behavior this
  release does not change) — the position is still counted
  (`positions_evaluated`), never silently dropped.
- **Stale contract** (right identity, wrong freshness): the unmodified
  quote-age freshness gate in `revalue_position` still rejects it —
  retrieving the *correct* contract is not sufficient on its own; it
  must also be fresh. Proven by Item 9.
- **Provider exception for one ticker**: isolated per-ticker, exactly
  like the pre-existing (V1.5.9) isolation doctrine — one ticker's
  `try/except` failure never aborts evaluation of another ticker's
  position. Proven by Item 10.
- **Chain-level quality-gate failure** (whole ticker's data rejected,
  e.g. staleness at the chain level): still surfaces via
  `ControlCycleRecord.symbols_failed`/`degraded_mode`, unchanged from
  V1.5.9.

## H. Gate-open vs gate-closed behavior

Both paths now share `_fetch_existing_position_chain` for existing
positions. The gate-closed path (`_run_lifecycle_only_safety_check`)
previously had **no** DTE-aware retrieval for existing positions at
all — this is the release's primary fix. The gate-open path previously
had DTE-aware coverage **only when a position's ticker happened to
also be an opportunity-scan universe ticker** (true for the active
cohort's SPY/QQQ universe, but not guaranteed by the code — a
coincidence, not a contract); it now has the same explicit, position-
expiration-driven guarantee the gate-closed path has, proven
independently of that coincidence by Item 12 of the new test suite
(a position at 10 DTE — outside both the provider's nearest-six
default AND the global `[20, 45]` scan window).

## I. Proof opportunity scanning remains disabled when gate closed

Unchanged from V1.5.9 and re-verified directly: `_run_lifecycle_only_
safety_check` still builds `OuterCycleInputs` with
`opportunity_scan=None, skip_opportunity_scan=True`, and still never
constructs a `PaperBroker`. Item 11 of the new test suite asserts, end
to end, that even when the V1.5.10 fix successfully retrieves and
evaluates a position that the old retrieval would have failed closed,
`review_store.all_candidates(...)` remains empty and the main
scan-eligible `cycle_id` record is never created.

## J. Proof lifecycle cadence/idempotency did NOT change

`_run_lifecycle_only_safety_check`'s own cycle-id construction
(`f"validation-{date}-lifecycle"`) and its own idempotency check
(`if control_loop_store.get_cycle_record(lifecycle_cycle_id) is not
None: ... return True`) were not touched by this release — the diff to
that function is confined to which fetch helper it calls per ticker.
Item 14 of the new test suite directly re-proves V1.5.9's own
idempotency property with the new retrieval code path: a second
same-day invocation makes **zero** additional calls to either
`get_option_chain` or `get_option_chain_for_dte_window`, and persists
no duplicate decision snapshot.

## K. Files changed

- `scripts/run_validation_cycle.py` — new `_fetch_existing_position_chain`
  helper; `_run_lifecycle_only_safety_check` and the main cycle's
  existing-position fetch branch both call it instead of a bare
  `get_option_chain`; module docstring updated.
- `tests/acceptance/test_lifecycle_exact_expiration_retrieval.py` (new,
  15 tests).
- `tests/acceptance/test_dte_window_chain_retrieval.py` — one
  pre-existing V1.5.6 test's exact call-list assertion updated (not
  deleted) to account for the new, intentional second exact-DTE call in
  the gate-open merge path; computed dynamically from the real wall
  clock, never hardcoded.
- `src/validation/freeze.py` — `FREEZE_NAME`/`MANIFEST_VERSION` bumped
  from `PAPER_TRADING_V1.5.9`/`1.5.9` to `PAPER_TRADING_V1.5.10`/`1.5.10`,
  with an extensive new changelog comment block.
- `tests/unit/validation/test_freeze.py` — version assertions bumped to
  `1.5.10`.
- `VALIDATION_MANIFEST.json` — regenerated via `make freeze-manifest`.
- `STEP_23_10_FREEZE_REPORT.md` (this file) — new.
- `progress.md` — new entry.

No changes to `src/risk/`, `src/quant/`, `src/portfolio/orchestrator.py`,
`src/portfolio/control_loop.py`, `src/portfolio/revaluation.py`,
`src/portfolio/market_session.py`, `src/lifecycle/`, `src/llm/`,
`src/brokers/fidelity.py`, `config/*.yaml`, or `data/options_agent.db`.

## L. Focused tests

All 15 new tests in
`tests/acceptance/test_lifecycle_exact_expiration_retrieval.py` passed.
All 3 tests in the updated `tests/acceptance/test_dte_window_chain_retrieval.py`
passed after the one intentional assertion update. The full
`tests/acceptance/test_lifecycle_market_hours_separation.py` (V1.5.9),
`test_review_only_daily_cycle.py`, `test_run_validation_cycle_cli.py`,
`test_market_hours_gate.py`, and `tests/unit/dashboard/` suites passed
except the known pre-existing failures (see N).

## M. Full-suite totals

3788 passed, 6 skipped, 9 failed, in ~65 seconds.

## N. Exact failures and pristine V1.5.9 comparison

The same 9 node IDs known since V1.5.7/V1.5.8/V1.5.9, unchanged in
count, identity, and root cause (the fixed-calendar-date
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

Re-ran all 9 against pristine V1.5.9 (`781263e`) via a temporary `git
worktree`, same wall clock — identical 9 failures, identical root
cause. Worktree cleanly removed afterward (`git status --short` clean
before and after). Confirmed not V1.5.10 regressions.

One additional test newly failed during the first full-suite run and
was fixed by updating its assertion (not by code changes):
`tests/acceptance/test_dte_window_chain_retrieval.py::TestDteWindowBranchTakenForUniverseTickers::test_ticker_both_position_and_universe_gets_both_fetches_merged[True]`
expected `get_option_chain_for_dte_window_calls == [("SPY", 20, 45)]`
exactly — V1.5.10's intentional fix adds a second, correct call for the
position's own real DTE. Updated per CLAUDE.md's own testing
discipline ("a test asserting a now-reversed architectural decision
should be updated to assert the new, intentional behavior — not
deleted and not left failing"). After the update, this test — and the
full suite — shows zero new failures.

## O. Freeze result

`make verify-freeze` passed cleanly against the regenerated V1.5.10
manifest: `PAPER_TRADING_V1.5.10 / SOFTWARE FREEZE VERIFIED`.

## P. Implementation commit

Pending — created immediately after this report (see final chat
report for the hash).

## Q. Freeze commit

Pending — created immediately after the implementation commit (see
final chat report for the hash).

## R. Push result

Pending — `git push -u origin claude/options-trading-agent-2b4yi8`
attempted after both commits (see final chat report for the result).

## S. Tag result

Pending — `git tag paper-trading-v1.5.10` created locally after the
freeze commit; `git push origin paper-trading-v1.5.10` attempted (see
final chat report for the result — every prior tag push this session
has failed with HTTP 403; reported honestly either way, never worked
around).

## T. Explicit confirmation — unchanged items

The active validation cohort (`paper-trading-v1.4.3-validation-2026-09-22`),
the operational DB `data/options_agent.db` (never opened, read, or
written by this work), $100,000 NAV, the SPY/QQQ universe, the enabled
strategy set, candidate-entry DTE 20-45, all liquidity/Quant/Risk
thresholds, risk-per-trade/capital-allocation settings, ranking policy,
Tradier/Fidelity configuration, the PaperBroker-only architecture, the
human-confirmation requirement, the `TradeProposal` validator, and all
canonical timestamp handling were **not changed**. No historical record
was rewritten. No official validation cycle was run. No candidate was
confirmed. No operational position was placed or simulated. Tradier
production was never contacted. No credential was printed.

## U. Remaining known risks/issues

- The 9 known date-rot test failures remain open by design (explicitly
  out of scope per instruction) and will keep drifting further out of
  the DTE window as wall-clock time advances until someone updates
  `tests/unit/review/conftest.py`'s fixture anchor.
- Lifecycle cadence/idempotency (the date-only cycle_id, once-daily
  behavior) remains the documented architectural limitation the
  V1.5.9 audit identified — explicitly out of scope for this release,
  per the task's own instruction, and unresolved.
- `_fetch_existing_position_chain` issues one additional
  `get_option_chain_for_dte_window` call per distinct held expiration
  per ticker, on top of the existing baseline `get_option_chain` call —
  a small, bounded increase in provider calls (never more than the
  number of distinct expirations a ticker's open positions actually
  hold), not a rate-limit concern for this cohort's position count but
  worth noting for a future cohort with many more concurrently open
  positions per ticker.
