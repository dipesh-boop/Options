"""The common strategy-evaluation contract every module under
`src/strategies/` produces (Step 19A). Every strategy module is a thin
leg-builder around this shared assembler — no strategy module derives
its own max-profit/max-loss/breakeven/Greeks/EV math; all of it comes
from `src.quant` (`src.quant.monte_carlo.payoff_profile`, `net_greeks`,
`monte_carlo_pop_and_ev`, `stress_test`), reused identically across all
16 strategy kinds, both the 9 wired all the way to a real order
(`StrategyKind` values that also exist in `src.llm.schemas.StrategyType`)
and the 3 evaluation-only ones (butterfly / iron condor / iron
butterfly — see `StrategyKind`'s own docstring).

This module never places, sizes-for-execution, or approves anything —
`StrategyEvaluation` is comparison/reporting data. A candidate that
becomes a real order still goes through the unchanged Python Quant /
Python Risk Engine path (`src.risk.trade_risk` / `src.risk.engine`),
exactly as before Step 19A.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Literal

import numpy as np

from src.data.option_chain import OptionContract
from src.llm.schemas import StrategyType
from src.quant.black_scholes import OptionRight, Side
from src.quant.greeks import net_greeks
from src.quant.monte_carlo import (
    STANDARD_SPOT_SHOCKS,
    MonteCarloResult,
    Position,
    StressScenario,
    monte_carlo_pop_and_ev,
    payoff_at_expiration,
    payoff_profile,
    simulate_terminal_prices,
    stress_test,
)
from src.risk.limits import RiskLimitsConfig


class StrategyKind(str, Enum):
    """Every strategy Step 19A names, 16 members. Deliberately a
    separate enum from `src.llm.schemas.StrategyType` (which has only
    the 12 members representable as a <=2-leg `TradeProposal` today) —
    every `StrategyKind` value below that has a same-named
    `StrategyType` member shares its exact string value, so converting
    between them for the 12 wired strategies is a plain
    `StrategyType(StrategyKind.X.value)` with no translation table.
    `LONG_CALL_BUTTERFLY`, `SHORT_IRON_CONDOR`, `SHORT_IRON_BUTTERFLY`
    have no `StrategyType` counterpart at all — they can be evaluated
    and compared (this module, `src.strategies.selector`/`comparison`)
    but never wired to `src.risk.engine.evaluate_trade_proposal` or a
    `FidelityTradeTicket` until a separate, dedicated hardening pass
    extends `TradeProposal`'s 2-leg cap."""

    CASH_SECURED_PUT = "cash_secured_put"
    COVERED_CALL = "covered_call"
    PUT_CREDIT_SPREAD = "put_credit_spread"
    CALL_CREDIT_SPREAD = "call_credit_spread"
    BULL_CALL_SPREAD = "bull_call_spread"
    BEAR_PUT_SPREAD = "bear_put_spread"
    PROTECTIVE_PUT = "protective_put"
    PROTECTIVE_COLLAR = "protective_collar"
    LONG_STRADDLE = "long_straddle"
    LONG_STRANGLE = "long_strangle"
    LONG_CALL = "long_call"
    LONG_PUT = "long_put"
    LONG_CALL_BUTTERFLY = "long_call_butterfly"
    SHORT_IRON_CONDOR = "short_iron_condor"
    SHORT_IRON_BUTTERFLY = "short_iron_butterfly"


_STRATEGY_TYPE_VALUES = {m.value for m in StrategyType}

# Every StrategyKind with a same-named, same-valued StrategyType member
# -- these, and only these, can become a real TradeProposal today.
TRADE_PROPOSAL_ELIGIBLE: frozenset[StrategyKind] = frozenset(k for k in StrategyKind if k.value in _STRATEGY_TYPE_VALUES)


def strategy_type_for(kind: StrategyKind) -> StrategyType | None:
    """`None` for the 3 evaluation-only kinds -- never raises, since
    "not yet order-eligible" is an expected, first-class outcome here,
    not an error."""
    try:
        return StrategyType(kind.value)
    except ValueError:
        return None


