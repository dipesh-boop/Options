# STEP 4 FREEZE REPORT — PAPER_TRADING_V1.5.5

## Executive Summary

Cohort `paper-trading-v1.4.3-validation-2026-09-22` remains ACTIVE (7/90
days recorded as of the Sept 30 cycle: 0 completed trades, 0 candidates,
0 open positions, NAV/cash unchanged at $100,000, drawdown 0%). The
zero-trade result was **not** treated as permission to loosen the
system: no trade frequency was increased, no threshold was optimized
toward the 50-trade validation minimum, and no candidate was forced.

This step makes the candidate-generation pipeline **observable** — what
was scanned, what survived each stage, what was rejected, and why —
**without changing candidate eligibility in any way**. The fundamental
requirement (SAME INPUT + SAME EXISTING RULES = SAME TRADING DECISION,
before and after this step) is proven structurally, not just tested:
`src.workflows.funnel_diagnostics.FunnelDiagnostics`'s `record_*`
methods all return `None` and are appended strictly AFTER the real
decision they describe has already happened, never read back into any
`if`/`return`/loop-control statement anywhere in
`src.workflows.candidate_generation.generate_candidates` or
`src.portfolio.opportunity_scan.scan_and_rank_opportunities`. This is
additionally proven behaviorally by
`tests/unit/workflows/test_candidate_funnel_equivalence.py`, which runs
the real, unmodified scan twice — once with diagnostics collection
absent, once with it enabled — across 10 named scenarios and asserts
the two `OpportunityScanResult`s are exactly equal.

## Read-Only Architecture Trace (19 stages)

Traced from `scripts/run_validation_cycle.py`'s `run_validation_cycle()`
down through `src.portfolio.orchestrator.run_outer_cycle` to
`src.workflows.candidate_generation.generate_candidates`, confirmed
against current source (never assumed):

1. **Official daily-cycle entry point**: `run_validation_cycle()`
   (`scripts/run_validation_cycle.py`). Cycle-level idempotency
   (`SqliteControlLoopStore.get_cycle_record` already-exists check)
   runs first.
2. **Market-hours gate**: `evaluate_validation_cycle_eligibility(now, ...)`
   — returns `False` (cycle-level) before any mutating call if the new-
   position scan window (regular session, buffer-adjusted) is closed.
   Input: current time. Output: allow/block + reason. Rejection reasons
   exist (`block_reason`), structured (`MarketSessionState` enum +
   string). No object is silently dropped — the cycle exits cleanly and
   logs why. Cycle-level, not symbol/contract-level. Observability was
   addable without behavior change (none needed — already fully
   observable via existing `OperatorStatusView` fields).
3. **Provider preflight**: `verify_official_provider_is_tradier_production`
   — raises `OfficialProviderPreflightError` (fail closed) if the
   configured provider isn't Tradier production. Cycle-level.
4. **Universe loading**: `load_universe()` (`config/universe.yaml`) —
   currently `[SPY, QQQ]`, confirmed from source, not assumed (see
   "Effective Opportunity Set" below). Input: config file. Output:
   `tuple[UniverseEntry, ...]`.
5. **Symbols actually scanned**: every `UniverseEntry` in the loaded
   universe — `opportunities_scanned = sum(1 for e in cfg.universe if
   e.ticker in cfg.chains_by_ticker)` (`orchestrator._run_opportunity_scan_stage`).
   Symbol-level.
6. **Market-data/option-chain acquisition**: per-ticker
   `provider.get_option_chain(ticker)` calls in
   `scripts/run_validation_cycle.py`'s fetch loop, isolated per-symbol
   try/except — one bad fetch never aborts the cycle. Output:
   `dict[str, OptionChain | Exception]`. Symbol-level. Rejection reason:
   the caught exception itself (not previously surfaced in aggregate —
   now is, via `FunnelDiagnostics`/`CandidateFunnel.symbols_market_data_failed`).
7. **Data-quality/freshness gates**: **two different, non-overlapping
   mechanisms**, confirmed from source (critical finding, see below):
   (a) `src.data.quality_gate.validate_option_chain` — used ONLY inside
   `run_control_cycle`'s `_quality_gate_market_data`, for EXISTING-
   POSITION revaluation (NaN/infinite checks, crossed-market detection,
   systemic-chain-failure escalation); (b) candidate generation
   (`generate_candidates`) checks ONLY
   `chain.freshness_status(now) == FreshnessStatus.STALE` (a simple
   age-vs-`DEFAULT_MAX_QUOTE_AGE` check) — it never calls
   `validate_option_chain` at all. Chain-level. Output boolean; this
   step now records it (`FunnelDiagnostics.record_chain(stale=...)`)
   before the existing early-return, changing nothing about the return
   itself.
