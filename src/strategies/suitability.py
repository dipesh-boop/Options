"""Position-aware strategy suitability: never offer a share-requiring
strategy (covered call, protective put, protective collar) unless the
portfolio actually holds enough shares of the underlying. This is the
structural guarantee behind "do not incorrectly classify a short call
as a Covered Call" — a strategy this module doesn't consider suitable
is never constructed or priced at all, not merely flagged after the
fact.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.risk.portfolio_risk import Portfolio
from src.strategies.base import StrategyKind
from src.strategies.regime_mapping import SHARE_REQUIRING_STRATEGIES

_SHARES_PER_CONTRACT = 100


@dataclass(frozen=True)
class SuitabilityResult:
    kind: StrategyKind
    suitable: bool
    reason: str


def shares_held(portfolio: Portfolio, ticker: str) -> int:
    holding = portfolio.underlying_holdings.get(ticker)
    return holding.shares if holding is not None else 0


def is_strategy_suitable(kind: StrategyKind, *, ticker: str, portfolio: Portfolio, contracts: int) -> SuitabilityResult:
    if kind not in SHARE_REQUIRING_STRATEGIES:
        return SuitabilityResult(kind=kind, suitable=True, reason="no existing position required")

    held = shares_held(portfolio, ticker)
    required = _SHARES_PER_CONTRACT * contracts
    if held >= required:
        return SuitabilityResult(kind=kind, suitable=True, reason=f"{held} shares of {ticker} held, {required} required")
    return SuitabilityResult(
        kind=kind, suitable=False,
        reason=f"requires {required} shares of {ticker} already held; portfolio holds {held}",
    )


def filter_suitable_strategies(
    kinds: tuple[StrategyKind, ...], *, ticker: str, portfolio: Portfolio, contracts: int
) -> tuple[StrategyKind, ...]:
    """The list `src.strategies.selector` actually constructs candidates
    for — every kind in `kinds` that this portfolio can legitimately
    enter, in the same order."""
    return tuple(
        k for k in kinds
        if is_strategy_suitable(k, ticker=ticker, portfolio=portfolio, contracts=contracts).suitable
    )
