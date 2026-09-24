"""Tradier real-time market-data-only provider (Step 22.4; timestamp
semantics corrected in Step 22.7, PAPER_TRADING_V1.4.6).

**MARKET DATA ONLY — never execution.** This module calls exclusively
Tradier's read-only `/v1/markets/*` GET endpoints (quotes, option
chains, option expirations). It makes no request to any
`/v1/accounts/*/orders` endpoint, defines no `place_order`/
`submit_order`/`cancel_order`/`preview_order`/`replace_order` method of
any kind, and issues no HTTP method other than GET anywhere —
`_request` below is the single HTTP choke point in this module and it
accepts no `method` parameter at all, only ever performing a GET. See
`tests/acceptance/test_tradier_market_data_only.py` for the structural
proof (a repo-wide grep for a Tradier order/trading-shaped identifier)
and `make verify-freeze`'s `tradier_market_data_only` check for the
same proof re-run on every freeze.

**Provider Greeks are reference data, never authoritative** (Part 29):
`iv`/`delta`/`gamma`/`theta`/`vega` on the `OptionContract`s this module
returns come straight from Tradier's own `greeks` object — exactly the
same "provider-reported reference value, not an authoritative
calculation" contract `OptionContract`'s own module docstring already
establishes for Alpaca. `src.quant` remains the sole authoritative
source for any Greek a Risk decision actually depends on; nothing here
substitutes for that.

**Missing means missing** (Part 5): volume/open interest/bid-ask sizes/
per-side timestamps are mapped exactly as Tradier reports them —
`0`/absent stays `0`/`None`, never silently upgraded to look like a
real quote. A response missing both bid and ask for a contract is
skipped entirely (the contract is simply not returned), never
fabricated.

**Redaction**: the bearer token never appears in a log line or
exception message — `_redact` strips it from any text before it can
reach either, and `_request`'s own exception paths never interpolate
raw response text that could still carry the `Authorization` header
verbatim.

**Canonical timestamp selection (Step 22.7)**: `UnderlyingQuote.timestamp`
/`OptionContract.timestamp` — the field every freshness check in this
codebase (`TimestampedModel.freshness_status`/`require_fresh`, the
Risk Engine, PaperBroker, the data quality gate) actually reads — is
selected by `_select_quote_timestamp`, never `trade_date` alone.
Tradier's `trade_date` is the last-*trade* timestamp, which can
meaningfully lag the current, actionable bid/ask quote (see that
function's own docstring for the full rule and rationale). This fixed
a real defect: a stale `trade_date` could make a currently-quoted,
tradable contract appear STALE (or, before this fix, could even make
an underlying quote appear fresher than it actually was if `trade_date`
happened to be newer than a stale `bid_date` — the old code preferred
`trade_date` unconditionally). `bid_timestamp`/`ask_timestamp`/
`trade_timestamp` on `OptionContract` are unaffected by this change —
they continue to carry Tradier's raw per-field timestamps exactly as
reported, missing means missing, same as before.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

from src.data.option_chain import OptionChain, OptionContract, OptionRight
from src.data.provider import MarketDataProvider, ProviderError
from src.data.quotes import UnderlyingQuote
from src.data.rate_limiter import RateLimitPriority, RateLimitState, may_proceed

SOURCE_TRADIER = "tradier"


# -------------------------------------------------------------- config


class TradierConfigError(RuntimeError):
    """Raised for invalid `TradierConfig` values — fails closed at
    construction, the same idiom every other config loader in this
    codebase uses (`AlpacaConfigError`, `RiskLimitsConfigError`, ...)."""


class TradierConfig(BaseSettings):
    """Every value here comes from environment variables / `.env`
    (`OPTIONS_AGENT_TRADIER_*`) — the token has no source-code default
    of any kind. See `.env.example`."""

    # Step 22.8: env_ignore_empty=True -- see DataProviderSelection's
    # comment in src/data/factory.py for why this matters given the
    # shipped .env template's intentionally-blank optional variables.
    model_config = SettingsConfigDict(env_prefix="OPTIONS_AGENT_TRADIER_", env_ignore_empty=True)

    token: str | None = None
    base_url: str = "https://api.tradier.com/v1"

    max_retries: int = 3
    retry_base_delay_seconds: float = 0.25
    timeout_seconds: float = 10.0

    # Chunk size for multi-symbol quote batching (Part 4: "multi-symbol
    # batching MUST be used"). The user's own manual test confirmed 10
    # symbols in one request; this default is a conservative, documented
    # research choice, not a claimed hard Tradier API ceiling.
    max_batch_symbols: int = 50
    # How many of a symbol's nearest expirations get a full chain
    # fetched by get_option_chain -- mirrors AlpacaConfig.max_expirations,
    # since fetching every expiration on every call would be wasteful
    # (Part 23: "do not download every option chain for every ticker").
    max_expirations: int = 6

    def validate_config(self) -> "TradierConfig":
        if not self.base_url.startswith("https://"):
            raise TradierConfigError(f"OPTIONS_AGENT_TRADIER_BASE_URL must be https://, got {self.base_url!r}")
        return self


# -------------------------------------------------------------- errors


class TradierAuthenticationError(ProviderError):
    """Raised when no token is configured, or Tradier rejects it (HTTP
    401/403-shaped failure)."""


class TradierRateLimitError(ProviderError):
    """Raised when a request is blocked by this module's own priority
    gate (`src.data.rate_limiter.may_proceed`) or after `max_retries`
    consecutive HTTP 429s."""


class TradierMalformedResponseError(ProviderError):
    """Raised when Tradier returns a 200 whose body doesn't match the
    expected shape at all (Part 7's "malformed provider response must
    not crash the control loop" -- this is the typed exception a caller
    catches to isolate that failure, rather than an unhandled KeyError/
    TypeError propagating from deep inside JSON parsing)."""


_TOKEN_PLACEHOLDER = "***REDACTED***"


def _redact(text: str, token: str | None) -> str:
    if token and token in text:
        return text.replace(token, _TOKEN_PLACEHOLDER)
    return text


def classify_tradier_error(status_code: int | None, body_text: str, token: str | None) -> ProviderError:
    safe_text = _redact(body_text, token)[:500]
    if status_code in (401, 403):
        return TradierAuthenticationError(f"Tradier authentication failed (HTTP {status_code}): {safe_text}")
    if status_code == 429:
        return TradierRateLimitError(f"Tradier rate limit exceeded (HTTP 429): {safe_text}")
    return ProviderError(f"Tradier market data request failed (HTTP {status_code}): {safe_text}")


# ---------------------------------------------------------- JSON mapping


def _epoch_ms_to_datetime(value: Any) -> datetime | None:
    """Tradier reports several timestamp fields as epoch milliseconds;
    `0`/missing/unparseable means "not reported," never "epoch zero.\""""
    if value is None:
        return None
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def _select_quote_timestamp(
    *, bid_date: Any, ask_date: Any, trade_date: Any, now: datetime
) -> datetime:
    """Step 22.7 (PAPER_TRADING_V1.4.6): the canonical freshness
    timestamp for a Tradier-sourced quote/contract.

    **Tradier semantics** (per Tradier's own API documentation for
    `/markets/quotes` and `/markets/options/chains`): `trade_date` is
    the timestamp of the security's *last printed trade* — for a
    thinly-traded name, or simply before the first trade of a session,
    this can legitimately sit well behind the current wall clock even
    while the market is open and actively quoting. `bid_date`/
    `ask_date` are the timestamps of the current NBBO bid/ask
    themselves, which update continuously as the market quotes,
    independent of whether a trade has actually printed recently.

    A quote/contract's *actionable* price — the number this platform
    actually screens, prices, and risk-checks — is its bid/ask, not its
    last trade. Using `trade_date` as the freshness timestamp therefore
    risks rejecting a perfectly current, actionable quote as STALE
    purely because the underlying hasn't traded in the last `max_age`
    window, even though its bid/ask (what would actually be bought or
    sold) is quoted right now. The fix: prefer the freshest available
    quote-side timestamp, falling through only when quote-side data is
    unavailable at all:

    1. Both `bid_date` and `ask_date` valid -> `max(bid_date, ask_date)`
       — the more recent of the two actionable-quote timestamps (a
       one-sided quote can update without the other side moving).
    2. Only one of `bid_date`/`ask_date` valid -> that one.
    3. Neither quote-side timestamp valid, but `trade_date` valid ->
       `trade_date` — a real, provider-reported timestamp is still
       strictly better than none, even though it may under-represent
       current quote freshness; this is the same trade-off the
       pre-existing code already accepted, just demoted to last resort
       rather than first choice.
    4. No provider timestamp at all -> `now`, the local capture time of
       this exact HTTP response. This is safe, not a freshness
       loophole: `now` reflects the moment THIS specific snapshot was
       received over the network, during this exact call — it cannot
       make genuinely stale data look artificially fresh, because there
       is no stale timestamp being overridden here; there is no
       provider timestamp at all. This mirrors the identical fallback
       every other provider adapter in this codebase already uses when
       its SDK/response carries no timestamp of its own (see
       `src/data/alpaca_provider.py`'s `getattr(quote, "timestamp",
       None) or now`).
    """
    bid_dt = _epoch_ms_to_datetime(bid_date)
    ask_dt = _epoch_ms_to_datetime(ask_date)
    if bid_dt is not None and ask_dt is not None:
        return max(bid_dt, ask_dt)
    if bid_dt is not None:
        return bid_dt
    if ask_dt is not None:
        return ask_dt
    trade_dt = _epoch_ms_to_datetime(trade_date)
    if trade_dt is not None:
        return trade_dt
    return now


