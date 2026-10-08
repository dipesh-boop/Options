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
already-resolved `price_history` mapping, an already-observed
`RateLimitState | None`) -- the async Tradier/portfolio I/O, and the
BATCHING/stop-before-exhaustion decisions that read it, live exclusively
in `scripts/run_validation_cycle.py::run_universe_feasibility_study`,
never here. This split is what makes every function in this module
directly unit-testable with plain fakes, with no event loop, no sqlite
file, and no network dependency at all.

**V1.5.14 acceptance correction (pre-acceptance; the operator never
installed the original 009c752 freeze).** Two issues an audit of that
freeze found are corrected here, in place, before acceptance:

1. `expected_tradier_request_count`'s formula undercounted the real
   worst case by exactly `num_symbols * max_expirations` requests,
   because it assumed `TradierMarketDataProvider
   .get_option_chain_for_expiration` costs 1 request per selected
   expiration. It actually costs 2 -- that method re-fetches the
   underlying quote internally before fetching the chain itself (see
   its own body in `src.data.tradier_provider`). The corrected formula,
   and the new phase-split helpers below, mirror that exactly. These
   are LOGICAL-request counts, before provider-level retries
   (`TradierMarketDataProvider._config.max_retries`, up to 3 real
   outbound HTTP attempts per logical request on a transient
   failure/429/5xx) -- actual outbound attempts can exceed them; this
   module never claims otherwise.
2. `classify_ticker_suitability` (renamed `classify_structural_suitability`
   below) conflated "this ticker structurally offers usable option
   chains" with "today's specific candidate happened to clear Quant and
   Risk" -- STRONG required `ranked_candidates > 0`, a today's-market
   fact, not a structural one. This is now two independent,
   observational fields: `classify_structural_suitability` (chain/
   construction facts ONLY, never Quant/Risk/ranking/the no-trade
   hurdle) and `classify_opportunity_today` (how far today's best
   candidate progressed -- purely observational, never read back into
   structural classification or any other decision).
"""
from __future__ import annotations

import os
from collections import Counter
from enum import Enum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from src.data.rate_limiter import RateLimitState
from src.portfolio.opportunity_scan import OpportunityScanResult, ScannedCandidate
from src.risk.reason_codes import RiskDecision
from src.workflows.funnel_diagnostics import FunnelDiagnostics

_ACCEPTABLE_RISK_DECISIONS = (RiskDecision.APPROVE, RiskDecision.RESIZE)


class RateLimitSafetyConfigError(RuntimeError):
    """Raised when `config/universe_feasibility.yaml`'s `rate_limit_
    safety` section is missing or malformed. Fails closed -- never
    silently substitutes an in-code default, matching
    `src.risk.limits.RiskLimitsConfigError`'s own discipline."""


class RateLimitSafetyConfig(BaseModel):
    """The batching/headroom policy `run_universe_feasibility_study`
    reads before starting each opportunity-scan batch and before
    starting the correlation phase. Every field is a POLICY choice
    (same YAML-plus-per-value-env-override pattern every other config
    file in this repository already establishes) -- never a Tradier
    plan assumption; see `config/universe_feasibility.yaml`'s own
    `rate_limit_safety` section comments."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_size: int = Field(gt=0)
    reserved_headroom_pct: float = Field(ge=0.0, lt=1.0)
    correlation_reserved_headroom_pct: float = Field(ge=0.0, lt=1.0)


def _resolved(section: dict, key: str):
    env_key = section.get(f"{key}_env")
    if env_key:
        override = os.environ.get(env_key)
        if override:  # blank-but-present means UNSET -- matches src.risk.limits._resolved
            return override
    if key not in section:
        raise RateLimitSafetyConfigError(f"missing required key {key!r} in 'rate_limit_safety' section")
    return section[key]


def load_rate_limit_safety_config(config_path: Path | str) -> RateLimitSafetyConfig:
    """Reads the `rate_limit_safety` section of the SAME universe-
    feasibility YAML file `load_universe`/`load_universe_strategies`
    already read (`config_path`, passed explicitly by the caller --
    this function has no default path of its own, since only the
    caller, `scripts/run_validation_cycle.py`, knows which file that
    is). Fails closed on a missing file or section, never silently
    substituting an in-code default."""
    path = Path(config_path)
    if not path.is_file():
        raise RateLimitSafetyConfigError(f"universe feasibility config not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    section = data.get("rate_limit_safety")
    if not section:
        raise RateLimitSafetyConfigError(f"{path} is missing required section 'rate_limit_safety'")
    return RateLimitSafetyConfig(
        batch_size=int(_resolved(section, "batch_size")),
        reserved_headroom_pct=float(_resolved(section, "reserved_headroom_pct")),
        correlation_reserved_headroom_pct=float(_resolved(section, "correlation_reserved_headroom_pct")),
    )

# ----------------------------------------------------------------------
# Request-budget estimation -- LOGICAL requests before retries.
# ----------------------------------------------------------------------
#
# Per symbol, the opportunity-scan phase
# (`TradierMarketDataProvider.get_option_chain_for_dte_window`) makes:
#   1 opening `get_underlying_quote`
#   1 `get_expirations`
#   + for each of up to `max_expirations` selected expirations:
#       1 `get_underlying_quote`  (get_option_chain_for_expiration's own
#                                  internal re-fetch -- see that
#                                  method's body; this is NOT optimized
#                                  away here, since doing so would
#                                  change shared, frozen
#                                  TradierMarketDataProvider behavior
#                                  used by the official cycle too, and
#                                  that requires its own, separate
#                                  review)
#       1 `/markets/options/chains` fetch
# i.e. 2 requests per selected expiration, not 1.
_OPPORTUNITY_SCAN_FIXED_PER_SYMBOL = 2  # opening quote + expirations list
_OPPORTUNITY_SCAN_PER_SELECTED_EXPIRATION = 2  # redundant quote re-fetch + chain fetch

# Correlation phase: exactly 1 request per symbol
# (`TradierMarketDataProvider.get_bars`), regardless of
# `max_expirations` -- see `src.portfolio.risk_data
# .resolve_price_history_for_correlation`.
_CORRELATION_REQUESTS_PER_SYMBOL = 1


