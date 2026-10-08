"""PAPER_TRADING_V1.5.14: pure, read-only aggregation for the universe-
feasibility study (`scripts/run_validation_cycle.py --universe-
feasibility`).

**Decision-neutral, like `src.workflows.candidate_funnel`.** Every
function here reads data an ordinary, unmodified
`scan_and_rank_opportunities` call (and the `FunnelDiagnostics` recorded
alongside it) already produced -- nothing here re-runs, re-derives, or
second-guesses a Quant/Risk/ranking decision, and nothing here is fed
back into one. This module exists purely to explain, across a WIDER
research universe than the official SPY/QQQ cycle, what a candidate scan
would have found -- never to decide, persist, or execute anything.

**No portfolio/database/network access of any kind.** Every function
below is synchronous and takes already-fetched data as plain arguments
(`OpportunityScanResult`, `dict[str, FunnelDiagnostics]`, an
already-resolved `price_history` mapping) -- the async Tradier/portfolio
I/O lives exclusively in `scripts/run_validation_cycle.py
::run_universe_feasibility_study`, never here. This split is what makes
every function in this module directly unit-testable with plain fakes,
with no event loop, no sqlite file, and no network dependency at all.
"""
from __future__ import annotations

from collections import Counter
from enum import Enum

from pydantic import BaseModel, ConfigDict

from src.portfolio.opportunity_scan import OpportunityScanResult, ScannedCandidate
from src.risk.reason_codes import RiskDecision
from src.workflows.funnel_diagnostics import FunnelDiagnostics

_ACCEPTABLE_RISK_DECISIONS = (RiskDecision.APPROVE, RiskDecision.RESIZE)

# Tradier requests per symbol for the opportunity-scan phase of one
# feasibility run: 1 (get_underlying_quote) + 1 (get_expirations) +
# up to `max_expirations` (get_option_chain_for_expiration, one per
# DTE-window-eligible expiration actually selected) -- see
# `TradierMarketDataProvider.get_option_chain_for_dte_window`'s own
# docstring, which this constant mirrors exactly rather than guessing.
_OPPORTUNITY_SCAN_REQUESTS_PER_SYMBOL_FIXED = 2  # quote + expirations list

# Correlation phase: exactly 1 request per symbol
# (`TradierMarketDataProvider.get_bars`), regardless of
# `max_expirations` -- see `src.portfolio.risk_data
# .resolve_price_history_for_correlation`.
_CORRELATION_REQUESTS_PER_SYMBOL = 1


def expected_tradier_request_count(*, num_symbols: int, max_expirations: int) -> int:
    """Deterministic UPPER BOUND on the number of Tradier HTTP requests
    one `--universe-feasibility` run makes, computed from this
    codebase's own documented provider behavior -- never a measured or
    guessed number. Per symbol: 1 quote + 1 expirations-list + up to
    `max_expirations` per-expiration chain fetches (opportunity-scan
    phase) + 1 historical-bars fetch (correlation phase). A real run
    typically uses fewer (a symbol can have fewer than `max_expirations`
    eligible expirations, or its chain fetch can fail and skip the rest
    of that symbol's requests) -- this function reports the worst case,
    which is what rate-limit-budget planning must size against."""
    if num_symbols < 0:
        raise ValueError("num_symbols must be non-negative")
    if max_expirations < 0:
        raise ValueError("max_expirations must be non-negative")
    per_symbol_opportunity_scan = _OPPORTUNITY_SCAN_REQUESTS_PER_SYMBOL_FIXED + max_expirations
    per_symbol_total = per_symbol_opportunity_scan + _CORRELATION_REQUESTS_PER_SYMBOL
    return num_symbols * per_symbol_total


class SuitabilityClassification(str, Enum):
    STRONG = "STRONG"
    ACCEPTABLE = "ACCEPTABLE"
    WEAK = "WEAK"
    UNSUITABLE = "UNSUITABLE"


