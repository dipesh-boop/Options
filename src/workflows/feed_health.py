"""Stages 1-2 of `/morning-scan`: verify market-data feeds, then verify
data freshness.

This module judges results a caller already attempted to fetch — it
does not own provider lifecycle or perform any I/O itself, the same
separation `src.data.provider.MarketDataProvider` keeps between
"normalizing what a provider said" and "fetching it." A future
scheduler/CLI wires real `MarketDataProvider.get_option_chain` calls to
`fetch_results` (catching `ProviderError` per symbol); this module only
ever sees the outcome.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from src.data.option_chain import OptionChain
from src.data.provider import DEFAULT_MAX_QUOTE_AGE, FreshnessStatus


@dataclass(frozen=True)
class FeedHealthResult:
    symbol: str
    healthy: bool
    detail: str


@dataclass(frozen=True)
class FeedHealthReport:
    results: tuple[FeedHealthResult, ...]

    @property
    def all_healthy(self) -> bool:
        return all(r.healthy for r in self.results)

    @property
    def unhealthy_symbols(self) -> tuple[str, ...]:
        return tuple(r.symbol for r in self.results if not r.healthy)


def verify_market_data_feeds(fetch_results: dict[str, OptionChain | Exception]) -> FeedHealthReport:
    """`fetch_results` maps a universe symbol to either its successfully
    fetched `OptionChain` or the exception raised while fetching it — a
    symbol whose fetch raised is unhealthy; nothing here retries or
    silently substitutes stale/cached data for a failure."""
    results = tuple(
        FeedHealthResult(symbol=symbol, healthy=False, detail=f"{type(outcome).__name__}: {outcome}")
        if isinstance(outcome, Exception)
        else FeedHealthResult(symbol=symbol, healthy=True, detail=f"ok ({len(outcome.contracts)} contract(s))")
        for symbol, outcome in fetch_results.items()
    )
    return FeedHealthReport(results=results)


@dataclass(frozen=True)
class FreshnessResult:
    symbol: str
    status: FreshnessStatus
    age_seconds: float


@dataclass(frozen=True)
class FreshnessReport:
    results: tuple[FreshnessResult, ...]

    @property
    def all_fresh(self) -> bool:
        return all(r.status == FreshnessStatus.FRESH for r in self.results)

    @property
    def stale_symbols(self) -> tuple[str, ...]:
        return tuple(r.symbol for r in self.results if r.status == FreshnessStatus.STALE)


def verify_data_freshness(
    chains: dict[str, OptionChain], as_of: datetime, max_age: timedelta = DEFAULT_MAX_QUOTE_AGE
) -> FreshnessReport:
    """Only successfully-fetched chains reach here — a feed that failed
    entirely (stage 1) has no timestamp to check for freshness (stage 2)
    and is already reported unhealthy by `verify_market_data_feeds`."""
    results = tuple(
        FreshnessResult(symbol=symbol, status=chain.freshness_status(as_of, max_age), age_seconds=chain.age(as_of).total_seconds())
        for symbol, chain in chains.items()
    )
    return FreshnessReport(results=results)