8. **Expiration filtering**: `_eligible_expirations` — filters
   `chain.contracts` by `quant_filter.min_dte <= dte <= max_dte` (default
   20-45). Contract-set-level (per expiration). This step now records
   `seen`/`eligible` counts via `diagnostics.record_expirations(...)`,
   computed from values the function already derived.
9. **Contract filtering**: `_closest_by_target_delta` (delta window,
   default `[0.15, 0.30]`) then `passes_liquidity_filter` (min open
   interest 100, min volume 10, non-zero mid, max spread — all from
   `config/risk_limits.yaml`, unchanged). Contract-level. Rejection
   reasons now recorded via the extracted `_liquidity_rejection_reason`
   (see "Single Source of Truth" below) and a new
   `"DELTA_OUT_OF_RANGE"` reason when no contract falls in the delta
   window at all.
10. **Strategy selection**: `CANDIDATE_GENERATION_ELIGIBLE_STRATEGIES`
    intersected with the caller-supplied strategy list — exactly 3
    strategies have actual candidate-generation branches
    (`CASH_SECURED_PUT`, `COVERED_CALL`, `PUT_CREDIT_SPREAD`), confirmed
    from source (see "Current Strategy Breadth" below). Strategy-level.
11. **Strategy-specific candidate construction**: `_short_put_candidate`
    (CSP), covered-call branch (requires `holding.shares >=
    _CONTRACT_MULTIPLIER`), `_short_put_candidate` + `_long_put_for_spread`
    (PCS, two-leg). Candidate-level (per strategy per eligible
    expiration). This step now records `record_strategy_attempt`/
    `record_strategy_ineligible`/`record_construction` at exactly this
    granularity — matching the real loop's own structure, never a finer
    or coarser one.
12. **Quant evaluation**: `default_quant_stage` (in
    `src.portfolio.opportunity_scan.scan_and_rank_opportunities`, one
    level above `generate_candidates`) — recomputes
    `QuantitativeAnalysis` from the chosen contract(s). Candidate-level.
    An uncaught exception here is isolated per-candidate (the existing
    try/except around both quant+risk), recorded as
    `quantitative_analysis=None`, `risk_decision=RiskDecision.REJECT`.
    This step's `CandidateFunnel.quant_rejected` counts exactly this
    branch.
13. **Liquidity evaluation**: already folded into contract filtering
    (stage 9) — there is no separate "liquidity re-check" stage between
    Quant and Risk; confirmed from source, not assumed. No artificial
    stage was invented for this.
14. **Risk evaluation**: `evaluate_trade_proposal` (the sole Risk
    Engine authority, unmodified) — APPROVE / RESIZE / REJECT / HALT.
    Candidate-level. `ScannedCandidate.risk_reason_codes` (new field,
    populated from `decision.reason_codes`, the Risk Engine's OWN
    existing structured vocabulary — reused, never duplicated) feeds
    this step's rejection-reason aggregation.
15. **Resizing, if applicable**: **not a separate stage with its own
    counter anywhere in the real architecture** — `RESIZE` is one of
    four possible `RiskDecision` outcomes `evaluate_trade_proposal`
    returns directly, with no separate "resizing attempt" tracked
    anywhere else in the codebase. Per the task's own instruction ("do
    not force artificial counts if a concept does not exist at that
    stage"), no `resizing_attempts`/`resizing_successes`/
    `resizing_rejections` fields were added — `RESIZE` outcomes are
    correctly folded into `risk_passed`/`by_strategy.risk_passed`
    alongside `APPROVE` (both are `_ACCEPTABLE_RISK_DECISIONS`), exactly
    matching how the rest of this codebase (`ScannedCandidate` filtering
    for `survivors`) already treats them as equivalent outcomes.
16. **Ranking/selection**: `scan_and_rank_opportunities` sorts
    `survivors` (Risk-approved/resized, priced) by
    `risk_adjusted_return` descending; `best` is the top one only if it
    clears `no_trade_hurdle` (CASH/NO_TRADE otherwise, a valid
    first-class outcome). Cycle-level (one winner across all symbols/
    strategies). `CandidateFunnel.candidates_ranked`/`candidates_selected`
    reflect this exactly, read from `scan_result` after the real
    decision.
17. **Candidate persistence**: `run_validation_cycle.py`'s own,
    separate, LATER decision (`if scan.best is not None and not already
    on file: review_store.save_candidate(...)`) — this happens AFTER
    `ControlCycleRecord` (which carries `candidate_funnel`) is already
    saved by `run_control_cycle`. Resolved via a documented proxy:
    `candidates_persisted_for_review = 1 if result.best is not None else
    0` — accurate for every real invocation (the only theoretical
    divergence is an idempotent retry of the exact same candidate id,
    which is a no-op, not a rejection).
