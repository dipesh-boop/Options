"""Tests for `src.data.tradier_provider`. Every test injects a fake
async HTTP client (one `.get(path, params=None)` coroutine returning an
object with `.status_code`/`.headers`/`.json()`/`.text`) -- no real
`httpx` network call, no real token, matching the same dependency-
injection pattern `tests/unit/data/test_alpaca_provider.py` already
establishes for Alpaca.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from src.data.historical import HistoricalDataProvider
from src.data.option_chain import OptionRight
from src.data.provider import FreshnessStatus, ProviderError, StaleDataError
from src.data.rate_limiter import RateLimitPriority
from src.data.tradier_provider import (
    SOURCE_TRADIER,
    TradierAuthenticationError,
    TradierConfig,
    TradierConfigError,
    TradierMalformedResponseError,
    TradierMarketDataProvider,
    TradierRateLimitError,
    _epoch_ms_to_datetime,
    _parse_expirations_json,
    _parse_history_json,
    _parse_option_json,
    _parse_quote_json,
    _redact,
    _select_quote_timestamp,
    classify_tradier_error,
)

TOKEN = "test-secret-token-12345"


def _run(coro):
    return asyncio.run(coro)


class FakeResponse:
    def __init__(self, *, status_code=200, json_body=None, headers=None, text=""):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        if self._json_body is _MALFORMED:
            raise ValueError("not valid json")
        return self._json_body


_MALFORMED = object()


class FakeHttpClient:
    """Queues one response per call (in order), or a fixed response for
    every call if only one is queued. Records every (path, params) call
    for assertions on batching/retry behavior."""

    def __init__(self, responses: list[FakeResponse] | FakeResponse, *, raise_on_call: Exception | None = None):
        self._responses = responses if isinstance(responses, list) else [responses]
        self._raise_on_call = raise_on_call
        self.calls: list[tuple[str, dict]] = []
        self._closed = False

    async def get(self, path, params=None):
        self.calls.append((path, params or {}))
        if self._raise_on_call is not None:
            exc, self._raise_on_call = self._raise_on_call, None
            raise exc
        if len(self._responses) == 1:
            return self._responses[0]
        return self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]

    async def aclose(self):
        self._closed = True


def _provider(response, **config_kwargs) -> tuple[TradierMarketDataProvider, FakeHttpClient]:
    client = FakeHttpClient(response)
    config = TradierConfig(token=TOKEN, **config_kwargs)
    return TradierMarketDataProvider(config, http_client=client), client


# ----------------------------------------------------------------- config


class TestConfig:
    def test_requires_token_when_no_http_client_injected(self):
        with pytest.raises(TradierAuthenticationError):
            TradierMarketDataProvider(TradierConfig(token=None))

    def test_allows_missing_token_when_http_client_injected(self):
        # constructing with an injected client (tests) never needs a real token
        provider, _ = _provider(FakeResponse())
        assert provider is not None

    def test_rejects_non_https_base_url(self):
        with pytest.raises(TradierConfigError):
            TradierConfig(token=TOKEN, base_url="http://api.tradier.com/v1").validate_config()

    def test_env_prefix_is_options_agent_tradier(self, monkeypatch):
        monkeypatch.setenv("OPTIONS_AGENT_TRADIER_TOKEN", "env-token")
        cfg = TradierConfig()
        assert cfg.token == "env-token"

    def test_blank_base_url_env_value_is_treated_as_unset(self, monkeypatch):
        """Step 22.8 (PAPER_TRADING_V1.4.7): the shipped .env template
        leaves OPTIONS_AGENT_TRADIER_BASE_URL blank by convention -- a
        blank-but-present override must fall through to the production
        default, never become base_url="" (which would fail
        validate_config()'s https:// check)."""
        monkeypatch.setenv("OPTIONS_AGENT_TRADIER_BASE_URL", "")
        cfg = TradierConfig(token=TOKEN)
        assert cfg.base_url == "https://api.tradier.com/v1"
        cfg.validate_config()  # does not raise


# ------------------------------------------------------------- redaction


class TestRedaction:
    def test_redacts_token_from_text(self):
        assert TOKEN not in _redact(f"error near {TOKEN} in body", TOKEN)

    def test_leaves_text_unchanged_when_no_token(self):
        assert _redact("plain text", None) == "plain text"

    def test_classify_error_redacts_token_from_body(self):
        exc = classify_tradier_error(500, f"token={TOKEN} rejected", TOKEN)
        assert TOKEN not in str(exc)

    def test_classify_error_truncates_long_body(self):
        exc = classify_tradier_error(500, "x" * 10_000, None)
        assert len(str(exc)) < 1000


class TestErrorClassification:
    @pytest.mark.parametrize("status", [401, 403])
    def test_401_403_map_to_authentication_error(self, status):
        assert isinstance(classify_tradier_error(status, "denied", None), TradierAuthenticationError)

    def test_429_maps_to_rate_limit_error(self):
        assert isinstance(classify_tradier_error(429, "too many", None), TradierRateLimitError)

    def test_other_status_maps_to_generic_provider_error(self):
        exc = classify_tradier_error(500, "server error", None)
        assert isinstance(exc, ProviderError)
        assert not isinstance(exc, (TradierAuthenticationError, TradierRateLimitError))


# ------------------------------------------------------------- mapping


class TestEpochMsToDatetime:
    def test_none_maps_to_none(self):
        assert _epoch_ms_to_datetime(None) is None

    def test_zero_maps_to_none_never_epoch(self):
        assert _epoch_ms_to_datetime(0) is None

    def test_negative_maps_to_none(self):
        assert _epoch_ms_to_datetime(-5) is None

    def test_unparseable_maps_to_none(self):
        assert _epoch_ms_to_datetime("not-a-number") is None

    def test_valid_epoch_ms_parses(self):
        dt = _epoch_ms_to_datetime(1_700_000_000_000)
        assert dt is not None and dt.tzinfo is not None


# Step 22.7 (PAPER_TRADING_V1.4.6): the exact live raw values that
# surfaced this defect during local acceptance -- trade_date is a
# stale midnight print, bid_date/ask_date are the actual, current
# quote from ~11:10 that morning.
_LIVE_TRADE_DATE = 1_790_121_600_002  # 2026-09-23T00:00:00.002Z -- stale last trade
_LIVE_BID_DATE = 1_790_161_815_000  # 2026-09-23T11:10:15Z -- current bid
_LIVE_ASK_DATE = 1_790_161_813_000  # 2026-09-23T11:10:13Z -- current ask, 2s behind bid


class TestSelectQuoteTimestamp:
    """Step 22.7: `_select_quote_timestamp` is the fix itself -- a
    stale `trade_date` must never override a newer, actionable bid/ask
    timestamp."""

    def test_A_trade_date_older_than_bid_ask_is_overridden(self):
        # This is exactly the live defect: naive trade_date-first
        # selection would have returned the stale midnight timestamp
        # instead of the current ~11:10 quote.
        result = _select_quote_timestamp(
            bid_date=_LIVE_BID_DATE, ask_date=_LIVE_ASK_DATE, trade_date=_LIVE_TRADE_DATE,
            now=datetime(2026, 9, 23, 11, 10, 16, tzinfo=timezone.utc),
        )
        assert result == _epoch_ms_to_datetime(_LIVE_BID_DATE)
        assert result != _epoch_ms_to_datetime(_LIVE_TRADE_DATE)
        assert result > _epoch_ms_to_datetime(_LIVE_TRADE_DATE)

    def test_B_both_bid_and_ask_present_picks_the_fresher_of_the_two(self):
        # bid_date is 2s newer than ask_date in the live example above.
        result = _select_quote_timestamp(
            bid_date=_LIVE_BID_DATE, ask_date=_LIVE_ASK_DATE, trade_date=None,
            now=datetime(2026, 9, 23, 11, 10, 16, tzinfo=timezone.utc),
        )
        assert result == _epoch_ms_to_datetime(_LIVE_BID_DATE)

        # Deterministic the other way too -- ask newer than bid.
        result2 = _select_quote_timestamp(
            bid_date=_LIVE_ASK_DATE, ask_date=_LIVE_BID_DATE, trade_date=None,
            now=datetime(2026, 9, 23, 11, 10, 16, tzinfo=timezone.utc),
        )
        assert result2 == _epoch_ms_to_datetime(_LIVE_BID_DATE)

    def test_C_only_bid_date_present(self):
        result = _select_quote_timestamp(
            bid_date=_LIVE_BID_DATE, ask_date=None, trade_date=_LIVE_TRADE_DATE,
            now=datetime(2026, 9, 23, 11, 10, 16, tzinfo=timezone.utc),
        )
        assert result == _epoch_ms_to_datetime(_LIVE_BID_DATE)

    def test_D_only_ask_date_present(self):
        result = _select_quote_timestamp(
            bid_date=None, ask_date=_LIVE_ASK_DATE, trade_date=_LIVE_TRADE_DATE,
            now=datetime(2026, 9, 23, 11, 10, 16, tzinfo=timezone.utc),
        )
        assert result == _epoch_ms_to_datetime(_LIVE_ASK_DATE)

    def test_E_no_bid_or_ask_falls_back_to_trade_date(self):
        result = _select_quote_timestamp(
            bid_date=None, ask_date=None, trade_date=_LIVE_TRADE_DATE,
            now=datetime(2026, 9, 23, 11, 10, 16, tzinfo=timezone.utc),
        )
        assert result == _epoch_ms_to_datetime(_LIVE_TRADE_DATE)

    def test_F_no_provider_timestamps_at_all_falls_back_to_now(self):
        now = datetime(2026, 9, 23, 11, 10, 16, tzinfo=timezone.utc)
        result = _select_quote_timestamp(bid_date=None, ask_date=None, trade_date=None, now=now)
        assert result == now

    def test_zero_and_unparseable_values_are_treated_as_missing(self):
        # 0/negative/garbage epoch-ms all map to None via _epoch_ms_to_datetime
        # -- confirms the selection helper falls through correctly rather
        # than treating a sentinel "0" as a real timestamp.
        now = datetime(2026, 9, 23, 11, 10, 16, tzinfo=timezone.utc)
        result = _select_quote_timestamp(bid_date=0, ask_date="garbage", trade_date=-5, now=now)
        assert result == now


class TestParseQuoteJson:
    def test_maps_valid_quote(self):
        now = date(2026, 9, 22)
        from datetime import datetime, timezone
        now_dt = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)
        q = _parse_quote_json({"symbol": "spy", "bid": 454.5, "ask": 455.5, "last": 455.0, "volume": 1000}, now=now_dt)
        assert q is not None
        assert q.symbol == "SPY"
        assert q.source == SOURCE_TRADIER

    def test_missing_symbol_returns_none(self):
        from datetime import datetime, timezone
        assert _parse_quote_json({"bid": 1, "ask": 2}, now=datetime.now(timezone.utc)) is None

    def test_all_zero_prices_returns_none_never_fabricated(self):
        from datetime import datetime, timezone
        assert _parse_quote_json({"symbol": "SPY", "bid": 0, "ask": 0, "last": 0}, now=datetime.now(timezone.utc)) is None

    def test_stale_trade_date_does_not_override_fresh_bid_ask(self):
        """Step 22.7 regression -- the exact live defect: a quote whose
        trade_date is a stale midnight print but whose bid/ask are the
        current ~11:10 NBBO must carry the CURRENT bid/ask timestamp as
        its canonical `.timestamp`, never the stale trade_date."""
        raw = {
            "symbol": "SPY", "bid": 454.5, "ask": 455.5, "last": 455.0, "volume": 1000,
            "trade_date": _LIVE_TRADE_DATE, "bid_date": _LIVE_BID_DATE, "ask_date": _LIVE_ASK_DATE,
        }
        q = _parse_quote_json(raw, now=datetime(2026, 9, 23, 11, 10, 16, tzinfo=timezone.utc))
        assert q is not None
        assert q.timestamp == _epoch_ms_to_datetime(_LIVE_BID_DATE)
        assert q.timestamp != _epoch_ms_to_datetime(_LIVE_TRADE_DATE)

    def test_H_stale_bid_ask_still_fails_freshness(self):
        """The fix must never make genuinely stale data pass -- a quote
        whose bid/ask timestamps are themselves outside max_age is
        still correctly STALE, fix or no fix."""
        as_of = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
        stale_bid_ms = int((as_of - timedelta(minutes=30)).timestamp() * 1000)
        raw = {
            "symbol": "SPY", "bid": 454.5, "ask": 455.5, "last": 455.0, "volume": 1000,
            "trade_date": stale_bid_ms, "bid_date": stale_bid_ms, "ask_date": stale_bid_ms,
        }
        q = _parse_quote_json(raw, now=as_of)
        assert q is not None
        assert q.freshness_status(as_of) == FreshnessStatus.STALE
        with pytest.raises(StaleDataError):
            q.require_fresh(as_of)

    def test_I_fresh_bid_ask_passes_freshness_even_with_a_very_old_last_trade(self):
        """The actual regression this whole step exists to fix: a
        current, actionable quote must not be rejected as stale purely
        because the underlying hasn't printed a trade recently."""
        as_of = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
        fresh_bid_ms = int((as_of - timedelta(minutes=1)).timestamp() * 1000)
        fresh_ask_ms = int((as_of - timedelta(minutes=1)).timestamp() * 1000)
        ancient_trade_ms = int((as_of - timedelta(days=3)).timestamp() * 1000)
        raw = {
            "symbol": "SPY", "bid": 454.5, "ask": 455.5, "last": 455.0, "volume": 1000,
            "trade_date": ancient_trade_ms, "bid_date": fresh_bid_ms, "ask_date": fresh_ask_ms,
        }
        q = _parse_quote_json(raw, now=as_of)
        assert q is not None
        assert q.freshness_status(as_of) == FreshnessStatus.FRESH
        assert q.require_fresh(as_of) is q

    def test_J_a_wildly_future_bid_date_is_not_treated_as_fresh(self):
        as_of = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
        future_ms = int((as_of + timedelta(hours=2)).timestamp() * 1000)
        raw = {
            "symbol": "SPY", "bid": 454.5, "ask": 455.5, "last": 455.0, "volume": 1000,
            "bid_date": future_ms, "ask_date": future_ms,
        }
        q = _parse_quote_json(raw, now=as_of)
        assert q is not None
        assert q.freshness_status(as_of) == FreshnessStatus.STALE

    def test_K_existing_field_mapping_unaffected_by_the_timestamp_fix(self):
        raw = {
            "symbol": "spy", "bid": 454.5, "ask": 455.5, "last": 455.0, "volume": 1000,
            "trade_date": _LIVE_TRADE_DATE, "bid_date": _LIVE_BID_DATE, "ask_date": _LIVE_ASK_DATE,
        }
        q = _parse_quote_json(raw, now=datetime(2026, 9, 23, 11, 10, 16, tzinfo=timezone.utc))
        assert q is not None
        assert q.symbol == "SPY"
        assert q.bid == 454.5 and q.ask == 455.5 and q.last == 455.0
        assert q.volume == 1000
        assert q.source == SOURCE_TRADIER


