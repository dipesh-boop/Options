# STEP 23.7 FREEZE REPORT — PAPER_TRADING_V1.5.7

## Status

**PAPER_TRADING_V1.5.7 / SOFTWARE FREEZE VERIFIED**

A narrow corrective release. Cohort `paper-trading-v1.4.3-validation-2026-09-22`
remains ACTIVE, untouched, and unreset by this step. The 2026-10-02
`ControlCycleRecord` (the evidence of the pre-fix defect) was not
rewritten, regenerated, or backfilled. No official validation cycle,
`confirm_candidate.py` invocation, or operational database access
occurred during development or testing.

## A. Root cause

**Exact timestamp mismatch.** `scripts/run_validation_cycle.py` captures
`now = now or datetime.now(timezone.utc)` once, before the fetch loop.
`TradierMarketDataProvider.get_option_chain_for_dte_window`/
`get_option_chain` each stamp their returned `OptionChain.timestamp`
with a fresh, later `datetime.now(timezone.utc)` taken DURING that same
fetch. `src.portfolio.orchestrator._run_opportunity_scan_stage` then
passed `inputs.as_of` (the pre-fetch `now`) straight through as
`scan_and_rank_opportunities`'s `now=`, which `generate_candidates`
used for `TradeProposal(timestamp=now, data_timestamp=chain.timestamp,
...)`. `TradeProposal`'s own model validator
(`src/llm/schemas.py::TradeProposal._validate_data_freshness`) rejects
`data_timestamp > timestamp` unconditionally (zero tolerance, by
design) — so every candidate whose chain fetch took any nonzero real
wall-clock time (always, in production; negligible but still nonzero
even in a fast test process) failed this check.

**Exact control flow.** `run_validation_cycle()` → `now` captured →
fetch loop (chain.timestamp stamped later) → `OuterCycleInputs(as_of=now,
...)` → `run_outer_cycle` → `_run_opportunity_scan_stage` →
`scan_and_rank_opportunities(..., now=inputs.as_of, ...)` →
`generate_candidates(..., now=now, ...)` → `_build_proposal(...)` →
`TradeProposal(timestamp=now, data_timestamp=chain.timestamp)` → raises.

**Exact silent-exception path.** `src/portfolio/opportunity_scan.py`'s
per-ticker loop:
```python
try:
    candidates = generate_candidates(...)
except Exception:  # noqa: BLE001
    continue
```
This caught the `ValidationError` from `_build_proposal` and discarded
it with zero record anywhere. Compounding it,
`src.workflows.candidate_generation.generate_candidates` called
`diagnostics.record_construction(strategy, success=True, reason=None)`
**before** calling `_build_proposal` — so a `FunnelDiagnostics`
"construction_success" event was already recorded for a candidate that
then never actually got built, matching the exact observed 2026-10-02
funnel shape: `construction_successes=2`, `quant_evaluations=0`,
`candidates_generated=0`, with nothing in `rejection_reasons`
explaining the gap.

**Telling corroboration.** `tests/acceptance/test_review_only_daily_cycle.py`'s
own `FakeMarketDataProvider` already subtracted a 5-second buffer from
every chain it returns, with a docstring explicitly describing this
exact bug as the reason — this test fixture had already discovered and
silently worked around the defect rather than exposing it. This file's
new acceptance test (`test_opportunity_evaluation_timestamp.py`)
removes that workaround's necessity by fixing the real defect instead.

## B. Architectural fix (timestamp semantics)

**Files changed**: `src/portfolio/orchestrator.py`,
`scripts/run_validation_cycle.py`.