class StrategyFamily(str, Enum):
    """The 8 families Step 14B names verbatim, plus one additional value
    (`TAIL_RISK_HEDGE`) this codebase already carried from Step 19A and
    keeps rather than discarding: protective put/collar are more than
    generically `PORTFOLIO_PROTECTION` (a covered call is arguably
    "protective" of nothing) -- they specifically hedge tail risk, a
    distinction worth keeping queryable on its own. Neither step's
    instructions said the classification must be *exactly* these 8 and
    no more, only that these 8 must exist; `TAIL_RISK_HEDGE` never
    substitutes for `PORTFOLIO_PROTECTION`, it is additive to it (see
    `STRATEGY_FAMILIES` below, where every `TAIL_RISK_HEDGE` entry also
    carries `PORTFOLIO_PROTECTION`)."""

    INCOME = "income"
    DIRECTIONAL_BULLISH = "directional_bullish"
    DIRECTIONAL_BEARISH = "directional_bearish"
    VOLATILITY_EXPANSION = "volatility_expansion"
    VOLATILITY_CONTRACTION = "volatility_contraction"
    PORTFOLIO_PROTECTION = "portfolio_protection"
    NEUTRAL_RANGE = "neutral_range"
    CAPITAL_PRESERVATION = "capital_preservation"
    TAIL_RISK_HEDGE = "tail_risk_hedge"


# Classification is intentionally multi-valued per strategy (a covered
# call is both INCOME and mildly DIRECTIONAL_BULLISH-capped; a
# protective put is both PORTFOLIO_PROTECTION and a TAIL_RISK_HEDGE) --
# "classify into one or more" per Step 19A/14B, never collapsed to a
# single label.
STRATEGY_FAMILIES: dict[StrategyKind, tuple[StrategyFamily, ...]] = {
    StrategyKind.CASH_SECURED_PUT: (StrategyFamily.INCOME, StrategyFamily.DIRECTIONAL_BULLISH),
    StrategyKind.COVERED_CALL: (StrategyFamily.INCOME, StrategyFamily.NEUTRAL_RANGE),
    StrategyKind.PUT_CREDIT_SPREAD: (StrategyFamily.INCOME, StrategyFamily.DIRECTIONAL_BULLISH),
    StrategyKind.CALL_CREDIT_SPREAD: (StrategyFamily.INCOME, StrategyFamily.DIRECTIONAL_BEARISH),
    StrategyKind.BULL_CALL_SPREAD: (StrategyFamily.DIRECTIONAL_BULLISH,),
    StrategyKind.BEAR_PUT_SPREAD: (StrategyFamily.DIRECTIONAL_BEARISH,),
    StrategyKind.PROTECTIVE_PUT: (StrategyFamily.PORTFOLIO_PROTECTION, StrategyFamily.TAIL_RISK_HEDGE),
    StrategyKind.PROTECTIVE_COLLAR: (
        StrategyFamily.PORTFOLIO_PROTECTION, StrategyFamily.TAIL_RISK_HEDGE, StrategyFamily.CAPITAL_PRESERVATION,
    ),
    StrategyKind.LONG_STRADDLE: (StrategyFamily.VOLATILITY_EXPANSION, StrategyFamily.NEUTRAL_RANGE),
    StrategyKind.LONG_STRANGLE: (StrategyFamily.VOLATILITY_EXPANSION, StrategyFamily.NEUTRAL_RANGE),
    StrategyKind.LONG_CALL: (StrategyFamily.DIRECTIONAL_BULLISH,),
    StrategyKind.LONG_PUT: (StrategyFamily.DIRECTIONAL_BEARISH,),
    StrategyKind.LONG_CALL_BUTTERFLY: (StrategyFamily.NEUTRAL_RANGE, StrategyFamily.VOLATILITY_CONTRACTION),
    StrategyKind.SHORT_IRON_CONDOR: (
        StrategyFamily.INCOME, StrategyFamily.NEUTRAL_RANGE, StrategyFamily.VOLATILITY_CONTRACTION,
    ),
    StrategyKind.SHORT_IRON_BUTTERFLY: (
        StrategyFamily.INCOME, StrategyFamily.NEUTRAL_RANGE, StrategyFamily.VOLATILITY_CONTRACTION,
    ),
}