class SymbolFeasibilitySummary(BaseModel):
    """Item F's required per-symbol fields -- one row per research
    ticker, computed directly from that ticker's own `FunnelDiagnostics`
    (construction/expiration counts) and its slice of
    `scan_result.scanned` (Quant/Risk/ranking counts), never
    re-evaluated."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    market_data_successful: bool
    chain_usable: bool
    contracts_seen: int
    expirations_seen: int
    expirations_eligible: int
    expirations_rejected: int
    strategy_attempts: int
    construction_attempts: int
    construction_successes: int
    construction_rejections: int
    generation_exceptions: int
    quant_evaluations: int
    quant_passed: int
    quant_rejected: int
    risk_evaluations: int
    risk_passed: int
    risk_rejected: int
    ranked_candidates: int
    selected_candidate_proposal_id: str | None
    dominant_rejection_reasons: tuple[str, ...]


class CandidateFeasibilityDetail(BaseModel):
    """Item F's second half -- sanitized economics for every candidate
    that reached ranking (Risk APPROVE/RESIZE with a computable
    risk-adjusted return), observational only. A candidate reported
    here is never persisted and never becomes confirmable -- see
    `run_universe_feasibility_study`'s own docstring."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    strategy: str
    expiration: str
    dte: int
    leg_rights: tuple[str, ...]
    leg_strikes: tuple[float, ...]
    leg_sides: tuple[str, ...]
    target_entry: float
    max_profit: float | None
    max_loss: float | None
    capital_required: float | None
    contracts_requested: int
    risk_decision: str
    risk_adjusted_return: float | None
    no_trade_hurdle: float
    would_have_been_selected: bool


