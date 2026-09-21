"""Alpaca market-data-only provider (pre-validation amendment, "Step
22.1").

**MARKET DATA ONLY — never execution.** This module imports exclusively
from `alpaca.data.*` (Alpaca's read-only historical/market-data
clients). It NEVER imports `alpaca.trading` (the order-submission
client) anywhere, and defines no order-submission, cancellation, or
modification method of any kind — `AlpacaMarketDataProvider` implements
only `src.data.provider.MarketDataProvider`'s two read methods
(`get_option_chain`, `get_underlying_quote`), the same interface `mock`
and `ibkr` already implement. See
`tests/acceptance/test_alpaca_market_data_only.py` for the regression
proof, including a repo-wide grep that no `alpaca.trading` import
exists anywhere in `src/`.

**Official SDK, verified endpoints (Step 22.1):** `alpaca-py`
(https://github.com/alpacahq/alpaca-py; version pinned in
`requirements.txt`). Endpoints/methods used, confirmed directly against
the SDK source before writing this module:
- `alpaca.data.historical.stock.StockHistoricalDataClient
  .get_stock_latest_quote` (request: `StockLatestQuoteRequest`) — the
  underlying quote.
- `alpaca.data.historical.option.OptionHistoricalDataClient
  .get_option_chain` (request: `OptionChainRequest`) — the full option
  chain snapshot (latest quote, latest trade, implied volatility, and
  greeks per contract, keyed by OCC-format option symbol).
No other Alpaca endpoint or client is imported or called by this
module — not `get_option_snapshot`, not any bars/trades endpoint here
(see `alpaca_historical.py` for the separate, minimal historical-bars
adapter), and never anything under `alpaca.trading`.

**OPRA vs indicative (Part 5):** Alpaca's options market data comes in
two feeds — `OptionsFeed.OPRA` (the real, paid, consolidated options
tape) and `OptionsFeed.INDICATIVE` (Alpaca's free, delayed/indicative
feed). `AlpacaConfig.options_feed` is explicit and never silently
auto-downgraded: this provider requests exactly the feed it is
configured for, and if Alpaca's API rejects that request because the
account isn't entitled to it, `AlpacaFeedEntitlementError` is raised
immediately (fail closed) rather than silently substituting the other
feed. The feed actually used is recorded in every canonical
`OptionContract`/`UnderlyingQuote`'s own `source` field (e.g.
`"alpaca_opra"`, `"alpaca_indicative"`, `"alpaca_sip"`, `"alpaca_iex"`)
so nothing downstream ever has to guess what fed a given quote.
"""
from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timezone

from pydantic_settings import BaseSettings, SettingsConfigDict

from src.data.option_chain import OptionChain, OptionContract, OptionRight
from src.data.provider import MarketDataProvider, ProviderError
from src.data.quotes import UnderlyingQuote

# -------------------------------------------------------------- config

_VALID_OPTIONS_FEEDS = frozenset({"opra", "indicative"})
_VALID_STOCK_FEEDS = frozenset({"iex", "sip", "delayed_sip"})


class AlpacaConfigError(RuntimeError):
    """Raised when `AlpacaConfig` is invalid (bad feed name) — fails
    closed at construction time, the same idiom `src.risk.limits
    .RiskLimitsConfigError` and `src.brokers.ibkr.IBKRConfig`'s own
    `require_paper_port` use, rather than deferring to a confusing
    failure on the first real API call."""


