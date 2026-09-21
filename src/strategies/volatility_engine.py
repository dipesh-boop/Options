"""The volatility-comparison engine Step 19A requires: implied vs.
realized volatility, and — for the two volatility-expansion strategies
— how large a move is actually required to overcome the premium paid,
compared against the market's own implied expected move and (where
supplied) a historical move distribution.

"Do not buy volatility simply because an event exists" and "do not sell
volatility merely because IV is elevated" are enforced by what this
module refuses to do: every function here returns a comparison figure
(a ratio, a required-move percentage), never a verdict ("buy" / "sell"
/ "approved"). Whether a given IV/RV spread or a given required move is
*worth acting on* is exactly the qualitative judgment
`src.llm.devils_advocate`/`src.llm.portfolio_manager` make and the
deterministic Risk Engine ultimately gates — this module only supplies
the Python-computed numbers those decisions are made from.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.llm.context import MarketContext
from src.strategies.long_straddle import required_move_pct as straddle_required_move_pct
from src.strategies.long_strangle import required_move_pct as strangle_required_move_pct


@dataclass(frozen=True)
class VolatilityComparison:
    implied_volatility: float
    realized_volatility: float | None
    iv_rv_spread: float | None  # implied_volatility - realized_volatility; None when realized_volatility is unavailable
    iv_percentile: float | None
    iv_rank: float | None
    volatility_skew: float | None
    term_structure: tuple[tuple[int, float], ...]
    term_structure_slope: float | None  # far-dated IV minus near-dated IV, from the supplied term structure's two ends


def compare_iv_to_rv(*, implied_volatility: float, market: MarketContext) -> VolatilityComparison:
    if implied_volatility <= 0:
        raise ValueError("implied_volatility must be positive")
    rv = market.realized_volatility
    spread = (implied_volatility - rv) if rv is not None else None

    slope = None
    if len(market.volatility_term_structure) >= 2:
        ordered = sorted(market.volatility_term_structure, key=lambda pair: pair[0])
        slope = ordered[-1][1] - ordered[0][1]

    return VolatilityComparison(
        implied_volatility=implied_volatility,
        realized_volatility=rv,
        iv_rv_spread=spread,
        iv_percentile=market.iv_percentile,
        iv_rank=market.iv_rank,
        volatility_skew=market.volatility_skew,
        term_structure=market.volatility_term_structure,
        term_structure_slope=slope,
    )


@dataclass(frozen=True)
class RequiredMoveComparison:
    required_move_pct: float
    implied_expected_move_pct: float | None  # e.g. ATM straddle-derived expected move for the same expiration, if supplied
    historical_move_pct: float | None  # e.g. the historical distribution's own move of similar magnitude/window, if supplied
    required_move_exceeds_implied_expected_move: bool | None
    required_move_exceeds_historical_move: bool | None


def straddle_required_move_comparison(
    *, spot: float, strike: float, call_premium: float, put_premium: float,
    implied_expected_move_pct: float | None = None, historical_move_pct: float | None = None,
) -> RequiredMoveComparison:
    required = straddle_required_move_pct(spot=spot, call_premium=call_premium, put_premium=put_premium)
    return RequiredMoveComparison(
        required_move_pct=required,
        implied_expected_move_pct=implied_expected_move_pct,
        historical_move_pct=historical_move_pct,
        required_move_exceeds_implied_expected_move=(
            required > implied_expected_move_pct if implied_expected_move_pct is not None else None
        ),
        required_move_exceeds_historical_move=(
            required > historical_move_pct if historical_move_pct is not None else None
        ),
    )


def strangle_required_move_comparison(
    *, spot: float, call_strike: float, put_strike: float, call_premium: float, put_premium: float,
    implied_expected_move_pct: float | None = None, historical_move_pct: float | None = None,
) -> RequiredMoveComparison:
    required = strangle_required_move_pct(
        spot=spot, call_strike=call_strike, put_strike=put_strike, call_premium=call_premium, put_premium=put_premium
    )
    return RequiredMoveComparison(
        required_move_pct=required,
        implied_expected_move_pct=implied_expected_move_pct,
        historical_move_pct=historical_move_pct,
        required_move_exceeds_implied_expected_move=(
            required > implied_expected_move_pct if implied_expected_move_pct is not None else None
        ),
        required_move_exceeds_historical_move=(
            required > historical_move_pct if historical_move_pct is not None else None
        ),
    )


def premium_compensates_for_tail_risk(*, credit_received: float, expected_shortfall: float) -> float:
    """For a short-volatility structure (credit spread, iron condor,
    iron butterfly): the ratio of credit actually received to the
    Monte Carlo-estimated expected shortfall (`src.strategies.base
    .ProbabilityMetrics.expected_shortfall`) of the SAME structure.
    A ratio well below 1.0 means the tail outcomes the simulation
    already found are large relative to what was collected for taking
    them on -- a number for a human/LLM reviewer to weigh, never a
    pass/fail this function decides itself."""
    if expected_shortfall >= 0:
        raise ValueError("expected_shortfall must be negative (a loss) for this comparison to be meaningful")
    return credit_received / abs(expected_shortfall)
