"""Selects which `MarketDataProvider` implementation is active, from one
explicit config value — `OPTIONS_AGENT_DATA_PROVIDER` (`mock` / `ibkr` /
`alpaca`) — so nothing in this codebase has to guess or silently default
to a different provider than the one actually configured.

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

_KNOWN_PROVIDERS = frozenset({SOURCE_MOCK, SOURCE_IBKR, SOURCE_ALPACA})


class DataProviderConfigError(RuntimeError):
    """Raised for an unknown `OPTIONS_AGENT_DATA_PROVIDER` value, or
    when building the configured provider itself fails (missing
    credentials, bad config) -- always fails closed, never silently
    substitutes a different provider than the one configured."""


class DataProviderSelection(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPTIONS_AGENT_")

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
    `mock`/`alpaca`; for `ibkr` it is `IBKRBroker`, which implements the
    same two methods structurally but is a `Broker` subclass, not a
    `MarketDataProvider` -- a pre-existing design choice in
    `src.brokers.ibkr`, unchanged by this module, not repeated here)."""
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
    # provider_name == SOURCE_ALPACA
    try:
        from src.data.alpaca_provider import AlpacaMarketDataProvider
    except ImportError as exc:  # pragma: no cover - alpaca-py always installed in this repo
        raise DataProviderConfigError(f"alpaca provider unavailable: {exc}") from exc
    try:
        return AlpacaMarketDataProvider()
    except Exception as exc:  # noqa: BLE001 -- any construction failure (bad creds/config) fails closed
        raise DataProviderConfigError(f"failed to construct Alpaca market data provider: {exc}") from exc
