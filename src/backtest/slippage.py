"""Realistic fill simulation for the backtest engine.

Reuses `src.brokers.paper`'s fill-price math (`FillModel`, `compute_fill`,
`LegQuote`) directly rather than re-implementing the same "BID/ASK/MID/
MID_WITH_SLIPPAGE/LIQUIDITY_ADJUSTED" model a second time — a backtest
fill and a paper-trading fill answer the identical question ("what price
would this order actually have gotten, given the quoted market"), just
from a historical quote instead of a live one. This module is the
`HistoricalOptionQuote` -> `LegQuote` adapter and nothing else; the
fill-price/quantity logic itself lives in one place, not two.

"Never automatically assume midpoint fills": every historical position
this package prices gets *two* results side by side —
`fill_realistic_entry`/`fill_realistic_exit` under whatever
`PaperBrokerConfig.fill_model` the backtest is configured with, and
`fill_theoretical` (always plain `FillModel.MID`, no slippage) for the
"theoretical midpoint return" line `src.backtest.engine` must report
separately from realistic execution.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.brokers.base import OrderAction, OrderLeg
from src.brokers.paper import FillModel, LegQuote, PaperBrokerConfig, compute_fill, fillable_quantity, price_satisfies_limit
from src.backtest.simulator import BacktestLeg, HistoricalOptionQuote

_THEORETICAL_CONFIG = PaperBrokerConfig(fill_model=FillModel.MID, commission_per_contract=0.0, slippage_bps=0.0)


class NoFillError(RuntimeError):
    """Raised when the historical quote's own liquidity can't support
    the requested size at any acceptable price — a backtest must not
    silently assume a fill happened when it wouldn't have."""


def _to_leg_quote(quote: HistoricalOptionQuote) -> LegQuote:
    return LegQuote(
        symbol=f"{quote.underlying}{quote.expiration.isoformat()}{quote.right.value}{quote.strike}",
        bid=quote.bid,
        ask=quote.ask,
        volume=quote.volume,
        open_interest=quote.open_interest,
        multiplier=100,
    )


def _to_order_leg(leg: BacktestLeg, expiration) -> OrderLeg:
    return OrderLeg(
        symbol=f"{leg.right.value}{leg.strike}{expiration.isoformat()}",
        right=leg.right,
        strike=leg.strike,
        expiration=expiration,
        action=OrderAction.SELL if leg.side == "sell" else OrderAction.BUY,
        quantity=leg.quantity_ratio,
    )


@dataclass(frozen=True)
class HistoricalFill:
    net_price: float  # signed: positive = net credit, negative = net debit
    filled_contracts: int
    min_leg_volume: int
    min_leg_open_interest: int
    max_leg_spread_pct: float


def fill_realistic(
    legs: list[BacktestLeg],
    expiration,
    quotes: list[HistoricalOptionQuote],
    requested_contracts: int,
    limit_price: float,
    config: PaperBrokerConfig,
) -> HistoricalFill:
    """The realistic-execution counterpart: same fill model math as
    `PaperBroker`, applied to a historical quote instead of a live
    market-data feed."""
    order_legs = [_to_order_leg(leg, expiration) for leg in legs]
    leg_quotes = [_to_leg_quote(q) for q in quotes]
    fill_quote = compute_fill(order_legs, leg_quotes, config)
    if not price_satisfies_limit(fill_quote, limit_price):
        raise NoFillError(
            f"requested limit {limit_price} not satisfied by simulated net price {fill_quote.net_price:.4f}"
        )
    qty = fillable_quantity(requested_contracts, fill_quote, config)
    if qty <= 0:
        raise NoFillError("insufficient simulated liquidity to fill any contracts")
    return HistoricalFill(
        net_price=fill_quote.net_price,
        filled_contracts=qty,
        min_leg_volume=fill_quote.min_leg_volume,
        min_leg_open_interest=fill_quote.min_leg_open_interest,
        max_leg_spread_pct=fill_quote.max_leg_spread_pct,
    )


def mark_to_market(legs: list[BacktestLeg], expiration, quotes: list[HistoricalOptionQuote], config: PaperBrokerConfig) -> float:
    """The current net price to close `legs` right now, under
    `config`'s fill model — used for profit-target checks and
    valuation, never to place an actual order (no limit-price gate, no
    quantity cap: this answers "what would it cost," not "did it
    fill")."""
    order_legs = [_to_order_leg(leg, expiration) for leg in legs]
    leg_quotes = [_to_leg_quote(q) for q in quotes]
    fill_quote = compute_fill(order_legs, leg_quotes, config)
    return fill_quote.net_price


def fill_theoretical(legs: list[BacktestLeg], expiration, quotes: list[HistoricalOptionQuote], contracts: int) -> float:
    """Always the plain midpoint, always fully filled at the requested
    size, no slippage, no commission, no liquidity cap — the
    "theoretical midpoint return" baseline `src.backtest.engine` must
    report *alongside*, never *instead of*, the realistic figure."""
    order_legs = [_to_order_leg(leg, expiration) for leg in legs]
    leg_quotes = [_to_leg_quote(q) for q in quotes]
    fill_quote = compute_fill(order_legs, leg_quotes, _THEORETICAL_CONFIG)
    return fill_quote.net_price