class AlpacaConfig(BaseSettings):
    """Every value here comes from environment variables / `.env`
    (`OPTIONS_AGENT_ALPACA_*`) — nothing is hardcoded, and no credential
    ever has a source-code default. See `.env.example`."""

    model_config = SettingsConfigDict(env_prefix="OPTIONS_AGENT_ALPACA_")

    api_key: str | None = None
    api_secret: str | None = None

    # "opra" (paid, real consolidated tape) or "indicative" (free,
    # delayed) — explicit, never auto-selected. See module docstring.
    options_feed: str = "opra"
    # "iex" (free), "sip" (paid, consolidated), or "delayed_sip".
    stock_feed: str = "sip"

    max_expirations: int = 6
    max_retries: int = 3
    retry_base_delay_seconds: float = 0.25

    def validate_feeds(self) -> "AlpacaConfig":
        if self.options_feed not in _VALID_OPTIONS_FEEDS:
            raise AlpacaConfigError(
                f"OPTIONS_AGENT_ALPACA_OPTIONS_FEED={self.options_feed!r} is not one of "
                f"{sorted(_VALID_OPTIONS_FEEDS)}"
            )
        if self.stock_feed not in _VALID_STOCK_FEEDS:
            raise AlpacaConfigError(
                f"OPTIONS_AGENT_ALPACA_STOCK_FEED={self.stock_feed!r} is not one of {sorted(_VALID_STOCK_FEEDS)}"
            )
        return self


# -------------------------------------------------------------- errors


class AlpacaAuthenticationError(ProviderError):
    """Raised when no API key/secret is configured, or Alpaca rejects
    them outright (HTTP 401-shaped failure)."""


class AlpacaFeedEntitlementError(ProviderError):
    """Raised when the configured feed (OPRA or SIP) is requested but
    the account isn't entitled to it (HTTP 403-shaped failure). Never
    silently retried against a different feed — see module docstring."""


class AlpacaRateLimitError(ProviderError):
    """Raised after `max_retries` attempts all hit a rate limit
    (HTTP 429-shaped failure)."""


def classify_alpaca_error(exc: Exception) -> ProviderError:
    """Best-effort classification of whatever the SDK raised, using
    only information present on the exception itself (status code if
    present, otherwise the message text) — never assumes a specific SDK
    internal shape beyond what's actually inspectable, and always
    surfaces the original exception (`from exc`) so nothing is ever
    silently swallowed even if the classification itself is generic."""
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    text = str(exc).lower()
    if status == 401 or "unauthoriz" in text or "invalid credentials" in text or "forbidden" in text and "401" in text:
        return AlpacaAuthenticationError(f"Alpaca authentication failed: {exc}")
    if status == 403 or "not entitled" in text or "subscription" in text or "forbidden" in text:
        return AlpacaFeedEntitlementError(f"Alpaca feed entitlement error: {exc}")
    if status == 429 or "rate limit" in text or "too many requests" in text:
        return AlpacaRateLimitError(f"Alpaca rate limit exceeded: {exc}")
    return ProviderError(f"Alpaca market data request failed: {exc}")


# --------------------------------------------------------- OCC parsing

# Standard OCC option symbol format (not Alpaca-specific): 1-6 char
# root symbol, YYMMDD expiration, C/P right, 8-digit strike (price *
# 1000, zero-padded). Alpaca's get_option_chain response is keyed by
# exactly this format, and it is fully self-describing -- parsing it
# directly means this provider never needs to touch Alpaca's *trading*
# API's separate option-contracts-metadata endpoint for strike/
# expiration/right, keeping this module strictly market-data-only.
_OCC_SYMBOL_RE = re.compile(r"^(?P<root>[A-Z]{1,6})(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})(?P<right>[CP])(?P<strike>\d{8})$")


class OccSymbolParseError(ValueError):
    """Raised when a string Alpaca returned as an option chain key does
    not match the standard OCC symbol format -- fails closed (the
    contract is skipped and counted as malformed, per Part 9) rather
    than guessing at a strike/expiration/right."""


def parse_occ_option_symbol(occ_symbol: str) -> tuple[str, date, OptionRight, float]:
    """Returns (underlying_root, expiration, right, strike)."""
    m = _OCC_SYMBOL_RE.match(occ_symbol)
    if not m:
        raise OccSymbolParseError(f"not a valid OCC option symbol: {occ_symbol!r}")
    year = 2000 + int(m.group("yy"))
    try:
        expiration = date(year, int(m.group("mm")), int(m.group("dd")))
    except ValueError as exc:
        raise OccSymbolParseError(f"invalid date embedded in OCC symbol {occ_symbol!r}: {exc}") from exc
    right = OptionRight.CALL if m.group("right") == "C" else OptionRight.PUT
    strike = int(m.group("strike")) / 1000.0
    return m.group("root"), expiration, right, strike