class ManagementConditionType(str, Enum):
    """The closed set of position-management condition types this Step
    19A re-ask names verbatim. The free-text `entry_rules`/`exit_rules`/
    `adjustment_rules`/`invalidation_rules` tuples on `StrategyEvaluation`
    remain human-readable prose (unchanged) — this enum is the actual
    enforcement mechanism behind "LLMs may interpret conditions, they
    may NOT improvise risk rules": which *categories* of management
    logic apply to a given strategy is a closed, Python-determined set
    (`MANAGEMENT_CONDITION_TYPES` below), never something an LLM can add
    to or invent on its own."""

    PROFIT_TARGET = "profit_target"
    MAX_LOSS = "max_loss"
    DTE_EXIT = "dte_exit"
    THESIS_INVALIDATION = "thesis_invalidation"
    DELTA_THRESHOLD = "delta_threshold"
    VOLATILITY_CHANGE = "volatility_change"
    ROLL_EVALUATION = "roll_evaluation"
    ASSIGNMENT_MANAGEMENT = "assignment_management"
    EXPIRATION_MANAGEMENT = "expiration_management"


_MCT = ManagementConditionType
# Every strategy gets the universal baseline (PROFIT_TARGET, MAX_LOSS,
# DTE_EXIT, THESIS_INVALIDATION, EXPIRATION_MANAGEMENT); additional
# types are added only where they actually apply -- a pure long-premium
# structure with no short leg has no assignment risk to manage, and a
# structure with no shares/short-option delta exposure worth actively
# monitoring has no reason to carry DELTA_THRESHOLD.
_BASELINE_MCT = (_MCT.PROFIT_TARGET, _MCT.MAX_LOSS, _MCT.DTE_EXIT, _MCT.THESIS_INVALIDATION, _MCT.EXPIRATION_MANAGEMENT)

MANAGEMENT_CONDITION_TYPES: dict[StrategyKind, tuple[ManagementConditionType, ...]] = {
    StrategyKind.CASH_SECURED_PUT: _BASELINE_MCT + (_MCT.ASSIGNMENT_MANAGEMENT, _MCT.ROLL_EVALUATION, _MCT.DELTA_THRESHOLD),
    StrategyKind.COVERED_CALL: _BASELINE_MCT + (_MCT.ASSIGNMENT_MANAGEMENT, _MCT.ROLL_EVALUATION, _MCT.DELTA_THRESHOLD),
    StrategyKind.PUT_CREDIT_SPREAD: _BASELINE_MCT + (_MCT.ASSIGNMENT_MANAGEMENT, _MCT.ROLL_EVALUATION, _MCT.DELTA_THRESHOLD),
    StrategyKind.CALL_CREDIT_SPREAD: _BASELINE_MCT + (_MCT.ASSIGNMENT_MANAGEMENT, _MCT.ROLL_EVALUATION, _MCT.DELTA_THRESHOLD),
    StrategyKind.BULL_CALL_SPREAD: _BASELINE_MCT + (_MCT.ASSIGNMENT_MANAGEMENT, _MCT.ROLL_EVALUATION, _MCT.DELTA_THRESHOLD),
    StrategyKind.BEAR_PUT_SPREAD: _BASELINE_MCT + (_MCT.ASSIGNMENT_MANAGEMENT, _MCT.ROLL_EVALUATION, _MCT.DELTA_THRESHOLD),
    StrategyKind.PROTECTIVE_PUT: _BASELINE_MCT + (_MCT.VOLATILITY_CHANGE, _MCT.ROLL_EVALUATION),
    StrategyKind.PROTECTIVE_COLLAR: _BASELINE_MCT + (_MCT.ASSIGNMENT_MANAGEMENT, _MCT.VOLATILITY_CHANGE, _MCT.ROLL_EVALUATION),
    StrategyKind.LONG_STRADDLE: _BASELINE_MCT + (_MCT.VOLATILITY_CHANGE, _MCT.DELTA_THRESHOLD),
    StrategyKind.LONG_STRANGLE: _BASELINE_MCT + (_MCT.VOLATILITY_CHANGE, _MCT.DELTA_THRESHOLD),
    StrategyKind.LONG_CALL: _BASELINE_MCT + (_MCT.VOLATILITY_CHANGE, _MCT.DELTA_THRESHOLD),
    StrategyKind.LONG_PUT: _BASELINE_MCT + (_MCT.VOLATILITY_CHANGE, _MCT.DELTA_THRESHOLD),
    StrategyKind.LONG_CALL_BUTTERFLY: _BASELINE_MCT + (_MCT.ASSIGNMENT_MANAGEMENT, _MCT.DELTA_THRESHOLD, _MCT.VOLATILITY_CHANGE),
    StrategyKind.SHORT_IRON_CONDOR: _BASELINE_MCT + (
        _MCT.ASSIGNMENT_MANAGEMENT, _MCT.ROLL_EVALUATION, _MCT.DELTA_THRESHOLD, _MCT.VOLATILITY_CHANGE,
    ),
    StrategyKind.SHORT_IRON_BUTTERFLY: _BASELINE_MCT + (
        _MCT.ASSIGNMENT_MANAGEMENT, _MCT.ROLL_EVALUATION, _MCT.DELTA_THRESHOLD, _MCT.VOLATILITY_CHANGE,
    ),
}