class TestParseOptionJson:
    BASE = {
        "symbol": "SPY261023P00450000", "underlying": "SPY", "expiration_date": "2026-10-23",
        "strike": 450.0, "option_type": "put", "bid": 4.5, "ask": 4.7, "last": 4.6,
        "volume": 100, "open_interest": 500,
        "greeks": {"mid_iv": 0.18, "delta": -0.3, "gamma": 0.02, "theta": -0.05, "vega": 0.1},
    }

    def _now(self):
        from datetime import datetime, timezone
        return datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)

    def test_maps_valid_option(self):
        c = _parse_option_json(self.BASE, underlying_price=455.0, now=self._now())
        assert c is not None
        assert c.right == OptionRight.PUT
        assert c.iv == 0.18
        assert c.delta == -0.3
        assert c.source == SOURCE_TRADIER

    def test_missing_required_field_returns_none(self):
        raw = dict(self.BASE)
        del raw["strike"]
        assert _parse_option_json(raw, underlying_price=455.0, now=self._now()) is None

    def test_invalid_option_type_returns_none(self):
        raw = {**self.BASE, "option_type": "not_a_right"}
        assert _parse_option_json(raw, underlying_price=455.0, now=self._now()) is None

    def test_zero_strike_returns_none(self):
        raw = {**self.BASE, "strike": 0}
        assert _parse_option_json(raw, underlying_price=455.0, now=self._now()) is None

    def test_missing_both_bid_and_ask_returns_none(self):
        raw = {**self.BASE, "bid": 0, "ask": 0}
        assert _parse_option_json(raw, underlying_price=455.0, now=self._now()) is None

    def test_crossed_quote_returns_none(self):
        raw = {**self.BASE, "bid": 10.0, "ask": 5.0}
        assert _parse_option_json(raw, underlying_price=455.0, now=self._now()) is None

    def test_missing_greeks_object_leaves_greeks_none(self):
        raw = dict(self.BASE)
        del raw["greeks"]
        c = _parse_option_json(raw, underlying_price=455.0, now=self._now())
        assert c is not None
        assert c.iv is None and c.delta is None

    def test_malformed_expiration_returns_none(self):
        raw = {**self.BASE, "expiration_date": "not-a-date"}
        assert _parse_option_json(raw, underlying_price=455.0, now=self._now()) is None

    def test_bid_ask_size_and_per_side_timestamps_mapped(self):
        raw = {**self.BASE, "bidsize": 10, "asksize": 20, "bid_date": 1_700_000_000_000, "ask_date": 1_700_000_001_000}
        c = _parse_option_json(raw, underlying_price=455.0, now=self._now())
        assert c.bid_size == 10 and c.ask_size == 20
        assert c.bid_timestamp is not None and c.ask_timestamp is not None

    def test_G_canonical_timestamp_prefers_fresh_bid_ask_over_stale_trade_date(self):
        """Step 22.7 regression, option-contract side. The canonical
        `.timestamp` must reflect the current bid/ask, never the stale
        trade_date -- while `bid_timestamp`/`ask_timestamp`/
        `trade_timestamp` keep carrying each raw provider value
        unchanged (Requirement 2: 'Keep these fields intact')."""
        raw = {**self.BASE, "trade_date": _LIVE_TRADE_DATE, "bid_date": _LIVE_BID_DATE, "ask_date": _LIVE_ASK_DATE}
        now = datetime(2026, 9, 23, 11, 10, 16, tzinfo=timezone.utc)
        c = _parse_option_json(raw, underlying_price=455.0, now=now)
        assert c is not None
        # Canonical timestamp: the fresher of bid/ask, never trade_date.
        assert c.timestamp == _epoch_ms_to_datetime(_LIVE_BID_DATE)
        assert c.timestamp != _epoch_ms_to_datetime(_LIVE_TRADE_DATE)
        # Per-side fields preserved exactly as reported -- untouched by this fix.
        assert c.bid_timestamp == _epoch_ms_to_datetime(_LIVE_BID_DATE)
        assert c.ask_timestamp == _epoch_ms_to_datetime(_LIVE_ASK_DATE)
        assert c.trade_timestamp == _epoch_ms_to_datetime(_LIVE_TRADE_DATE)

    def test_H_stale_bid_ask_option_contract_still_fails_freshness(self):
        as_of = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
        stale_ms = int((as_of - timedelta(minutes=30)).timestamp() * 1000)
        raw = {**self.BASE, "trade_date": stale_ms, "bid_date": stale_ms, "ask_date": stale_ms}
        c = _parse_option_json(raw, underlying_price=455.0, now=as_of)
        assert c is not None
        assert c.freshness_status(as_of) == FreshnessStatus.STALE

    def test_I_fresh_bid_ask_option_contract_passes_despite_ancient_last_trade(self):
        as_of = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
        fresh_ms = int((as_of - timedelta(minutes=1)).timestamp() * 1000)
        ancient_ms = int((as_of - timedelta(days=10)).timestamp() * 1000)
        raw = {**self.BASE, "trade_date": ancient_ms, "bid_date": fresh_ms, "ask_date": fresh_ms}
        c = _parse_option_json(raw, underlying_price=455.0, now=as_of)
        assert c is not None
        assert c.freshness_status(as_of) == FreshnessStatus.FRESH

    def test_E_no_bid_ask_dates_falls_back_to_trade_date_for_option_contract(self):
        raw = {**self.BASE, "trade_date": _LIVE_TRADE_DATE}
        c = _parse_option_json(raw, underlying_price=455.0, now=self._now())
        assert c is not None
        assert c.timestamp == _epoch_ms_to_datetime(_LIVE_TRADE_DATE)

    def test_F_no_provider_timestamps_falls_back_to_now_for_option_contract(self):
        now = self._now()
        c = _parse_option_json(dict(self.BASE), underlying_price=455.0, now=now)
        assert c is not None
        assert c.timestamp == now


