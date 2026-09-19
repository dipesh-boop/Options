"""Canonical earnings-event schema and the earnings-window check the
Strategy Screener needs to enforce the platform's "no earnings-window
entries" universe rule (ARCHITECTURE.md §1).

An earnings calendar is itself a form of market data with a freshness
concern: a six-month-old confirmed date is not something to trade
around with confidence, and an unconfirmed/estimated date is weaker
evidence than a company-confirmed one — both are tracked explicitly
rather than conflated.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from enum import Enum

from pydantic import Field

from src.data.provider import TimestampedModel


class EarningsTiming(str, Enum):
    BEFORE_MARKET = "before_market"
    AFTER_MARKET = "after_market"
    UNKNOWN = "unknown"


class EarningsEvent(TimestampedModel):
    """A normalized earnings-date record. `timestamp`/`source`
    (inherited from `TimestampedModel`) describe when *this calendar
    entry* was fetched/confirmed — not the earnings date itself — so its
    own freshness can be checked independently of `earnings_date`."""

    symbol: str = Field(min_length=1, max_length=10)
    earnings_date: date
    timing: EarningsTiming = EarningsTiming.UNKNOWN
    confirmed: bool = False


class EarningsCalendarProviderError(RuntimeError):
    pass


def is_within_earnings_window(event: EarningsEvent, expiration: date, window_days: int = 7) -> bool:
    """True if `expiration` falls within `window_days` of the earnings
    date, in either direction — the check a Strategy Screener runs
    before allowing a candidate expiring near an earnings date into the
    eligible set, per the platform's "no earnings-window entries" rule.
    Symmetric (before *and* after) since a position opened just before
    earnings and one opened just after but still exposed to the
    aftermath are both in scope for exclusion."""
    if window_days < 0:
        raise ValueError("window_days cannot be negative")
    delta_days = abs((expiration - event.earnings_date).days)
    return delta_days <= window_days


class EarningsCalendarProvider(ABC):
    """Every concrete earnings calendar source implements this. Returns
    canonical `EarningsEvent` objects only — never a raw vendor
    response."""

    @abstractmethod
    async def get_next_earnings(self, symbol: str) -> EarningsEvent | None:
        raise NotImplementedError