MarketOutlook = Literal[
    "bullish", "moderately_bullish", "neutral", "moderately_bearish", "bearish", "protection",
]
VolatilityOutlook = Literal["expansion", "contraction", "neutral", "irrelevant"]
RiskLevel = Literal["none", "low", "medium", "high"]


@dataclass(frozen=True)
class ProbabilityMetrics:
    probability_of_profit: float
    probability_of_max_loss: float | None  # "where calculable" -- None when the payoff has no single max-loss terminal region
    expected_shortfall: float | None  # == CVaR at the configured confidence; same figure, one field, not two
    monte_carlo: MonteCarloResult


@dataclass(frozen=True)
class StrategyEvaluation:
    """One fully-priced candidate structure, ready to compare against
    every other candidate for the same opportunity (including NO_TRADE,
    which `src.strategies.comparison` represents separately, never as a
    member of this dataclass)."""

    strategy_kind: StrategyKind
    strategy_family: tuple[StrategyFamily, ...]
    market_outlook: MarketOutlook
    volatility_outlook: VolatilityOutlook
    ticker: str
    expiration: date
    position: Position  # the priced legs + any underlying shares -- payoff_at(price) below reads this
    required_positions: str  # human-readable prerequisite, e.g. "100 shares of XYZ already held"
    number_of_legs: int
    contracts: int

    capital_requirement: float
    buying_power_requirement: float
    net_credit_or_debit: float  # signed: positive = credit received, negative = debit paid

    maximum_profit: float  # may be math.inf
    maximum_loss: float
    breakeven_points: tuple[float, ...]
    current_mark_to_market: float

    delta: float
    gamma: float
    theta: float
    vega: float

    probability_metrics: ProbabilityMetrics
    expected_value: float
    return_on_capital: float
    annualized_return_on_capital: float

    liquidity_score: float  # 0 (illiquid/fails minimums) to 1 (comfortably liquid)
    estimated_slippage: float  # dollars, whole position
    execution_complexity: Literal["single_leg", "two_leg", "three_leg", "four_leg"]

    assignment_risk: RiskLevel
    early_exercise_risk: RiskLevel
    event_risk: RiskLevel

    entry_rules: tuple[str, ...]
    exit_rules: tuple[str, ...]
    adjustment_rules: tuple[str, ...]
    invalidation_rules: tuple[str, ...]
    management_condition_types: tuple[ManagementConditionType, ...]  # auto-derived from MANAGEMENT_CONDITION_TYPES[kind], never caller-supplied

    fidelity_compatible: bool
    fidelity_incompatibility_reason: str | None

    def payoff_at(self, terminal_price: float) -> float:
        return payoff_at_expiration(self.position, terminal_price)