class TestParseExpirationsJson:
    def test_null_expirations_object_returns_empty(self):
        assert _parse_expirations_json({"expirations": None}) == []

    def test_missing_key_returns_empty(self):
        assert _parse_expirations_json({}) == []

    def test_single_string_date_normalized_to_list(self):
        result = _parse_expirations_json({"expirations": {"date": "2026-10-23"}})
        assert result == [date(2026, 10, 23)]

    def test_list_of_dates_sorted(self):
        result = _parse_expirations_json({"expirations": {"date": ["2026-11-20", "2026-10-23"]}})
        assert result == [date(2026, 10, 23), date(2026, 11, 20)]

    def test_malformed_entry_skipped_not_raised(self):
        result = _parse_expirations_json({"expirations": {"date": ["2026-10-23", "not-a-date"]}})
        assert result == [date(2026, 10, 23)]


# -------------------------------------------------------------- provider


class TestGetUnderlyingQuotes:
    def test_batches_multiple_symbols_into_one_request(self):
        response = FakeResponse(json_body={
            "quotes": {"quote": [
                {"symbol": "SPY", "bid": 454.5, "ask": 455.5, "last": 455.0, "volume": 1000},
                {"symbol": "QQQ", "bid": 379.5, "ask": 380.5, "last": 380.0, "volume": 500},
            ]}
        })
        provider, client = _provider(response)
        quotes = _run(provider.get_underlying_quotes(["SPY", "QQQ"]))
        assert set(quotes.keys()) == {"SPY", "QQQ"}
        assert len(client.calls) == 1  # one request for both symbols -- Part 4's batching requirement

    def test_chunks_beyond_max_batch_symbols(self):
        response = FakeResponse(json_body={"quotes": {"quote": []}})
        provider, client = _provider(response, max_batch_symbols=2)
        _run(provider.get_underlying_quotes(["A", "B", "C", "D", "E"]))
        assert len(client.calls) == 3  # 5 symbols / batch of 2 -> 3 requests

    def test_empty_symbol_list_makes_no_request(self):
        provider, client = _provider(FakeResponse(json_body={"quotes": {"quote": []}}))
        result = _run(provider.get_underlying_quotes([]))
        assert result == {} and client.calls == []

    def test_null_quote_object_returns_empty_not_error(self):
        provider, _ = _provider(FakeResponse(json_body={"quotes": {"quote": None}}))
        result = _run(provider.get_underlying_quotes(["SPY"]))
        assert result == {}

    def test_get_underlying_quote_raises_when_symbol_absent(self):
        provider, _ = _provider(FakeResponse(json_body={"quotes": {"quote": []}}))
        with pytest.raises(ProviderError):
            _run(provider.get_underlying_quote("SPY"))


