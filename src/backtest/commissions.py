"""Commission modeling for the backtest engine — deliberately its own
tiny module (the spec names it as its own required file) even though
the formula itself is one line, so a future per-broker commission
schedule has an obvious, single place to live rather than being buried
inside `execution.py`.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CommissionSchedule:
    per_contract: float = 0.65
    per_leg_base: float = 0.0  # some brokers also charge a flat per-leg ticket fee; 0 by default


def calculate_commission(*, contracts: int, num_legs: int, schedule: CommissionSchedule) -> float:
    if contracts < 0:
        raise ValueError("contracts cannot be negative")
    if num_legs < 1:
        raise ValueError("num_legs must be at least 1")
    return contracts * num_legs * (schedule.per_contract) + num_legs * schedule.per_leg_base
