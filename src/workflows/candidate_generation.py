"""Stages 10-13 of `/morning-scan`: scan the approved universe, apply
liquidity filters, apply quant filters, and generate candidate
`TradeProposal`s for the platform's three approved strategies.

No Strategy Screener has existed anywhere in this codebase before this
module — every prior step's `TradeProposal`s came from a test fixture or
a human. This is a deliberately simple, fully deterministic one (no LLM
call anywhere in this file): pick contracts whose data-reported delta
falls in a configured target range and that clear this platform's own
liquidity thresholds (`src.risk.limits`, reused rather than a second set
of numbers), then construct a `TradeProposal` whose `thesis`/`risk_thesis`
are template strings describing what the screen found — never a claim
about the future, never a number this module didn't just compute from
the contract in hand. Every `TradeProposal` this module builds is still
independently repriced and gated by the full pipeline
(`src.orchestration.pipeline.run_order_pipeline`) exactly like any other
proposal — this module only decides what's worth asking the pipeline
about.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from src.data.option_chain import OptionChain, OptionContract
from src.data.option_chain import OptionRight as DataOptionRight
from src.data.provider import FreshnessStatus
from src.llm.schemas import (
    Conviction,
    LegSide,
    MarketRegimeLabel,
    OptionLeg,
    OptionRight,
    StrategyType,
    TradeDirection,
    TradeProposal,
)
from src.risk.limits import RiskLimitsConfig
from src.risk.portfolio_risk import Portfolio

_CONTRACT_MULTIPLIER = 100


@dataclass(frozen=True)
class UniverseEntry:
    ticker: str
    sector: str


@dataclass(frozen=True)
class QuantFilterConfig:
    """Screening preferences, not risk limits — nothing here gates a
    trade the way `RiskLimitsConfig` does (that happens downstream, in
    the Risk Engine); this only decides what's worth proposing at all.
    Plain dataclass with defaults, the same status
    `src.brokers.paper.PaperBrokerConfig` has."""

    short_delta_low: float = 0.15
    short_delta_high: float = 0.30
    pcs_spread_width: float = 5.0
    min_dte: int = 20
    max_dte: int = 45
    management_dte: int = 21
    profit_target: float = 0.5

    def __post_init__(self) -> None:
        if not (0.0 < self.short_delta_low < self.short_delta_high <= 1.0):
            raise ValueError("short_delta_low must be less than short_delta_high, both in (0, 1]")
        if self.pcs_spread_width <= 0:
            raise ValueError("pcs_spread_width must be positive")
        if self.min_dte < 0 or self.max_dte <= self.min_dte:
            raise ValueError("min_dte must be non-negative and less than max_dte")
        if not (0.0 < self.profit_target <= 1.0):
            raise ValueError("profit_target must be in (0, 1]")


@dataclass(frozen=True)
class Candidate:
    proposal: TradeProposal
    entry_delta: float
    entry_iv: float | None
    sector: str


def passes_liquidity_filter(contract: OptionContract, limits: RiskLimitsConfig) -> bool:
    """Stage 11. A pure, non-raising screen (unlike
    `src.risk.trade_risk.check_liquidity`, which raises for a proposal
    already under formal review) — a contract that fails this is simply
    not offered as a candidate, never an error."""
    if contract.open_interest < limits.min_open_interest:
        return False
    if contract.volume < limits.min_volume:
        return False
    mid = contract.mid
    if mid <= 0:
        return False
    return (contract.ask - contract.bid) / mid <= limits.max_bid_ask_spread_pct


def _eligible_expirations(chain: OptionChain, as_of: datetime, quant_filter: QuantFilterConfig) -> list[date]:
    today = as_of.date()
    expirations = {
        c.expiration for c in chain.contracts
        if quant_filter.min_dte <= (c.expiration - today).days <= quant_filter.max_dte
    }
    return sorted(expirations)


def _closest_by_target_delta(contracts: list[OptionContract], quant_filter: QuantFilterConfig) -> OptionContract | None:
    mid_target = (quant_filter.short_delta_low + quant_filter.short_delta_high) / 2.0
    in_range = [c for c in contracts if c.delta is not None and quant_filter.short_delta_low <= abs(c.delta) <= quant_filter.short_delta_high]
    if not in_range:
        return None
    return min(in_range, key=lambda c: abs(abs(c.delta) - mid_target))  # type: ignore[arg-type]


def _short_put_candidate(chain: OptionChain, expiration: date, quant_filter: QuantFilterConfig, limits: RiskLimitsConfig) -> OptionContract | None:
    underlying_price = chain.underlying.mid
    puts = [c for c in chain.contracts if c.expiration == expiration and c.right == DataOptionRight.PUT and c.strike < underlying_price]
    best = _closest_by_target_delta(puts, quant_filter)
    if best is None or not passes_liquidity_filter(best, limits):
        return None
    return best


def _short_call_candidate(chain: OptionChain, expiration: date, quant_filter: QuantFilterConfig, limits: RiskLimitsConfig) -> OptionContract | None:
    underlying_price = chain.underlying.mid
    calls = [c for c in chain.contracts if c.expiration == expiration and c.right == DataOptionRight.CALL and c.strike > underlying_price]
    best = _closest_by_target_delta(calls, quant_filter)
    if best is None or not passes_liquidity_filter(best, limits):
        return None
    return best


def _long_put_for_spread(chain: OptionChain, short_put: OptionContract, quant_filter: QuantFilterConfig, limits: RiskLimitsConfig) -> OptionContract | None:
    target_strike = short_put.strike - quant_filter.pcs_spread_width
    candidates = [
        c for c in chain.contracts
        if c.expiration == short_put.expiration and c.right == DataOptionRight.PUT and c.strike < short_put.strike
    ]
    if not candidates:
        return None
    best = min(candidates, key=lambda c: abs(c.strike - target_strike))
    if not passes_liquidity_filter(best, limits):
        return None
    return best


def _management_dte(total_dte: int, quant_filter: QuantFilterConfig) -> int:
    return min(quant_filter.management_dte, total_dte)


def _build_proposal(
    *, proposal_id: str, ticker: str, strategy: StrategyType, expiration: date, legs: list[OptionLeg],
    target_entry: float, market_regime: MarketRegimeLabel, thesis: str, risk_thesis: str,
    invalidation_conditions: list[str], data_timestamp: datetime, data_source: str,
    now: datetime, quant_filter: QuantFilterConfig,
) -> TradeProposal:
    total_dte = (expiration - now.date()).days
    return TradeProposal(
        proposal_id=proposal_id,
        timestamp=now,
        ticker=ticker,
        strategy=strategy,
        market_regime=market_regime,
        expiration=expiration,
        legs=legs,
        direction=TradeDirection.NEUTRAL,
        contracts_requested=1,
        target_entry=target_entry,
        profit_target=quant_filter.profit_target,
        management_dte=_management_dte(total_dte, quant_filter),
        thesis=thesis,
        risk_thesis=risk_thesis,
        confidence=Conviction.MEDIUM,
        data_sources=[data_source],
        data_timestamp=data_timestamp,
        invalidation_conditions=invalidation_conditions,
    )


def generate_candidates(
    universe_entry: UniverseEntry,
    chain: OptionChain,
    strategies: list[StrategyType],
    quant_filter: QuantFilterConfig,
    limits: RiskLimitsConfig,
    portfolio: Portfolio,
    market_regime: MarketRegimeLabel,
    *,
    now: datetime,
    proposal_id_prefix: str = "scan",
) -> list[Candidate]:
    """One candidate at most per requested strategy for this ticker — the
    single best (closest-to-target-delta) eligible structure, not every
    expiration/strike combination that happens to clear the filters.
    Returns an empty list for a stale chain (stage 1-2 already reports
    that separately) rather than letting a stale `data_timestamp` fail
    deep inside `TradeProposal` construction."""
    if chain.freshness_status(now) == FreshnessStatus.STALE:
        return []

    ticker = universe_entry.ticker
    expirations = _eligible_expirations(chain, now, quant_filter)
    candidates: list[Candidate] = []
    counter = 0

    def _next_id(strategy_tag: str, expiration: date, strikes: tuple[float, ...]) -> str:
        # SY-001 fix: a bare in-call counter (`f"{prefix}-{ticker}-{n}"`)
        # is unique only within one `generate_candidates` call. Reused
        # across separate scan runs against the same broker/idempotency
        # instance (a persistent multi-day paper-trading loop), two
        # different days' first candidates for the same ticker would
        # collide on the identical id, and the second day's genuinely
        # new order would be silently treated as a duplicate of the
        # first. Including the scan date, expiration, and strike(s)
        # makes the id unique per *intended trade*, not per call order.
        nonlocal counter
        counter += 1
        strike_part = "-".join(f"{s:g}" for s in strikes)
        return f"{proposal_id_prefix}-{ticker}-{now.date().isoformat()}-{strategy_tag}-{expiration.isoformat()}-{strike_part}-{counter}"

    if StrategyType.CASH_SECURED_PUT in strategies:
        best: tuple[date, OptionContract] | None = None
        best_score = None
        for exp in expirations:
            contract = _short_put_candidate(chain, exp, quant_filter, limits)
            if contract is None:
                continue
            score = abs(abs(contract.delta) - (quant_filter.short_delta_low + quant_filter.short_delta_high) / 2.0)  # type: ignore[arg-type]
            if best_score is None or score < best_score:
                best, best_score = (exp, contract), score
        if best is not None:
            exp, contract = best
            proposal = _build_proposal(
                proposal_id=_next_id("csp", exp, (contract.strike,)), ticker=ticker, strategy=StrategyType.CASH_SECURED_PUT, expiration=exp,
                legs=[OptionLeg(right=OptionRight.PUT, strike=contract.strike, side=LegSide.SELL)],
                target_entry=contract.mid, market_regime=market_regime,
                thesis=f"Systematic screen: {abs(contract.delta):.2f}-delta cash-secured put on {ticker}, {(exp - now.date()).days} DTE, IV {contract.iv:.0%}" if contract.iv is not None else f"Systematic screen: {abs(contract.delta):.2f}-delta cash-secured put on {ticker}, {(exp - now.date()).days} DTE",
                risk_thesis=f"Max loss capped at strike ({contract.strike:g}) minus premium collected, times 100 shares per contract.",
                invalidation_conditions=[f"underlying closes below {contract.strike:g} before expiration"],
                data_timestamp=chain.timestamp, data_source=chain.source, now=now, quant_filter=quant_filter,
            )
            candidates.append(Candidate(proposal=proposal, entry_delta=contract.delta, entry_iv=contract.iv, sector=universe_entry.sector))  # type: ignore[arg-type]

    if StrategyType.COVERED_CALL in strategies:
        holding = portfolio.underlying_holdings.get(ticker)
        if holding is not None and holding.shares >= _CONTRACT_MULTIPLIER:
            best = None
            best_score = None
            for exp in expirations:
                contract = _short_call_candidate(chain, exp, quant_filter, limits)
                if contract is None:
                    continue
                score = abs(abs(contract.delta) - (quant_filter.short_delta_low + quant_filter.short_delta_high) / 2.0)  # type: ignore[arg-type]
                if best_score is None or score < best_score:
                    best, best_score = (exp, contract), score
            if best is not None:
                exp, contract = best
                proposal = _build_proposal(
                    proposal_id=_next_id("cc", exp, (contract.strike,)), ticker=ticker, strategy=StrategyType.COVERED_CALL, expiration=exp,
                    legs=[OptionLeg(right=OptionRight.CALL, strike=contract.strike, side=LegSide.SELL)],
                    target_entry=contract.mid, market_regime=market_regime,
                    thesis=f"Systematic screen: {abs(contract.delta):.2f}-delta covered call on {ticker} against {holding.shares} held shares, {(exp - now.date()).days} DTE",
                    risk_thesis=f"Upside capped at strike ({contract.strike:g}); downside is the underlying shares' own risk, unchanged by selling the call.",
                    invalidation_conditions=[f"underlying closes above {contract.strike:g} before expiration"],
                    data_timestamp=chain.timestamp, data_source=chain.source, now=now, quant_filter=quant_filter,
                )
                candidates.append(Candidate(proposal=proposal, entry_delta=contract.delta, entry_iv=contract.iv, sector=universe_entry.sector))  # type: ignore[arg-type]

    if StrategyType.PUT_CREDIT_SPREAD in strategies:
        best = None
        best_score = None
        for exp in expirations:
            short_put = _short_put_candidate(chain, exp, quant_filter, limits)
            if short_put is None:
                continue
            long_put = _long_put_for_spread(chain, short_put, quant_filter, limits)
            if long_put is None:
                continue
            credit = short_put.mid - long_put.mid
            if credit <= 0:
                continue
            score = abs(abs(short_put.delta) - (quant_filter.short_delta_low + quant_filter.short_delta_high) / 2.0)  # type: ignore[arg-type]
            if best_score is None or score < best_score:
                best, best_score = (exp, short_put, long_put, credit), score
        if best is not None:
            exp, short_put, long_put, credit = best
            proposal = _build_proposal(
                proposal_id=_next_id("pcs", exp, (short_put.strike, long_put.strike)), ticker=ticker, strategy=StrategyType.PUT_CREDIT_SPREAD, expiration=exp,
                legs=[
                    OptionLeg(right=OptionRight.PUT, strike=short_put.strike, side=LegSide.SELL),
                    OptionLeg(right=OptionRight.PUT, strike=long_put.strike, side=LegSide.BUY),
                ],
                target_entry=credit, market_regime=market_regime,
                thesis=f"Systematic screen: {abs(short_put.delta):.2f}-delta put credit spread on {ticker} ({short_put.strike:g}/{long_put.strike:g}), {(exp - now.date()).days} DTE",
                risk_thesis=f"Max loss capped at strike width ({short_put.strike - long_put.strike:g}) minus credit collected, times 100 shares per contract.",
                invalidation_conditions=[f"underlying closes below {long_put.strike:g} before expiration"],
                data_timestamp=chain.timestamp, data_source=chain.source, now=now, quant_filter=quant_filter,
            )
            candidates.append(Candidate(proposal=proposal, entry_delta=short_put.delta, entry_iv=short_put.iv, sector=universe_entry.sector))  # type: ignore[arg-type]

    return candidates


# Step 22.5 (PAPER_TRADING_V1.4.4): the only strategies with real
# candidate-generation logic today -- see this function's own
# `if StrategyType.X in strategies:` branches above. Lives here (not in
# `src.data.universe`, the config loader that feeds a *configured*
# strategy list into `candidate_eligible_strategies` below) because
# `src.data` must never import `src.llm` (see
# `tests/unit/data/test_architecture_boundary.py` -- the Market Data
# Layer sits strictly beneath the Multi-Agent Layer, ARCHITECTURE.md
# §3/§9) and `StrategyType` lives in `src.llm.schemas`. This module
# already depends on both, and is the authoritative source of "which
# strategies can this module actually produce a candidate for" in the
# first place.
CANDIDATE_GENERATION_ELIGIBLE_STRATEGIES: tuple[StrategyType, ...] = (
    StrategyType.CASH_SECURED_PUT,
    StrategyType.COVERED_CALL,
    StrategyType.PUT_CREDIT_SPREAD,
)


def candidate_eligible_strategies(strategies: tuple[StrategyType, ...]) -> tuple[StrategyType, ...]:
    """Narrows a configured strategy list down to the ones
    `generate_candidates` can actually produce a candidate for today,
    preserving the configured order. See
    `CANDIDATE_GENERATION_ELIGIBLE_STRATEGIES`'s own docstring for why the
    other 12 approved strategies are excluded here -- not a rejection of
    those strategies, just an honest statement that no scan logic exists
    for them yet."""
    eligible = set(CANDIDATE_GENERATION_ELIGIBLE_STRATEGIES)
    return tuple(s for s in strategies if s in eligible)