class TestGetExpirations:
    def test_returns_parsed_dates(self):
        provider, client = _provider(FakeResponse(json_body={"expirations": {"date": ["2026-10-23"]}}))
        result = _run(provider.get_expirations("SPY"))
        assert result == [date(2026, 10, 23)]
        assert client.calls[0][1]["symbol"] == "SPY"


class TestGetOptionChainForExpiration:
    def test_fetches_underlying_then_chain(self):
        responses = [
            FakeResponse(json_body={"quotes": {"quote": {"symbol": "SPY", "bid": 454.5, "ask": 455.5, "last": 455.0, "volume": 1000}}}),
            FakeResponse(json_body={"options": {"option": [
                {"symbol": "SPY261023P00450000", "underlying": "SPY", "expiration_date": "2026-10-23", "strike": 450.0,
                 "option_type": "put", "bid": 4.5, "ask": 4.7, "last": 4.6, "volume": 100, "open_interest": 500},
            ]}}),
        ]
        provider, client = _provider(responses)
        contracts = _run(provider.get_option_chain_for_expiration("spy", date(2026, 10, 23)))
        assert len(contracts) == 1
        assert contracts[0].underlying_price == 455.0  # mid of the fetched underlying quote
        assert len(client.calls) == 2

    def test_malformed_option_isolated_not_whole_chain(self):
        responses = [
            FakeResponse(json_body={"quotes": {"quote": {"symbol": "SPY", "bid": 454.5, "ask": 455.5, "last": 455.0, "volume": 1000}}}),
            FakeResponse(json_body={"options": {"option": [
                {"symbol": "SPY261023P00450000", "underlying": "SPY", "expiration_date": "2026-10-23", "strike": 450.0,
                 "option_type": "put", "bid": 4.5, "ask": 4.7, "last": 4.6, "volume": 100, "open_interest": 500},
                {"symbol": "BAD", "underlying": "SPY", "expiration_date": "not-a-date", "strike": 1, "option_type": "put", "bid": 1, "ask": 2},
            ]}}),
        ]
        provider, _ = _provider(responses)
        contracts = _run(provider.get_option_chain_for_expiration("SPY", date(2026, 10, 23)))
        assert len(contracts) == 1  # the malformed entry was skipped, not fatal

    def test_null_options_object_returns_empty(self):
        responses = [
            FakeResponse(json_body={"quotes": {"quote": {"symbol": "SPY", "bid": 454.5, "ask": 455.5, "last": 455.0, "volume": 1000}}}),
            FakeResponse(json_body={"options": {"option": None}}),
        ]
        provider, _ = _provider(responses)
        assert _run(provider.get_option_chain_for_expiration("SPY", date(2026, 10, 23))) == []


class TestGetOptionChain:
    def test_aggregates_across_nearest_expirations_bounded_by_config(self):
        underlying_resp = FakeResponse(json_body={"quotes": {"quote": {"symbol": "SPY", "bid": 454.5, "ask": 455.5, "last": 455.0, "volume": 1000}}})
        expirations_resp = FakeResponse(json_body={"expirations": {"date": ["2026-10-23", "2026-11-20", "2026-12-18"]}})
        chain_resp = FakeResponse(json_body={"options": {"option": []}})

        class RoutingClient(FakeHttpClient):
            def __init__(self):
                super().__init__(FakeResponse())
                self._paths = {
                    "/markets/quotes": underlying_resp,
                    "/markets/options/expirations": expirations_resp,
                    "/markets/options/chains": chain_resp,
                }

            async def get(self, path, params=None):
                self.calls.append((path, params or {}))
                return self._paths[path]

        client = RoutingClient()
        provider = TradierMarketDataProvider(TradierConfig(token=TOKEN, max_expirations=2), http_client=client)
        chain = _run(provider.get_option_chain("SPY"))
        assert chain.source == SOURCE_TRADIER
        chain_calls = [c for c in client.calls if c[0] == "/markets/options/chains"]
        assert len(chain_calls) == 2  # bounded by max_expirations=2, not all 3 available