# ------------------------------------------------------------ provider


class AlpacaMarketDataProvider(MarketDataProvider):
    """Real-market-data-only Alpaca adapter. `stock_client`/
    `option_client` are accepted as constructor overrides purely so
    tests can inject a fake implementing the same two method names
    (`get_stock_latest_quote`, `get_option_chain`) without ever
    importing the real `alpaca` package or touching real credentials —
    exactly the `IBClientLike`-injection pattern `src.brokers.ibkr`
    already established."""

    def __init__(self, config: AlpacaConfig | None = None, *, stock_client=None, option_client=None) -> None:
        self._config = (config or AlpacaConfig()).validate_feeds()
        if stock_client is None or option_client is None:
            if not self._config.api_key or not self._config.api_secret:
                raise AlpacaAuthenticationError(
                    "Alpaca market data requires OPTIONS_AGENT_ALPACA_API_KEY and "
                    "OPTIONS_AGENT_ALPACA_API_SECRET to be set (see .env.example)."
                )
        self._stock_client = stock_client or self._build_real_stock_client()
        self._option_client = option_client or self._build_real_option_client()

    def _build_real_stock_client(self):
        from alpaca.data.historical.stock import StockHistoricalDataClient

        return StockHistoricalDataClient(self._config.api_key, self._config.api_secret)

    def _build_real_option_client(self):
        from alpaca.data.historical.option import OptionHistoricalDataClient

        return OptionHistoricalDataClient(self._config.api_key, self._config.api_secret)

    async def _with_retry(self, fn, *args, **kwargs):
        """Bounded retry for transient failures only. A rate-limit hit
        retries with backoff up to `max_retries`; authentication and
        entitlement failures are never retried (retrying a 401/403
        cannot succeed and would only obscure a real configuration
        problem behind a delay) -- they raise immediately."""
        last_exc: ProviderError | None = None
        for attempt in range(self._config.max_retries):
            try:
                return await asyncio.to_thread(fn, *args, **kwargs)
            except (AlpacaAuthenticationError, AlpacaFeedEntitlementError):
                raise
            except Exception as exc:  # noqa: BLE001 -- classified immediately below
                classified = classify_alpaca_error(exc)
                if isinstance(classified, (AlpacaAuthenticationError, AlpacaFeedEntitlementError)):
                    raise classified from exc
                last_exc = classified
                await asyncio.sleep(self._config.retry_base_delay_seconds * (2**attempt))
        raise last_exc or ProviderError("Alpaca market data request failed with no captured error")

    async def get_underlying_quote(self, symbol: str) -> UnderlyingQuote:
        return await self._with_retry(self._get_underlying_quote_sync, symbol)

    def _get_underlying_quote_sync(self, symbol: str) -> UnderlyingQuote:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockLatestQuoteRequest

        symbol = symbol.upper()
        request = StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=DataFeed(self._config.stock_feed))
        result = self._stock_client.get_stock_latest_quote(request)
        quote = result[symbol]
        timestamp = quote.timestamp
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        bid = float(quote.bid_price or 0.0)
        ask = float(quote.ask_price or 0.0)
        # StockLatestQuoteRequest returns a bid/ask quote, not a trade
        # -- there is no separate "last trade price" field here, so
        # `last` is the bid/ask midpoint (or whichever side is
        # available) rather than a fabricated trade price.
        last = round((bid + ask) / 2, 4) if bid > 0 and ask > 0 else (ask or bid)
        return UnderlyingQuote(
            symbol=symbol,
            bid=bid,
            ask=ask,
            last=last,
            volume=0,  # StockLatestQuoteRequest carries no volume field; see get_stock_bars for OHLCV.
            timestamp=timestamp,
            source=f"alpaca_{self._config.stock_feed}",
        )

    async def get_option_chain(self, symbol: str) -> OptionChain:
        return await self._with_retry(self._get_option_chain_sync, symbol)

    def _get_option_chain_sync(self, symbol: str) -> OptionChain:
        from alpaca.data.enums import OptionsFeed
        from alpaca.data.requests import OptionChainRequest

        symbol = symbol.upper()
        underlying = self._get_underlying_quote_sync(symbol)
        if underlying.bid <= 0 and underlying.ask <= 0 and underlying.last <= 0:
            raise ProviderError(f"Alpaca returned no usable underlying quote for {symbol!r} (bid/ask/last all 0)")
        now = datetime.now(timezone.utc)

        request = OptionChainRequest(underlying_symbol=symbol, feed=OptionsFeed(self._config.options_feed))
        raw_chain = self._option_client.get_option_chain(request)

        contracts: list[OptionContract] = []
        for occ_symbol, snapshot in raw_chain.items():
            try:
                root, expiration, right, strike = parse_occ_option_symbol(occ_symbol)
            except OccSymbolParseError:
                continue  # malformed key -- skip rather than fabricate a contract
            if root != symbol:
                continue  # defense-in-depth: never let a mismatched-underlying key slip through
            contract = _snapshot_to_option_contract(
                occ_symbol=occ_symbol, underlying=symbol, expiration=expiration, right=right, strike=strike,
                snapshot=snapshot, underlying_price=underlying.mid, now=now,
                source=f"alpaca_{self._config.options_feed}",
            )
            if contract is not None:
                contracts.append(contract)

        return OptionChain(underlying=underlying, contracts=contracts, timestamp=now, source=f"alpaca_{self._config.options_feed}")

    async def close(self) -> None:
        return None


