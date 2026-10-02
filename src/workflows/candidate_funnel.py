"""PAPER_TRADING_V1.5.5, Step 4: candidate-funnel observability.

**This module changes no trading decision.** Its job is to make an
existing, already-deterministic pipeline (`src.workflows
.candidate_generation.generate_candidates` -> `src.portfolio
.opportunity_scan.scan_and_rank_opportunities`) explain itself: what was
scanned, what survived each stage, what was rejected, and why -- without
altering what candidate (if any) is generated, ranked, selected, or
persisted for human review.

**How decision-neutrality is structurally guaranteed, not just tested.**
`FunnelDiagnostics` is a plain, mutable collector with `record_*` methods
that return `None` and are never read by the code that calls them --
every `record_*` call in `src.workflows.candidate_generation` is appended
strictly AFTER the real decision (a filter pass/fail, a strategy
eligibility check, a constructed `Candidate`) already happened, using the
exact same values that decision already computed. Nothing in this module,
or in the `diagnostics=`/`diagnostics_by_ticker=` parameters this step
adds to `generate_candidates`/`scan_and_rank_opportunities`, is ever
read back into an `if`/`return`/loop-control statement in either of
those two functions. `build_candidate_funnel` below is a second,
entirely separate pass: it reads the `FunnelDiagnostics` objects and the
already-complete `OpportunityScanResult` (built by the ordinary,
unmodified scan) AFTER the scan is completely finished, and produces a
read-only summary. See `tests/unit/workflows/test_candidate_funnel_equivalence.py`
for the behavioral-equivalence proof this design makes possible: running
the exact same inputs through the scan with `diagnostics_by_ticker=None`
(V1.5.4 behavior) and with it populated (V1.5.5) must, and does, produce
byte-identical `OpportunityScanResult`s.

**Granularity.** `construction_attempts`/`construction_successes`/
`construction_rejections` are counted at the same granularity the real
code already loops at: once per (strategy, eligible expiration) pair for
`CASH_SECURED_PUT`/`COVERED_CALL`, and up to twice per (strategy,
eligible expiration) pair for `PUT_CREDIT_SPREAD` (short leg, then --
only if the short leg was found and liquid -- the long leg and the
credit sign). This is a finer granularity than `candidates_generated`
(the final, single best-scoring `TradeProposal` per strategy per ticker,
if any) -- both concepts are real and distinct in the actual
architecture, so both are reported, never conflated.

**Cardinality safety.** Nothing here stores one row per option contract.
`FunnelDiagnostics` accumulates small, bounded per-ticker counters and a
list of (strategy, event, reason) tuples bounded by the number of
(strategy x eligible-expiration) combinations actually attempted --
for this platform's 2-ticker universe and <=45-day DTE window, at most a
few dozen entries per cycle, never per-contract. `CandidateFunnel`'s own
`rejection_reasons`/`top_bottlenecks`/`by_symbol`/`by_strategy` fields
are each explicitly bounded (see the `_MAX_*` constants below) before
being persisted.

**No secrets.** Every value here is derived from ticker symbols,
strategy names, contract counts, and this codebase's own existing
`ReasonCode`/rejection-reason vocabulary -- never a token, header, or raw
provider payload.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from src.portfolio.opportunity_scan import OpportunityScanResult
from src.risk.reason_codes import RiskDecision
from src.workflows.funnel_diagnostics import FunnelDiagnostics

_MAX_REJECTION_REASONS = 25
_MAX_TOP_BOTTLENECKS = 5
_MAX_SYMBOL_SUMMARIES = 50
_MAX_STRATEGY_SUMMARIES = 20

_ACCEPTABLE_RISK_DECISIONS = (RiskDecision.APPROVE, RiskDecision.RESIZE)


class RejectionReasonCount(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: str
    reason: str
    count: int


class SymbolFunnelSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    market_data_successful: bool
    chain_usable: bool
    contracts_seen: int
    expirations_seen: int
    expirations_eligible: int
    strategy_attempts: int
    construction_successes: int


class StrategyFunnelSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy: str
    attempts: int
    ineligible: int
    construction_successes: int
    construction_rejections: int
    # PAPER_TRADING_V1.5.7: a liquid, in-range contract was found, but
    # the final TradeProposal construction step itself raised (e.g. a
    # data_timestamp > timestamp integrity failure) -- distinct from
    # both construction_rejections (a legitimate policy rejection, e.g.
    # liquidity) and construction_successes (a real, returned Candidate).
    # Never counted toward quant_evaluations -- a candidate that failed
    # here never reached Quant at all.
    generation_exceptions: int
    quant_passed: int
    quant_rejected: int
    risk_passed: int
    risk_rejected: int


class CandidateFunnel(BaseModel):
    """One cycle's full candidate-generation funnel -- an optional,
    additive field on `src.portfolio.cycle_record.ControlCycleRecord`
    (see that model's own field comment). A historical record from
    before this step (or any cycle run with diagnostics collection
    disabled) simply has `candidate_funnel=None`; nothing here is ever
    backfilled or reconstructed retroactively."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cycle_id: str
    generated_at: datetime

    symbols_requested: int
    symbols_market_data_successful: int
    symbols_market_data_failed: int

    option_chains_received: int
    option_chains_quality_passed: int
    option_chains_quality_failed: int

    expirations_seen: int
    expirations_eligible: int
    expirations_rejected: int

    contracts_seen: int

    strategy_attempts: int
    strategy_ineligible: int

    construction_attempts: int
    construction_successes: int
    construction_rejections: int
    # PAPER_TRADING_V1.5.7: see StrategyFunnelSummary.generation_exceptions
    # -- the total across every ticker/strategy this cycle. A positive
    # count here, with quant_evaluations unaffected, means a candidate
    # was found and then lost to a proposal-construction exception
    # BEFORE Quant ever ran on it -- never silently absent from this
    # funnel the way it was before this field existed.
    generation_exceptions: int

    quant_evaluations: int
    quant_passed: int
    quant_rejected: int

    risk_evaluations: int
    risk_passed: int
    risk_rejected: int

    candidates_generated: int
    candidates_ranked: int
    candidates_selected: int
    candidates_persisted_for_review: int

    rejection_reasons: tuple[RejectionReasonCount, ...] = ()
    by_symbol: tuple[SymbolFunnelSummary, ...] = ()
    by_strategy: tuple[StrategyFunnelSummary, ...] = ()
    top_bottlenecks: tuple[str, ...] = ()
    zero_candidate_summary: str | None = None


def build_candidate_funnel(
    *,
    cycle_id: str,
    generated_at: datetime,
    universe_tickers: tuple[str, ...],
    chain_received_tickers: frozenset[str],
    diagnostics_by_ticker: dict[str, FunnelDiagnostics],
    scan_result: OpportunityScanResult,
    candidates_persisted: int,
) -> CandidateFunnel:
    """Pure aggregation: reads the already-finished `scan_result` and the
    `FunnelDiagnostics` collected alongside it, and produces one
    deterministic `CandidateFunnel`. Never re-runs, re-derives, or
    second-guesses any decision -- every count here is read directly off
    data the real scan already produced."""
    symbols_requested = len(universe_tickers)
    symbols_ok = len(chain_received_tickers)
    symbols_failed = symbols_requested - symbols_ok

    quality_passed = sum(
        1 for t in universe_tickers
        if t in chain_received_tickers and t in diagnostics_by_ticker and not diagnostics_by_ticker[t].chain_stale
    )
    quality_failed = sum(
        1 for t in universe_tickers
        if t in diagnostics_by_ticker and diagnostics_by_ticker[t].chain_stale
    )

    expirations_seen_total = sum(d.expirations_seen for d in diagnostics_by_ticker.values())
    expirations_eligible_total = sum(d.expirations_eligible for d in diagnostics_by_ticker.values())
    contracts_seen_total = sum(d.chain_contracts_seen for d in diagnostics_by_ticker.values())

    strategy_attempts = 0
    strategy_ineligible = 0
    construction_attempts = 0
    construction_successes = 0
    construction_rejections = 0
    generation_exceptions = 0
    reason_counter: Counter[tuple[str, str]] = Counter()
    per_strategy: dict[str, dict[str, int]] = {}

    def _bucket(name: str) -> dict[str, int]:
        return per_strategy.setdefault(
            name,
            dict(
                attempts=0, ineligible=0, construction_successes=0, construction_rejections=0,
                generation_exceptions=0, quant_passed=0, quant_rejected=0, risk_passed=0, risk_rejected=0,
            ),
        )

    by_symbol: list[SymbolFunnelSummary] = []
    for ticker in universe_tickers:
        diag = diagnostics_by_ticker.get(ticker)
        if diag is None:
            by_symbol.append(
                SymbolFunnelSummary(
                    ticker=ticker, market_data_successful=ticker in chain_received_tickers,
                    chain_usable=False, contracts_seen=0, expirations_seen=0, expirations_eligible=0,
                    strategy_attempts=0, construction_successes=0,
                )
            )
            continue

        # PAPER_TRADING_V1.5.6: a V1.5.5 observability gap -- expiration
        # eligibility was counted (`expirations_seen`/`expirations_eligible`)
        # but never turned into a rejection_reasons/top_bottlenecks entry,
        # so a cycle where DTE eligibility eliminated every expiration
        # (zero `strategy_events` of any kind ever recorded, since no
        # expiration survived to attempt a strategy against) showed NO
        # bottleneck at all for the actual cause, only whatever incidental
        # strategy-prerequisite reason happened to exist. Read directly
        # from already-computed counts -- never re-derives or second-
        # guesses the real DTE decision candidate_generation.py already
        # made.
        rejected_expirations = diag.expirations_seen - diag.expirations_eligible
        if rejected_expirations > 0:
            reason_counter[("expiration", "EXPIRATION_DTE_OUT_OF_RANGE")] += rejected_expirations

        sym_attempts = 0
        sym_successes = 0
        for strategy, event_type, reason in diag.strategy_events:
            bucket = _bucket(strategy)
            if event_type == "attempt":
                strategy_attempts += 1
                bucket["attempts"] += 1
                sym_attempts += 1
            elif event_type == "ineligible":
                strategy_ineligible += 1
                bucket["ineligible"] += 1
                if reason:
                    reason_counter[("strategy_prerequisite", reason)] += 1
            elif event_type == "construction_success":
                construction_attempts += 1
                construction_successes += 1
                bucket["construction_successes"] += 1
                sym_successes += 1
            elif event_type == "construction_rejected":
                construction_attempts += 1
                construction_rejections += 1
                bucket["construction_rejections"] += 1
                if reason:
                    reason_counter[("construction", reason)] += 1
            elif event_type == "generation_exception":
                # PAPER_TRADING_V1.5.7: deliberately NOT counted toward
                # construction_attempts/construction_successes/
                # construction_rejections -- a generation_exception means
                # a liquid, in-range contract WAS found (the construction
                # step itself never ran to a pass/fail verdict), but the
                # downstream TradeProposal build raised before a
                # Candidate could ever be returned. Also never counted
                # toward quant_evaluations below, which is derived solely
                # from `scan_result.scanned` -- a candidate that failed
                # here never reached that list at all.
                generation_exceptions += 1
                bucket["generation_exceptions"] += 1
                if reason:
                    reason_counter[("generation_exception", reason)] += 1

        by_symbol.append(
            SymbolFunnelSummary(
                ticker=ticker, market_data_successful=ticker in chain_received_tickers,
                chain_usable=not diag.chain_stale, contracts_seen=diag.chain_contracts_seen,
                expirations_seen=diag.expirations_seen, expirations_eligible=diag.expirations_eligible,
                strategy_attempts=sym_attempts, construction_successes=sym_successes,
            )
        )

    quant_evaluations = 0
    quant_passed = 0
    quant_rejected = 0
    risk_evaluations = 0
    risk_passed = 0
    risk_rejected = 0

    for scanned in scan_result.scanned:
        # `.name` (e.g. "CASH_SECURED_PUT"), never `.value` (the enum's
        # lowercase wire value, e.g. "cash_secured_put") -- must match
        # the strategy_tag strings `src.workflows.candidate_generation`
        # passes to `FunnelDiagnostics.record_strategy_attempt` above, or
        # this loop's quant/risk counts land in a second, spurious
        # per-strategy bucket instead of merging into the real one.
        strategy_name = scanned.candidate.proposal.strategy.name
        bucket = _bucket(strategy_name)
        if scanned.quantitative_analysis is None:
            # scan_and_rank_opportunities only reaches this branch when
            # default_quant_stage/evaluate_trade_proposal itself raised --
            # isolated per-candidate, never a chain of evaluation.
            quant_evaluations += 1
            quant_rejected += 1
            bucket["quant_rejected"] += 1
            reason_counter[("quant_or_risk", "EVALUATION_RAISED")] += 1
            continue

        quant_evaluations += 1
        quant_passed += 1
        bucket["quant_passed"] += 1

        risk_evaluations += 1
        if scanned.risk_decision in _ACCEPTABLE_RISK_DECISIONS:
            risk_passed += 1
            bucket["risk_passed"] += 1
        else:
            risk_rejected += 1
            bucket["risk_rejected"] += 1
            codes = scanned.risk_reason_codes or ("RISK_REJECTED_UNSPECIFIED",)
            for code in codes:
                reason_counter[("risk", code)] += 1

    candidates_generated = scan_result.candidates_generated
    candidates_ranked = sum(
        1 for s in scan_result.scanned
        if s.risk_decision in _ACCEPTABLE_RISK_DECISIONS and s.risk_adjusted_return is not None
    )
    candidates_selected = 1 if scan_result.best is not None else 0

    rejection_reasons = tuple(
        RejectionReasonCount(stage=stage, reason=reason, count=count)
        for (stage, reason), count in reason_counter.most_common(_MAX_REJECTION_REASONS)
    )
    top_bottlenecks = tuple(
        f"{reason} ({stage}) -- {count}"
        for (stage, reason), count in reason_counter.most_common(_MAX_TOP_BOTTLENECKS)
    )
    by_strategy = tuple(
        StrategyFunnelSummary(strategy=name, **counts)
        for name, counts in sorted(per_strategy.items())
    )[:_MAX_STRATEGY_SUMMARIES]

    zero_candidate_summary = None
    if candidates_persisted == 0:
        zero_candidate_summary = (
            f"symbols_scanned={symbols_requested}, chains_usable={quality_passed}, "
            f"contracts_examined={contracts_seen_total}, strategy_attempts={strategy_attempts}, "
            f"construction_successes={construction_successes}, generation_exceptions={generation_exceptions}, "
            f"quant_passed={quant_passed}, risk_passed={risk_passed}, "
            f"dominant_rejections={list(top_bottlenecks[:3])}"
        )

    return CandidateFunnel(
        cycle_id=cycle_id, generated_at=generated_at,
        symbols_requested=symbols_requested,
        symbols_market_data_successful=symbols_ok,
        symbols_market_data_failed=symbols_failed,
        option_chains_received=symbols_ok,
        option_chains_quality_passed=quality_passed,
        option_chains_quality_failed=quality_failed,
        expirations_seen=expirations_seen_total,
        expirations_eligible=expirations_eligible_total,
        expirations_rejected=expirations_seen_total - expirations_eligible_total,
        contracts_seen=contracts_seen_total,
        strategy_attempts=strategy_attempts,
        strategy_ineligible=strategy_ineligible,
        construction_attempts=construction_attempts,
        construction_successes=construction_successes,
        construction_rejections=construction_rejections,
        generation_exceptions=generation_exceptions,
        quant_evaluations=quant_evaluations, quant_passed=quant_passed, quant_rejected=quant_rejected,
        risk_evaluations=risk_evaluations, risk_passed=risk_passed, risk_rejected=risk_rejected,
        candidates_generated=candidates_generated, candidates_ranked=candidates_ranked,
        candidates_selected=candidates_selected, candidates_persisted_for_review=candidates_persisted,
        rejection_reasons=rejection_reasons,
        by_symbol=tuple(by_symbol[:_MAX_SYMBOL_SUMMARIES]),
        by_strategy=by_strategy,
        top_bottlenecks=top_bottlenecks,
        zero_candidate_summary=zero_candidate_summary,
    )
