"""Deterministic market-data provider health check (Part 8 of the
Alpaca pre-validation amendment).

The owner should never have to guess what kind of data the system is
using. `check_provider_health` probes the configured provider with one
real (or, for `mock`, synthetic) request each for equity and options
data, and reports back a small set of unambiguous facts: which provider
is selected, whether authentication succeeded, whether equity/options
data is actually available, which feed served it (OPRA vs indicative,
SIP vs IEX vs delayed, or "n/a" for a provider with no feed concept),
whether the market is currently open (`src.data.market_calendar`), how
fresh the data is, and the exact error text for anything that failed.
This is read-only and side-effect-free: it never places any order,
and — for `alpaca` — never imports `alpaca.trading`, only ever
delegating to `AlpacaMarketDataProvider`/`AlpacaHistoricalDataProvider`
which already never do.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from src.data.factory import SOURCE_ALPACA, SOURCE_MOCK, DataProviderConfigError, DataProviderSelection, get_configured_market_data_provider
from src.data.market_calendar import market_status

_PROBE_SYMBOL = "SPY"


class DataConnectionStatus:
    REAL_DATA_CONNECTED = "REAL_DATA_CONNECTED"
    MOCK_DATA = "MOCK_DATA"
    REAL_DATA_UNAVAILABLE = "REAL_DATA_UNAVAILABLE"


@dataclass(frozen=True)
class ProviderHealthReport:
    provider_selected: str
    connection_status: str  # DataConnectionStatus value

    authenticated: bool | None  # None when not applicable (mock)
    equity_data_available: bool
    options_data_available: bool

    equity_feed_type: str | None  # e.g. "alpaca_sip", "alpaca_iex", "mock", None if unavailable
    options_feed_type: str | None  # e.g. "alpaca_opra", "alpaca_indicative", "mock", None if unavailable
    opra_entitled: bool | None  # True/False when determinable (alpaca), None otherwise

    market_open: bool
    market_status_detail: str

    checked_at: datetime
    last_successful_fetch_at: datetime | None
    equity_quote_age_seconds: float | None

    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_opra(self) -> bool:
        return self.options_feed_type is not None and self.options_feed_type.endswith("_opra")

    @property
    def is_indicative_or_delayed(self) -> bool:
        return self.options_feed_type is not None and (
            self.options_feed_type.endswith("_indicative") or "delayed" in self.options_feed_type
        )


async def check_provider_health(
    *, now: datetime, selection: DataProviderSelection | None = None, probe_symbol: str = _PROBE_SYMBOL,
) -> ProviderHealthReport:
    selection = selection or DataProviderSelection()
    provider_name = selection.data_provider.lower()
    errors: list[str] = []

    status = market_status(now)
    market_open = status.is_market_open
    market_detail = "market open" if market_open else f"market closed (next open: {status.next_market_open.isoformat()})"

    try:
        provider = get_configured_market_data_provider(selection)
    except DataProviderConfigError as exc:
        return ProviderHealthReport(
            provider_selected=provider_name, connection_status=DataConnectionStatus.REAL_DATA_UNAVAILABLE,
            authenticated=False, equity_data_available=False, options_data_available=False,
            equity_feed_type=None, options_feed_type=None, opra_entitled=None,
            market_open=market_open, market_status_detail=market_detail,
            checked_at=now, last_successful_fetch_at=None, equity_quote_age_seconds=None,
            errors=(f"provider configuration error: {exc}",),
        )

    authenticated: bool | None = True if provider_name != SOURCE_MOCK else None
    equity_available = False
    options_available = False
    equity_feed_type: str | None = None
    options_feed_type: str | None = None
    last_success: datetime | None = None
    equity_age: float | None = None

    try:
        quote = await provider.get_underlying_quote(probe_symbol)
        equity_available = True
        equity_feed_type = quote.source
        last_success = now
        equity_age = quote.age(now).total_seconds()
    except Exception as exc:  # noqa: BLE001 -- every failure mode is reported, not swallowed
        errors.append(f"equity probe failed: {exc}")
        if _looks_like_auth_failure(exc):
            authenticated = False

    try:
        chain = await provider.get_option_chain(probe_symbol)
        options_available = True
        options_feed_type = chain.source
        if last_success is None:
            last_success = now
    except Exception as exc:  # noqa: BLE001
        errors.append(f"options probe failed: {exc}")
        if _looks_like_auth_failure(exc):
            authenticated = False

    opra_entitled: bool | None = None
    if provider_name == SOURCE_ALPACA:
        if options_feed_type is not None:
            opra_entitled = options_feed_type.endswith("_opra")
        elif any("entitl" in e.lower() or "subscription" in e.lower() for e in errors):
            opra_entitled = False

    if provider_name == SOURCE_MOCK:
        connection_status = DataConnectionStatus.MOCK_DATA
    elif equity_available and options_available:
        connection_status = DataConnectionStatus.REAL_DATA_CONNECTED
    else:
        connection_status = DataConnectionStatus.REAL_DATA_UNAVAILABLE

    return ProviderHealthReport(
        provider_selected=provider_name, connection_status=connection_status,
        authenticated=authenticated, equity_data_available=equity_available, options_data_available=options_available,
        equity_feed_type=equity_feed_type, options_feed_type=options_feed_type, opra_entitled=opra_entitled,
        market_open=market_open, market_status_detail=market_detail,
        checked_at=now, last_successful_fetch_at=last_success, equity_quote_age_seconds=equity_age,
        errors=tuple(errors),
    )


def _looks_like_auth_failure(exc: Exception) -> bool:
    name = type(exc).__name__
    return "Authentication" in name or "auth" in str(exc).lower()
