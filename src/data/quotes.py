"""Canonical underlying (stock/ETF/index) quote schema."""
from __future__ import annotations

from pydantic import Field

from src.data.provider import TimestampedModel


class UnderlyingQuote(TimestampedModel):
    """A normalized underlying quote. Every provider adapter converts
    its raw response into this — never expose the raw response itself
    to any downstream consumer, including (eventually) the LLM layer."""

    symbol: str = Field(min_length=1, max_length=10)
    bid: float = Field(ge=0)
    ask: float = Field(ge=0)
    last: float = Field(ge=0)
    volume: int = Field(ge=0)

    @property
    def mid(self) -> float:
        if self.bid <= 0 and self.ask <= 0:
            return self.last
        return round((self.bid + self.ask) / 2, 4)