def expected_opportunity_scan_request_count(*, num_symbols: int, max_expirations: int) -> int:
    """LOGICAL-request upper bound, BEFORE retries, for the option-chain
    opportunity-scan phase alone. A real run typically uses fewer (a
    symbol can have fewer than `max_expirations` eligible expirations,
    or an earlier failure can skip the rest of that symbol's calls) --
    this is the worst case, which is what batch-sizing/headroom
    decisions must size against."""
    if num_symbols < 0:
        raise ValueError("num_symbols must be non-negative")
    if max_expirations < 0:
        raise ValueError("max_expirations must be non-negative")
    per_symbol = _OPPORTUNITY_SCAN_FIXED_PER_SYMBOL + _OPPORTUNITY_SCAN_PER_SELECTED_EXPIRATION * max_expirations
    return num_symbols * per_symbol


def expected_correlation_request_count(*, num_symbols: int) -> int:
    """LOGICAL-request upper bound, BEFORE retries, for the correlation
    phase alone (`TradierMarketDataProvider.get_bars`, 1 per symbol)."""
    if num_symbols < 0:
        raise ValueError("num_symbols must be non-negative")
    return num_symbols * _CORRELATION_REQUESTS_PER_SYMBOL


def expected_tradier_request_count(*, num_symbols: int, max_expirations: int) -> int:
    """The complete LOGICAL-request worst case, BEFORE retries, across
    BOTH phases -- the sum of `expected_opportunity_scan_request_count`
    and `expected_correlation_request_count`. This is an upper bound on
    logical call sites in this codebase's own request pattern, never a
    guaranteed ceiling on actual outbound HTTP attempts: provider-level
    retries (`TradierMarketDataProvider._config.max_retries`, up to 3
    real attempts per logical request on a transient failure/429/5xx)
    are not counted here and can push real network traffic higher."""
    return expected_opportunity_scan_request_count(
        num_symbols=num_symbols, max_expirations=max_expirations,
    ) + expected_correlation_request_count(num_symbols=num_symbols)


