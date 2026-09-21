"""Combines `slippage.py` (fill price) and `commissions.py` (fees) into
one execution result — used for both opening and early-closing a
position. Expiration/assignment settlement is a different kind of event
(no order is placed against a live quote) and lives in `assignment.py`
instead.

Every call produces a *realistic* and a *theoretical* result side by
side, never one without the other — the concrete mechanism behind
`src.backtest.engine` being able to report "theoretical midpoint return"
and "realistic execution return" as two genuinely independently-computed
series, not one derived from the other after the fact.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from src.backtest.commissions import CommissionSchedule, calculate_commission
from src.backtest.simulator import BacktestLeg, HistoricalOptionQuote
from src.backtest.slippage import fill_realistic, fill_theoretical
from src.brokers.paper import PaperBrokerConfig


@dataclass(frozen=True)
class ExecutionResult:
    realistic_price: float
    theoretical_price: float
    filled_contracts: int
    commission: float
    min_leg_volume: int
    min_leg_open_interest: int
    max_leg_spread_pct: float


def flip_legs(legs: list[BacktestLeg]) -> list[BacktestLeg]:
    """A closing order trades the opposite side of each leg — selling
    back what was bought, buying back what was sold — with the same
    strikes/rights."""
    return [
        BacktestLeg(
            right=leg.right, strike=leg.strike, side=("buy" if leg.side == "sell" else "sell"),
            quantity_ratio=leg.quantity_ratio,
        )
        for leg in legs
    ]


def execute_entry(
    *,
    legs: list[BacktestLeg],
    expiration: date,
    quotes: list[HistoricalOptionQuote],
    requested_contracts: int,
    limit_price: float,
    fill_config: PaperBrokerConfig,
    commission_schedule: CommissionSchedule,
) -> ExecutionResult:
    fill = fill_realistic(legs, expiration, quotes, requested_contracts, limit_price, fill_config)
    theoretical_price = fill_theoretical(legs, expiration, quotes, requested_contracts)
    commission = calculate_commission(contracts=fill.filled_contracts, num_legs=len(legs), schedule=commission_schedule)
    return ExecutionResult(
        realistic_price=fill.net_price,
        theoretical_price=theoretical_price,
        filled_contracts=fill.filled_contracts,
        commission=commission,
        min_leg_volume=fill.min_leg_volume,
        min_leg_open_interest=fill.min_leg_open_interest,
        max_leg_spread_pct=fill.max_leg_spread_pct,
    )


def execute_exit(
    *,
    legs: list[BacktestLeg],
    expiration: date,
    quotes: list[HistoricalOptionQuote],
    contracts: int,
    limit_price: float,
    fill_config: PaperBrokerConfig,
    commission_schedule: CommissionSchedule,
) -> ExecutionResult:
    """`legs` here are the *opening* legs (as still held); this function
    builds the closing order itself via `flip_legs`. `limit_price` is
    the maximum acceptable net debit to buy the structure back (or
    minimum credit, for the rare case of closing a debit position) —
    same limit-order semantics `src.brokers.paper.price_satisfies_limit`
    already enforces."""
    closing_legs = flip_legs(legs)
    fill = fill_realistic(closing_legs, expiration, quotes, contracts, limit_price, fill_config)
    theoretical_price = fill_theoretical(closing_legs, expiration, quotes, contracts)
    commission = calculate_commission(contracts=fill.filled_contracts, num_legs=len(legs), schedule=commission_schedule)
    return ExecutionResult(
        realistic_price=fill.net_price,
        theoretical_price=theoretical_price,
        filled_contracts=fill.filled_contracts,
        commission=commission,
        min_leg_volume=fill.min_leg_volume,
        min_leg_open_interest=fill.min_leg_open_interest,
        max_leg_spread_pct=fill.max_leg_spread_pct,
    )