class AggregateFeasibilityReport(BaseModel):
    """Item G's aggregate fields, computed once across every symbol
    summary and the whole `scan_result` -- never a second pass over raw
    diagnostics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbols_requested: int
    successful_chains: int
    failed_chains: int
    total_contracts_examined: int
    total_eligible_expirations: int
    total_strategy_attempts: int
    total_construction_attempts: int
    total_construction_successes: int
    total_quant_passed: int
    total_risk_passed: int
    total_ranked_candidates: int
    total_would_clear_no_trade_hurdle: int
    by_strategy: dict[str, int]
    by_ticker_ranked: dict[str, int]
    top_construction_bottlenecks: tuple[str, ...]
    top_quant_rejection_reasons: tuple[str, ...]
    top_risk_rejection_reasons: tuple[str, ...]


class CorrelationFeasibilitySummary(BaseModel):
    """Item H's required fields, computed from an already-resolved
    `price_history` mapping (`src.portfolio.risk_data
    .resolve_price_history_for_correlation`'s own return value) --
    never a second correlation-data fetch mechanism."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbols_with_sufficient_history: tuple[str, ...]
    symbols_with_insufficient_history: tuple[str, ...]
    high_correlation_pairs: tuple[tuple[str, str, float], ...]


def _expirations_rejected(diag: FunnelDiagnostics) -> int:
    return diag.expirations_seen - diag.expirations_eligible


def build_symbol_feasibility_summaries(
    *,
    universe_tickers: tuple[str, ...],
    chain_received_tickers: frozenset[str],
    diagnostics_by_ticker: dict[str, FunnelDiagnostics],
    scan_result: OpportunityScanResult,
    no_trade_hurdle: float = 0.0,
) -> tuple[SymbolFeasibilitySummary, ...]:
    """One `SymbolFeasibilitySummary` per ticker in `universe_tickers`,
    in that order. Construction/expiration counts come from
    `diagnostics_by_ticker[ticker].strategy_events` (the exact same
    event stream `src.workflows.candidate_funnel.build_candidate_funnel`
    reads for the OFFICIAL funnel -- same semantics, just grouped and
    reported per-symbol instead of aggregated across the whole
    universe); Quant/Risk/ranking counts come from filtering
    `scan_result.scanned` by `candidate.proposal.ticker`, since the
    shared, frozen `SymbolFunnelSummary` (candidate_funnel.py) does not
    carry per-symbol Quant/Risk breakdowns at all -- this module adds
    that granularity without touching the official, already-frozen
    aggregator."""
    summaries: list[SymbolFeasibilitySummary] = []
    for ticker in universe_tickers:
        diag = diagnostics_by_ticker.get(ticker)
        scanned_for_ticker = [s for s in scan_result.scanned if s.candidate.proposal.ticker == ticker]

        reason_counter: Counter[str] = Counter()
        strategy_attempts = construction_attempts = construction_successes = 0
        construction_rejections = generation_exceptions = 0
        if diag is not None:
            for _strategy, event_type, reason in diag.strategy_events:
                if event_type == "attempt":
                    strategy_attempts += 1
                elif event_type == "construction_success":
                    construction_attempts += 1
                    construction_successes += 1
                elif event_type == "construction_rejected":
                    construction_attempts += 1
                    construction_rejections += 1
                    if reason:
                        reason_counter[reason] += 1
                elif event_type == "generation_exception":
                    generation_exceptions += 1
                    if reason:
                        reason_counter[reason] += 1

        quant_evaluations = sum(1 for s in scanned_for_ticker)
        quant_rejected = sum(1 for s in scanned_for_ticker if s.quantitative_analysis is None)
        quant_passed = quant_evaluations - quant_rejected
        risk_evaluations = quant_passed
        risk_passed = sum(
            1 for s in scanned_for_ticker
            if s.quantitative_analysis is not None and s.risk_decision in _ACCEPTABLE_RISK_DECISIONS
        )
        risk_rejected = risk_evaluations - risk_passed
        ranked = [
            s for s in scanned_for_ticker
            if s.risk_decision in _ACCEPTABLE_RISK_DECISIONS and s.risk_adjusted_return is not None
        ]
        for s in scanned_for_ticker:
            if s.risk_decision not in _ACCEPTABLE_RISK_DECISIONS:
                reason_counter[s.risk_reason] += 1

        selected_id = None
        if scan_result.best is not None and scan_result.best.candidate.proposal.ticker == ticker:
            selected_id = scan_result.best.candidate.proposal.proposal_id

        summaries.append(
            SymbolFeasibilitySummary(
                ticker=ticker,
                market_data_successful=ticker in chain_received_tickers,
                chain_usable=diag is not None and not diag.chain_stale,
                contracts_seen=diag.chain_contracts_seen if diag is not None else 0,
                expirations_seen=diag.expirations_seen if diag is not None else 0,
                expirations_eligible=diag.expirations_eligible if diag is not None else 0,
                expirations_rejected=_expirations_rejected(diag) if diag is not None else 0,
                strategy_attempts=strategy_attempts,
                construction_attempts=construction_attempts,
                construction_successes=construction_successes,
                construction_rejections=construction_rejections,
                generation_exceptions=generation_exceptions,
                quant_evaluations=quant_evaluations,
                quant_passed=quant_passed,
                quant_rejected=quant_rejected,
                risk_evaluations=risk_evaluations,
                risk_passed=risk_passed,
                risk_rejected=risk_rejected,
                ranked_candidates=len(ranked),
                selected_candidate_proposal_id=selected_id,
                dominant_rejection_reasons=tuple(r for r, _ in reason_counter.most_common(3)),
            )
        )
    return tuple(summaries)


def build_candidate_feasibility_details(
    scan_result: OpportunityScanResult, *, no_trade_hurdle: float = 0.0,
) -> tuple[CandidateFeasibilityDetail, ...]:
    """One `CandidateFeasibilityDetail` per candidate that reached
    ranking (Risk APPROVE/RESIZE with a computable risk-adjusted
    return) -- `would_have_been_selected` is `True` for exactly the one
    candidate (if any) that `is` `scan_result.best`, never re-derived
    from the ranking score independently (which could disagree with the
    real selection by construction error); this module always asks the
    real result which one it actually picked."""
    details: list[CandidateFeasibilityDetail] = []
    for scanned in scan_result.scanned:
        if scanned.risk_decision not in _ACCEPTABLE_RISK_DECISIONS or scanned.risk_adjusted_return is None:
            continue
        details.append(_candidate_detail(scanned, scan_result, no_trade_hurdle))
    return tuple(details)


def _candidate_detail(
    scanned: ScannedCandidate, scan_result: OpportunityScanResult, no_trade_hurdle: float,
) -> CandidateFeasibilityDetail:
    proposal = scanned.candidate.proposal
    qa = scanned.quantitative_analysis
    return CandidateFeasibilityDetail(
        ticker=proposal.ticker,
        strategy=proposal.strategy.value,
        expiration=proposal.expiration.isoformat(),
        dte=(proposal.expiration - proposal.timestamp.date()).days,
        leg_rights=tuple(leg.right.value for leg in proposal.legs),
        leg_strikes=tuple(leg.strike for leg in proposal.legs),
        leg_sides=tuple(leg.side.value for leg in proposal.legs),
        target_entry=proposal.target_entry,
        max_profit=qa.max_profit if qa is not None else None,
        max_loss=qa.max_loss if qa is not None else None,
        capital_required=qa.capital_required if qa is not None else None,
        contracts_requested=proposal.contracts_requested,
        risk_decision=scanned.risk_decision.value,
        risk_adjusted_return=scanned.risk_adjusted_return,
        no_trade_hurdle=no_trade_hurdle,
        would_have_been_selected=scan_result.best is scanned,
    )


def build_aggregate_feasibility_report(
    *,
    universe_tickers: tuple[str, ...],
    chain_received_tickers: frozenset[str],
    symbol_summaries: tuple[SymbolFeasibilitySummary, ...],
    scan_result: OpportunityScanResult,
    no_trade_hurdle: float = 0.0,
) -> AggregateFeasibilityReport:
    """A single pass over `symbol_summaries` (themselves already a pure
    function of `diagnostics_by_ticker`/`scan_result`) plus one pass
    over `scan_result.scanned` for the by-strategy/rejection-reason
    breakdowns -- reconciles exactly with the per-symbol output by
    construction, since both read the same underlying data."""
    construction_reason_counter: Counter[str] = Counter()
    quant_reason_counter: Counter[str] = Counter()
    risk_reason_counter: Counter[str] = Counter()
    by_strategy: Counter[str] = Counter()
    by_ticker_ranked: Counter[str] = Counter()

    for s in symbol_summaries:
        for reason in s.dominant_rejection_reasons:
            construction_reason_counter[reason] += 1

    total_would_clear = 0
    for scanned in scan_result.scanned:
        strategy_name = scanned.candidate.proposal.strategy.value
        if scanned.quantitative_analysis is None:
            quant_reason_counter["EVALUATION_RAISED"] += 1
            continue
        if scanned.risk_decision not in _ACCEPTABLE_RISK_DECISIONS:
            risk_reason_counter[scanned.risk_reason] += 1
            continue
        by_strategy[strategy_name] += 1
        by_ticker_ranked[scanned.candidate.proposal.ticker] += 1
        if scanned.risk_adjusted_return is not None and scanned.risk_adjusted_return > no_trade_hurdle:
            total_would_clear += 1

    return AggregateFeasibilityReport(
        symbols_requested=len(universe_tickers),
        successful_chains=len(chain_received_tickers),
        failed_chains=len(universe_tickers) - len(chain_received_tickers),
        total_contracts_examined=sum(s.contracts_seen for s in symbol_summaries),
        total_eligible_expirations=sum(s.expirations_eligible for s in symbol_summaries),
        total_strategy_attempts=sum(s.strategy_attempts for s in symbol_summaries),
        total_construction_attempts=sum(s.construction_attempts for s in symbol_summaries),
        total_construction_successes=sum(s.construction_successes for s in symbol_summaries),
        total_quant_passed=sum(s.quant_passed for s in symbol_summaries),
        total_risk_passed=sum(s.risk_passed for s in symbol_summaries),
        total_ranked_candidates=sum(s.ranked_candidates for s in symbol_summaries),
        total_would_clear_no_trade_hurdle=total_would_clear,
        by_strategy=dict(by_strategy),
        by_ticker_ranked=dict(by_ticker_ranked),
        top_construction_bottlenecks=tuple(r for r, _ in construction_reason_counter.most_common(5)),
        top_quant_rejection_reasons=tuple(r for r, _ in quant_reason_counter.most_common(5)),
        top_risk_rejection_reasons=tuple(r for r, _ in risk_reason_counter.most_common(5)),
    )


# ---------------------------------------------------- suitability classification
#
# Deterministic, transparent rules -- never based on today's market
# direction/P&L. Each rule reads only already-computed
# SymbolFeasibilitySummary fields (chain retrieval, DTE-window
# eligibility, contract/liquidity-implied construction success, and
# whether the symbol could reach Quant/Risk with a rankable, defined-
# risk opportunity) -- never a fabricated performance claim.
#
# STRONG:      chain usable, >=1 eligible 20-45 DTE expiration, >=1
#              construction success, AND >=1 candidate reached Risk
#              APPROVE/RESIZE with a computable risk-adjusted return
#              (i.e. this symbol could have produced a rankable
#              opportunity today).
# ACCEPTABLE:  chain usable, >=1 eligible expiration, AND >=1
#              construction success, but no candidate reached Risk
#              APPROVE/RESIZE (construction/liquidity/DTE infrastructure
#              works; today's specific economics/Risk did not clear --
#              a legitimate, expected outcome, not a defect).
# WEAK:        chain usable and >=1 eligible expiration, but ZERO
#              construction successes (every strategy attempt was
#              rejected before a candidate was even built -- usually a
#              liquidity or delta-range bottleneck).
# UNSUITABLE:  chain not usable, OR zero eligible 20-45 DTE
#              expirations, OR market data failed outright for this
#              symbol.


def classify_ticker_suitability(summary: SymbolFeasibilitySummary) -> SuitabilityClassification:
    if not summary.market_data_successful or not summary.chain_usable or summary.expirations_eligible == 0:
        return SuitabilityClassification.UNSUITABLE
    if summary.construction_successes == 0:
        return SuitabilityClassification.WEAK
    if summary.ranked_candidates > 0:
        return SuitabilityClassification.STRONG
    return SuitabilityClassification.ACCEPTABLE