**How the two timestamps now differ.** A new, optional field,
`OpportunityScanConfig.evaluation_as_of: datetime | None = None`
(`src/portfolio/orchestrator.py`). `_run_opportunity_scan_stage` computes
`scan_as_of = cfg.evaluation_as_of if cfg.evaluation_as_of is not None
else inputs.as_of` and uses `scan_as_of` for both
`scan_and_rank_opportunities`'s `now=` and `build_candidate_funnel`'s
`generated_at=`. `OuterCycleInputs.as_of` itself is **never** touched —
it remains exactly what it always was: the cycle's own audit identity
(`cycle_id`), the `ControlCycleInputs.as_of` passed to the unmodified
`run_control_cycle` (existing-position lifecycle/Risk kill-switch
monitoring), the pending-ticket monitor's `as_of`, the alert-generation
`now`, and the opportunity decision snapshot's `timestamp`. Every one
of those uses was independently confirmed from source (see the
Architecture Trace below) before deciding not to touch `as_of`.

`scripts/run_validation_cycle.py` captures a second timestamp,
`evaluation_as_of = datetime.now(timezone.utc)`, immediately after its
fetch loop's `finally:` block (i.e., after every chain fetch for this
cycle has actually completed), passes it as
`OpportunityScanConfig(evaluation_as_of=evaluation_as_of, ...)`, and
reuses the same value for the later `evaluate_trade_proposal` re-run
that recovers the full `RiskDecisionResult` for the persisted
`ReviewedCandidate` (previously re-run with the stale pre-fetch `now`,
inconsistent with what the scan itself had used).

**Why this preserves truthful market-data timestamps.** `chain.timestamp`
is never modified, read back, or overridden anywhere in this fix —
`merge_option_chains` (V1.5.6) and the Tradier provider's own stamping
are completely untouched. The fix only changes which `now` the
*evaluator* uses, never what the *data* claims about itself.

**Why `TradeProposal` integrity remains intact.** `TradeProposal._validate_data_freshness`
(`src/llm/schemas.py`) was not modified, weakened, given a tolerance,
or bypassed. A dedicated test
(`TestTimestampDomainIntegrityRegressionV157::test_future_data_integrity_validator_itself_is_unmodified_and_still_rejects_bad_input`)
constructs a `TradeProposal` directly with `data_timestamp` after
`timestamp` and asserts it still raises, byte-for-byte the same as
before this step. The fix instead guarantees, by construction, that
`evaluation_as_of` (captured strictly after the fetch loop completes)
is `>=` every chain timestamp that fetch produced — so a correctly-built
proposal satisfies the check honestly, not by loosening it.

## C. Observability fix (generation exceptions)

**Files changed**: `src/workflows/funnel_diagnostics.py`,
`src/workflows/candidate_generation.py`, `src/workflows/candidate_funnel.py`.

**How generation exceptions are now represented.** `FunnelDiagnostics`
gained `record_generation_exception(strategy: str, category: str) ->
None` (same return-None, purely-additive, never-read-back pattern as
every other `record_*` method). `generate_candidates`'s three strategy
blocks (`CASH_SECURED_PUT`, `COVERED_CALL`, `PUT_CREDIT_SPREAD`) each
now wrap their own `_build_proposal(...)` call in a `try/except`:
`record_construction(strategy, success=True, ...)` is called **only**
in the `else` branch, after `_build_proposal` has actually returned a
proposal; on exception, `record_generation_exception(strategy,
type(exc).__name__)` is called instead, and nothing is appended to
`candidates`. This is per-strategy, not per-ticker — a ticker requesting
multiple strategies no longer loses ALL of them to one strategy's
exception (see the section-15 fix below). The catch is unconditional
(not gated on `diagnostics is not None`), so `generate_candidates`
itself never raises for this failure mode regardless of caller —
protecting `src.workflows.morning_scan`'s own call site too, which
passes no diagnostics at all.

**Funnel stage/reason.** `CandidateFunnel` and `StrategyFunnelSummary`
each gained a `generation_exceptions: int` field.
`build_candidate_funnel` aggregates the new `"generation_exception"`
event type into its own counter (never mixed into
`construction_attempts`/`construction_successes`/
`construction_rejections`) and into `reason_counter[("generation_exception",
category)]`, so it appears in `rejection_reasons`/`top_bottlenecks` and
in the `zero_candidate_summary` string.