_CONTRACT_MULTIPLIER = 100
_MC_PATHS = 20_000
_MC_SEED = 20260101
_VAR_TAIL_FRACTION = 0.05  # bottom 5% of simulated outcomes -> expected shortfall / CVaR


def _liquidity_score(contracts: list[OptionContract], limits: RiskLimitsConfig) -> float:
    if not contracts:
        return 1.0  # a shares-only leg set (none here today, but keep this total) has nothing to score
    scores = []
    for c in contracts:
        oi_score = min((c.open_interest or 0) / max(limits.min_open_interest, 1), 1.0)
        vol_score = min((c.volume or 0) / max(limits.min_volume, 1), 1.0)
        mid = c.mid
        spread_pct = (c.ask - c.bid) / mid if mid > 0 else 1.0
        spread_score = max(1.0 - spread_pct / max(limits.max_bid_ask_spread_pct, 1e-6), 0.0)
        scores.append((oi_score + vol_score + spread_score) / 3.0)
    return max(min(sum(scores) / len(scores), 1.0), 0.0)


def _estimated_slippage(contracts: list[OptionContract], quantity: int) -> float:
    """Half the widest bid/ask spread across legs, in dollars for the
    whole position -- a standard, simple "expected cost to cross the
    spread" estimate, not a full execution-cost model (that already
    exists, more precisely, in `src.brokers.paper.compute_fill` for an
    actual simulated fill; this is a pre-trade comparison figure only)."""
    total = 0.0
    for c in contracts:
        total += (c.ask - c.bid) / 2.0 * _CONTRACT_MULTIPLIER * quantity
    return total


def _execution_complexity(num_legs: int) -> Literal["single_leg", "two_leg", "three_leg", "four_leg"]:
    return {1: "single_leg", 2: "two_leg", 3: "three_leg", 4: "four_leg"}[num_legs]


def _assignment_and_exercise_risk(position: Position, spot: float) -> tuple[RiskLevel, RiskLevel]:
    """A short leg that is currently in-the-money carries real
    assignment risk; a short leg comfortably out-of-the-money carries
    low risk; a position with no short legs at all (every long-premium
    strategy) carries none -- a long option is never assigned, only
    exercised at the holder's own choice, which this platform's
    strategies never do early by design (no naked/early-exercise
    trading logic exists anywhere in this codebase)."""
    short_legs = [leg for leg in position.legs if leg.side == Side.SELL]
    if not short_legs:
        return "none", "none"
    itm_short = any(
        (leg.right == OptionRight.CALL and spot > leg.strike) or (leg.right == OptionRight.PUT and spot < leg.strike)
        for leg in short_legs
    )
    return ("high" if itm_short else "low"), ("high" if itm_short else "low")