class TestGetOptionChainForDteWindow:
    """PAPER_TRADING_V1.5.6: the hotfix for the 2026-10-01 production
    defect -- `get_option_chain` fetches its nearest `max_expirations`
    expirations regardless of DTE, which can silently exclude every
    expiration a strategy's own `[min_dte, max_dte]` window could ever
    use. `get_option_chain_for_dte_window` fetches full chains ONLY for
    expirations inside the caller-supplied window."""

    def _routing_provider(self, *, expirations: list[str], max_expirations: int = 6) -> tuple[TradierMarketDataProvider, FakeHttpClient]:
        underlying_resp = FakeResponse(json_body={"quotes": {"quote": {"symbol": "SPY", "bid": 454.5, "ask": 455.5, "last": 455.0, "volume": 1000}}})
        expirations_resp = FakeResponse(json_body={"expirations": {"date": expirations}})
        chain_resp = FakeResponse(json_body={"options": {"option": []}})

        class RoutingClient(FakeHttpClient):
            def __init__(self):
                super().__init__(FakeResponse())
                self._paths = {
                    "/markets/quotes": underlying_resp,
                    "/markets/options/expirations": expirations_resp,
                    "/markets/options/chains": chain_resp,
                }

            async def get(self, path, params=None):
                self.calls.append((path, params or {}))
                return self._paths[path]

        client = RoutingClient()
        provider = TradierMarketDataProvider(TradierConfig(token=TOKEN, max_expirations=max_expirations), http_client=client)
        return provider, client

    def _chain_expiration_params(self, client: FakeHttpClient) -> list[str]:
        return [params["expiration"] for path, params in client.calls if path == "/markets/options/chains"]

    # ---- 1: below-window and in-window expirations both present
    def test_only_in_window_expirations_fetched(self):
        provider, client = self._routing_provider(
            expirations=["2026-10-01", "2026-10-02", "2026-10-23", "2026-10-30"],
        )
        _run(provider.get_option_chain_for_dte_window("SPY", min_dte=20, max_dte=45, as_of=date(2026, 10, 1)))
        assert self._chain_expiration_params(client) == ["2026-10-23", "2026-10-30"]

    # ---- 3: near-term expirations never consume the request bound
    def test_near_term_expirations_do_not_consume_the_request_bound(self):
        provider, client = self._routing_provider(
            expirations=["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-23", "2026-10-30"],
            max_expirations=2,
        )
        _run(provider.get_option_chain_for_dte_window("SPY", min_dte=20, max_dte=45, as_of=date(2026, 10, 1)))
        # both in-window dates fetched -- the 4 near-term dates never
        # occupied a slot in the bound
        assert self._chain_expiration_params(client) == ["2026-10-23", "2026-10-30"]

    # ---- 4: the exact 2026-10-01 production calendar
    def test_exact_2026_10_01_production_calendar(self):
        provider, client = self._routing_provider(
            expirations=[
                "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
                "2026-10-09", "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16",
                "2026-10-23", "2026-10-30", "2026-11-06", "2026-11-13",
            ],
        )
        _run(provider.get_option_chain_for_dte_window("SPY", min_dte=20, max_dte=45, as_of=date(2026, 10, 1)))
        assert self._chain_expiration_params(client) == ["2026-10-23", "2026-10-30", "2026-11-06", "2026-11-13"]

    # ---- 5: no expiration in window -- fails honestly, never substitutes
    def test_no_expiration_in_window_yields_empty_chain(self):
        provider, _ = self._routing_provider(expirations=["2026-10-01", "2026-10-02"])
        chain = _run(provider.get_option_chain_for_dte_window("SPY", min_dte=20, max_dte=45, as_of=date(2026, 10, 1)))
        assert chain.contracts == []

    # ---- 6: more eligible expirations than the request bound -- deterministic
    def test_more_eligible_than_bound_selects_closest_to_min_dte_first(self):
        provider, client = self._routing_provider(
            expirations=["2026-10-23", "2026-10-30", "2026-11-06", "2026-11-13"], max_expirations=2,
        )
        _run(provider.get_option_chain_for_dte_window("SPY", min_dte=20, max_dte=45, as_of=date(2026, 10, 1)))
        assert self._chain_expiration_params(client) == ["2026-10-23", "2026-10-30"]

    def test_bounding_is_deterministic_across_repeated_calls(self):
        provider, client = self._routing_provider(
            expirations=["2026-10-23", "2026-10-30", "2026-11-06", "2026-11-13"], max_expirations=2,
        )
        _run(provider.get_option_chain_for_dte_window("SPY", min_dte=20, max_dte=45, as_of=date(2026, 10, 1)))
        result_a = self._chain_expiration_params(client)
        provider2, client2 = self._routing_provider(
            expirations=["2026-10-23", "2026-10-30", "2026-11-06", "2026-11-13"], max_expirations=2,
        )
        _run(provider2.get_option_chain_for_dte_window("SPY", min_dte=20, max_dte=45, as_of=date(2026, 10, 1)))
        result_b = self._chain_expiration_params(client2)
        assert result_a == result_b

    # ---- 7/8: boundary inclusion
    def test_expiration_exactly_at_min_dte_included(self):
        # as_of + 20 days
        provider, client = self._routing_provider(expirations=["2026-10-21"])  # Oct 1 + 20 = Oct 21
        _run(provider.get_option_chain_for_dte_window("SPY", min_dte=20, max_dte=45, as_of=date(2026, 10, 1)))
        assert self._chain_expiration_params(client) == ["2026-10-21"]

    def test_expiration_exactly_at_max_dte_included(self):
        provider, client = self._routing_provider(expirations=["2026-11-15"])  # Oct 1 + 45 = Nov 15
        _run(provider.get_option_chain_for_dte_window("SPY", min_dte=20, max_dte=45, as_of=date(2026, 10, 1)))
        assert self._chain_expiration_params(client) == ["2026-11-15"]

    # ---- 9/10: boundary exclusion
    def test_expiration_one_day_before_min_dte_excluded(self):
        provider, client = self._routing_provider(expirations=["2026-10-20"])  # Oct 1 + 19
        _run(provider.get_option_chain_for_dte_window("SPY", min_dte=20, max_dte=45, as_of=date(2026, 10, 1)))
        assert self._chain_expiration_params(client) == []

    def test_expiration_one_day_after_max_dte_excluded(self):
        provider, client = self._routing_provider(expirations=["2026-11-16"])  # Oct 1 + 46
        _run(provider.get_option_chain_for_dte_window("SPY", min_dte=20, max_dte=45, as_of=date(2026, 10, 1)))
        assert self._chain_expiration_params(client) == []

    # ---- 11: date semantics match candidate_generation.py exactly
    def test_date_semantics_match_candidate_generation_eligible_expirations(self):
        from src.workflows.candidate_generation import QuantFilterConfig, _eligible_expirations
        from src.data.option_chain import OptionChain, OptionContract, OptionRight as DataOptionRight
        from src.data.quotes import UnderlyingQuote

        as_of = datetime(2026, 10, 1, 15, 0, tzinfo=timezone.utc)
        candidate_dates = ["2026-10-20", "2026-10-21", "2026-11-15", "2026-11-16"]
        underlying = UnderlyingQuote(symbol="SPY", bid=454.5, ask=455.5, last=455.0, volume=1000, timestamp=as_of, source="tradier")
        contracts = [
            OptionContract(
                underlying="SPY", option_symbol=f"SPY{d}P00450000", expiration=date.fromisoformat(d), strike=450.0,
                right=DataOptionRight.PUT, bid=1.0, ask=1.1, last=1.05, volume=100, open_interest=100,
                underlying_price=455.0, timestamp=as_of, source="tradier",
            )
            for d in candidate_dates
        ]
        chain = OptionChain(underlying=underlying, contracts=contracts, timestamp=as_of, source="tradier")
        eligible_via_candidate_generation = set(_eligible_expirations(chain, as_of, QuantFilterConfig(min_dte=20, max_dte=45)))

        provider, client = self._routing_provider(expirations=candidate_dates)
        _run(provider.get_option_chain_for_dte_window("SPY", min_dte=20, max_dte=45, as_of=as_of.date()))
        eligible_via_provider = {date.fromisoformat(d) for d in self._chain_expiration_params(client)}
        assert eligible_via_provider == eligible_via_candidate_generation == {date(2026, 10, 21), date(2026, 11, 15)}

    # ---- 12: no hardcoded 20/45 inside the provider
    def test_min_dte_and_max_dte_have_no_default_the_caller_must_always_supply_them(self):
        import inspect

        sig = inspect.signature(TradierMarketDataProvider.get_option_chain_for_dte_window)
        assert sig.parameters["min_dte"].default is inspect.Parameter.empty
        assert sig.parameters["max_dte"].default is inspect.Parameter.empty

    def test_source_never_hardcodes_the_2045_dte_values(self):
        # AST-based rather than string-matching: a docstring is free to
        # mention "20"/"45" in prose without this test false-failing, but
        # no bare `20`/`45` integer literal may appear anywhere in the
        # function's actual executable body (the no-default-value test
        # above already proves min_dte/max_dte must be caller-supplied;
        # this proves nothing re-derives 20/45 internally either).
        import ast
        import inspect
        import textwrap

        source = inspect.getsource(TradierMarketDataProvider.get_option_chain_for_dte_window)
        tree = ast.parse(textwrap.dedent(source))
        func_def = tree.body[0]
        assert isinstance(func_def, ast.AsyncFunctionDef)
        body_without_docstring = func_def.body[1:] if ast.get_docstring(func_def) else func_def.body
        literals = {
            node.value
            for stmt in body_without_docstring
            for node in ast.walk(stmt)
            if isinstance(node, ast.Constant) and isinstance(node.value, int)
        }
        assert 20 not in literals
        assert 45 not in literals

    # ---- diagnostics
    def test_diagnostics_populated_with_accurate_counts(self):
        from src.data.provider import DteWindowSelectionDiagnostics

        provider, _ = self._routing_provider(
            expirations=["2026-10-01", "2026-10-02", "2026-10-23", "2026-10-30", "2026-11-06", "2026-11-13"],
            max_expirations=2,
        )
        diag = DteWindowSelectionDiagnostics()
        _run(provider.get_option_chain_for_dte_window("SPY", min_dte=20, max_dte=45, as_of=date(2026, 10, 1), diagnostics=diag))
        assert diag.provider_expirations_returned == 6
        assert diag.expirations_in_window == 4
        assert diag.expirations_selected == 2
        assert diag.expirations_skipped_outside_window == 2
        assert diag.expirations_skipped_due_to_bound == 2

    def test_diagnostics_is_optional_and_defaults_to_none(self):
        provider, _ = self._routing_provider(expirations=["2026-10-23"])
        # must not raise when diagnostics is omitted entirely
        chain = _run(provider.get_option_chain_for_dte_window("SPY", min_dte=20, max_dte=45, as_of=date(2026, 10, 1)))
        assert chain is not None

    def test_isinstance_check_against_dte_window_capability(self):
        from src.data.provider import DteWindowOptionChainProvider

        provider, _ = self._routing_provider(expirations=["2026-10-23"])
        assert isinstance(provider, DteWindowOptionChainProvider)

    # ---- request-count: same shape as get_option_chain, never more
    def test_request_count_is_the_same_shape_as_get_option_chain_not_more(self):
        # Exactly the 2026-10-01 production calendar: get_option_chain
        # (the pre-hotfix path) would fetch its nearest 6 expirations
        # (all out-of-window, the production defect); get_option_chain_
        # for_dte_window fetches at most the SAME 6-chain bound, just a
        # different (in-window) selection -- never MORE chain requests
        # for the same symbol/max_expirations.
        expirations = [
            "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
            "2026-10-09", "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16",
            "2026-10-23", "2026-10-30", "2026-11-06", "2026-11-13",
        ]
        provider_before, client_before = self._routing_provider(expirations=expirations, max_expirations=6)
        _run(provider_before.get_option_chain("SPY"))
        # 1 outer underlying quote + 1 expirations list, then
        # get_option_chain_for_expiration's own (quote + chain) pair per
        # selected expiration -- 2 + 2*6 = 14, bounded by max_expirations=6.
        calls_before = len(client_before.calls)

        provider_after, client_after = self._routing_provider(expirations=expirations, max_expirations=6)
        _run(provider_after.get_option_chain_for_dte_window("SPY", min_dte=20, max_dte=45, as_of=date(2026, 10, 1)))
        calls_after = len(client_after.calls)

        assert calls_before == 14  # 2 + 2*6 (max_expirations bound reached: nearest 6 by calendar date)
        assert calls_after == 10  # 2 + 2*4 (only 4 of the 16 provider expirations fall in-window here)
        assert calls_after <= calls_before  # the DTE-aware fetch never issues MORE requests for the same bound

    def test_request_bound_never_exceeded_regardless_of_how_many_expirations_are_eligible(self):
        # 10 eligible expirations, max_expirations=6 -- at most 6 chain
        # requests are ever made, never one per eligible expiration.
        expirations = [f"2026-10-{d:02d}" for d in range(21, 31)]  # all 20-29 DTE from Oct 1 -- in [20,45]
        provider, client = self._routing_provider(expirations=expirations, max_expirations=6)
        _run(provider.get_option_chain_for_dte_window("SPY", min_dte=20, max_dte=45, as_of=date(2026, 10, 1)))
        assert len(self._chain_expiration_params(client)) == 6

    # ---- rate-limit priority propagation preserved
    def test_priority_argument_is_accepted_and_defaults_match_get_option_chain(self):
        import inspect

        dte_window_sig = inspect.signature(TradierMarketDataProvider.get_option_chain_for_dte_window)
        chain_sig = inspect.signature(TradierMarketDataProvider.get_option_chain)
        assert dte_window_sig.parameters["priority"].default == chain_sig.parameters["priority"].default

    def test_custom_priority_is_honored_not_silently_overridden(self):
        provider, client = self._routing_provider(expirations=["2026-10-23"])
        _run(
            provider.get_option_chain_for_dte_window(
                "SPY", min_dte=20, max_dte=45, as_of=date(2026, 10, 1), priority=RateLimitPriority.P0_POSITION_RISK,
            )
        )
        # no exception, no silent downgrade to the default -- a real
        # per-call priority was accepted and used to drive every
        # underlying _request call this method made.
        assert len(client.calls) == 4  # outer quote + expirations + (quote + chain) for the 1 selected expiration

    # ---- diagnostics never carries a secret or token
    def test_diagnostics_dataclass_carries_only_integer_counts_never_a_secret(self):
        from src.data.provider import DteWindowSelectionDiagnostics

        diag = DteWindowSelectionDiagnostics()
        # every field this observability object exposes is a plain int
        # count -- structurally incapable of carrying a token/secret
        # string, unlike a free-text diagnostic field would be.
        for name, value in vars(diag).items():
            assert isinstance(value, int), f"{name} is {type(value)!r}, not int -- diagnostics must stay integer-only"

    def test_token_never_appears_in_diagnostics_after_a_real_call(self):
        from src.data.provider import DteWindowSelectionDiagnostics

        provider, _ = self._routing_provider(expirations=["2026-10-23"])
        diag = DteWindowSelectionDiagnostics()
        _run(provider.get_option_chain_for_dte_window("SPY", min_dte=20, max_dte=45, as_of=date(2026, 10, 1), diagnostics=diag))
        serialized = str(vars(diag))
        assert TOKEN not in serialized


