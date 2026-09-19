"""Expiration settlement: intrinsic-value cash settlement plus real
assignment/exercise share and cash flows — the same modeling choice
`src.brokers.paper.PaperBroker.settle_expiration` already makes,
applied here to a `BacktestPosition` instead of the paper broker's live
position book. Kept as its own module (not folded into
`src.brokers.paper`) because a backtest settles thousands of historical
expirations against a known, already-realized settlement price, not a
handful of live ones against a value the caller supplies at the moment
it happens.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.backtest.simulator import BacktestLeg
from src.data.option_chain import OptionRight

_CONTRACT_MULTIPLIER = 100


@dataclass(frozen=True)
class LegSettlement:
    leg: BacktestLeg
    intrinsic_value: float  # per share, always >= 0
    was_itm: bool
    assigned_or_exercised: bool
    cash_impact: float  # signed, total across all contracts
    share_impact: int  # signed change to the underlying equity position


def intrinsic_value(right: OptionRight, strike: float, settlement_price: float) -> float:
    if right == OptionRight.CALL:
        return max(settlement_price - strike, 0.0)
    return max(strike - settlement_price, 0.0)


def settle_leg(leg: BacktestLeg, contracts: int, settlement_price: float) -> LegSettlement:
    """One leg's terminal settlement. A short leg's intrinsic value is
    assigned (buys shares for a put, sells shares for a call); a long
    leg's is exercised (the mirror image). An OTM leg (`intrinsic == 0`)
    expires worthless with no further cash or share impact — the
    premium already changed hands at entry."""
    intrinsic = intrinsic_value(leg.right, leg.strike, settlement_price)
    was_itm = intrinsic > 0
    if not was_itm:
        return LegSettlement(leg=leg, intrinsic_value=0.0, was_itm=False, assigned_or_exercised=False, cash_impact=0.0, share_impact=0)

    is_short = leg.side == "sell"
    # short put -> assigned -> buy shares; long put -> exercised -> sell shares
    # short call -> assigned -> sell shares; long call -> exercised -> buy shares
    buys_shares = (leg.right == OptionRight.PUT) == is_short
    share_impact = _CONTRACT_MULTIPLIER * contracts * (1 if buys_shares else -1)
    cash_impact = -leg.strike * _CONTRACT_MULTIPLIER * contracts * (1 if buys_shares else -1)
    return LegSettlement(leg=leg, intrinsic_value=intrinsic, was_itm=True, assigned_or_exercised=True, cash_impact=cash_impact, share_impact=share_impact)


def settle_position(legs: list[BacktestLeg], contracts: int, settlement_price: float) -> list[LegSettlement]:
    return [settle_leg(leg, contracts, settlement_price) for leg in legs]