18. **Human-review boundary**: `ReviewedCandidate(status=AWAITING_HUMAN)`
    — no LLM review, `llm_review_performed=False`, explicit. Unchanged
    by this step.
19. **PaperBroker boundary**: `scripts/run_validation_cycle.py` never
    calls `PaperBroker.place_order` — confirmed unchanged (existing
    `daily_cycle_never_calls_place_order` freeze check still passes).

## Effective Opportunity Set — Confirmed From Source

`config/universe.yaml` currently lists exactly `SPY`, `QQQ` (2 tickers).
`CANDIDATE_GENERATION_ELIGIBLE_STRATEGIES`
(`src/workflows/candidate_generation.py`) is exactly
`{CASH_SECURED_PUT, COVERED_CALL, PUT_CREDIT_SPREAD}` — confirmed by
reading the module, not assumed. **`COVERED_CALL` is confirmed inert**
against the active cohort's all-cash, zero-share portfolio: the branch's
own guard is `if holding is not None and holding.shares >=
_CONTRACT_MULTIPLIER (100): attempt; else: ineligible`. An all-cash
portfolio has no `underlying_holdings` entry for SPY/QQQ at all, so
`holding is None` and the branch is always ineligible. This step now
records that fact deterministically (`record_strategy_ineligible("COVERED_CALL",
"COVERED_CALL_NO_SHARES")`) rather than silently producing nothing — it
does **not** buy shares, simulate share ownership, synthesize stock
inventory, convert it into a Cash-Secured Put, or alter the strategy in
any way. **Effective opportunity set for the active cohort: 2 tickers ×
2 live strategies (`CASH_SECURED_PUT`, `PUT_CREDIT_SPREAD`) = at most 4
strategy-attempts per cycle, `COVERED_CALL` always ineligible.** This
set is unchanged by this step.

## Current Strategy Breadth — Confirmed From Source

- `StrategyKind` (`src/strategies/base.py`): **16 members** (15 matching
  `StrategyType` + `WHEEL`).
- `TRADE_PROPOSAL_ELIGIBLE` (`src/llm/schemas.py`): **15 members** — all
  of `StrategyKind` except `WHEEL` (`WHEEL` is a meta/composite strategy,
  never its own `TradeProposal.strategy` value).
- `CANDIDATE_GENERATION_ELIGIBLE_STRATEGIES`
  (`src/workflows/candidate_generation.py`): **3 members**
  (`CASH_SECURED_PUT`, `COVERED_CALL`, `PUT_CREDIT_SPREAD`) — the only
  strategies with actual candidate-generation branches in
  `generate_candidates`. The other 12 `TRADE_PROPOSAL_ELIGIBLE`
  strategies have no candidate-generation logic anywhere in this
  codebase (they can still be proposed via `run_morning_scan`'s
  LLM-authored path, but never via the deterministic daily-cycle
  scanner this step instruments). **No candidate-generation branch was
  added in this step** — this is a future step, not this one.

## Zero-Candidate Blind Spots Found Before This Step

Before this step, a zero-`candidates_persisted_for_review` day gave the
operator no way to distinguish, from the daily cycle's own output alone:
a data-fetch failure from a liquidity rejection from a Quant rejection
from a Risk rejection from Covered Call's inert prerequisite from a
genuinely-empty universe intersection. `run_validation_cycle.py` printed
only "no new-position candidate -- {no_trade_reason}", which itself was
only populated in the NO_TRADE (hurdle-not-cleared) and empty-survivors
cases — a candidate that was never even constructed (stale chain,
illiquid contract, no in-range delta, Covered Call's inert prerequisite)
left no trace anywhere. This step closes that gap without touching
eligibility.

## Zero-Candidate Diagnostic Funnel — Design

`src.workflows.candidate_funnel.CandidateFunnel` (Pydantic, frozen,
`extra="forbid"`): `symbols_requested`, `symbols_market_data_successful`,
`symbols_market_data_failed`, `option_chains_received`,
`option_chains_quality_passed`, `option_chains_quality_failed`,
`expirations_seen`, `expirations_eligible`, `expirations_rejected`,
`contracts_seen`, `strategy_attempts`, `strategy_ineligible`,
`construction_attempts`, `construction_successes`,
`construction_rejections`, `quant_evaluations`, `quant_passed`,
`quant_rejected`, `risk_evaluations`, `risk_passed`, `risk_rejected`,
`candidates_generated`, `candidates_ranked`, `candidates_selected`,
`candidates_persisted_for_review`, plus bounded
`rejection_reasons`/`by_symbol`/`by_strategy`/`top_bottlenecks` and a
deterministic `zero_candidate_summary`. Every field name maps to a real
stage traced above; no `resizing_*` fields exist (see stage 15).
`build_candidate_funnel` is a pure aggregation function: it reads the
already-finished `FunnelDiagnostics` objects and `OpportunityScanResult`
AFTER the real scan is completely done, and never re-runs, re-derives,
or second-guesses any decision.

## Rejection-Reason Mechanism — Reuse, Never Duplicate

- **Liquidity**: `_liquidity_rejection_reason(contract, limits) ->
  str | None` extracted as the single source of truth —
  `passes_liquidity_filter` is now defined as `return
  _liquidity_rejection_reason(contract, limits) is None`, so the boolean
  gate and the diagnostic reason can never diverge. Reasons:
  `LIQUIDITY_OPEN_INTEREST`, `LIQUIDITY_VOLUME`, `LIQUIDITY_ZERO_MID`,
  `LIQUIDITY_SPREAD_TOO_WIDE`.
- **Delta**: `DELTA_OUT_OF_RANGE` when `_closest_by_target_delta` finds
  no contract in the target window.
- **Strategy prerequisite**: `COVERED_CALL_NO_SHARES` (the only one that
  exists in the real architecture — no others were invented).
  `PCS_NO_LONG_LEG_CANDIDATE`/`PCS_NEGATIVE_CREDIT` for the two-leg
  spread's own construction gates.
- **Quant/Risk**: `EVALUATION_RAISED` when `default_quant_stage`/
  `evaluate_trade_proposal` itself raises (isolated per-candidate); the
  Risk Engine's own `ReasonCode` values (`src.risk.reason_codes`,
  unmodified) for every Risk rejection — reused verbatim via the new
  `ScannedCandidate.risk_reason_codes: tuple[str, ...] = ()` field,
  never a second rejection-reason vocabulary.