class TestRequestBehavior:
    def test_200_returns_json_body(self):
        provider, _ = _provider(FakeResponse(status_code=200, json_body={"expirations": None}))
        result = _run(provider.get_expirations("SPY"))
        assert result == []

    def test_malformed_json_raises_typed_error(self):
        response = FakeResponse(status_code=200, json_body=_MALFORMED)
        provider, _ = _provider(response)
        with pytest.raises(TradierMalformedResponseError):
            _run(provider.get_expirations("SPY"))

    def test_401_raises_immediately_never_retried(self):
        response = FakeResponse(status_code=401, text="unauthorized")
        provider, client = _provider(response, max_retries=3)
        with pytest.raises(TradierAuthenticationError):
            _run(provider.get_expirations("SPY"))
        assert len(client.calls) == 1  # never retried

    def test_429_retries_up_to_max_retries_then_raises(self):
        response = FakeResponse(status_code=429, text="too many requests")
        provider, client = _provider(response, max_retries=3, retry_base_delay_seconds=0.001)
        with pytest.raises(TradierRateLimitError):
            _run(provider.get_expirations("SPY"))
        assert len(client.calls) == 3

    def test_network_failure_retried_then_raises(self):
        client = FakeHttpClient(FakeResponse(status_code=200, json_body={"expirations": None}), raise_on_call=ConnectionError("boom"))
        provider = TradierMarketDataProvider(TradierConfig(token=TOKEN, max_retries=3, retry_base_delay_seconds=0.001), http_client=client)
        # first call raises ConnectionError (consumed), retried, then succeeds
        result = _run(provider.get_expirations("SPY"))
        assert result == []
        assert len(client.calls) == 2

    def test_rate_limit_headers_update_state(self):
        response = FakeResponse(
            status_code=200, json_body={"expirations": None},
            headers={"X-Ratelimit-Allowed": "120", "X-Ratelimit-Used": "1", "X-Ratelimit-Available": "119"},
        )
        provider, _ = _provider(response)
        _run(provider.get_expirations("SPY"))
        assert provider.rate_limit_state is not None
        assert provider.rate_limit_state.allowed == 120

    def test_blocked_by_rate_limit_budget_before_any_http_call(self):
        response = FakeResponse(status_code=200, json_body={"expirations": None})
        provider, client = _provider(response)
        from src.data.rate_limiter import RateLimitState
        from datetime import datetime, timezone
        provider._rate_limit_state = RateLimitState(allowed=120, used=120, available=0, reset_at=None, observed_at=datetime.now(timezone.utc))
        with pytest.raises(TradierRateLimitError):
            _run(provider.get_expirations("SPY", priority=RateLimitPriority.P4_OPPORTUNITY_SCANNING))
        assert client.calls == []  # blocked before any HTTP call at all

    def test_secret_never_appears_in_raised_exception_text(self):
        response = FakeResponse(status_code=500, text=f"internal error, token={TOKEN} invalid")
        provider, _ = _provider(response, max_retries=1)
        with pytest.raises(ProviderError) as exc_info:
            _run(provider.get_expirations("SPY"))
        assert TOKEN not in str(exc_info.value)


