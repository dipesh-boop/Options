# STEP 23.14 FREEZE REPORT — PAPER_TRADING_V1.5.14

## Status

**PAPER_TRADING_V1.5.14 / SOFTWARE FREEZE VERIFIED**

Adds `--universe-feasibility`, a READ-ONLY research study that runs
the exact same candidate -> Quant -> Risk pipeline as the official
cycle over a WIDER 12-symbol research universe than the official
active universe, so the operator can decide, from real Tradier
production data, whether and how to expand the official universe --
without activating anything. Cohort
`paper-trading-v1.4.3-validation-2026-09-22` remains ACTIVE, untouched,
and unreset. `config/universe.yaml` (SPY, QQQ) and the active
strategy set (`CASH_SECURED_PUT`/`COVERED_CALL`/`PUT_CREDIT_SPREAD`)
are unchanged. No official validation cycle, no `--universe-
feasibility`/`--diagnostic-scan` invocation against live Tradier
production, and no production Tradier network call of any kind
occurred during development -- every test runs against fakes only. No
risk limit, Quant threshold, liquidity threshold, candidate DTE
policy, no-trade hurdle, ranking logic, market-hours behavior,
lifecycle behavior, Tradier/Fidelity behavior, human-confirmation
requirement, or PaperBroker execution behavior was changed. Starting
NAV remains $100,000.

## A. Root architectural approach

`run_universe_feasibility_study` is a direct extension of V1.5.13's
`run_diagnostic_scan` architecture: same Tradier-production preflight,
same market-hours gate before any provider is constructed, same
`load_portfolio_read_only` (never `SqlitePortfolioStore`), same
`scan_and_rank_opportunities` call with `proposal_id_prefix=
"validation-scan"`, same post-fetch `evaluation_as_of` discipline --
scaled to a 12-symbol research universe loaded from its own, separate
config file. A new pure module, `src.workflows.universe_feasibility`,
provides per-symbol diagnostics, ranked-candidate economics,
aggregate reporting, and deterministic suitability classification --
every function in it is synchronous, takes already-fetched data as
plain arguments, and is independently unit-tested with zero event
loop, sqlite file, or network dependency.

## B. New CLI command

`python scripts/run_validation_cycle.py --universe-feasibility`
(`make universe-feasibility`) -- added to the existing mutually
exclusive group alongside `--preflight`/`--diagnostic-scan`.

## C. Research universe source/configuration

`config/universe_feasibility.yaml` (new file): SPY, QQQ, IWM, DIA,
AAPL, MSFT, NVDA, AMZN, META, GOOGL, JPM, XOM, with `strategies:
[CASH_SECURED_PUT, COVERED_CALL, PUT_CREDIT_SPREAD]`. Loaded via the
EXISTING, unmodified `src.data.universe.load_universe`/
`load_universe_strategies`, called with this file's own path --
`config/universe.yaml` is never read, merged, or written by this path.

## D/E. Official universe and active strategies unchanged

`config/universe.yaml` was not edited by this release (still SPY,
QQQ). `candidate_eligible_strategies` -- the SAME filter the official
cycle and the V1.5.13 diagnostic already apply -- narrows the
feasibility study's strategy list to exactly `CASH_SECURED_PUT`/
`COVERED_CALL`/`PUT_CREDIT_SPREAD`, structurally preventing this study
from testing strategy breadth alongside universe breadth even if a
future edit to `universe_feasibility.yaml` listed more. Proven by
`TestResearchUniverseConfigurable` (unit) and
`TestOfficialCycleAndDiagnosticUnchanged` (acceptance), the latter
hashing `config/universe.yaml`'s content before/after a feasibility
run.

## F. Quant/Risk/liquidity/DTE/no-trade-hurdle unchanged