def usable_request_headroom(rate_limit_state: RateLimitState | None, *, reserved_headroom_pct: float) -> int | None:
    """How many more requests the provider's own last-observed state
    indicates can still safely be made, after reserving
    `reserved_headroom_pct` of the TOTAL ALLOWANCE (`allowed`, not
    `available`) as a safety margin for other concurrent consumers of
    the same real, Tradier-account-wide budget (the official cycle, a
    dashboard-triggered run, a second `--diagnostic-scan`) that this
    process cannot see or coordinate with --
    `TradierMarketDataProvider._rate_limit_state` is a plain,
    per-instance attribute with zero cross-process sharing (see
    `get_configured_market_data_provider`, which constructs a fresh
    provider, and therefore a fresh `None` state, on every call).

    Returns `None` -- never a number -- when `rate_limit_state` is
    `None` (no response observed yet this run): there is nothing to
    measure against, and the caller should proceed as if unconstrained,
    exactly like `has_sufficient_observed_headroom`'s own bootstrap
    case. Never negative otherwise (floors at 0). Reads ONLY
    `rate_limit_state.allowed`/`.available`, both sourced exclusively
    from the provider's own most recently observed response headers --
    never a hardcoded number like "120/minute.\""""
    if rate_limit_state is None:
        return None
    if not 0.0 <= reserved_headroom_pct < 1.0:
        raise ValueError("reserved_headroom_pct must be in [0.0, 1.0)")
    reserved = rate_limit_state.allowed * reserved_headroom_pct
    usable = rate_limit_state.available - reserved
    return max(0, int(usable))


def has_sufficient_observed_headroom(
    rate_limit_state: RateLimitState | None, *, projected_requests: int, reserved_headroom_pct: float,
) -> bool:
    """The decision of whether the feasibility study's orchestration
    layer may safely begin its NEXT batch/phase -- never a decision
    about whether any SINGLE request may proceed (that remains
    `src.data.rate_limiter.may_proceed`'s job at the provider's own
    `_request` choke point, completely unmodified). Built on
    `usable_request_headroom` -- `rate_limit_state=None` always returns
    `True` (nothing observed yet to measure against; see that
    function's own docstring for the full bootstrap reasoning)."""
    if projected_requests < 0:
        raise ValueError("projected_requests must be non-negative")
    headroom = usable_request_headroom(rate_limit_state, reserved_headroom_pct=reserved_headroom_pct)
    if headroom is None:
        return True
    return headroom >= projected_requests


# ----------------------------------------------------------------------
# Skip / correlation-phase status vocabulary
# ----------------------------------------------------------------------


class SkipReason(str, Enum):
    """Why a ticker this run was ENTITLED to scan was never attempted at
    all. Currently only one cause exists -- observed rate-limit
    headroom was insufficient to safely start that ticket's batch --
    kept as an enum (not a bare bool) so a future second cause never
    requires a breaking field-shape change."""

    RATE_LIMIT_HEADROOM = "RATE_LIMIT_HEADROOM"


class CorrelationSymbolStatus(str, Enum):
    """Item E's required 3-way distinction for why a symbol does or
    does not appear in the correlation summary's sufficient-history set.
    `PROVIDER_FAILURE` is deliberately NOT a fourth, separately
    reported value here: distinguishing it from `INSUFFICIENT_HISTORY`
    would require changing `src.portfolio.risk_data
    .resolve_price_history_for_correlation`'s own internal per-ticker
    `except Exception: continue` (which already collapses "too few
    observations" and "the fetch itself failed" into the same "omitted"
    outcome, by that function's own documented contract) -- a shared,
    frozen function used by other callers too, out of scope for this
    correction (see module docstring, point 1, and the requesting
    audit's explicit "do not change shared Tradier provider behavior"
    constraint). `INSUFFICIENT_HISTORY` here therefore means exactly
    what that function's own docstring already promises: too few
    aligned observations, OR the fetch failed for a reason that was
    NOT this study's own budget decision."""

    SUFFICIENT_HISTORY = "SUFFICIENT_HISTORY"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    SKIPPED_BUDGET = "SKIPPED_BUDGET"


# ----------------------------------------------------------------------
# Structural suitability vs. today's opportunity -- two independent,
# observational fields. Neither is ever read by the other.
# ----------------------------------------------------------------------


class StructuralSuitability(str, Enum):
    """Whether a ticker STRUCTURALLY offers usable option chains for the
    active strategies -- chain retrieval, DTE-window eligibility, and
    construction feasibility ONLY. Never depends on Quant pass/fail,
    Risk pass/fail, ranking score, or the no-trade hurdle -- see
    `classify_structural_suitability`'s own docstring for the exact,
    deterministic rules. `NOT_EVALUATED` is never returned by
    `classify_structural_suitability` itself -- it is assigned directly
    by the orchestration layer for a ticker this run skipped outright
    (observed rate-limit headroom was insufficient to even attempt it),
    since there is no `SymbolFeasibilitySummary` to classify for a
    ticker whose chain was never fetched. A skipped ticker must never
    be reported as `UNSUITABLE` -- that would misrepresent a budget
    decision as a data-quality finding about the ticker itself."""

    STRONG = "STRONG"
    ACCEPTABLE = "ACCEPTABLE"
    WEAK = "WEAK"
    UNSUITABLE = "UNSUITABLE"
    NOT_EVALUATED = "NOT_EVALUATED"


