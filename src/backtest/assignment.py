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
    premium already changed hands at entry.

    ACCEPT-003 (Step 21 acceptance-test finding): this leg's actual
    contract count is `contracts * leg.quantity_ratio`, not `contracts`
    alone — `BacktestLeg.quantity_ratio`'s own docstring notes the
    ratio is applied on the ENTRY fill path (`_to_order_leg` ->
    `compute_fill`), but settlement is a separate code path that never
    went through that conversion, so `LONG_CALL_BUTTERFLY`'s 2x middle
    leg was previously settled at expiration as if it were only 1x —
    understating its assignment cash/share impact by half. Every other
    strategy in the library uses `quantity_ratio=1` on every leg (see
    `src.llm.schemas`'s own structural validators), so this is a no-op
    everywhere except the butterfly."""
    leg_contracts = contracts * leg.quantity_ratio
    intrinsic = intrinsic_value(leg.right, leg.strike, settlement_price)
    was_itm = intrinsic > 0
    if not was_itm:
        return LegSettlement(leg=leg, intrinsic_value=0.0, was_itm=False, assigned_or_exercised=False, cash_impact=0.0, share_impact=0)

    is_short = leg.side == "sell"
    # short put -> assigned -> buy shares; long put -> exercised -> sell shares
    # short call -> assigned -> sell shares; long call -> exercised -> buy shares
    buys_shares = (leg.right == OptionRight.PUT) == is_short
    share_impact = _CONTRACT_MULTIPLIER * leg_contracts * (1 if buys_shares else -1)
    cash_impact = -leg.strike * _CONTRACT_MULTIPLIER * leg_contracts * (1 if buys_shares else -1)
    return LegSettlement(leg=leg, intrinsic_value=intrinsic, was_itm=True, assigned_or_exercised=True, cash_impact=cash_impact, share_impact=share_impact)


def settle_position(legs: list[BacktestLeg], contracts: int, settlement_price: float) -> list[LegSettlement]:
    return [settle_leg(leg, contracts, settlement_price) for leg in legs]


def realized_settlement_pnl(
    settlements: list[LegSettlement],
    contracts: int,
    *,
    underlying_shares_held: int = 0,
    underlying_cost_basis: float = 0.0,
) -> float:
    """Converts `settle_position`'s per-leg `cash_impact`/`share_impact`
    into a realized P&L contribution, using **intrinsic value** rather
    than the full strike notional. Neither the backtest engine nor the
    rejected-trade-review "what if" pricer maintains an ongoing share
    ledger, so any stock position freshly *created* by assignment/
    exercise (a cash-secured put assigned, or either leg of a spread) is
    treated as immediately valued at the settlement price it was created
    at — economically equivalent to marking it to market and closing it
    out on the spot, which is exactly what "intrinsic value" means here.

    The one exception is a leg that *disposes of* shares the caller
    already held before this settlement (a covered position, signaled by
    `underlying_shares_held > 0` — the only shape in this codebase that
    ever sets it, a covered call's short call leg being assigned): that
    leg's gain/loss is realized against its *actual* cost basis, not the
    option's own intrinsic value, since those shares were not created by
    this settlement and may have a cost basis far from the strike.
    """
    total = 0.0
    for settlement in settlements:
        if not settlement.assigned_or_exercised:
            continue
        # ACCEPT-003: this leg's own quantity_ratio (see `settle_leg`'s
        # docstring) -- a flat `contracts` here would understate a
        # 2x-ratio leg's (e.g. LONG_CALL_BUTTERFLY's middle leg)
        # realized settlement P&L by half.
        leg_contracts = contracts * settlement.leg.quantity_ratio
        is_short = settlement.leg.side == "sell"
        disposes_shares = settlement.share_impact < 0
        if is_short and disposes_shares and underlying_shares_held > 0:
            total += (settlement.leg.strike - underlying_cost_basis) * _CONTRACT_MULTIPLIER * leg_contracts
        elif is_short:
            total -= settlement.intrinsic_value * _CONTRACT_MULTIPLIER * leg_contracts
        else:
            total += settlement.intrinsic_value * _CONTRACT_MULTIPLIER * leg_contracts
    return total