def _parse_quote_json(raw: dict, *, now: datetime) -> UnderlyingQuote | None:
    """Maps one Tradier `quote` object (from `/markets/quotes`) to the
    canonical `UnderlyingQuote`. Returns `None` — never a fabricated
    quote — if the symbol or both price sides are missing."""
    symbol = raw.get("symbol")
    if not symbol:
        return None
    bid = float(raw.get("bid") or 0.0)
    ask = float(raw.get("ask") or 0.0)
    last = float(raw.get("last") or 0.0)
    if bid <= 0 and ask <= 0 and last <= 0:
        return None
    volume = int(raw.get("volume") or 0)
    timestamp = _select_quote_timestamp(
        bid_date=raw.get("bid_date"), ask_date=raw.get("ask_date"), trade_date=raw.get("trade_date"), now=now
    )
    try:
        return UnderlyingQuote(symbol=str(symbol).upper(), bid=bid, ask=ask, last=last, volume=volume, timestamp=timestamp, source=SOURCE_TRADIER)
    except ValueError:
        return None


_TRADIER_TYPE_TO_RIGHT = {"call": OptionRight.CALL, "put": OptionRight.PUT}


def _parse_option_json(raw: dict, *, underlying_price: float, now: datetime) -> OptionContract | None:
    """Maps one Tradier `option` object (from `/markets/options/chains`)
    to the canonical `OptionContract`. Returns `None` -- the contract is
    simply skipped, never fabricated -- for a malformed entry or one
    missing both bid and ask."""
    symbol = raw.get("symbol")
    underlying = raw.get("underlying") or raw.get("root_symbol")
    expiration_raw = raw.get("expiration_date")
    strike_raw = raw.get("strike")
    right_raw = raw.get("option_type")
    if not symbol or not underlying or not expiration_raw or strike_raw is None or right_raw not in _TRADIER_TYPE_TO_RIGHT:
        return None
    try:
        expiration = date.fromisoformat(str(expiration_raw))
        strike = float(strike_raw)
    except (ValueError, TypeError):
        return None
    if strike <= 0:
        return None

    bid = float(raw.get("bid") or 0.0)
    ask = float(raw.get("ask") or 0.0)
    if bid <= 0 and ask <= 0:
        return None
    if bid > 0 and ask > 0 and bid > ask:
        return None  # crossed quote -- skip rather than construct an invalid contract

    last = float(raw.get("last") or 0.0)
    volume = int(raw.get("volume") or 0)
    open_interest = int(raw.get("open_interest") or 0)
    bid_size = raw.get("bidsize")
    ask_size = raw.get("asksize")

    greeks = raw.get("greeks") or {}
    iv = greeks.get("mid_iv")
    delta = greeks.get("delta")
    gamma = greeks.get("gamma")
    theta = greeks.get("theta")
    vega = greeks.get("vega")

    timestamp = _select_quote_timestamp(
        bid_date=raw.get("bid_date"), ask_date=raw.get("ask_date"), trade_date=raw.get("trade_date"), now=now
    )

    try:
        return OptionContract(
            underlying=str(underlying).upper(),
            option_symbol=str(symbol),
            expiration=expiration,
            strike=strike,
            right=_TRADIER_TYPE_TO_RIGHT[right_raw],
            bid=bid,
            ask=ask,
            last=last,
            volume=volume,
            open_interest=open_interest,
            iv=float(iv) if iv is not None else None,
            delta=float(delta) if delta is not None else None,
            gamma=float(gamma) if gamma is not None else None,
            theta=float(theta) if theta is not None else None,
            vega=float(vega) if vega is not None else None,
            bid_size=int(bid_size) if bid_size is not None else None,
            ask_size=int(ask_size) if ask_size is not None else None,
            bid_timestamp=_epoch_ms_to_datetime(raw.get("bid_date")),
            ask_timestamp=_epoch_ms_to_datetime(raw.get("ask_date")),
            trade_timestamp=_epoch_ms_to_datetime(raw.get("trade_date")),
            underlying_price=underlying_price,
            timestamp=timestamp,
            source=SOURCE_TRADIER,
        )
    except ValueError:
        return None  # canonical-schema rejection -- skipped, never coerced into validity