**Confirmation they no longer silently disappear.** Proven at three
levels: `generate_candidates` unit tests
(`TestGenerationExceptionObservabilityV157`), the real
`build_candidate_funnel` pipeline
(`TestGenerationExceptionFunnelAggregationV157::test_reproduces_the_2026_10_02_shape_fully_explained_by_generation_exceptions`),
and the real `run_outer_cycle` entry point
(`TestEvaluationAsOfWiringV157`), and finally the real
`scripts/run_validation_cycle.py` script itself
(`test_opportunity_evaluation_timestamp.py`).

**Confirmation pre-Quant failures never count as Quant evaluations.**
`quant_evaluations` in `build_candidate_funnel` is derived solely from
`scan_result.scanned` — a candidate whose proposal construction raised
never enters that list (`generate_candidates` returns before appending
it), so it structurally cannot be double-counted. Verified directly:
`funnel.quant_evaluations == 0` alongside `funnel.generation_exceptions
== 1` in the reproduction test.

**No sensitive payload persisted.** `category` is always
`type(exc).__name__` (e.g. `"ValidationError"`) — never `str(exc)`,
`exc.args`, or any field value. A dedicated test
(`test_generation_exception_category_is_bounded_never_the_raw_message`)
asserts the recorded category is short (`< 64` chars) and contains no
newline, ruling out a multi-line pydantic error dump.

## D. Regression tests

**Exact T / T+delta reproduction**
(`tests/unit/workflows/test_candidate_generation.py::TestTimestampDomainIntegrityRegressionV157`):
`T = NOW`, `T_PLUS_DELTA = NOW + timedelta(seconds=5)`. Calling
`generate_candidates(..., now=T)` against a chain timestamped
`T_PLUS_DELTA` (the broken, pre-fetch ordering) now returns `[]` with a
`generation_exception`/`"ValidationError"` diagnostic, never a crash,
never a false `construction_success`. Calling it with
`now=T_PLUS_DELTA + timedelta(seconds=1)` (the corrected, post-fetch
ordering) returns exactly one `Candidate` whose
`proposal.timestamp >= proposal.data_timestamp`, with
`proposal.data_timestamp == T_PLUS_DELTA` (the real chain timestamp,
untouched) and `proposal.timestamp == <the supplied evaluation time>`
(a genuine value, never fabricated). A direct boundary test proves
exactly-equal timestamps are valid, not a boundary failure. A direct
test proves `TradeProposal`'s own integrity validator is unmodified and
still rejects bad input.

**Generation-exception test**
(`TestGenerationExceptionObservabilityV157`): proves one strategy's
exception no longer blocks a later strategy's own diagnostic events on
the same ticker (the exact section-15 "strategy anomaly" fix, see
below); proves the category is bounded; proves
`generate_candidates` never raises for this failure mode even when
`diagnostics` is omitted entirely.

**Orchestrator-level wiring**
(`tests/unit/portfolio/test_orchestrator.py::TestEvaluationAsOfWiringV157`):
proves `evaluation_as_of` defaulting to `None` reproduces the ordering
bug at the real `run_outer_cycle` entry point; proves supplying it after
the chain timestamp fixes it; proves `OuterCycleInputs.as_of` itself is
never moved.

**Acceptance-level, real script**
(`tests/acceptance/test_opportunity_evaluation_timestamp.py`): a
`FakeLaggyMarketDataProvider` stamps chains with the real
`datetime.now(timezone.utc)` at fetch time (no artificial offset in
either direction — the real production shape, since any nonzero real
elapsed time between the pre-fetch `now` and the fetch call reproduces
the mismatch). Proves the real `scripts/run_validation_cycle.py`
produces exactly one `AWAITING_HUMAN` candidate (not silently zero) and
that its persisted `ControlCycleRecord.candidate_funnel.generation_exceptions
== 0`.

