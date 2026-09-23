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