class OpportunityToday(str, Enum):
    """How far TODAY's best candidate for this ticker progressed through
    construction -> Quant -> Risk -> ranking -> the no-trade hurdle.
    Purely observational -- see `classify_opportunity_today`'s own
    docstring. This field NEVER affects `StructuralSuitability`; a
    ticker can be structurally STRONG while reaching only `NONE` today
    (today's specific economics/IV simply did not produce a candidate),
    and a structurally WEAK ticker could in principle still reach
    `CLEARED_HURDLE` on an unusually favorable day -- both are
    legitimate, expected outcomes under this field's own definition,
    not a contradiction to reconcile."""

    NONE = "NONE"
    CONSTRUCTED = "CONSTRUCTED"
    QUANT_PASS = "QUANT_PASS"
    RISK_PASS = "RISK_PASS"
    RANKED = "RANKED"
    CLEARED_HURDLE = "CLEARED_HURDLE"


class SymbolFeasibilitySummary(BaseModel):
    """Item F's required per-symbol fields -- one row per ATTEMPTED
    research ticker (a ticker this run's orchestration layer actually
    called the provider for, whether that call succeeded or failed; a
    ticker SKIPPED for rate-limit headroom never gets one of these --
    see `SkipReason`), computed directly from that ticker's own
    `FunnelDiagnostics` (construction/expiration counts) and its slice
    of `scan_result.scanned` (Quant/Risk/ranking counts), never
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
    ranked_candidates_clearing_hurdle: int
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
    """Item G's aggregate fields, computed once across every attempted
    symbol's summary and the whole `scan_result` -- never a second pass
    over raw diagnostics. Item I's evaluated-vs-skipped and structural-
    suitability/opportunity-today breakdowns are additive fields here,
    never a second report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbols_requested: int
    symbols_evaluated: int
    symbols_skipped: int
    skipped_tickers: tuple[str, ...]
    skip_reason: SkipReason | None
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
    structural_suitability_counts: dict[str, int]
    opportunity_today_counts: dict[str, int]