def _parse_expirations_json(raw: dict) -> list[date]:
    """Maps `/markets/options/expirations`'s response to a sorted list
    of `date`s. Tradier returns `{"expirations": null}` (not an empty
    dict) for a symbol with no listed options -- both that and a
    missing key map to an empty list, never an error."""
    expirations_obj = raw.get("expirations")
    if not expirations_obj:
        return []
    dates_raw = expirations_obj.get("date")
    if dates_raw is None:
        return []
    if isinstance(dates_raw, str):
        dates_raw = [dates_raw]
    result: list[date] = []
    for d in dates_raw:
        try:
            result.append(date.fromisoformat(str(d)))
        except ValueError:
            continue  # malformed entry -- skipped, never fabricated
    return sorted(result)


# ------------------------------------------------------------ provider


class TradierMarketDataProvider(MarketDataProvider):
    """Real-market-data-only Tradier adapter. `http_client` is accepted
    as a constructor override purely so tests can inject a fake
    implementing one async `get(path, params=None)` method (returning
    an object with `.status_code`/`.headers`/`.json()`) without ever
    importing `httpx` or touching a real token -- the same
    dependency-injection pattern `AlpacaMarketDataProvider`/
    `src.brokers.ibkr` already establish."""

    def __init__(self, config: TradierConfig | None = None, *, http_client=None) -> None:
        self._config = (config or TradierConfig()).validate_config()
        if http_client is None and not self._config.token:
            raise TradierAuthenticationError(
                "Tradier market data requires OPTIONS_AGENT_TRADIER_TOKEN to be set (see .env.example)."
            )
        self._http_client = http_client or self._build_real_http_client()
        self._rate_limit_state: RateLimitState | None = None

    def _build_real_http_client(self):
        import httpx

        return httpx.AsyncClient(
            base_url=self._config.base_url,
            headers={"Authorization": f"Bearer {self._config.token}", "Accept": "application/json"},
            timeout=self._config.timeout_seconds,
        )

    @property
    def rate_limit_state(self) -> RateLimitState | None:
        """The most recently observed rate-limit accounting -- `None`
        until the first real response, `src.data.provider_health`/the
        Portfolio Control Loop read this to report/gate on it."""
        return self._rate_limit_state

    async def _request(self, path: str, params: dict[str, Any], *, priority: RateLimitPriority) -> dict:
        """The single HTTP choke point: always GET, always priority-
        gated, always retried with backoff on transient failure, and
        always updates `self._rate_limit_state` from whatever headers
        came back -- never from a hardcoded assumption."""
        if not may_proceed(priority, self._rate_limit_state):
            raise TradierRateLimitError(
                f"request to {path!r} at priority {priority.name} blocked by the rate-limit budget "
                f"(state={self._rate_limit_state!r})"
            )
        import asyncio

        last_exc: ProviderError | None = None
        for attempt in range(self._config.max_retries):
            try:
                response = await self._http_client.get(path, params=params)
            except Exception as exc:  # noqa: BLE001 -- network/timeout failure, retried below
                last_exc = ProviderError(f"Tradier request to {path!r} failed: {_redact(str(exc), self._config.token)}")
                await asyncio.sleep(self._config.retry_base_delay_seconds * (2**attempt))
                continue

            now = datetime.now(timezone.utc)
            state = RateLimitState.from_headers(dict(response.headers), observed_at=now)
            if state is not None:
                self._rate_limit_state = state

            if response.status_code == 200:
                try:
                    return response.json()
                except Exception as exc:  # noqa: BLE001
                    raise TradierMalformedResponseError(f"Tradier response for {path!r} was not valid JSON: {exc}") from exc

            classified = classify_tradier_error(response.status_code, getattr(response, "text", ""), self._config.token)
            if isinstance(classified, TradierAuthenticationError):
                raise classified  # never retried -- see module docstring
            last_exc = classified
            await asyncio.sleep(self._config.retry_base_delay_seconds * (2**attempt))

        raise last_exc or ProviderError(f"Tradier request to {path!r} failed with no captured error")

    # ------------------------------------------------------- quotes

    async def get_underlying_quote(self, symbol: str, *, priority: RateLimitPriority = RateLimitPriority.P2_PORTFOLIO_VALUATION) -> UnderlyingQuote:
        quotes = await self.get_underlying_quotes([symbol], priority=priority)
        quote = quotes.get(symbol.upper())
        if quote is None:
            raise ProviderError(f"Tradier returned no usable quote for {symbol!r}")
        return quote

    async def get_underlying_quotes(
        self, symbols: list[str], *, priority: RateLimitPriority = RateLimitPriority.P2_PORTFOLIO_VALUATION
    ) -> dict[str, UnderlyingQuote]:
        """Part 4: multi-symbol batching -- one request per
        `max_batch_symbols`-sized chunk, never one request per symbol."""
        if not symbols:
            return {}
        now = datetime.now(timezone.utc)
        result: dict[str, UnderlyingQuote] = {}
        unique = sorted({s.upper() for s in symbols})
        chunk_size = max(1, self._config.max_batch_symbols)
        for i in range(0, len(unique), chunk_size):
            chunk = unique[i : i + chunk_size]
            data = await self._request("/markets/quotes", {"symbols": ",".join(chunk), "greeks": "false"}, priority=priority)
            quotes_obj = (data.get("quotes") or {}).get("quote")
            if quotes_obj is None:
                continue
            entries = quotes_obj if isinstance(quotes_obj, list) else [quotes_obj]
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                quote = _parse_quote_json(entry, now=now)
                if quote is not None:
                    result[quote.symbol] = quote
        return result

    # --------------------------------------------------- expirations

    async def get_expirations(
        self, symbol: str, *, priority: RateLimitPriority = RateLimitPriority.P4_OPPORTUNITY_SCANNING
    ) -> list[date]:
        data = await self._request("/markets/options/expirations", {"symbol": symbol.upper()}, priority=priority)
        return _parse_expirations_json(data)

    # -------------------------------------------------- option chains

    async def get_option_chain_for_expiration(
        self, symbol: str, expiration: date, *, priority: RateLimitPriority = RateLimitPriority.P1_POSITION_LIFECYCLE
    ) -> list[OptionContract]:
        symbol = symbol.upper()
        underlying = await self.get_underlying_quote(symbol, priority=priority)
        now = datetime.now(timezone.utc)
        data = await self._request(
            "/markets/options/chains", {"symbol": symbol, "expiration": expiration.isoformat(), "greeks": "true"}, priority=priority,
        )
        options_obj = (data.get("options") or {}).get("option")
        if options_obj is None:
            return []
        entries = options_obj if isinstance(options_obj, list) else [options_obj]
        contracts: list[OptionContract] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            contract = _parse_option_json(entry, underlying_price=underlying.mid, now=now)
            if contract is not None:
                contracts.append(contract)
        return contracts

    async def get_option_chain(
        self, symbol: str, *, priority: RateLimitPriority = RateLimitPriority.P4_OPPORTUNITY_SCANNING
    ) -> OptionChain:
        """Satisfies `MarketDataProvider`'s abstract interface: fetches
        the nearest `max_expirations` expirations and aggregates every
        contract across them into one snapshot (Part 23: never fetch
        every expiration for every ticker on every call -- this bounds
        it, the same conservative-default idiom `AlpacaConfig
        .max_expirations` already establishes)."""
        symbol = symbol.upper()
        now = datetime.now(timezone.utc)
        underlying = await self.get_underlying_quote(symbol, priority=priority)
        expirations = await self.get_expirations(symbol, priority=priority)
        contracts: list[OptionContract] = []
        for expiration in expirations[: self._config.max_expirations]:
            contracts.extend(await self.get_option_chain_for_expiration(symbol, expiration, priority=priority))
        return OptionChain(underlying=underlying, contracts=contracts, timestamp=now, source=SOURCE_TRADIER)

    async def close(self) -> None:
        aclose = getattr(self._http_client, "aclose", None)
        if aclose is not None:
            await aclose()
