"""DTE bookkeeping and management-rule triggers — the questions the
simulator asks about every open position on every simulated day, before
ever touching a quote: how many days remain, has the configured
management DTE been reached, has it expired outright today.
"""
from __future__ import annotations

from datetime import date

from src.backtest.simulator import BacktestPosition


def days_to_expiration(position: BacktestPosition, as_of: date) -> int:
    return (position.expiration - as_of).days


def is_expiring_today(position: BacktestPosition, as_of: date) -> bool:
    return position.expiration == as_of


def management_dte_reached(position: BacktestPosition, as_of: date) -> bool:
    """True once the position has drifted down to (or past) its
    configured management DTE — the "review/close/roll at N DTE" rule
    every ticket in this codebase already carries as a field
    (`management_dte`), now actually checked against simulated time."""
    return days_to_expiration(position, as_of) <= position.management_dte


def profit_target_reached(position: BacktestPosition, current_realistic_value_to_close: float, max_profit_per_contract: float) -> bool:
    """`current_realistic_value_to_close` is the net debit (positive
    number) it would currently cost to close the position — realized
    profit so far is `max_profit_per_contract - current_realistic_value_to_close`
    for a credit strategy. True once that's at or above the configured
    fraction of max profit."""
    if max_profit_per_contract <= 0:
        return False
    realized_fraction = 1.0 - (current_realistic_value_to_close / max_profit_per_contract)
    return realized_fraction >= position.profit_target_pct
