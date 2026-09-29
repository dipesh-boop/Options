# STEP 3 FREEZE REPORT — PAPER_TRADING_V1.5.3

## Executive Summary

This step wires sector-concentration and correlation risk controls into
the production validation path so they are operationally effective, not
merely present in code. `Portfolio.sector_by_ticker`/`price_history` were
always plain dict fields the Risk Engine's own `check_sector_concentration`/
`check_correlation` already read — but nothing in
`scripts/run_validation_cycle.py`/`scripts/confirm_candidate.py` ever
populated them before constructing or loading a `Portfolio`. New
`src/portfolio/risk_data.py` wires sector classification from the
existing, already-operator-maintained `config/universe.yaml` (no LLM, no
new provider) and installs fail-closed correlation-data enforcement — but
ships with no live historical-bars provider wired into official
validation (none exists in this codebase today), so the capability is
**installed, not activated**, and the active cohort's candidate
eligibility is byte-for-byte unaffected. A genuine external-dependency
gap (historical price bars for correlation) was identified and is
reported below, per the task's own explicit instruction, rather than
silently filled.

## Read-Only Architecture Trace

1. **Where `Portfolio` is constructed for the official daily validation
   path**: exactly one call site, `scripts/run_validation_cycle.py`
   (bootstraps `Portfolio(as_of=now, nav=..., cash=..., peak_equity=...)`
   with no `sector_by_ticker`/`price_history` argument the very first
   time an account has no saved state; every subsequent cycle loads it
   from `SqlitePortfolioStore`). `scripts/confirm_candidate.py` never
   constructs one directly — it loads whatever `run_validation_cycle.py`
   last saved, via `src.review.confirmation.confirm_candidate`'s own
   `portfolio_store.get(...)` call.