class CorrelationFeasibilitySummary(BaseModel):
    """Item H's required fields, computed from an already-resolved
    `price_history` mapping (`src.portfolio.risk_data
    .resolve_price_history_for_correlation`'s own return value) --
    never a second correlation-data fetch mechanism. `symbols_skipped_
    budget` is populated entirely by the orchestration layer's own
    pre-flight decision about which tickers to even include in the
    `tickers` argument of that call -- never inferred after the fact
    from an absence in `price_history` (which, per that function's own
    contract, cannot be distinguished from genuine insufficient
    history -- see `CorrelationSymbolStatus`'s own docstring)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbols_with_sufficient_history: tuple[str, ...]
    symbols_with_insufficient_history: tuple[str, ...]
    symbols_skipped_budget: tuple[str, ...]
    high_correlation_pairs: tuple[tuple[str, str, float], ...]


def build_correlation_feasibility_summary(
    *,
    correlation_attempted_tickers: tuple[str, ...],
    correlation_skipped_tickers: tuple[str, ...],
    price_history: dict[str, list[float]],
    high_correlation_pairs: tuple[tuple[str, str, float], ...] = (),
) -> CorrelationFeasibilitySummary:
    """`correlation_attempted_tickers` is exactly the ticker set the
    orchestration layer actually passed into
    `resolve_price_history_for_correlation` -- any of those absent from
    `price_history` is `INSUFFICIENT_HISTORY` (that function's own
    documented contract). `correlation_skipped_tickers` is decided
    ENTIRELY by the caller's own pre-flight budget check, before that
    call ever happens -- never inferred here."""
    sufficient = tuple(sorted(t for t in correlation_attempted_tickers if t in price_history))
    insufficient = tuple(sorted(t for t in correlation_attempted_tickers if t not in price_history))
    skipped = tuple(sorted(correlation_skipped_tickers))
    return CorrelationFeasibilitySummary(
        symbols_with_sufficient_history=sufficient,
        symbols_with_insufficient_history=insufficient,
        symbols_skipped_budget=skipped,
        high_correlation_pairs=high_correlation_pairs,
    )


def _expirations_rejected(diag: FunnelDiagnostics) -> int:
    return diag.expirations_seen - diag.expirations_eligible


def build_symbol_feasibility_summaries(
    *,
    evaluated_tickers: tuple[str, ...],
    chain_received_tickers: frozenset[str],
    diagnostics_by_ticker: dict[str, FunnelDiagnostics],
    scan_result: OpportunityScanResult,
    no_trade_hurdle: float = 0.0,
) -> tuple[SymbolFeasibilitySummary, ...]:
    """One `SymbolFeasibilitySummary` per ticker in `evaluated_tickers`
    (ATTEMPTED this run -- never a ticker skipped for rate-limit
    headroom; see `SkipReason`), in that order. Construction/expiration
    counts come from `diagnostics_by_ticker[ticker].strategy_events`
    (the exact same event stream
    `src.workflows.candidate_funnel.build_candidate_funnel` reads for
    the OFFICIAL funnel -- same semantics, just grouped and reported
    per-symbol instead of aggregated across the whole universe);
    Quant/Risk/ranking counts come from filtering `scan_result.scanned`
    by `candidate.proposal.ticker`, since the shared, frozen
    `SymbolFunnelSummary` (candidate_funnel.py) does not carry
    per-symbol Quant/Risk breakdowns at all -- this module adds that
    granularity without touching the official, already-frozen
    aggregator."""
    summaries: list[SymbolFeasibilitySummary] = []
    for ticker in evaluated_tickers:
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
        ranked_clearing_hurdle = [s for s in ranked if s.risk_adjusted_return > no_trade_hurdle]  # type: ignore[operator]
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
                ranked_candidates_clearing_hurdle=len(ranked_clearing_hurdle),
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
    symbols_requested: int,
    symbol_summaries: tuple[SymbolFeasibilitySummary, ...],
    chain_received_tickers: frozenset[str],
    scan_result: OpportunityScanResult,
    skipped_tickers: tuple[str, ...] = (),
    skip_reason: SkipReason | None = None,
    no_trade_hurdle: float = 0.0,
) -> AggregateFeasibilityReport:
    """A single pass over `symbol_summaries` (themselves already a pure
    function of `diagnostics_by_ticker`/`scan_result`, one per ATTEMPTED
    ticker only) plus one pass over `scan_result.scanned` for the
    by-strategy/rejection-reason breakdowns -- reconciles exactly with
    the per-symbol output by construction, since both read the same
    underlying data. `symbols_requested` is the full universe size
    (attempted + skipped); `skipped_tickers`/`skip_reason` describe
    exactly what this run chose not to attempt and why -- never folded
    into `failed_chains` (a real market-data failure for an ATTEMPTED
    ticker), which would misrepresent a budget decision as a data-
    quality finding."""
    construction_reason_counter: Counter[str] = Counter()
    quant_reason_counter: Counter[str] = Counter()
    risk_reason_counter: Counter[str] = Counter()
    by_strategy: Counter[str] = Counter()
    by_ticker_ranked: Counter[str] = Counter()
    structural_counts: Counter[str] = Counter()
    opportunity_counts: Counter[str] = Counter()

    for s in symbol_summaries:
        for reason in s.dominant_rejection_reasons:
            construction_reason_counter[reason] += 1
        structural_counts[classify_structural_suitability(s).value] += 1
        opportunity_counts[classify_opportunity_today(s).value] += 1
    structural_counts[StructuralSuitability.NOT_EVALUATED.value] += len(skipped_tickers)

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
        symbols_requested=symbols_requested,
        symbols_evaluated=len(symbol_summaries),
        symbols_skipped=len(skipped_tickers),
        skipped_tickers=tuple(skipped_tickers),
        skip_reason=skip_reason,
        successful_chains=len(chain_received_tickers),
        failed_chains=len(symbol_summaries) - len(chain_received_tickers),
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
        structural_suitability_counts=dict(structural_counts),
        opportunity_today_counts=dict(opportunity_counts),
    )


# ---------------------------------------------------- structural suitability
#
# Deterministic, transparent rules -- never based on today's market
# direction/P&L, and never based on Quant pass/fail, Risk pass/fail,
# ranking score, or the no-trade hurdle (see `OpportunityToday` for
# that). Each rule reads only already-computed SymbolFeasibilitySummary
# fields describing chain retrieval, DTE-window eligibility, and
# construction feasibility.
#
# STRONG:      chain usable, >=1 eligible 20-45 DTE expiration, every
#              STRATEGY actually attempted for this ticker (strategy_
#              attempts, at most 3 -- one per configured strategy) found
#              a constructible candidate, with zero generation
#              exceptions. Deliberately uses strategy_attempts, NEVER
#              construction_attempts, as the denominator -- see the
#              long comment on classify_structural_suitability's own
#              body for why construction_attempts is the wrong
#              denominator (it scales with how many ELIGIBLE
#              EXPIRATIONS were internally searched while looking for
#              the single best candidate per strategy, not with how
#              many independent strategy ideas were tried).
# ACCEPTABLE:  chain usable, >=1 eligible expiration, AND >=1
#              construction success, but not every attempted strategy
#              succeeded, or a generation exception occurred --
#              construction/liquidity/DTE infrastructure partially
#              works.
# WEAK:        chain usable and >=1 eligible expiration, but ZERO
#              construction successes (every strategy attempt was
#              rejected before a candidate was even built -- usually a
#              liquidity or delta-range bottleneck).
# UNSUITABLE:  chain not usable, OR zero eligible 20-45 DTE
#              expirations, OR market data failed outright for this
#              symbol.
# NOT_EVALUATED: never returned here -- assigned directly by the
#              orchestration layer for a ticker this run skipped
#              outright (see StructuralSuitability's own docstring).


def classify_structural_suitability(summary: SymbolFeasibilitySummary) -> StructuralSuitability:
    if not summary.market_data_successful or not summary.chain_usable or summary.expirations_eligible == 0:
        return StructuralSuitability.UNSUITABLE
    if summary.construction_successes == 0:
        return StructuralSuitability.WEAK
    # Deliberately `strategy_attempts`, NEVER `construction_attempts`:
    # candidate_generation.py's own best-across-all-eligible-expirations
    # search (e.g. CASH_SECURED_PUT's `for exp in expirations:` loop)
    # calls `_short_put_candidate` once per eligible expiration, and
    # EACH call that fails the delta-range/liquidity screen records its
    # own `construction_rejected` event -- BEFORE the single best
    # candidate across every expiration is chosen and actually built. A
    # liquid, reliable ticker with 6 eligible expirations can therefore
    # accumulate up to 5 `construction_rejected` events and still have
    # found an excellent candidate on the 6th; `construction_attempts`
    # (which counts every one of those internal search steps) would be
    # the WRONG denominator here -- it would incorrectly downgrade a
    # ticker purely because it had many eligible expirations to search,
    # never because its options market was actually unreliable.
    # `strategy_attempts` (at most 3 -- one per configured strategy
    # this ticker was even eligible to attempt) is the stable, correct
    # denominator for "did this ticker reliably offer usable option
    # structures for every strategy we tried."
    if summary.strategy_attempts > 0 and summary.construction_successes == summary.strategy_attempts and summary.generation_exceptions == 0:
        return StructuralSuitability.STRONG
    return StructuralSuitability.ACCEPTABLE


# ---------------------------------------------------- opportunity today
#
# Purely observational -- describes how far TODAY's best candidate for
# this ticker progressed. NEVER read by classify_structural_suitability
# or any other decision. RISK_PASS and RANKED are structurally
# separable in today's implementation (not a manufactured distinction):
# `ScannedCandidate.risk_adjusted_return` is `None` whenever
# `capital_required <= 0` or the economics are otherwise unpriced, even
# for an otherwise Risk-APPROVED/RESIZED candidate -- see
# `src.portfolio.opportunity_scan._risk_adjusted_return`'s own comment.
# In the overwhelming majority of real scans the two counts coincide
# (a Risk-approved candidate with a comparable return almost always has
# a computable risk_adjusted_return too); this field exists to report
# the rare case honestly rather than collapsing it into RANKED.


def classify_opportunity_today(summary: SymbolFeasibilitySummary) -> OpportunityToday:
    if summary.construction_successes == 0:
        return OpportunityToday.NONE
    if summary.quant_passed == 0:
        return OpportunityToday.CONSTRUCTED
    if summary.risk_passed == 0:
        return OpportunityToday.QUANT_PASS
    if summary.ranked_candidates == 0:
        return OpportunityToday.RISK_PASS
    if summary.ranked_candidates_clearing_hurdle == 0:
        return OpportunityToday.RANKED
    return OpportunityToday.CLEARED_HURDLE