**V1.5.6 DTE regression status**: unmodified and still passing.
`tests/unit/data/test_tradier_provider.py` (123), `tests/unit/data/test_option_chain.py`
(29), `tests/unit/workflows/test_candidate_funnel.py`'s V1.5.6-specific
`TestExpirationDteOutOfRangeRejectionReason` class, and
`tests/acceptance/test_dte_window_chain_retrieval.py` (3) all pass
unchanged. `TradierMarketDataProvider.get_option_chain_for_dte_window`
itself was not touched in this step.

## E. Strategy anomaly (2026-10-02: only CASH_SECURED_PUT in `by_strategy`)

**Root cause confirmed, not a separate defect.** `generate_candidates`'s
three strategy blocks run sequentially in source order:
`CASH_SECURED_PUT` → `COVERED_CALL` → `PUT_CREDIT_SPREAD`. Before this
fix, an exception raised inside `_build_proposal` for `CASH_SECURED_PUT`
propagated straight out of `generate_candidates` (no internal
try/except existed), aborting the function entirely — so
`COVERED_CALL`'s `record_strategy_ineligible`/`record_strategy_attempt`
and `PUT_CREDIT_SPREAD`'s `record_strategy_attempt` calls, both later in
the same function body, never executed at all for that ticker. This
exactly explains why only `CASH_SECURED_PUT` appeared in `by_strategy`
on 2026-10-02 even though `config/universe.yaml` configures all three
strategies: it is the SAME root cause as the timestamp defect (section
A), manifesting as a second, downstream observability gap, not a
regime-behavior change, not a funnel-aggregation artifact, and not an
independent defect.

**Confirmed by a dedicated regression test**
(`test_an_exception_for_one_strategy_does_not_block_a_later_strategy_on_the_same_ticker`):
reproducing the exact mismatched-timestamp scenario with BOTH
`CASH_SECURED_PUT` and `PUT_CREDIT_SPREAD` requested, this test asserts
`PUT_CREDIT_SPREAD`'s own diagnostic events are now present (proving
`generate_candidates` no longer aborts after `CASH_SECURED_PUT`'s
failure) and that both strategies show their own
`generation_exception` entries.

**No policy change made.** This was fixed as a direct, necessary
consequence of the per-strategy exception isolation required by section
C (B above) — no strategy activation, selection, or regime-mapping logic
was touched to "fix" this observation.

## F. Safety invariants — explicit confirmation of no changes

| Invariant | Confirmation |
|---|---|
| DTE policy (`min_dte=20`, `max_dte=45`, `management_dte=21`) | `src/workflows/candidate_generation.py`'s `QuantFilterConfig` defaults: no diff. |
| Liquidity/OI policy | `_liquidity_rejection_reason`/`passes_liquidity_filter`: no diff. `LIQUIDITY_OPEN_INTEREST` rejections on 2026-10-02 were NOT loosened or reinterpreted. |
| Quant thresholds | `src/quant/`: no diff; full quant suite unchanged and passing. |
| Risk limits | `src/risk/`: no diff; full risk suite (including `test_architecture_boundary.py`) unchanged and passing. |
| Position sizing | `src/risk/trade_risk.py`/`src/quant/position_sizing.py`: no diff. |
| Universe | `config/universe.yaml`: no diff (still SPY, QQQ, CASH_SECURED_PUT/COVERED_CALL/PUT_CREDIT_SPREAD). |
| Strategy activation | `config/brokers.yaml` `allowed_strategies`: no diff. |
| Broker/execution policy | `src/brokers/paper.py`, `src/brokers/fidelity.py`: not touched. `daily_cycle_never_calls_place_order` freeze check: `[OK]`. |
| Tradier market-data-only | `src/data/tradier_provider.py`: not touched this step. `tradier_market_data_only` freeze check: `[OK]`. |
| Human confirmation requirement | `src/review/confirmation.py`, `scripts/confirm_candidate.py`: not touched. `dashboard_cannot_confirm_candidates` freeze check: `[OK]`. |

## G. Validation history

- Active cohort `paper-trading-v1.4.3-validation-2026-09-22`: not reset,
  not reinitialized; no code in this diff calls `start_new_cohort` or
  any reset path.