`run_universe_feasibility_study` calls the real, unmodified
`scan_and_rank_opportunities` directly (never a reimplemented screen),
with the real `QuantFilterConfig()`/`get_default_limits()`/
`load_broker_capabilities("internal_paper")` -- identical to the
official cycle and the V1.5.13 diagnostic. Proven by
`TestProductionPipelineEquivalence` (spy-wrapped call asserting
exactly one real call, with the real prefix) and `TestDteAwareRetrieval`
(DTE-window bounds `(20, 45)` for every symbol).

## G. Read-only portfolio mechanism

`load_portfolio_read_only(ops.account_state_db_path, ops.account_id)`
-- the exact V1.5.13 true-read-only SQLite `mode=ro` loader. A
`PortfolioLoadError` fails the study closed, never silently
substituting a fresh portfolio for unreadable real account data.

## H. Proof SqlitePortfolioStore not instantiated

A static `ast`-based source scan (docstring stripped) forbids the bare
name anywhere in the function body, reinforced by a behavioral test
that monkeypatches `SqlitePortfolioStore.__init__` to raise and proves
the study still completes successfully (i.e. the raise never fires).

## I. Proof of zero database mutation

`TestReadOnlyPortfolioAndZeroPersistence::test_operational_db_byte_hash_unchanged`
hashes the operational DB file (with the `account_portfolio` table
pre-seeded, so `CREATE TABLE IF NOT EXISTS` is a genuine no-op even if
ever reached) before/after a full 12-symbol run and asserts identical
SHA-256. A companion test asserts zero rows in the candidate-review
store, zero rows in the idempotency store, and no official cycle
record -- after a real run.

## J. No PaperBroker/execution path

The same static source scan forbids `PaperBroker(`, `place_order(`,
`confirm_fill(`, and `confirm_candidate(` anywhere in the function
body.

## K. Tradier endpoints used

Exactly the same three the official cycle and V1.5.13 diagnostic
already use for market data (`/markets/quotes`, `/markets/options/
expirations`, `/markets/options/chains`, via
`get_option_chain_for_dte_window`), plus ONE new endpoint already
implemented for Step 3B and newly exercised here: `/markets/history`
(via `get_bars`, for the read-only correlation summary only). No new
endpoint was added to `TradierMarketDataProvider`.

## L. Expected request count for a 12-symbol run

`expected_tradier_request_count(num_symbols=12, max_expirations=6)` =
**108** (upper bound): per symbol, 1 quote + 1 expirations-list + up
to 6 per-expiration chain fetches (opportunity-scan phase) + 1
historical-bars fetch (correlation phase) = 9 requests/symbol x 12.
Printed by the study itself before any network call, computed from
this codebase's own documented provider behavior, never measured or
guessed. Deterministic unit tests cover 0/12-symbol and
negative-input cases.

## M. Rate-limit protection

Reuses the existing, unmodified `TradierMarketDataProvider._request`
choke point, which already gates every single HTTP call through
`src.data.rate_limiter.may_proceed` at the SAME priority
(`RateLimitPriority.P4_OPPORTUNITY_SCANNING` for chain/quote calls,
`P5_BACKGROUND_RESEARCH` for correlation bars) the official cycle
already uses -- no new rate-limiting mechanism was built. A per-symbol
fetch failure (including a `TradierRateLimitError` if headroom runs
out mid-run) is isolated exactly like the official cycle's own
per-ticker isolation: that one symbol is marked failed, the study
continues cleanly to the next symbol and still produces a full
aggregate report, never a crash. No aggressive concurrency was
introduced -- the fetch loop remains strictly sequential, matching the
official cycle and V1.5.13 diagnostic.

## N. Per-symbol diagnostic fields implemented

Exactly item F's required list: market-data success, chain usable,
contracts examined, expirations seen/eligible/rejected, strategy
attempts, construction attempts/successes/rejections, generation
exceptions, Quant evaluations/passes/rejects, Risk evaluations/passes/
rejects, ranked candidates, selected candidate (observational only),
dominant rejection reasons -- plus sanitized per-candidate economics
(ticker, strategy, expiration, DTE, leg rights/strikes/sides, target
entry, max profit/loss, capital required, contracts requested, Risk
decision, risk-adjusted return vs. no-trade hurdle, would-have-been-
selected marker) for every candidate that reached ranking.