No rejection rule was invented — every reason string traces to an
actual `if`/`elif` branch already present in the pipeline before this
step, or to the Risk Engine's own existing `ReasonCode` enum.

## Aggregation / Bounding Mechanism

`Counter[(stage, reason)]` accumulated across the cycle, emitted as
`rejection_reasons: tuple[RejectionReasonCount, ...]` (bounded to the 25
most common), `top_bottlenecks: tuple[str, ...]` (bounded to the 5 most
common, formatted `"{reason} ({stage}) -- {count}"`), `by_symbol`
(bounded to 50), `by_strategy` (bounded to 20, sorted alphabetically).
No per-contract row is ever stored — `FunnelDiagnostics` accumulates
small per-ticker counters plus a bounded list of `(strategy, event,
reason)` tuples, sized by the number of (strategy × eligible-expiration)
combinations actually attempted (for a 2-ticker, ≤45-day-DTE universe:
at most a few dozen entries per cycle).

## Zero-Candidate Summary — Deterministic, No LLM

Built only when `candidates_persisted == 0`, as an f-string from
already-computed integer counts and the top-3 bottleneck strings — no
LLM call anywhere in `src.workflows.candidate_funnel` or
`src.workflows.funnel_diagnostics` (confirmed: neither module imports
anything from `src.llm`).

## Persistence — Backward-Compatible, No Migration

`ControlCycleRecord.candidate_funnel: CandidateFunnel | None = None`
(`src/portfolio/cycle_record.py`) — the exact same additive-optional-
field pattern V1.5.0's `experiment_version_id` established.
`CONTROL_LOOP_DATABASE_SCHEMA_VERSION` (`src/portfolio/persistence.py`)
remains `"1.0.0"`, byte-for-byte unchanged — no `ALTER TABLE`, no new
table, `record_json TEXT NOT NULL` already stores whatever the current
Pydantic model serializes to. `tests/unit/workflows/test_candidate_funnel.py
::TestControlCycleRecordBackwardCompatibility` proves: (a) a hand-built
JSON blob lacking the `candidate_funnel` key entirely (and the
`experiment_version_id` key, a genuine pre-V1.5.0 shape) still
deserializes via `model_validate_json`, filling in `None`; (b)
`InMemoryControlLoopStore`/`SqliteControlLoopStore` round-trip a record
with and without a populated `candidate_funnel` correctly. No record
from September 22-30 was read, rewritten, or reconstructed by this step.

## Behavioral-Equivalence Proof

`tests/unit/workflows/test_candidate_funnel_equivalence.py`,
`TestCandidateFunnelBehavioralEquivalence`, 10 named scenarios, each
running `scan_and_rank_opportunities` twice (diagnostics absent vs.
enabled) and asserting `OpportunityScanResult` equality via
`_assert_scan_equivalent` (exact `scanned` tuple equality —
candidate identity/count/order/content, Quant result, Risk decision +
reason codes, post-trade exposure — plus `best`/`no_trade_reason`
equality):

