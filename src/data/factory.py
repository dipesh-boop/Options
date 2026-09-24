"""Selects which `MarketDataProvider` implementation is active, from one
explicit config value — `OPTIONS_AGENT_DATA_PROVIDER` (`mock` / `ibkr` /
`alpaca` / `tradier`) — so nothing in this codebase has to guess or
silently default to a different provider than the one actually
configured.

Nothing before this module (Step 22.1) actually selected a provider at
runtime: `src.dashboard.service`/`src.workflows.feed_health` both take
already-fetched `OptionChain`s as plain parameters (see their own
docstrings — "this module has no market-data connection of its own"),
and `/morning-scan` is a Claude Code skill whose runner is expected to
construct and call a provider directly. This module is that one
explicit, documented construction point, so a caller (a script, the
`/morning-scan` skill, a future scheduler) never has to hand-roll
"which provider class do I import for this config value" logic itself.

`get_configured_market_data_provider` deliberately does NOT catch a
misconfiguration and fall back to `mock` — Part 7's own explicit
requirement ("Do NOT make a real provider silently default to mock if
configuration fails") — an invalid/incomplete `ibkr`/`alpaca`
configuration raises here, the same fail-closed idiom every other
config loader in this codebase uses.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

from src.data.provider import MarketDataProvider

SOURCE_MOCK = "mock"
SOURCE_IBKR = "ibkr"
SOURCE_ALPACA = "alpaca"
SOURCE_TRADIER = "tradier"

_KNOWN_PROVIDERS = frozenset({SOURCE_MOCK, SOURCE_IBKR, SOURCE_ALPACA, SOURCE_TRADIER})


class DataProviderConfigError(RuntimeError):
    """Raised for an unknown `OPTIONS_AGENT_DATA_PROVIDER` value, or
    when building the configured provider itself fails (missing
    credentials, bad config) -- always fails closed, never silently
    substitutes a different provider than the one configured."""


_OFFICIAL_VALIDATION_PROVIDER = SOURCE_TRADIER
# Tradier's own production market-data host (`.env.example`'s own
# documented default) -- distinct from Tradier's separate sandbox host,
# which serves delayed/simulated data and is legitimate for local
# development but not for the official 90-day validation cycle.
_OFFICIAL_VALIDATION_TRADIER_BASE_URL = "https://api.tradier.com/v1"


class OfficialProviderPreflightError(RuntimeError):
    """Step 22.6 (PAPER_TRADING_V1.4.5): raised when the configured
    market-data provider is not Tradier's own production market-data
    endpoint with a token configured -- the one provider configuration
    the official, state-mutating 90-day validation cycle is permitted to
    run against. Distinct from `DataProviderConfigError` (which reports
    a provider that's simply broken/misconfigured for ANY use): this is
    a policy check specific to the official validation runner, verifying
    configuration VALUES only -- it never constructs a real provider
    instance, makes no network call, and its own message never contains
    a token (only a yes/no presence check)."""


def verify_official_provider_is_tradier_production(
    selection: DataProviderSelection | None = None,
    tradier_config: TradierConfig | None = None,
) -> None:
    """Fails closed, before any official-validation-cycle state
    mutation, unless the configured provider is exactly Tradier's
    production market-data endpoint with a token configured. Read-only:
    reads only `DataProviderSelection`/`TradierConfig` (both plain
    environment-variable reads via `pydantic_settings.BaseSettings`) --
    never constructs `TradierMarketDataProvider` itself (no `httpx`
    client is built, no network request is made, no market data is
    fetched). This is deliberately a CONFIGURATION preflight, distinct
    from PROVIDER CONNECTIVITY (the real market-data fetch that may
    only happen once this check has already passed): a Tradier API
    outage discovered during that later fetch is legitimately recorded
    as a degraded cycle by the existing architecture (Part 7's
    isolation doctrine); an explicitly disallowed provider (`mock`,
    `alpaca`, `ibkr`, an unknown value, or Tradier's own sandbox host)
    is refused here instead, before that fetch, and before any
    validation state -- a cycle record, a daily snapshot, a candidate
    expiry, a paper-account bootstrap -- is touched.

    Raises `OfficialProviderPreflightError` on failure; returns `None`
    (no value) on success."""
    from src.data.tradier_provider import TradierConfig as _TradierConfig

    selection = selection or DataProviderSelection()
    provider_name = selection.data_provider.lower()
    if provider_name != _OFFICIAL_VALIDATION_PROVIDER:
        raise OfficialProviderPreflightError(
            f"official validation requires OPTIONS_AGENT_DATA_PROVIDER={_OFFICIAL_VALIDATION_PROVIDER!r} "
            f"(Tradier production market data) -- currently configured as {selection.data_provider!r}. "
            "Refusing to run an official cycle against mock/synthetic/non-Tradier data."
        )

    config = tradier_config or _TradierConfig()
    if not config.token:
        raise OfficialProviderPreflightError(
            "official validation requires OPTIONS_AGENT_TRADIER_TOKEN to be set -- no Tradier token is "
            "configured (see .env.example). Refusing to run an official cycle with no Tradier credentials."
        )
    normalized_base_url = config.base_url.rstrip("/")
    if normalized_base_url != _OFFICIAL_VALIDATION_TRADIER_BASE_URL:
        raise OfficialProviderPreflightError(
            "official validation requires Tradier's production market-data endpoint "
            f"({_OFFICIAL_VALIDATION_TRADIER_BASE_URL!r}) -- the configured OPTIONS_AGENT_TRADIER_BASE_URL "
            f"{config.base_url!r} does not match (this looks like Tradier's sandbox/delayed-data host, or "
            "a non-standard override). Refusing to run an official cycle against non-production data."
        )


class DataProviderSelection(BaseSettings):
    # Step 22.8: env_ignore_empty=True -- the shipped .env template
    # leaves this (and every other optional variable) blank by
    # convention ("leave unset for the default"); without this, a
    # blank OPTIONS_AGENT_DATA_PROVIDER would set data_provider="" and
    # break provider resolution instead of falling through to "mock".
    model_config = SettingsConfigDict(env_prefix="OPTIONS_AGENT_", env_ignore_empty=True)

    data_provider: str = "mock"


class _MockMarketDataProvider(MarketDataProvider):
    """Deterministic, offline, synthetic provider -- exists so the
    dashboard/tests can be exercised without any real or paper-broker
    connection. Distinct from (and much simpler than) the legacy,
    unreferenced `app/data/mock_provider.py` prototype -- see
    ARCHITECTURE.md/progress.md's Step 22.1 note on that dead code."""

    async def get_underlying_quote(self, symbol: str):
        from datetime import datetime, timezone

        from src.data.quotes import UnderlyingQuote

        return UnderlyingQuote(
            symbol=symbol.upper(), bid=99.5, ask=100.5, last=100.0, volume=0,
            timestamp=datetime.now(timezone.utc), source=SOURCE_MOCK,
        )

    async def get_option_chain(self, symbol: str):
        from datetime import datetime, timezone

        from src.data.option_chain import OptionChain

        underlying = await self.get_underlying_quote(symbol)
        return OptionChain(underlying=underlying, contracts=[], timestamp=datetime.now(timezone.utc), source=SOURCE_MOCK)


def get_configured_market_data_provider(selection: DataProviderSelection | None = None):
    """Returns a provider exposing `get_underlying_quote`/
    `get_option_chain` (a genuine `MarketDataProvider` instance for
    `mock`/`alpaca`/`tradier`; for `ibkr` it is `IBKRBroker`, which
    implements the same two methods structurally but is a `Broker`
    subclass, not a `MarketDataProvider` -- a pre-existing design choice
    in `src.brokers.ibkr`, unchanged by this module, not repeated here).
    A caller wanting Tradier's own additional methods (batch quotes,
    priority-tagged requests, rate-limit state) should import
    `TradierMarketDataProvider` directly rather than going through this
    generic factory, exactly like `AlpacaMarketDataProvider`'s own
    Alpaca-specific extras already work today."""
    selection = selection or DataProviderSelection()
    provider_name = selection.data_provider.lower()
    if provider_name not in _KNOWN_PROVIDERS:
        raise DataProviderConfigError(
            f"OPTIONS_AGENT_DATA_PROVIDER={selection.data_provider!r} is not one of {sorted(_KNOWN_PROVIDERS)}"
        )
    if provider_name == SOURCE_MOCK:
        return _MockMarketDataProvider()
    if provider_name == SOURCE_IBKR:
        try:
            from src.brokers.ibkr import IBKRBroker

            return IBKRBroker()  # implements get_underlying_quote/get_option_chain structurally
        except Exception as exc:  # noqa: BLE001 -- e.g. ib_insync not installed, or a non-paper port configured
            raise DataProviderConfigError(f"ibkr provider unavailable: {exc}") from exc
    if provider_name == SOURCE_ALPACA:
        try:
            from src.data.alpaca_provider import AlpacaMarketDataProvider
        except ImportError as exc:  # pragma: no cover - alpaca-py always installed in this repo
            raise DataProviderConfigError(f"alpaca provider unavailable: {exc}") from exc
        try:
            return AlpacaMarketDataProvider()
        except Exception as exc:  # noqa: BLE001 -- any construction failure (bad creds/config) fails closed
            raise DataProviderConfigError(f"failed to construct Alpaca market data provider: {exc}") from exc
    # provider_name == SOURCE_TRADIER (Step 22.4) -- the only remaining member of _KNOWN_PROVIDERS.
    try:
        from src.data.tradier_provider import TradierMarketDataProvider
    except ImportError as exc:  # pragma: no cover - httpx always installed in this repo
        raise DataProviderConfigError(f"tradier provider unavailable: {exc}") from exc
    try:
        return TradierMarketDataProvider()
    except Exception as exc:  # noqa: BLE001 -- any construction failure (missing token/bad config) fails closed
        raise DataProviderConfigError(f"failed to construct Tradier market data provider: {exc}") from exc