## O. Aggregate diagnostic fields implemented

Exactly item G's required list, computed once from the per-symbol
summaries plus one pass over `scan_result.scanned` for rejection-
reason/by-strategy breakdowns -- reconciles with the per-symbol output
by construction (proven by `TestAggregateDiagnosticsReconcile`).

## P. Suitability classification rules

Deterministic, documented in `src.workflows.universe_feasibility`'s
own module comment: **UNSUITABLE** (market data failed, chain
unusable, or zero eligible 20-45 DTE expirations) -> **WEAK** (chain
usable, eligible expiration exists, but zero construction successes)
-> **ACCEPTABLE** (construction succeeded, but no candidate reached
Risk APPROVE/RESIZE) -> **STRONG** (at least one candidate reached
ranking). Never based on today's market direction or P&L -- proven by
dedicated unit tests for every branch plus a determinism check.

## Q. Correlation methodology

A read-only side channel: `src.portfolio.risk_data
.resolve_price_history_for_correlation` (unmodified -- date-
intersection alignment, `min_correlation_observations`/
`correlation_lookback_days` from `config/operations.yaml`'s existing
`risk_data_wiring` section, fail-closed on insufficient observations),
followed by `src.quant.correlations.flag_highly_correlated_pairs`
(unmodified) at the configured `high_correlation_threshold`. Never
calls `apply_risk_data_wiring`/`apply_correlation_wiring` (which would
attach the result to the `Portfolio` object Quant/Risk evaluate
candidates against) and never reads or toggles `risk_data_wiring
.enabled` -- confirmed by the static source scan, which forbids both
function names in the body.

## R. Ranked-candidate observability (item J)