1. zero-candidate day (empty `chains_by_ticker`)
2. rejected by data quality (2-hour-stale chain)
3. rejected by liquidity (open_interest=50 < min 100 on every contract)
4. rejected by Quant (monkeypatched `default_quant_stage` raises)
5. rejected by Risk (undersized $100k portfolio)
6. valid candidate survives to review (permissive `no_trade_hurdle`)
7. Covered Call prerequisite failure (no shares)
8. degraded provider (one ticker's chain missing from `chains_by_ticker`)
9. empty portfolio (no existing positions)
10. portfolio with an existing position (post-trade exposure computed
    identically both ways)

A second, coarser test
(`TestFunnelDiagnosticsCollectionItselfNeverAffectsDecisions`) proves
the same equivalence one layer down, directly against
`generate_candidates`. **All 11 tests pass.**

## Evidence Candidate Eligibility Did Not Change

- No edit to any `if`/`elif`/`return`/`continue` condition in
  `generate_candidates`'s decision logic — every `diagnostics.record_*`
  call was inserted immediately AFTER an existing decision, never before
  or inside one (confirmed by diff review of every edit).
- `passes_liquidity_filter`'s boolean result is unchanged — it now
  delegates to `_liquidity_rejection_reason(...) is None`, which
  evaluates the same 4 conditions in the same order as the pre-existing
  inline checks it replaced.
- `config/risk_limits.yaml`, `config/brokers.yaml`,
  `config/validation.yaml`, `config/universe.yaml`,
  `config/operations.yaml`, `config/llm.yaml`: all six byte-for-byte
  unchanged (hashes below, identical to the V1.5.4 freeze report's own
  recorded values).
- `tests/unit/workflows/test_candidate_generation.py`: all 31
  pre-existing tests pass unmodified.

## Evidence Risk/Quant/Sizing/Ranking Did Not Change

- `src/risk/`, `src/quant/` directories: not touched by this step at
  all (confirmed via `git diff --stat` — zero files under either path).
- `evaluate_trade_proposal`/`default_quant_stage` are called identically
  (same arguments, same order) in `scan_and_rank_opportunities` whether
  or not `diagnostics_by_ticker` is populated — the only new code reads
  their already-returned results into `ScannedCandidate.risk_reason_codes`,
  never into a branch.
- `tests/unit/portfolio/test_opportunity_scan.py`: all 6 pre-existing
  tests pass unmodified. `scan_and_rank_opportunities`'s ranking
  (`sorted(survivors, key=risk_adjusted_return, reverse=True)`) and
  selection (`best`/`no_trade_hurdle` comparison) logic: byte-identical,
  confirmed by diff.

## Dashboard Diagnostics Added

A new read-only "Candidate Funnel — Today's Diagnostics" card on the
existing Daily Validation Control section
(`src/dashboard/static/index.html`, `#cc-candidate-funnel`): symbols
scanned, usable option chains, contracts considered, strategy
construction attempts, construction successes, Quant-rejected,
Risk-rejected, candidates awaiting review, top rejection reasons, and
the zero-candidate summary (when applicable). Populated from 10 new,
purely presentational fields on `OperatorStatusView`
(`src/dashboard/validation_ops.py`), read from
`cycle_record.candidate_funnel` (today's `ControlCycleRecord`, already
fetched by `build_operator_status`). No confirmation control, no
threshold input, no strategy toggle, no universe control, no "trade
anyway" button anywhere in `renderCcCandidateFunnel` — proven by a
dedicated structural test
(`test_candidate_funnel_card_adds_no_confirmation_or_control_capability`)
and a new freeze check (`dashboard_candidate_funnel_is_read_only`).

## Backend-Derived Software-Version Display

`OperatorStatusView.software_version: str = FREEZE_NAME`
(`src/dashboard/validation_ops.py`) — a fixed Python default sourced
from `src.validation.freeze.FREEZE_NAME`, present even in the
`configured=False` degraded branch. `dashboard.js`'s
`document.getElementById("cc-software-badge").textContent` now reads
`status.software_version` (falling back to a labeled
`FALLBACK_SOFTWARE_VERSION` constant only before the first
`/api/operator-status` response ever lands, or if that request fails) —
replacing the previous hardcoded `const SOFTWARE_VERSION =
"PAPER_TRADING_V1.5.2"`, which had drifted stale across V1.5.3 and
V1.5.4. This change is purely presentational: no trading behavior, no
cycle behavior, no auto-restart, no process-killing, no deployment
mechanism, and no database mutation of any kind. This addresses the
Sept 30 stale-dashboard-process incident's own recommendation (a small,
decision-neutral fix) — see "Stale Dashboard Process Investigation"
below; the trading-logic bug that incident actually surfaced
(`'OperationsConfig' object has no attribute
'risk_data_wiring_enabled'`) was a stale-process artifact, not a code
defect, and this step made no trading-logic change because of it.

## Current vs. Historical Alert Distinction

