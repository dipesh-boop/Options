"""Abstract market data provider interface.

Both the mock and IBKR providers implement this so the rest of the app
(analytics, API routes) never needs to know which one is active.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from app.data.models import OptionChain


class MarketDataProvider(ABC):
    @abstractmethod
    async def get_option_chain(self, symbol: str) -> OptionChain:
        """Return the current option chain (all expiries within the
        configured window) plus the underlying quote for `symbol`."""
        raise NotImplementedError

    async def close(self) -> None:  # pragma: no cover - default no-op
        """Release any held connections."""
        return None
