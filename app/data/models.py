"""Core data types shared by providers, analytics, and the API layer."""
from __future__ import annotations

from datetime import date
from enum import Enum

from pydantic import BaseModel


class Right(str, Enum):
    CALL = "C"
    PUT = "P"


class UnderlyingQuote(BaseModel):
    symbol: str
    price: float
    historical_volatility_30d: float  # annualized, e.g. 0.28 for 28%


class OptionContract(BaseModel):
    symbol: str  # underlying symbol
    expiry: date
    strike: float
    right: Right
    bid: float
    ask: float
    last: float
    volume: int
    open_interest: int

    @property
    def mid(self) -> float:
        if self.bid <= 0 and self.ask <= 0:
            return self.last
        return round((self.bid + self.ask) / 2, 4)


class OptionChain(BaseModel):
    underlying: UnderlyingQuote
    contracts: list[OptionContract]