**Deferred, not implemented.** Adding an optional, additive field to
the shared, already-frozen `CandidateFunnel`/`build_candidate_funnel`
(used by BOTH the official cycle and the V1.5.13 diagnostic) carries
real regression risk for a feature that is not this release's primary
deliverable, and the task's own item T would then require a dedicated
equivalence test proving zero behavior change to ranking/selection/
persistence/Quant/Risk for that shared aggregator -- scope the task
explicitly allows deferring ("If it requires risky persistence/schema
changes, defer it and explain why"). This release's own feasibility-
specific module (`universe_feasibility.py`) already provides the
equivalent observational detail for the RESEARCH path without
touching the official, frozen aggregator at all.

## S. Proof official decision behavior unchanged

`TestOfficialCycleAndDiagnosticUnchanged` runs the real, unmodified
no-argument `run_validation_cycle()` end-to-end (hashing
`config/universe.yaml` before/after) and the real `--diagnostic-scan`
path end-to-end, both against fakes, both passing unchanged.
`TestOfficialBehaviorEquivalence`-style reasoning from V1.5.13 applies
unchanged here: nothing in this release touches
`scan_and_rank_opportunities`, `generate_candidates`, the Risk Engine,
or the Quant Engine.

## T. Focused test results

- `tests/unit/workflows/test_universe_feasibility.py`: **19 passed**.
- `tests/acceptance/test_universe_feasibility_v1514.py`: **25 passed**
  (21 from the initial pass covering items A-N/R-S, plus 4 added after
  a self-review against the item-by-item test list identified items O
  -- correlation methodology -- and P -- rate-limit fail-closed
  behavior -- as covered only incidentally, not by a dedicated test:
  `TestCorrelationMethodology` (2 tests, exercising the real
  date-intersection `resolve_price_history_for_correlation` against a
  sufficient/insufficient-history mix and a high-correlation pair) and
  `TestRateLimitSafety` (2 tests, injecting the real
  `TradierRateLimitError` for a subset and for all symbols, proving
  per-symbol isolation and a clean, zero-mutation completion). Writing
  these also surfaced that the shared `_FeasibilityFakeProvider` fixture
  only inherited `DteWindowOptionChainProvider`, not
  `HistoricalDataProvider` -- so `isinstance(provider,
  HistoricalDataProvider)` was `False` for every prior test using it,
  meaning the correlation branch was never actually exercised by any
  test despite `get_bars` being implemented on the fixture. Fixed by
  making the fixture inherit both ABCs, exactly matching production's
  own `TradierMarketDataProvider(MarketDataProvider,
  HistoricalDataProvider, DteWindowOptionChainProvider)` declaration --
  a test-fixture-only fix, zero production code changed.
- Combined with `tests/unit/workflows/`, `tests/unit/portfolio/`,
  `test_diagnostic_scan_v1513.py`, `test_run_validation_cycle_cli.py`,
  `test_review_only_daily_cycle.py`, `test_market_hours_gate.py`,
  `test_dte_window_chain_retrieval.py`,
  `test_opportunity_evaluation_timestamp.py`: **486 passed, 7 failed**
  -- all 7 within the known date-rot set.

## U. Full-suite result

**3889 passed, 6 skipped, 9 failed**, in ~97 seconds (baseline before
this change: 3845 passed, 6 skipped, 9 failed -- the +44 passing count
is exactly this release's new tests: 19 unit + 25 acceptance).

## V. Confirmation of zero new regressions

The same 9 node IDs known since V1.5.7 through the V1.5.13 acceptance
correction, unchanged in count, identity, and root cause (the
fixed-calendar-date `tests/unit/review/conftest.py::EXPIRATION`
fixture drifting outside the `[20, 45]` DTE window as real wall-clock
time advances):

1. `tests/acceptance/test_market_hours_gate.py::TestBackendPostInsideSession::test_reaches_the_mocked_runner_path`
2. `tests/acceptance/test_market_hours_gate.py::TestCliInsideSession::test_reaches_the_mocked_normal_path`
3. `tests/acceptance/test_opportunity_evaluation_timestamp.py::TestOpportunityEvaluationTimestampFixV157::test_a_chain_timestamped_after_the_cycle_start_no_longer_silently_loses_the_candidate`
4. `tests/acceptance/test_opportunity_evaluation_timestamp.py::TestOpportunityEvaluationTimestampFixV157::test_control_cycle_record_shows_zero_silent_generation_exceptions`
5. `tests/acceptance/test_review_only_daily_cycle.py::TestReviewOnlyDailyCycleEndToEnd::test_daily_cycle_never_auto_fills_and_surfaces_exactly_one_candidate`
6. `tests/acceptance/test_review_only_daily_cycle.py::TestReviewOnlyDailyCycleEndToEnd::test_confirm_candidate_places_exactly_one_order_even_when_invoked_twice`
7. `tests/acceptance/test_run_validation_cycle_cli.py::TestTradierProductionProviderPasses::test_tradier_production_reaches_the_mutating_cycle`
8. `tests/unit/dashboard/test_frontend_control_center.py::TestValidationEndpointIdempotencyUnaffected::test_dashboard_route_running_twice_the_same_day_is_a_documented_no_op`
9. `tests/unit/dashboard/test_operator_status.py::TestValidationCycleRunRoute::test_end_to_end_dashboard_trigger_matches_the_cli_safety_outcome`

Identical node IDs, identical count, identical root cause. **Zero new
failures anywhere in the full suite.**

## W. make verify-freeze result

`PAPER_TRADING_V1.5.14 / SOFTWARE FREEZE VERIFIED` -- clean on the
first run after the manifest regeneration (no production module hash
outside `scripts/run_validation_cycle.py`'s own tracked script hash
was touched other than through the new, separate
`src.workflows.universe_feasibility` module and `config/
universe_feasibility.yaml`, neither individually hash-tracked by this
freeze machinery beyond the whole-repository checks that already
passed).

## X. Implementation commit / freeze commit / push / tag

Pending -- created immediately after this report (see final chat
report for hashes and push/tag results).

## Y. Explicit confirmation -- did NOT

- run an official validation cycle
- run `--universe-feasibility`/`--diagnostic-scan` against Tradier
  production (every test uses fakes only)
- rerun the October 8 official cycle
- run `confirm_candidate.py`
- create a PaperBroker fill or a real/simulated order
- create a candidate in the real review store
- touch `data/options_agent.db` (confirmed: the file does not exist in
  this development sandbox)
- reset or reinitialize the validation cohort/database
- modify the active cohort, NAV, cash, or positions
- alter the official universe (`config/universe.yaml` unchanged, SPY/
  QQQ) or activate the expanded universe
- alter strategy activation
- loosen Quant, Risk, liquidity, DTE, or freshness rules
- lower the no-trade hurdle or increase risk-per-trade
- enable live trading

## AA. V1.5.14 ACCEPTANCE CORRECTION (supersedes the original 009c752 freeze)

**The operator never accepted or installed the original 009c752
freeze.** A pre-acceptance audit of it found two acceptance-blocking
issues, both corrected here, in place, on top of 009c752 -- no history
rewritten, no force-push, a corrective implementation commit followed
by a new freeze commit.

**1. Corrected request-budget formula.** The audit proved
`expected_tradier_request_count`'s original formula undercounted the
real worst case by `num_symbols * max_expirations` requests, because
it assumed `TradierMarketDataProvider.get_option_chain_for_expiration`
costs 1 request per selected expiration. It actually costs 2 -- that
method re-fetches the underlying quote internally before fetching the
chain itself. Verified both by reading `src.data.tradier_provider`'s
source and by instrumenting the REAL `TradierMarketDataProvider`
against a fake HTTP client (`TestRequestFormulaMatchesInstrumentedRealProvider`
in the acceptance suite): one symbol, 6 expirations, 14 real outbound
calls, not 7. The corrected formula (`3 + 2*max_expirations` per
symbol) is now split into `expected_opportunity_scan_request_count`
and `expected_correlation_request_count` (summed by
`expected_tradier_request_count`), all explicitly documented as
LOGICAL-request counts BEFORE provider-level retries (up to 3 real
outbound attempts per logical request on a transient failure/429) --
never claimed as a guaranteed ceiling on actual network traffic. For
12 symbols/6 expirations: **180**, not 108.

**2. Conservative, sequential, budget-aware fetching -- never a burst.**
`src.workflows.universe_feasibility.usable_request_headroom`/
`has_sufficient_observed_headroom` are new, pure, synchronous functions
that decide -- using ONLY the provider's own observed
`RateLimitState.allowed`/`.available` (sourced exclusively from real
response headers; never a hardcoded figure such as "120/minute") --
whether the study may safely begin its NEXT batch or phase. New
`config/universe_feasibility.yaml` section `rate_limit_safety`
(`batch_size: 3`, `reserved_headroom_pct: 0.20`,
`correlation_reserved_headroom_pct: 0.30`, each with its own `*_env`
override) drives this. Mechanics, implemented in
`run_universe_feasibility_study`:
- **Phase 1 (option-chain opportunity scan)**: strictly sequential
  batches of `batch_size` symbols. The FIRST batch always proceeds
  (`rate_limit_state` starts `None` -- nothing observed yet; this IS
  the documented bootstrap: allow only enough initial work to obtain
  real response headers, then decide every subsequent batch from what
  was actually observed). Before every later batch, if observed
  headroom is insufficient for that batch's worst-case logical
  request count, the phase stops BEFORE that batch starts. Every
  ticker in a stopped/un-reached batch is `SkipReason
  .RATE_LIMIT_HEADROOM`-skipped -- never attempted, never counted
  against `failed_chains`, and reported with `StructuralSuitability
  .NOT_EVALUATED`, never `UNSUITABLE` (a budget decision is never
  misrepresented as a data-quality finding).
- **A `TradierRateLimitError` mid-symbol** (the provider's own
  existing retries already exhausted) is isolated to that one symbol
  exactly like any other fetch failure -- verified
  (`TestRateLimitErrorIsolationNeverRetriedAtOrchestrationLayer`) that
  this orchestration layer makes EXACTLY one fetch attempt per symbol,
  success or failure, never a second, outer retry loop on top of the
  provider's own.
- **Phase 2 (correlation/history)** begins only AFTER phase 1
  completes or stops, and only for tickers phase 1 actually attempted.
  Its own, separate, larger-reserve headroom check
  (`correlation_reserved_headroom_pct`) decides how many of those
  attempted tickers current headroom can still support -- all of
  them, a subset (partial completion), or none. A ticker phase 2
  deliberately never asked about is `CorrelationSymbolStatus
  .SKIPPED_BUDGET`, reported SEPARATELY from, and never conflated
  with, `INSUFFICIENT_HISTORY` (a ticker phase 2 DID ask about, whose
  own fetch via the unmodified `resolve_price_history_for_correlation`
  did not yield enough aligned observations).
- **Zero changes to shared Tradier provider behavior.** The redundant
  quote re-fetch inside `get_option_chain_for_expiration` that CAUSES
  the higher true request cost is deliberately left alone --
  optimizing it away would change production semantics the official
  cycle also depends on, and is explicitly out of scope for this
  correction (flagged as the recommended target fix for a SEPARATE,
  dedicated change). `TradierMarketDataProvider`'s retry policy, DTE
  retrieval, and canonical timestamps are all untouched; `git diff`
  confirms zero lines changed in `src/data/tradier_provider.py`
  anywhere in this correction.

**3. Structural suitability vs. today's opportunity -- split into two
independent, observational fields.** The audit found
`classify_ticker_suitability`'s STRONG tier required
`ranked_candidates > 0`, a today's-market fact (today's IV/strikes/
portfolio-exposure-dependent Quant+Risk outcome), not a structural
property of the ticker. Replaced with:
- **`StructuralSuitability`** (`STRONG`/`ACCEPTABLE`/`WEAK`/
  `UNSUITABLE`/`NOT_EVALUATED`), computed by
  `classify_structural_suitability`, reading ONLY chain/DTE/
  construction facts -- NEVER Quant pass/fail, Risk pass/fail, ranking
  score, or the no-trade hurdle. Deliberately uses `strategy_attempts`
  (at most 3 -- one per configured strategy actually attempted for
  this ticker) as STRONG's denominator, NEVER `construction_attempts`
  (which the audit's own follow-up correctly flagged as unsafe: it
  scales with how many DTE-eligible expirations
  `candidate_generation.py`'s own best-of-all-expirations search
  internally examined while looking for the single best candidate per
  strategy, not with how many independent strategy ideas were tried --
  a liquid, reliable ticker with 6 eligible expirations can rack up 5
  `construction_rejected` events and still have found an excellent
  candidate on the 6th). `NOT_EVALUATED` is never returned by the
  classifier itself -- it is assigned directly by the orchestration
  layer for a ticker this run skipped for budget reasons.
- **`OpportunityToday`** (`NONE`/`CONSTRUCTED`/`QUANT_PASS`/
  `RISK_PASS`/`RANKED`/`CLEARED_HURDLE`), computed by
  `classify_opportunity_today`, purely observational, reusing a new
  per-symbol `ranked_candidates_clearing_hurdle` field (the same
  `risk_adjusted_return > no_trade_hurdle` comparison the aggregate
  report's `total_would_clear_no_trade_hurdle` already made, now also
  surfaced per-symbol) -- NEVER read back into `StructuralSuitability`
  or any other decision. `RISK_PASS` and `RANKED` are kept as
  genuinely separable levels (not a manufactured distinction):
  `ScannedCandidate.risk_adjusted_return` can be `None` for an
  otherwise Risk-approved candidate whenever `capital_required <= 0`
  or the economics are unpriced.
- Both fields are now printed per-symbol and tallied in the aggregate
  report (`structural_suitability_counts`/`opportunity_today_counts`),
  with an explicit printed line stating permanent-universe guidance is
  driven primarily by structural suitability and diversification, NOT
  by whether one day's candidate happened to rank or clear the hurdle.

**4. Correlation status, three-way.** `CorrelationFeasibilitySummary`
now carries `symbols_with_sufficient_history`/
`symbols_with_insufficient_history`/`symbols_skipped_budget`
separately -- `symbols_skipped_budget` is decided ENTIRELY by the
orchestration layer's own pre-flight ticket subset chosen BEFORE
calling the unmodified `resolve_price_history_for_correlation`, never
inferred after the fact from an absence in its return value (which
cannot itself distinguish "too few observations" from "the fetch
failed" -- a `PROVIDER_FAILURE` fourth status was considered and
explicitly NOT added, since doing so would require changing that
shared function's own internal exception handling, out of scope here).

**5. Test coverage.** `tests/unit/workflows/test_universe_feasibility.py`:
**54 passed** (up from 19) -- corrected-formula assertions, headroom/
bootstrap behavior (including a dedicated "never reads a hardcoded
plan allowance" test), structural/opportunity classification branch
tests (including the exact construction_attempts-vs-strategy_attempts
scenario the audit flagged), correlation-status disjointness.
`tests/acceptance/test_universe_feasibility_v1514.py`: **34 passed**
(up from 25) -- bootstrap-then-observed-state batching, ample-headroom
completion, stop-before-exhaustion with `NOT_EVALUATED`
reporting/zero-DB-mutation, correlation full-skip/partial-completion/
distinct-status, exactly-one-fetch-attempt-per-symbol (no outer
retry), and the instrumented-real-provider formula-match test. Full
suite: **3933 passed, 6 skipped, 9 failed** -- the identical known
9 date-rot node IDs (§V above), zero new failures. `make verify-freeze`:
`PAPER_TRADING_V1.5.14 / SOFTWARE FREEZE VERIFIED`, clean on the first
run after manifest regeneration.

**6. Confirmed unchanged by this correction** (re-verified via
`git diff` against the entire correction): `config/universe.yaml`
(SPY, QQQ), the active strategy set, `src/risk/`, `src/quant/`,
`src/strategies/`, `src/data/tradier_provider.py`,
`src/workflows/candidate_generation.py`,
`src/portfolio/opportunity_scan.py` -- zero lines changed in any of
them. The official no-argument cycle and `--diagnostic-scan` continue
to pass their own existing, unmodified acceptance tests unchanged.

## Z. Remaining limitations

- The human operator must now run the first real
  `--universe-feasibility` study against live Tradier production
  manually, as explicitly instructed -- no production Tradier call of
  any kind occurred during this release's development.
- Item J (ranked-candidate observability on the OFFICIAL, persisted
  `CandidateFunnel`) is deferred -- see §R.
- The 9 known date-rot test failures remain open by design, unrelated
  to this release.
- `UNIVERSE_EXPANSION_ACTIVATION_PLAN.md` documents the Phase A ->
  Phase B mechanism but creates no `ExperimentVersion` record and
  edits no config -- the operator's own review of a real feasibility
  run is the prerequisite this release deliberately does not skip.
- The research universe's sector labels in `config/
  universe_feasibility.yaml` (Technology/Financials/Energy/Consumer
  Discretionary/ETF) are a reasonable, documented placeholder for
  sector-concentration reporting, same as the operator-reviewed
  convention `config/universe.yaml` already uses for SPY/QQQ -- not
  independently re-verified against a third-party GICS classification
  in this release.
