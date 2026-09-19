"""Canonical historical underlying price data, for backtesting.

Historical *options chain* data (as opposed to underlying OHLCV bars) is
explicitly not covered here — ARCHITECTURE.md §12 flags the historical
options data vendor as an open question, not yet decided, and this
module doesn't quietly assume an answer. What's here (underlying bars,
the abstract provider interface, and the no-lookahead guard) is
vendor-agnostic and useful regardless of that decision.

A `HistoricalBar` deliberately does not inherit `TimestampedModel` /
carry live-data "freshness": a bar for 2024-03-15 is exactly as valid
today as it was the day after that date. The integrity property that
matters for historical data is point-in-time correctness — a backtest
must never see a bar dated after its simulated "as of" date
(ARCHITECTURE.md §9, §11: "strict point-in-time data discipline"). That's
what `assert_no_lookahead` enforces.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from pydantic import Field, model_validator

from src.data.provider import StrictModel


class HistoricalBar(StrictModel):
    """One OHLCV bar for `symbol` on `bar_date`."""

    symbol: str = Field(min_length=1, max_length=10)
    bar_date: date
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: int = Field(ge=0)
    source: str = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def _high_low_consistency(self) -> "HistoricalBar":
        if self.high < self.low:
            raise ValueError(f"high ({self.high}) cannot be below low ({self.low})")
        if not (self.low <= self.open <= self.high):
            raise ValueError(f"open ({self.open}) must be within [low, high] ([{self.low}, {self.high}])")
        if not (self.low <= self.close <= self.high):
            raise ValueError(f"close ({self.close}) must be within [low, high] ([{self.low}, {self.high}])")
        return self


class HistoricalDataProvider(ABC):
    """Every concrete historical data source implements this. Returns
    canonical `HistoricalBar` objects only — never a raw vendor
    response."""

    @abstractmethod
    async def get_bars(self, symbol: str, start: date, end: date) -> list[HistoricalBar]:
        raise NotImplementedError


def assert_no_lookahead(bars: list[HistoricalBar], as_of: date) -> list[HistoricalBar]:
    """Raises ValueError if any bar is dated after `as_of` — the
    concrete no-lookahead guard a future backtest engine must run every
    bar list through before using it, so a strategy can never see data
    from its own future. Returns `bars` unchanged if none violate this."""
    future_bars = [b for b in bars if b.bar_date > as_of]
    if future_bars:
        offending_dates = sorted({b.bar_date.isoformat() for b in future_bars})
        raise ValueError(
            f"lookahead violation: {len(future_bars)} bar(s) dated after as_of={as_of.isoformat()}: "
            f"{offending_dates}"
        )
    return bars