**Not implemented this step.** The dashboard's existing Alerts card
already renders every unresolved `ControlLoopAlert` without a
current/historical grouping; adding that distinction was evaluated as
optional/presentational per the task's own "if easy" framing but was
not judged worth the additional dashboard-surface risk this late in the
step, given the Candidate Funnel card was the higher-value, explicitly-
requested addition. No alert was deleted, hidden, or reordered.

## Definitive Market-Hours-Gate vs. Lifecycle-Monitoring Answer

**Re-confirmed by a fresh full read of `scripts/run_validation_cycle.py`
(unchanged since V1.5.4): YES.** The top-level market-hours gate
(`evaluate_validation_cycle_eligibility`, called at `run_validation_cycle`'s
own entry, before `_expire_stale_candidates`/provider construction/any
mutating call) `return`s `False` — the ENTIRE function returns —
strictly BEFORE the single call site of `run_outer_cycle` (which is the
only call path to `run_control_cycle`, and through it to
`src.lifecycle.engine.evaluate_position`/`src.risk.kill_switch
.check_kill_switch`). When the new-position scan window is closed (pre-
market, after-hours, non-trading day), this entrypoint never reaches
existing-position Lifecycle Engine or Risk kill-switch monitoring at
all. This is unchanged from V1.5.1's original finding and V1.5.4's
re-confirmation — **not fixed by this step, per explicit instruction**;
recorded again below as an unresolved, future safety-hardening item.

## Stale Dashboard Process Investigation