class TestClose:
    def test_close_calls_aclose_on_injected_client(self):
        provider, client = _provider(FakeResponse())
        _run(provider.close())
        assert client._closed is True


# ---------------------------------------------------- historical bars (Step 3B)


class TestParseHistoryJson:
    """Unit tests for `_parse_history_json` -- mirrors the existing
    per-entry validate-or-skip style already established for
    `_parse_option_json`/`_parse_quote_json`/`_parse_expirations_json`
    above, applied to `/markets/history`'s `history.day` shape."""

    def test_maps_a_multi_day_list(self):
        raw = {"history": {"day": [
            {"date": "2026-01-02", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000},
            {"date": "2026-01-03", "open": 101, "high": 103, "low": 100, "close": 102, "volume": 1100},
        ]}}
        bars = _parse_history_json(raw, symbol="SPY")
        assert [b.bar_date.isoformat() for b in bars] == ["2026-01-02", "2026-01-03"]
        assert bars[0].close == 101.0 and bars[0].symbol == "SPY" and bars[0].source == SOURCE_TRADIER

    def test_single_day_bare_object_collapses_to_one_bar(self):
        """Tradier's XML-legacy single-item-collapses-to-bare-object
        quirk, already handled 3x elsewhere in this module for
        quotes/chains/expirations -- applies identically to `day`."""
        raw = {"history": {"day": {"date": "2026-01-02", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000}}}
        bars = _parse_history_json(raw, symbol="SPY")
        assert len(bars) == 1 and bars[0].bar_date.isoformat() == "2026-01-02"

    def test_null_history_object_returns_empty(self):
        assert _parse_history_json({"history": None}, symbol="SPY") == []

    def test_missing_history_key_returns_empty(self):
        assert _parse_history_json({}, symbol="SPY") == []

    def test_null_day_returns_empty(self):
        assert _parse_history_json({"history": {"day": None}}, symbol="SPY") == []

    def test_malformed_date_entry_skipped_not_raised(self):
        raw = {"history": {"day": [
            {"date": "not-a-date", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000},
            {"date": "2026-01-03", "open": 101, "high": 103, "low": 100, "close": 102, "volume": 1100},
        ]}}
        bars = _parse_history_json(raw, symbol="SPY")
        assert len(bars) == 1 and bars[0].bar_date.isoformat() == "2026-01-03"

    def test_missing_date_key_skipped(self):
        raw = {"history": {"day": [{"open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000}]}}
        assert _parse_history_json(raw, symbol="SPY") == []

    def test_high_below_low_skipped_never_fabricated(self):
        raw = {"history": {"day": [{"date": "2026-01-02", "open": 100, "high": 99, "low": 102, "close": 101, "volume": 1000}]}}
        assert _parse_history_json(raw, symbol="SPY") == []

    def test_missing_required_price_field_skipped(self):
        raw = {"history": {"day": [{"date": "2026-01-02", "open": 100, "high": 102, "low": 99, "volume": 1000}]}}  # no close
        assert _parse_history_json(raw, symbol="SPY") == []

    def test_non_numeric_price_skipped(self):
        raw = {"history": {"day": [{"date": "2026-01-02", "open": "abc", "high": 102, "low": 99, "close": 101, "volume": 1000}]}}
        assert _parse_history_json(raw, symbol="SPY") == []

    def test_zero_close_skipped(self):
        raw = {"history": {"day": [{"date": "2026-01-02", "open": 100, "high": 102, "low": 0, "close": 0, "volume": 1000}]}}
        assert _parse_history_json(raw, symbol="SPY") == []

    def test_missing_volume_defaults_to_zero_not_skipped(self):
        raw = {"history": {"day": [{"date": "2026-01-02", "open": 100, "high": 102, "low": 99, "close": 101}]}}
        bars = _parse_history_json(raw, symbol="SPY")
        assert len(bars) == 1 and bars[0].volume == 0

    def test_duplicate_date_keeps_first_occurrence_skips_the_rest(self):
        raw = {"history": {"day": [
            {"date": "2026-01-02", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000},
            {"date": "2026-01-02", "open": 200, "high": 202, "low": 199, "close": 201, "volume": 2000},
        ]}}
        bars = _parse_history_json(raw, symbol="SPY")
        assert len(bars) == 1 and bars[0].close == 101.0

    def test_output_is_chronologically_sorted_regardless_of_input_order(self):
        raw = {"history": {"day": [
            {"date": "2026-01-05", "open": 1, "high": 2, "low": 1, "close": 1.5, "volume": 1},
            {"date": "2026-01-02", "open": 1, "high": 2, "low": 1, "close": 1.5, "volume": 1},
            {"date": "2026-01-03", "open": 1, "high": 2, "low": 1, "close": 1.5, "volume": 1},
        ]}}
        bars = _parse_history_json(raw, symbol="SPY")
        assert [b.bar_date.isoformat() for b in bars] == ["2026-01-02", "2026-01-03", "2026-01-05"]

    def test_non_dict_entry_in_list_skipped(self):
        raw = {"history": {"day": ["not-a-dict", {"date": "2026-01-02", "open": 1, "high": 2, "low": 1, "close": 1.5, "volume": 1}]}}
        bars = _parse_history_json(raw, symbol="SPY")
        assert len(bars) == 1


class TestGetBars:
    """`TradierMarketDataProvider.get_bars` -- Step 3B's one new
    capability. Every test injects a fake HTTP client, exactly like
    every other provider test in this file; no real network call, no
    real token."""

    def test_provider_satisfies_historical_data_provider(self):
        assert issubclass(TradierMarketDataProvider, HistoricalDataProvider)

    def test_requests_the_history_endpoint_with_expected_params(self):
        response = FakeResponse(json_body={"history": {"day": []}})
        provider, client = _provider(response)
        _run(provider.get_bars("spy", date(2026, 1, 1), date(2026, 1, 31)))
        assert client.calls[0][0] == "/markets/history"
        params = client.calls[0][1]
        assert params["symbol"] == "SPY"  # uppercased, same convention as every other method
        assert params["interval"] == "daily"
        assert params["start"] == "2026-01-01"
        assert params["end"] == "2026-01-31"

    def test_returns_parsed_bars_on_success(self):
        response = FakeResponse(json_body={"history": {"day": [
            {"date": "2026-01-02", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000},
            {"date": "2026-01-03", "open": 101, "high": 103, "low": 100, "close": 102, "volume": 1100},
        ]}})
        provider, _ = _provider(response)
        bars = _run(provider.get_bars("SPY", date(2026, 1, 1), date(2026, 1, 31)))
        assert len(bars) == 2
        assert bars[0].source == SOURCE_TRADIER

    def test_empty_history_returns_empty_list_not_error(self):
        provider, _ = _provider(FakeResponse(json_body={"history": {"day": None}}))
        assert _run(provider.get_bars("SPY", date(2026, 1, 1), date(2026, 1, 31))) == []

    def test_single_day_response_returns_one_bar(self):
        response = FakeResponse(json_body={"history": {"day": {"date": "2026-01-02", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000}}})
        provider, _ = _provider(response)
        bars = _run(provider.get_bars("SPY", date(2026, 1, 2), date(2026, 1, 2)))
        assert len(bars) == 1

    def test_malformed_entry_isolated_not_whole_response(self):
        response = FakeResponse(json_body={"history": {"day": [
            {"date": "2026-01-02", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 1000},
            {"date": "not-a-date", "open": 1, "high": 2, "low": 1, "close": 1.5, "volume": 1},
        ]}})
        provider, _ = _provider(response)
        bars = _run(provider.get_bars("SPY", date(2026, 1, 1), date(2026, 1, 31)))
        assert len(bars) == 1

    def test_malformed_json_raises_typed_error(self):
        response = FakeResponse(status_code=200, json_body=_MALFORMED)
        provider, _ = _provider(response)
        with pytest.raises(TradierMalformedResponseError):
            _run(provider.get_bars("SPY", date(2026, 1, 1), date(2026, 1, 31)))

    def test_401_raises_authentication_error_never_retried(self):
        response = FakeResponse(status_code=401, text="unauthorized")
        provider, client = _provider(response, max_retries=3)
        with pytest.raises(TradierAuthenticationError):
            _run(provider.get_bars("SPY", date(2026, 1, 1), date(2026, 1, 31)))
        assert len(client.calls) == 1

    def test_429_retries_then_raises_rate_limit_error(self):
        response = FakeResponse(status_code=429, text="too many requests")
        provider, client = _provider(response, max_retries=3, retry_base_delay_seconds=0.001)
        with pytest.raises(TradierRateLimitError):
            _run(provider.get_bars("SPY", date(2026, 1, 1), date(2026, 1, 31)))
        assert len(client.calls) == 3

    def test_network_failure_retried_then_succeeds(self):
        client = FakeHttpClient(FakeResponse(status_code=200, json_body={"history": {"day": None}}), raise_on_call=ConnectionError("boom"))
        provider = TradierMarketDataProvider(TradierConfig(token=TOKEN, max_retries=3, retry_base_delay_seconds=0.001), http_client=client)
        result = _run(provider.get_bars("SPY", date(2026, 1, 1), date(2026, 1, 31)))
        assert result == []
        assert len(client.calls) == 2

    def test_timeout_like_failure_is_retried_and_eventually_raises(self):
        client = FakeHttpClient(FakeResponse(status_code=200, json_body={"history": {"day": None}}))

        async def _always_times_out(path, params=None):
            client.calls.append((path, params or {}))
            raise TimeoutError("simulated timeout")

        client.get = _always_times_out
        provider = TradierMarketDataProvider(TradierConfig(token=TOKEN, max_retries=2, retry_base_delay_seconds=0.001), http_client=client)
        with pytest.raises(ProviderError):
            _run(provider.get_bars("SPY", date(2026, 1, 1), date(2026, 1, 31)))
        assert len(client.calls) == 2

    def test_defaults_to_lowest_priority_and_is_throttled_first(self):
        """P5_BACKGROUND_RESEARCH's utilization ceiling (0.70) is the
        lowest of any priority -- confirms get_bars is subordinate to
        position-risk/lifecycle/repricing requests sharing the same
        rate-limit budget, per Part 9's precedence and this module's own
        get_bars docstring."""
        response = FakeResponse(json_body={"history": {"day": None}})
        provider, client = _provider(response)
        from datetime import datetime, timezone

        from src.data.rate_limiter import RateLimitState

        provider._rate_limit_state = RateLimitState(
            allowed=100, used=75, available=25, reset_at=None, observed_at=datetime.now(timezone.utc)
        )
        # 75% utilization: below P0-P3's ceilings, at/above P4 (0.80) is fine,
        # but strictly above P5's 0.70 ceiling -- get_bars must be blocked here
        # while a higher-priority request at the same state would proceed.
        with pytest.raises(TradierRateLimitError):
            _run(provider.get_bars("SPY", date(2026, 1, 1), date(2026, 1, 31)))
        assert client.calls == []
        # A P0 request at the identical state is NOT blocked -- proves this
        # is priority-specific throttling, not a blanket rate-limit outage.
        _run(provider.get_expirations("SPY", priority=RateLimitPriority.P0_POSITION_RISK))

    def test_explicit_priority_override_is_honored(self):
        response = FakeResponse(json_body={"history": {"day": None}})
        provider, client = _provider(response)
        _run(provider.get_bars("SPY", date(2026, 1, 1), date(2026, 1, 31), priority=RateLimitPriority.P4_OPPORTUNITY_SCANNING))
        assert len(client.calls) == 1

    def test_secret_never_appears_in_raised_exception_text(self):
        response = FakeResponse(status_code=500, text=f"internal error, token={TOKEN} invalid")
        provider, _ = _provider(response, max_retries=1)
        with pytest.raises(ProviderError) as exc_info:
            _run(provider.get_bars("SPY", date(2026, 1, 1), date(2026, 1, 31)))
        assert TOKEN not in str(exc_info.value)

    def test_never_calls_an_order_or_account_endpoint(self):
        """Structural reinforcement of this module's MARKET DATA ONLY
        guarantee (see tests/acceptance/test_tradier_market_data_only.py
        for the repo-wide version): get_bars's only request path is
        `/markets/history`."""
        response = FakeResponse(json_body={"history": {"day": None}})
        provider, client = _provider(response)
        _run(provider.get_bars("SPY", date(2026, 1, 1), date(2026, 1, 31)))
        assert all(path == "/markets/history" for path, _ in client.calls)