def _snapshot_to_option_contract(
    *, occ_symbol: str, underlying: str, expiration: date, right: OptionRight, strike: float,
    snapshot, underlying_price: float, now: datetime, source: str,
) -> OptionContract | None:
    """Maps one Alpaca `OptionsSnapshot` into the canonical
    `OptionContract`. Returns None (contract skipped, never fabricated)
    if the snapshot is missing the fields a tradable quote requires
    (a quote object at all, and both a bid and ask) -- per Part 4, this
    provider never invents a missing value."""
    quote = getattr(snapshot, "latest_quote", None)
    if quote is None:
        return None
    bid = float(quote.bid_price or 0.0)
    ask = float(quote.ask_price or 0.0)
    trade = getattr(snapshot, "latest_trade", None)
    last = float(trade.price) if trade is not None and getattr(trade, "price", None) is not None else 0.0
    greeks = getattr(snapshot, "greeks", None)
    iv = getattr(snapshot, "implied_volatility", None)

    timestamp = getattr(quote, "timestamp", None) or now
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)

    try:
        return OptionContract(
            underlying=underlying,
            option_symbol=occ_symbol,
            expiration=expiration,
            strike=strike,
            right=right,
            bid=bid,
            ask=ask,
            last=last,
            volume=0,  # not carried on OptionsSnapshot's latest_quote/latest_trade
            open_interest=0,  # Alpaca's options snapshot does not report open interest
            iv=float(iv) if iv is not None else None,
            delta=float(greeks.delta) if greeks is not None and getattr(greeks, "delta", None) is not None else None,
            gamma=float(greeks.gamma) if greeks is not None and getattr(greeks, "gamma", None) is not None else None,
            theta=float(greeks.theta) if greeks is not None and getattr(greeks, "theta", None) is not None else None,
            vega=float(greeks.vega) if greeks is not None and getattr(greeks, "vega", None) is not None else None,
            underlying_price=underlying_price,
            timestamp=timestamp,
            source=source,
        )
    except ValueError:
        # A canonical-schema rejection (e.g. bid > ask on a crossed/bad
        # quote) means this one contract is skipped, never fabricated
        # or silently coerced into validity.
        return None