- The 2026-10-02 `ControlCycleRecord`: not rerun, not edited, not
  backfilled. No test or development command targeted the operator's
  real `config/validation.yaml`/`config/operations.yaml` paths or
  `data/options_agent.db` — every new/modified test uses `tmp_path`
  sqlite files or `InMemory*` stores, consistent with every existing
  test in this suite.
- No PaperBroker position was created by any test in this step —
  verified explicitly (`SqliteIdempotencyStore.all() == []`) in the new
  acceptance test.
- The repository has no tracked `data/` directory and no
  `data/options_agent.db` file exists in this sandbox at any point
  during this work (confirmed via `git status --porcelain data/` and
  `ls data/`, both empty/absent) — there is no operational database
  hash to compare before/after, since none exists in this environment.
  This mirrors the exact state confirmed during V1.5.6.

## H. Test results

- `tests/unit/workflows/test_candidate_generation.py`: **38 passed** (31
  pre-existing + 7 new, `TestTimestampDomainIntegrityRegressionV157`).
- `tests/unit/workflows/test_candidate_funnel.py` +
  `test_candidate_funnel_equivalence.py`: **31 passed** (29 pre-existing
  + 2 new, `TestGenerationExceptionFunnelAggregationV157`).
- `tests/unit/portfolio/test_orchestrator.py`: **22 passed** (19
  pre-existing + 3 new, `TestEvaluationAsOfWiringV157`).
- `tests/acceptance/test_opportunity_evaluation_timestamp.py` (new): **2
  passed**.
- `tests/unit/validation/test_freeze.py`: **94 passed** (version
  assertions bumped; `record_generation_exception` added to the
  observability-only check's enumerated method list).
- Full `tests/acceptance/`: **343 passed, 2 skipped** (includes the
  unmodified V1.5.6 DTE-retrieval suite).
- **Full suite**: `python -m pytest -q` → **3754 passed, 6 skipped**,
  zero failures (up from 3740 before this step).
- `./scripts/verify_freeze.sh` → **PAPER_TRADING_V1.5.7 / SOFTWARE
  FREEZE VERIFIED**, every check `[OK]` (two modules — `portfolio_module_hash`,
  `run_validation_cycle_script_hash` — legitimately drifted before the
  manifest regeneration, exactly the genuine-new-capability pattern
  every prior freeze bump has shown; both show `[OK]` against the
  regenerated manifest).

## I. Version control

- Implementation commit: see repository log (source + test changes).
- Freeze-artifacts commit: this report, `VALIDATION_MANIFEST.json`,
  `progress.md`, and the `src/validation/freeze.py` /
  `tests/unit/validation/test_freeze.py` version bumps.
- Branch push: attempted; report actual result in the final chat
  summary (same branch, forward commits only — V1.5.6 history was not
  rewritten).
- Tag: `paper-trading-v1.5.7` created locally after verification; tag
  push historically hits an HTTP 403 in this environment (every prior
  freeze) — reported honestly if it recurs, never force-pushed or
  worked around.

## J. Remaining known issues (explicitly retained, not fixed this step)

1. **`src/review/confirmation.py`'s nearest-N refresh / DTE mismatch.**
   Its fresh-quote refetch at confirmation time still uses
   `get_option_chain()` (nearest-N by calendar date), not the V1.5.6
   `DteWindowOptionChainProvider` capability — a candidate whose
   expiration has since rolled outside the nearest-N window could fail
   to be found again at confirmation time. Unrelated to this step's
   timestamp/observability fixes; deliberately not bundled in.
2. **Market-hours gate suppressing lifecycle monitoring outside the
   new-position window** (first identified V1.5.1, reaffirmed every
   step since). Unchanged by this step — the timestamp fix touches only
   opportunity-scan evaluation timing, never the market-hours gate
   itself, and does not make lifecycle monitoring any less safe than
   before.

Neither is implemented here without separate approval, per instruction.