def build_strategy_evaluation(
    *,
    kind: StrategyKind,
    ticker: str,
    expiration: date,
    position: Position,
    contracts: list[OptionContract],
    limits: RiskLimitsConfig,
    spot: float,
    sigma: float,
    t: float,
    rate: float,
    days_to_expiry: int,
    num_contracts: int,
    market_outlook: MarketOutlook,
    volatility_outlook: VolatilityOutlook,
    required_positions: str,
    capital_requirement: float,
    buying_power_requirement: float,
    entry_rules: tuple[str, ...],
    exit_rules: tuple[str, ...],
    adjustment_rules: tuple[str, ...],
    invalidation_rules: tuple[str, ...],
    event_risk: RiskLevel = "low",
    fidelity_compatible: bool = True,
    fidelity_incompatibility_reason: str | None = None,
) -> StrategyEvaluation:
    """The single assembler every `src/strategies/<name>.py` module
    calls. Every dollar/probability/Greek figure below comes from
    `src.quant` -- this function performs no independent financial
    calculation of its own, only assembly."""
    profile = payoff_profile(position)
    greeks = net_greeks(
        [leg for leg in position.legs], spot, t, rate, sigma, underlying_shares=position.underlying_shares
    )
    mc = monte_carlo_pop_and_ev(position, spot, sigma, t, rate, _MC_PATHS, seed=_MC_SEED)

    # Expected shortfall / CVaR from the same Monte Carlo sample: the
    # mean payoff among the worst _VAR_TAIL_FRACTION of simulated
    # outcomes -- reuses simulate_terminal_prices' own paths rather than
    # a second simulation.
    terminal_prices = simulate_terminal_prices(spot, sigma, t, rate, _MC_PATHS, seed=_MC_SEED)
    payoffs = np.array([payoff_at_expiration(position, float(p)) for p in terminal_prices])
    tail_count = max(1, int(len(payoffs) * _VAR_TAIL_FRACTION))
    worst = np.sort(payoffs)[:tail_count]
    expected_shortfall = float(np.mean(worst))
    prob_of_max_loss = float(np.mean(payoffs <= -profile.max_loss + 1e-6)) if math.isfinite(profile.max_loss) else None

    current_mtm = payoff_at_expiration(position, spot)  # today's intrinsic-only snapshot; see docstring note below
    net_credit_or_debit = sum(
        (leg.entry_price if leg.side == Side.SELL else -leg.entry_price) * _CONTRACT_MULTIPLIER * leg.quantity
        for leg in position.legs
    ) / (_CONTRACT_MULTIPLIER * max(num_contracts, 1))

    roc = 0.0 if capital_requirement <= 0 else mc.expected_value / capital_requirement
    annualized_roc = roc * (365.0 / days_to_expiry) if days_to_expiry > 0 else roc

    liquidity = _liquidity_score(contracts, limits)
    slippage = _estimated_slippage(contracts, num_contracts)
    assignment_risk, early_exercise_risk = _assignment_and_exercise_risk(position, spot)

    return StrategyEvaluation(
        strategy_kind=kind,
        strategy_family=STRATEGY_FAMILIES[kind],
        market_outlook=market_outlook,
        volatility_outlook=volatility_outlook,
        ticker=ticker,
        expiration=expiration,
        position=position,
        required_positions=required_positions,
        number_of_legs=len(position.legs),
        contracts=num_contracts,
        capital_requirement=capital_requirement,
        buying_power_requirement=buying_power_requirement,
        net_credit_or_debit=net_credit_or_debit,
        maximum_profit=profile.max_profit,
        maximum_loss=profile.max_loss,
        breakeven_points=profile.breakeven_points,
        current_mark_to_market=current_mtm,
        delta=greeks.delta,
        gamma=greeks.gamma,
        theta=greeks.theta,
        vega=greeks.vega,
        probability_metrics=ProbabilityMetrics(
            probability_of_profit=mc.probability_of_profit,
            probability_of_max_loss=prob_of_max_loss,
            expected_shortfall=expected_shortfall,
            monte_carlo=mc,
        ),
        expected_value=mc.expected_value,
        return_on_capital=roc,
        annualized_return_on_capital=annualized_roc,
        liquidity_score=liquidity,
        estimated_slippage=slippage,
        execution_complexity=_execution_complexity(len(position.legs)),
        assignment_risk=assignment_risk,
        early_exercise_risk=early_exercise_risk,
        event_risk=event_risk,
        entry_rules=entry_rules,
        exit_rules=exit_rules,
        adjustment_rules=adjustment_rules,
        invalidation_rules=invalidation_rules,
        management_condition_types=MANAGEMENT_CONDITION_TYPES[kind],
        fidelity_compatible=fidelity_compatible,
        fidelity_incompatibility_reason=fidelity_incompatibility_reason,
    )


def stress_grid(evaluation: StrategyEvaluation, spot: float, sigma: float, t: float, rate: float) -> list[StressScenario]:
    """Thin pass-through to `src.quant.monte_carlo.stress_test` at the
    six standard spot shocks -- exposed here so every `src.strategies`
    consumer (comparison, portfolio_fit) gets the same stress figures
    the Risk Engine itself already computes for an approved order,
    without a second implementation."""
    return stress_test(evaluation.position, spot, sigma, t, rate, spot_shocks=STANDARD_SPOT_SHOCKS)
