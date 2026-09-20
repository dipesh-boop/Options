"""FIDELITY EXECUTION QUALITY section of `/weekly-review` (Step 16):
compare recommended limit, market midpoint at recommendation, actual
Fidelity fill, and the delay between them; compute average/median
slippage and slippage broken down by strategy, underlying, and time of
day.

No new schema is needed to carry "recommended limit" and "market
midpoint when recommended" — `FidelityTradeTicket.limit_price`/`net_mid`
already store exactly those, captured at ticket-generation time
(Step 8/12). "Actual Fidelity fill" and "time... execution" are
`ExecutionConfirmation.fill_price`/`confirmed_at` — the only evidence
this codebase ever accepts for a real fill (`src.brokers.fidelity
.confirm_fill`). This module is a pure aggregator over
`(ticket, confirmation)` pairs a human has already supplied via that
existing confirmation flow; it captures no new data and confirms
nothing itself.

Slippage is expressed as `recommended_limit - actual_fill_price`
(positive = worse), which assumes a net-credit recommendation — true of
all three of this platform's currently approved strategies
(cash-secured put, covered call, put credit spread all collect a net
credit at entry).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from src.brokers.fidelity import ExecutionConfirmation, FidelityTradeTicket

TimeOfDayBucket = Literal["pre_market", "morning", "midday", "afternoon", "after_hours"]


def _time_of_day_bucket(dt: datetime) -> TimeOfDayBucket:
    """Buckets by the hour of `dt` exactly as given — the caller is
    responsible for supplying a timestamp in whatever single timezone
    this platform's reporting standardizes on (every timestamp in this
    codebase is timezone-aware; this function does no conversion)."""
    hour = dt.hour
    if hour < 9:
        return "pre_market"
    if hour < 11:
        return "morning"
    if hour < 13:
        return "midday"
    if hour < 16:
        return "afternoon"
    return "after_hours"


@dataclass(frozen=True)
class FidelitySlippageRecord:
    proposal_id: str
    ticker: str
    strategy: str
    recommended_limit: float
    market_midpoint_at_recommendation: float
    actual_fill_price: float
    recommended_at: datetime
    executed_at: datetime
    slippage: float
    delay_seconds: float
    time_of_day: TimeOfDayBucket


def build_slippage_record(ticket: FidelityTradeTicket, confirmation: ExecutionConfirmation) -> FidelitySlippageRecord:
    if confirmation.confirmed_at < ticket.timestamp:
        raise ValueError(
            f"confirmation ({confirmation.confirmed_at.isoformat()}) cannot predate the recommendation "
            f"({ticket.timestamp.isoformat()})"
        )
    recommended_limit = abs(ticket.limit_price)
    slippage = recommended_limit - confirmation.fill_price
    delay_seconds = (confirmation.confirmed_at - ticket.timestamp).total_seconds()
    return FidelitySlippageRecord(
        proposal_id=ticket.risk_approval_id,
        ticker=ticket.ticker,
        strategy=ticket.strategy,
        recommended_limit=recommended_limit,
        market_midpoint_at_recommendation=ticket.net_mid,
        actual_fill_price=confirmation.fill_price,
        recommended_at=ticket.timestamp,
        executed_at=confirmation.confirmed_at,
        slippage=slippage,
        delay_seconds=delay_seconds,
        time_of_day=_time_of_day_bucket(confirmation.confirmed_at),
    )


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    return ordered[mid] if n % 2 == 1 else (ordered[mid - 1] + ordered[mid]) / 2.0


def _group_average(records: list[FidelitySlippageRecord], key) -> dict[str, float]:
    buckets: dict[str, list[float]] = {}
    for r in records:
        buckets.setdefault(key(r), []).append(r.slippage)
    return {k: _mean(v) for k, v in buckets.items()}


@dataclass(frozen=True)
class SlippageSummary:
    count: int
    average_slippage: float
    median_slippage: float
    by_strategy: dict[str, float]
    by_underlying: dict[str, float]
    by_time_of_day: dict[str, float]


def summarize_slippage(records: list[FidelitySlippageRecord]) -> SlippageSummary:
    if not records:
        return SlippageSummary(count=0, average_slippage=0.0, median_slippage=0.0, by_strategy={}, by_underlying={}, by_time_of_day={})
    values = [r.slippage for r in records]
    return SlippageSummary(
        count=len(records),
        average_slippage=_mean(values),
        median_slippage=_median(values),
        by_strategy=_group_average(records, lambda r: r.strategy),
        by_underlying=_group_average(records, lambda r: r.ticker),
        by_time_of_day=_group_average(records, lambda r: r.time_of_day),
    )