2. **Every production call site evaluating sector concentration,
   correlation, or portfolio concentration/exposure**:
   `src.risk.engine._evaluate` (steps 13/14, the Risk Engine's own
   choke point — `check_underlying_concentration`/`check_sector_concentration`/
   `check_correlation`); `src.portfolio.exposure.build_exposure_snapshot`
   (Part 15's read-only exposure snapshot, same documented gap);
   `src.portfolio.opportunity_scan.scan_and_rank_opportunities` (reads
   `entry.sector` from the universe directly for post-trade exposure
   display, a DIFFERENT source than the Risk Engine's own
   `sector_by_ticker` lookup — see root cause below);
   `src.dashboard.risk_state`/`src.dashboard.service` (dashboard display,
   same `sector_by_ticker`/`price_history` reads).
3. **How `sector_by_ticker` was populated before this step**: never.
   Every read site (`src.risk.engine`, `src.risk.portfolio_risk
   .sector_exposure_pct`, `src.portfolio.exposure`,
   `src.orchestration.pipeline`, `src.dashboard.service`) used
   `.get(ticker, "UNKNOWN")`, and the dict was always empty in the
   official validation path, so every ticker always fell into the
   single "UNKNOWN" bucket.
4. **How `price_history` was populated before this step**: never — same
   finding. `src.risk.correlation.check_correlation`'s own docstring
   already documented this as a known, deliberate gap ("no live
   historical-data wiring exists yet... the check is skipped rather than
   treated as fail-closed").
5. **Existing market-data abstractions that can provide historical
   prices**: `src.data.historical.HistoricalDataProvider` (an abstract
   `get_bars(symbol, start, end) -> list[HistoricalBar]` interface,
   already defined, explicitly documented as "vendor-agnostic... useful
   regardless of" which historical-options-data vendor is chosen) —
   reused, unmodified, as the interface `src.portfolio.risk_data` accepts.
   No concrete implementation is wired into the official validation
   provider (`src.data.tradier_provider.TradierMarketDataProvider`
   implements no historical-bars endpoint at all — confirmed by reading
   its full source, which only calls `/markets/quotes`,
   `/markets/options/chains`, `/markets/options/expirations`).
   `src.data.alpaca_historical.py` exists as a concrete implementation
   elsewhere in the codebase (for backtesting) but introducing it into
   the OFFICIAL validation path would be a new provider dependency in
   that path — flagged below, not silently done.
6. **Existing metadata/security-master abstractions for sector
   classification**: `config/universe.yaml`'s per-ticker `sector` field,
   loaded by `src.data.universe.load_universe` into
   `src.workflows.candidate_generation.UniverseEntry.sector` — already
   populated (`SPY: ETF`, `QQQ: ETF`) and already read by
   `generate_candidates` for a brand-new `Candidate.sector`. This is the
   cleanest, and only, existing deterministic sector source in the
   codebase; reused verbatim.
7. **Whether Tradier itself supplies anything usable**: no. Confirmed by
   a full read of `src/data/tradier_provider.py` — it calls exclusively
   `/markets/quotes`, `/markets/options/chains`, `/markets/options/expirations`.
   No sector/fundamental/security-master endpoint, no historical-bars
   endpoint, is called or parsed anywhere in that file.
8. **Existing modules that already solve part of this problem**:
   `config/universe.yaml`/`UniverseEntry.sector` (sector — fully reused,
   see above); `src.data.historical.HistoricalDataProvider`/`HistoricalBar`/
   `assert_no_lookahead` (the correlation-data interface and point-in-time
   guard — fully reused, see above). No parallel component was built for
   either.
9. **Required lookback/sample requirements the correlation logic
   assumed**: `src.quant.correlations.correlation_matrix` only required
   `len(series) >= 2` (via `returns_from_prices`) and equal-length series
   across symbols — no minimum-sample-size policy existed anywhere.
   This step adds one: `min_correlation_observations` (default 20) /
   `correlation_lookback_days` (default 60), new config values under
   `config/operations.yaml`'s new `risk_data_wiring` section (not
   `config/risk_limits.yaml` — this is a NEW policy, not a change to an
   existing threshold).
10. **Behavior today when sector or history is missing**: sector —
    silently "UNKNOWN" (see finding 3). Correlation — silently skipped
    (see finding 4, and `check_correlation`'s own prior docstring).
    Neither ever blocked a trade or was independently observable.

## Root Cause

Two distinct, confirmed defects, not one:
1. `src.risk.engine._evaluate`'s own sector-concentration step
   (`sector = portfolio.sector_by_ticker.get(proposal.ticker, "UNKNOWN")`)
   never received real data, because `Portfolio` is always constructed/
   loaded with an empty `sector_by_ticker` in the official path.
2. `src.risk.correlation.check_correlation` never received real data for
   the identical reason on `price_history`, and its own prior docstring
   already documented this as a deliberate, temporary gap pending "live
   historical-data wiring... into the Risk Engine" — this step is that
   wiring.

Neither defect was in the Risk Engine's own check logic — both were a
missing production wiring step between account-state load and Risk
evaluation.

## Existing Components Reused

`config/universe.yaml`/`src.data.universe.load_universe`/`UniverseEntry`
(sector source); `src.data.historical.HistoricalDataProvider`/
`HistoricalBar`/`assert_no_lookahead` (correlation-data interface and
no-lookahead guard); `src.risk.portfolio_risk.Portfolio.sector_by_ticker`/
`price_history` (the existing fields the Risk Engine already reads — no
new Portfolio fields for the data itself, only the additive
`risk_data_required` activation flag); `config/operations.yaml`'s
existing YAML-plus-per-value-env-override pattern
(`src.portfolio.operations_config._resolved`).

## New Components Added

`src/portfolio/risk_data.py` — `classify_universe_sectors`,
`resolve_sector_by_ticker`, `apply_sector_wiring`,
`resolve_price_history_for_correlation`, `apply_correlation_wiring`,
`apply_risk_data_wiring` (the single entry point the two scripts call).
`Portfolio.risk_data_required: bool = False` (additive field).
`ReasonCode.REJECT_SECTOR_DATA_UNAVAILABLE`/`REJECT_CORRELATION_DATA_UNAVAILABLE`
(new fail-closed outcomes, distinct from an actual limit breach).
`CorrelationDataUnavailableError` (`src/risk/correlation.py`).
`config/operations.yaml`'s new `risk_data_wiring` section +
`OperationsConfig.risk_data_wiring_enabled`/`min_correlation_observations`/
`correlation_lookback_days`. `ExperimentVersion.risk_data_wiring_enabled`
(additive, default `False`, participates in `version_id`).
`OperatorStatusView.risk_data_wiring_status` (dashboard observability).
Two new freeze checks: `risk_data_wiring_fail_closed_verified`,
`risk_data_wiring_inactive_for_active_cohort`.

## Sector-Data Source and Provenance

Source: `config/universe.yaml`'s per-ticker `sector` field, via the
existing `src.data.universe.load_universe`/`UniverseEntry`. Deterministic
(no LLM, no inference from the ticker symbol), operator-maintained,
already the source `src.workflows.candidate_generation.generate_candidates`
uses for a new candidate's own `Candidate.sector`. Every classified
ticker carries a `SectorClassification(ticker, sector, is_etf, source="config/universe.yaml")`
record for observability. A ticker not listed in `config/universe.yaml`
is never fabricated a sector — it is reported as `unclassified` and, when
risk-data wiring is active, fails closed in the Risk Engine
(`REJECT_SECTOR_DATA_UNAVAILABLE`) rather than defaulting to "UNKNOWN."

## Correlation-History Source and Provenance

Interface: `src.data.historical.HistoricalDataProvider.get_bars`. As of
this freeze, every production call site passes `historical_provider=None`
— see "External Dependency Discovered" below. When a real provider IS
supplied (future step, once approved), `resolve_price_history_for_correlation`
fetches `correlation_lookback_days` of daily bars per ticker ending
strictly before the evaluation date, rejects a bar dated on/after that
date via the existing `assert_no_lookahead` guard (a violation taints the
whole ticker's series, never silently trimmed), aligns every surviving
ticker's series to the shortest common length, and omits (never
fabricates) any ticker with fewer than `min_correlation_observations`
points. One ticker's fetch failure is isolated and never aborts the
others (`resolve_price_history_for_correlation`'s per-ticker try/except).

## Required Observation/Lookback Policy

`min_correlation_observations = 20`, `correlation_lookback_days = 60`
(config/operations.yaml, new `risk_data_wiring` section, `*_env`
overridable). A conservative, documented default — a policy choice, not
a derived number, pending operator review, exactly matching this
codebase's established convention for every other new numeric policy
(e.g. Step 2's market-hours buffers).

## ETF Treatment

Deterministic: a `config/universe.yaml` entry whose `sector` value is
exactly `"ETF"` (case-insensitive) is classified `is_etf=True`, `sector="ETF"`
— never mapped onto a single-name GICS sector. This is the existing
operator convention already used for SPY/QQQ, made explicit and
load-bearing by this step rather than merely descriptive. No new policy
decision was required — the existing config already encoded it.

## Missing-Data Fail-Closed Behavior

Governed by `Portfolio.risk_data_required` (set only by
`apply_risk_data_wiring` when `config/operations.yaml`'s
`risk_data_wiring.enabled` is `true`):
- `risk_data_required=False` (default, the active cohort today): an
  unclassified ticker still falls back to `"UNKNOWN"`; a missing
  correlation history still silently skips the check — the exact
  pre-V1.5.3 behavior, unchanged.
- `risk_data_required=True`: an unclassified ticker rejects with
  `REJECT_SECTOR_DATA_UNAVAILABLE` before `check_sector_concentration`
  ever runs; missing correlation history for a portfolio that already
  holds another position rejects with `REJECT_CORRELATION_DATA_UNAVAILABLE`
  (`CorrelationDataUnavailableError`) rather than being silently skipped.
  An empty portfolio (no other positions) never requires correlation
  data at all, regardless of `risk_data_required` — correctly
  mathematically vacuous, not manufactured. Once real data IS available
  and sufficient, the pre-existing correlation LIMIT
  (`high_correlation_threshold`, untouched) is still enforced exactly as
  before (proven in `tests/unit/risk/test_risk_data_fail_closed.py::TestCorrelationFailClosed
  ::test_sufficient_highly_correlated_history_still_rejects_on_the_limit_itself`).

## Active-Cohort Compatibility Mechanism

Installation vs. activation, enforced at three layers:
1. `config/operations.yaml`'s new `risk_data_wiring.enabled` defaults to
   `false` — the active cohort's own real config file has this value.
2. `apply_risk_data_wiring` is a complete no-op (`return portfolio`,
   object identity preserved) when `enabled=False` — proven directly
   (`tests/unit/portfolio/test_risk_data.py::TestApplyRiskDataWiring
   ::test_disabled_is_a_complete_no_op`) and end-to-end through the real
   script (`tests/acceptance/test_risk_data_wiring_cycle.py
   ::TestActiveCohortCompatibility`).
3. A new freeze check, `risk_data_wiring_inactive_for_active_cohort`,
   reads the REAL `config/operations.yaml` at freeze time and fails the
   whole freeze if `enabled` is ever `true` — `make verify-freeze` would
   catch an accidental or malicious activation for the live cohort, not
   just this report's own claim.

`config/operations.yaml`'s `cohort_id`/`account_id` (identifying the
active cohort) were not touched. `src.validation.cohort.start_new_cohort`
is not imported anywhere touched by this step (already true, re-verified).

## Experiment-Version Integration

`ExperimentVersion` gained `risk_data_wiring_enabled: bool = False`
(additive, default preserves every pre-V1.5.3 caller's exact `version_id`
— verified: `tests/unit/validation/test_experiment_version.py`'s full
29-test suite passes unmodified), now participating in
`compute_experiment_version_id`'s digest — a cohort that activates
risk-data wiring in the future will therefore have a genuinely different,
attributable experiment identity from one that doesn't.
`ExperimentVersion` is still not wired into
`scripts/run_validation_cycle.py`/any live cohort by this step (matching
Step 1's own stated scope) — this step only extends the schema so that
future wiring can attribute a cohort's experiment identity correctly.

## Observability Added

`SectorClassification(ticker, sector, is_etf, source)` — full provenance
for every classified ticker. `resolve_sector_by_ticker` returns the set
of unclassified tickers explicitly, never silently. `ReasonCode
.REJECT_SECTOR_DATA_UNAVAILABLE`/`REJECT_CORRELATION_DATA_UNAVAILABLE` —
distinct, structured, machine-readable outcomes, each carrying a specific
`message` (which ticker, which existing positions it couldn't correlate
against). `OperatorStatusView.risk_data_wiring_status`
("INSTALLED_ACTIVE"/"INSTALLED_INACTIVE", `None` only in the degraded
`configured=False` branch) — a minimal, read-only dashboard addition, no
new control. No SQLite schema was touched — nothing in this step required
a new persisted field.

## Files Changed

Implementation commit (`bfe4b01`):
`src/portfolio/risk_data.py` (new), `src/risk/correlation.py`,
`src/risk/engine.py`, `src/risk/portfolio_risk.py`,
`src/risk/reason_codes.py`, `src/portfolio/operations_config.py`,
`config/operations.yaml`, `scripts/run_validation_cycle.py`,
`scripts/confirm_candidate.py`, `src/dashboard/validation_ops.py`,
`src/validation/experiment_version.py`, plus new/modified tests:
`tests/unit/portfolio/test_risk_data.py` (new),
`tests/unit/risk/test_risk_data_fail_closed.py` (new),
`tests/acceptance/test_risk_data_wiring_cycle.py` (new),
`tests/unit/portfolio/test_operations_config.py`,
`tests/unit/dashboard/test_operator_status.py`,
`tests/acceptance/test_review_only_daily_cycle.py` (fixture-only:
added the new required `risk_data_wiring` section to its own
`operations.yaml` fixture).

Freeze commit (this one): `src/validation/freeze.py` (version bump +
two new checks + corrected `verify-freeze` banner wording),
`tests/unit/validation/test_freeze.py`, `VALIDATION_MANIFEST.json`,
`STEP_23_3_FREEZE_REPORT.md`, `progress.md`.

## Files Intentionally NOT Changed

`config/risk_limits.yaml` (every existing threshold, including
`max_sector_exposure_pct`/`high_correlation_threshold`, byte-for-byte
unchanged — the new observation/lookback policy lives in
`config/operations.yaml` instead, since it is a NEW policy, not a
revision to an existing one). `config/universe.yaml`, `config/brokers.yaml`,
`config/validation.yaml` — byte-for-byte unchanged (verified by hash
below). `src/quant/`, `src/brokers/paper.py`, `src/lifecycle/`,
`src/data/tradier_provider.py` (no historical-bars method added — see
external dependency below), `src/data/alpaca_historical.py` (not wired
into official validation), `src.risk.limits`, `src.risk.stress`,
`src.risk.kill_switch`, `src.risk.drawdown`, `src.risk.trade_risk` — no
Risk/Quant threshold or sizing logic changed. `src.validation.cohort`
(no cohort start/reset capability touched). The active cohort's own
operational database was never accessed (see below).

## Focused Test Results

`tests/unit/portfolio/test_risk_data.py`: 20 passed.
`tests/unit/risk/test_risk_data_fail_closed.py`: 8 passed.
`tests/acceptance/test_risk_data_wiring_cycle.py`: 3 passed.
`tests/unit/risk/` (full directory, regression): 255 passed, 4 skipped.
`tests/unit/portfolio/` + `tests/acceptance/test_review_only_daily_cycle.py`
+ `test_market_hours_gate.py` + `test_run_validation_cycle_cli.py`:
391 passed. `tests/unit/dashboard/`: 189 passed. `tests/unit/validation/test_freeze.py`:
79 passed.

## Full-Suite Result

`python -m pytest -q`: **3627 passed, 6 skipped, 0 failed** (up from the
pre-step V1.5.2 baseline of 3583 passed — net new: 44 tests).

## Freeze Result

`make verify-freeze` against the regenerated `PAPER_TRADING_V1.5.3`
manifest: **all checks pass**, including the two new
`risk_data_wiring_fail_closed_verified`/`risk_data_wiring_inactive_for_active_cohort`
checks and every check carried forward from V1.5.2 unmodified.

## No Official Validation Cycle Ran

`scripts/run_validation_cycle.py` was never invoked directly against real
config/data — every exercise of it in this step was through
`tests/acceptance/test_risk_data_wiring_cycle.py`'s reused `scripts`
fixture (which loads the script as a module against temporary
`tmp_path` sqlite files and a `FakeMarketDataProvider`, per
`tests/acceptance/test_review_only_daily_cycle.py`'s own established
pattern) or as a manifest/freeze-check source-text read.

## No Candidate Was Confirmed

`scripts/confirm_candidate.py`'s `_run` function was never called in
this step — no test in this step exercises confirmation at all; the
existing confirmation coverage (`tests/acceptance/test_review_only_daily_cycle.py
::test_confirm_candidate_places_exactly_one_order_even_when_invoked_twice`)
was re-run unmodified as part of the full suite and still passes.

## No PaperBroker Fill Was Created

No test in this step calls `PaperBroker.place_order`. The one acceptance
test that pre-seeds an existing position
(`test_existing_position_with_no_provider_fails_closed_on_the_next_scan`)
does so by directly constructing and saving a `Portfolio`/`PortfolioPosition`
object to a temporary `SqlitePortfolioStore` — never through a broker fill.

## No Real Brokerage/Order API Was Called

No network call of any kind was made by this step's implementation or
tests. `src/data/tradier_provider.py` was read but not modified, and its
own structural freeze check (`tradier_market_data_only`) still passes,
confirming no order/trading-shaped identifier exists anywhere in `src/`.

## Operational DB Not Accessed/Mutated

`data/options_agent.db` was absent before this step and absent after
(confirmed by `ls` before/after every test batch; `rm -rf data` run
after each). Every test in this step uses either `tmp_path`-based sqlite
files or in-memory fixtures. No `Sqlite*Store` was ever constructed
against `config/operations.yaml`'s real default paths during this step's
own new tests (the pre-existing sandbox artifact where OTHER,
already-existing tests reach real default config paths is unrelated and
pre-dated this step, per prior freeze reports' own documented findings).

## External Dependency Discovered

1. **What data is unavailable**: historical daily price bars (OHLCV) for
   underlyings, needed to populate `Portfolio.price_history` for
   correlation evaluation once a real position exists.
2. **Why existing components cannot supply it**: `src.data
   .tradier_provider.TradierMarketDataProvider` (the only market-data
   provider official validation may use) implements no historical-bars
   endpoint — confirmed by a full read of that file, which calls only
   `/markets/quotes`, `/markets/options/chains`, `/markets/options/expirations`.
   `src.data.historical.HistoricalDataProvider` is an existing abstract
   interface with no concrete implementation wired into the official
   validation path (a concrete `src.data.alpaca_historical` implementation
   exists elsewhere, used for backtesting — a different, non-official
   data flow).
3. **Proposed provider**: extend `TradierMarketDataProvider` with a new
   read-only method calling Tradier's own `GET /v1/markets/history`
   endpoint (a documented Tradier REST endpoint for historical daily
   bars) — the SAME already-configured, already-authenticated Tradier
   production account/token this platform already uses for quotes and
   option chains. This is explicitly NOT a proposal to add Alpaca, a new
   vendor, or any other new external service into official validation.
4. **Authentication requirements**: none beyond what is already
   configured (`OPTIONS_AGENT_TRADIER_TOKEN`, already required for every
   other official-validation market-data call).
5. **Cost/licensing implications**: none known to differ from the
   existing Tradier market-data subscription already in use — historical
   daily bars are part of Tradier's standard market-data API surface, not
   a separately licensed product, to the best of this codebase's existing
   documentation; not independently verified against Tradier's current
   pricing terms from this sandbox.
6. **New network/API behavior**: one new GET-only endpoint call
   (`/markets/history`) per ticker per cycle where correlation data is
   needed, using the same rate-limiter (`src.data.rate_limiter`) and
   retry/backoff machinery every other Tradier call in this codebase
   already uses.
7. **Failure behavior**: exactly what this step already installed —
   `resolve_price_history_for_correlation` isolates one ticker's fetch
   failure from the others, and `check_correlation` fails closed
   (`CorrelationDataUnavailableError`) whenever coverage remains
   insufficient after the fetch.
8. **Security implications**: none beyond the existing Tradier
   integration's own (bearer-token redaction, HTTPS-only, no new
   credential type).
9. **Provenance change**: none — historical bars from Tradier would carry
   the identical `source="tradier"` provenance convention every other
   canonical data type in this codebase already uses
   (`HistoricalBar.source`).

**This was not implemented in this step.** Per the task's explicit
instruction, this is reported for the operator's explicit approval
before any code change adds this new production network call path.

## Unresolved Safety Issues

None identified. Every fail-closed path introduced by this step degrades
to REJECT, never to a silent APPROVE, and is exercised by at least one
test proving that outcome (see Focused Test Results).

## Implementation Commit Hash

`bfe4b01`

## Freeze Commit Hash

Recorded in the final report after this commit completes (this file is
part of that commit).

## Push Status

Reported in the final report after `git push` completes.

## Working-Tree Status

Reported in the final report after the freeze commit and push complete.

## Recommended Next Step

Operator sign-off on the external-dependency proposal above (extending
`TradierMarketDataProvider` with a `GET /v1/markets/history` method,
using the existing token/account) so a future step can wire a real
historical-bars provider into `apply_correlation_wiring`, at which point
correlation evaluation will succeed (rather than fail closed) for any
future cohort that activates `risk_data_wiring.enabled: true`. Separately,
once that data source exists, the operator should decide when (if ever)
to activate risk-data wiring for a SUCCESSOR cohort — this step
deliberately does not recommend activating it for the currently active
`paper-trading-v1.4.3-validation-2026-09-22` cohort, since doing so mid-
cohort would be exactly the behavior change this step's own design
avoids. **This step does not implement, and was not asked to implement,
universe expansion or additional strategy activation — both remain
explicitly out of scope for any future step.**