The Sept 30 incident (an older dashboard process, started before the
V1.5.4 pull, serving `AttributeError("'OperationsConfig' object has no
attribute 'risk_data_wiring_enabled'")` even though the on-disk source
already had that field) was a stale-in-memory-process artifact, not a
code defect — confirmed by the fact that restarting the process resolved
it with no code change. No trading logic was altered because of this
incident (explicitly prohibited by the task). The one safe, decision-
neutral remedy identified — making the dashboard's displayed version
come from the backend rather than a hard-coded frontend string, so a
stale PROCESS becomes visible as a stale VERSION NUMBER on the badge
rather than a silent mismatch — was implemented (see "Backend-Derived
Software-Version Display" above). No auto-restart, process-killing, or
deployment-automation capability was added.

## Provider / Tradier Requests

No new Tradier API endpoint was added or called by this step. No
network request count increased — `FunnelDiagnostics` instruments data
already flowing through `generate_candidates`/`scan_and_rank_opportunities`
(the chain, expirations, and contracts already fetched/loop-processed
once per cycle for the existing candidate-generation logic itself).
Tradier remains market-data-only (`tradier_market_data_only` freeze
check unmodified, still passes).

## `risk_data_wiring` / V1.5.4 Protections Preserved

`config/operations.yaml`'s `risk_data_wiring.enabled` remains `false`
(confirmed by hash, byte-for-byte identical to V1.5.4's own recorded
value). `min_correlation_observations`/`correlation_lookback_days`
unchanged. `src/portfolio/risk_data.py` was not touched by this step at
all (zero diff). The `historical_data_capability_installed` and
`correlation_alignment_uses_date_intersection` freeze checks
(unmodified) still pass.

## Existing Components Reused

`src.risk.reason_codes.ReasonCode`/`RiskDecision` (Risk's own
vocabulary, reused verbatim). `src.risk.engine.evaluate_trade_proposal`/
`src.orchestration.pipeline.default_quant_stage` (called identically,
never duplicated). `src.portfolio.cycle_record.ControlCycleRecord`'s
existing additive-optional-field pattern (`experiment_version_id`).
`src.portfolio.persistence.SqliteControlLoopStore`'s existing
`record_json`/`schema_version` storage (unmodified).
`OperatorStatusView`'s existing degraded-branch convention.

## New Components Added

`src/workflows/funnel_diagnostics.py` (`FunnelDiagnostics`).
`src/workflows/candidate_funnel.py` (`CandidateFunnel`,
`build_candidate_funnel`, `RejectionReasonCount`, `SymbolFunnelSummary`,
`StrategyFunnelSummary`). New optional parameters:
`generate_candidates(..., diagnostics=None)`,
`scan_and_rank_opportunities(..., diagnostics_by_ticker=None)`. New
config flag: `OpportunityScanConfig.collect_candidate_funnel: bool =
False`. New field: `ControlCycleRecord.candidate_funnel`,
`ControlCycleInputs.candidate_funnel`, `OuterCycleResult.candidate_funnel`.
10 new `OperatorStatusView` fields + `software_version`. 3 new freeze
checks (`candidate_funnel_is_observability_only`,
`candidate_funnel_field_is_optional_and_additive`,
`dashboard_candidate_funnel_is_read_only`).

## Files Changed

Implementation commit (`99632af`): `src/workflows/funnel_diagnostics.py`
(new), `src/workflows/candidate_funnel.py` (new),
`src/workflows/candidate_generation.py`,
`src/portfolio/opportunity_scan.py`, `src/portfolio/orchestrator.py`,
`src/portfolio/control_loop.py`, `src/portfolio/cycle_record.py`,
`scripts/run_validation_cycle.py`, `src/dashboard/validation_ops.py`,
`src/dashboard/static/dashboard.js`, `src/dashboard/static/index.html`,
`src/validation/freeze.py` (3 new checks), plus new/modified tests:
`tests/unit/workflows/test_candidate_funnel_equivalence.py` (new),
`tests/unit/workflows/test_candidate_funnel.py` (new),
`tests/unit/validation/test_freeze.py`,
`tests/unit/dashboard/test_frontend_control_center.py`,
`tests/frontend/operator_control_browser_scope.test.js`.

Freeze commit (this one): `src/validation/freeze.py` (version bump),
`tests/unit/validation/test_freeze.py` (version-string assertions),
`VALIDATION_MANIFEST.json`, `STEP_23_5_FREEZE_REPORT.md`, `progress.md`.

## Files Intentionally NOT Changed

`config/risk_limits.yaml`, `config/brokers.yaml`,
`config/validation.yaml`, `config/universe.yaml`,
`config/operations.yaml`, `config/llm.yaml` — every value byte-for-byte
unchanged (verified by hash below, identical to V1.5.4's own recorded
values). `src/risk/`, `src/quant/` (all deterministic Risk/Quant logic
untouched). `src/brokers/paper.py`, `src/brokers/fidelity.py`
(PaperBroker/Fidelity execution semantics untouched). `src/lifecycle/`
(untouched). `src/portfolio/risk_data.py` (V1.5.4 correlation logic
untouched). `src/data/tradier_provider.py` (no new endpoint).
`src/review/` (candidate-confirmation semantics untouched — CLI-only,
unchanged). `src/dashboard/app.py` (no new route added or removed —
only `validation_ops.py`'s existing `OperatorStatusView` gained
fields). `scripts/confirm_candidate.py` (untouched). The active
cohort's own operational database (`data/options_agent.db`) was never
created, accessed, or mutated by this step's own new production code or
tests.

## Focused Test Results

`tests/unit/workflows/test_candidate_funnel_equivalence.py`: 11 passed
(new). `tests/unit/workflows/test_candidate_funnel.py`: 13 passed (new).
`tests/unit/workflows/test_candidate_generation.py`: 31 passed
(unmodified). `tests/unit/portfolio/test_opportunity_scan.py`: 6 passed
(unmodified). `tests/unit/portfolio/`: 226 passed.
`tests/acceptance/test_orchestrator_security.py`: unmodified, still
passing. `tests/acceptance/test_run_validation_cycle_cli.py` +
`test_review_only_daily_cycle.py`: 19 passed. `tests/unit/dashboard/`:
194 passed (2 new). `tests/frontend/*.test.js` (Node): 45 passed (5
new). `tests/unit/validation/test_freeze.py`: 94 passed (8 new).

## Full-Suite Result

`python -m pytest -q`: **3704 passed, 6 skipped, 0 failed** (up from the
pre-step V1.5.4 baseline of 3670 — net new: 34 tests, all accounted for
above). Zero real network calls anywhere in this step's own tests. No
test in this step's own files accesses `data/options_agent.db` — every
store used is `InMemory*` or `tmp_path`-backed.

## Freeze Result

`make verify-freeze` (`python -m src.validation.freeze verify`) against
the regenerated `PAPER_TRADING_V1.5.5` manifest: **all 71 checks pass**,
including the three new `candidate_funnel_is_observability_only`/
`candidate_funnel_field_is_optional_and_additive`/
`dashboard_candidate_funnel_is_read_only` checks and every check carried
forward from V1.5.4 unmodified. `portfolio_module_hash`,
`run_validation_cycle_script_hash`, and
`dashboard_validation_ops_module_hash` legitimately changed (this step
adds genuine new production capability to files those hashes cover —
the same, expected pattern V1.4.4's own freeze established) and now
correctly show as `unchanged` against the freshly-regenerated manifest.
The `SOFTWARE FREEZE VERIFIED` banner wording is preserved unchanged,
not reverted; this step does not reintroduce any misleading claim that
the real validation cohort has not started.

## Protected-File Integrity (Before and After)

Confirmed byte-identical, before this step's first edit and again after
the implementation commit — **identical to the values V1.5.4's own
freeze report recorded**, confirming zero drift across this step:
- `config/risk_limits.yaml`: `e502d64800785ffd9d21980f250f9822ac287850a798d6e935d26acd4894c8b9`
- `config/brokers.yaml`: `99a3d9dddb0ca1c8a425ac0bee6f18cf4ee0c468e451f0689450720bb5b9fed9`
- `config/validation.yaml`: `d5f3bfe9eb951362bd050d20a6f08301f5db7707689e72c932a0801732445f52`
- `config/llm.yaml`: `625e965b04ea0cca2718ec7d0b7b0fda957cde2949d94a732d0ce408fea16838`
- `config/operations.yaml`: `786303e8c2f37cf9093b32a2a9f25e8cd7545b2fad53738412d414d1b59097a7`
- `config/universe.yaml`: `b88aac45f5d0eb15a974f228f12257d16d70153611767cf63ae5e5f3f26dd30c`

## Operational Database — Absent Before, Absent After

`data/options_agent.db`/the entire `data/` directory was absent in this
sandbox before this step's first edit and remains absent after every
subsequent full regression run in this step, including the final one
before this report was written (`ls data/` fails both times). The
operator's own real database (referenced by the fingerprint
`9fc235065aa5da43b3b82333229960780a401df2eecfb5bf4d645125a87779f8` in
this task's own instructions) was never present in, or reachable from,
this sandbox at any point — it was not accessed, mutated, initialized,
migrated, reset, deleted, rewritten, or tested against.

## No Official Validation Cycle Ran

`scripts/run_validation_cycle.py` was never invoked directly against
real config/data in this step. Every exercise of it was through
`tests/acceptance/test_review_only_daily_cycle.py`/
`test_run_validation_cycle_cli.py`'s reused, `tmp_path`-isolated
`scripts`/`environment` fixtures, or as a freeze-check source-text read.

## No Candidate Was Confirmed

`scripts/confirm_candidate.py` was not touched by this step at all and
was not invoked by any new test.

## No PaperBroker Fill Was Created

No new test in this step calls `PaperBroker.place_order` against
anything but a `tmp_path`/in-memory store; the pre-existing acceptance
tests that do were re-run unmodified and still pass unchanged.

## No Real Brokerage/Order API Was Called

No network call of any kind was made by this step's implementation or
tests. `tradier_market_data_only` (unmodified) still passes.

## No Secret or Token Persisted in Diagnostics

`tests/unit/workflows/test_candidate_funnel.py
::test_no_secret_or_token_ever_appears_in_a_built_funnel` proves a built
`CandidateFunnel`'s full JSON serialization contains none of `bearer`,
`token`, `authorization`, `api_key`, `apikey`, `secret`, `password`
(case-insensitive). No option chain, bearer token, or raw provider
payload is ever stored by `FunnelDiagnostics`/`CandidateFunnel` — only
ticker symbols, strategy names, contract counts, and this codebase's own
existing rejection-reason vocabulary.

## Unresolved Safety/Architecture Issues

**The market-hours-gate-vs-existing-position-lifecycle-monitoring
suppression** (see "Definitive Market-Hours-Gate vs. Lifecycle-
Monitoring Answer" above) remains unresolved — flagged again, not fixed,
per explicit instruction. Recommended as a candidate for a future,
explicitly-approved safety-hardening step (e.g., splitting the
entrypoint so existing-position monitoring runs independently of the
new-position scan window). **A pre-existing freeze-manifest coverage
gap, noted but not fixed**: `src/workflows/` (which now contains
`candidate_funnel.py`/`funnel_diagnostics.py`, and already contained
`candidate_generation.py`, modified by this step) has never had its own
dedicated hash target in `src/validation/freeze.py` — this predates this
step (confirmed: no `workflows_module_hash` field exists in
`FreezeManifest` at all, for any prior version) and is out of scope to
add here without a separate, explicitly-approved decision about whether
to widen freeze-hash coverage; this step's own behavior-preservation for
those files is instead proven by the dedicated equivalence test suite,
re-run on every `python -m pytest`.

## Implementation Commit Hash

`99632af`

## Freeze Commit Hash

Recorded in the final report after this commit completes (this file is
part of that commit).

## Push Status

Reported in the final report after `git push` completes.

## Working-Tree Status

Reported in the final report after the freeze commit and push complete.

## Recommended Next Step

**DO NOT IMPLEMENT IT.** The candidate funnel this step builds is now
available to help the operator understand WHY the active cohort remains
at 0 candidates through Day 7 of 90 — that diagnosis (reading the
funnel's `rejection_reasons`/`top_bottlenecks`/`zero_candidate_summary`
output over the next several real daily cycles) is the operator's own
next step, not a recommendation this report makes or a change this step
implements. This step does not recommend, and was not asked to
recommend, loosening any threshold, expanding the universe, activating
additional strategies, activating `risk_data_wiring`, or forcing
candidate creation. Universe expansion, additional strategy activation,
successor-cohort creation, `risk_data_wiring` activation, threshold
tuning, and candidate confirmation all remain explicitly out of scope
for any future step.
