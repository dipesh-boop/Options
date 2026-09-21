"""Tests for `src.data.alpaca_provider`. Every test uses a fake
stock/option client (no real `alpaca` package call, no real
credentials) -- the fakes implement only `get_stock_latest_quote`/
`get_option_chain`, exactly the two methods this module actually calls,
so these tests also serve as documentation of the module's real
surface area."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.data.alpaca_provider import (
    AlpacaAuthenticationError,
    AlpacaConfig,
    AlpacaConfigError,
    AlpacaFeedEntitlementError,
    AlpacaMarketDataProvider,
    AlpacaRateLimitError,
    OccSymbolParseError,
    classify_alpaca_error,
    parse_occ_option_symbol,
)
from src.data.option_chain import OptionRight
from src.data.provider import ProviderError

NOW = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)


def _make_quote(symbol: str, bid: float, ask: float, *, timestamp: datetime = NOW):
    from alpaca.data.models.quotes import Quote

    return Quote.model_construct(
        symbol=symbol, timestamp=timestamp, bid_price=bid, bid_size=1.0, ask_price=ask, ask_size=1.0,
        bid_exchange=None, ask_exchange=None, conditions=None, tape=None,
    )


def _make_trade(symbol: str, price: float, *, timestamp: datetime = NOW):
    from alpaca.data.models.trades import Trade

    return Trade.model_construct(symbol=symbol, timestamp=timestamp, exchange="X", price=price, size=1.0, id=1, conditions=None, tape=None)


def _make_snapshot(occ_symbol: str, *, bid=5.0, ask=5.2, trade_price=5.1, iv=0.28, delta=0.5, gamma=0.02, theta=-0.03, vega=0.1, timestamp=NOW):
    from alpaca.data.models.snapshots import OptionsGreeks, OptionsSnapshot

    greeks = OptionsGreeks.model_construct(delta=delta, gamma=gamma, rho=0.01, theta=theta, vega=vega) if delta is not None else None
    return OptionsSnapshot.model_construct(
        symbol=occ_symbol,
        latest_quote=_make_quote(occ_symbol, bid, ask, timestamp=timestamp) if bid is not None else None,
        latest_trade=_make_trade(occ_symbol, trade_price, timestamp=timestamp) if trade_price is not None else None,
        implied_volatility=iv,
        greeks=greeks,
    )


class FakeStockClient:
    def __init__(self, quotes: dict | None = None, raise_exc: Exception | None = None):
        self._quotes = quotes or {"AAPL": _make_quote("AAPL", 229.5, 229.7)}
        self._raise = raise_exc

    def get_stock_latest_quote(self, request):
        if self._raise is not None:
            raise self._raise
        return self._quotes


class FakeOptionClient:
    def __init__(self, chain: dict | None = None, raise_exc: Exception | None = None):
        self._chain = chain if chain is not None else {"AAPL251219C00230000": _make_snapshot("AAPL251219C00230000")}
        self._raise = raise_exc

    def get_option_chain(self, request):
        if self._raise is not None:
            raise self._raise
        return self._chain


def _provider(*, stock_client=None, option_client=None, **config_kwargs) -> AlpacaMarketDataProvider:
    config = AlpacaConfig(api_key="key", api_secret="secret", **config_kwargs)
    return AlpacaMarketDataProvider(config, stock_client=stock_client or FakeStockClient(), option_client=option_client or FakeOptionClient())


class TestOccSymbolParsing:
    def test_valid_call_symbol(self):
        root, expiration, right, strike = parse_occ_option_symbol("AAPL251219C00230000")
        assert root == "AAPL"
        assert expiration == date(2025, 12, 19)
        assert right == OptionRight.CALL
        assert strike == 230.0

    def test_valid_put_symbol_with_fractional_strike(self):
        root, expiration, right, strike = parse_occ_option_symbol("SPY260320P00450500")
        assert root == "SPY"
        assert right == OptionRight.PUT
        assert strike == 450.5

    def test_short_root_symbol(self):
        root, _, _, _ = parse_occ_option_symbol("F251219C00012000")
        assert root == "F"

    @pytest.mark.parametrize("bad", ["not-a-symbol", "AAPL251219X00230000", "AAPL2512190230000", "", "AAPL251219C0023000"])
    def test_malformed_symbol_raises(self, bad):
        with pytest.raises(OccSymbolParseError):
            parse_occ_option_symbol(bad)

    def test_invalid_embedded_date_raises(self):
        with pytest.raises(OccSymbolParseError):
            parse_occ_option_symbol("AAPL251399C00230000")  # month 13


class TestAlpacaConfig:
    def test_defaults(self):
        config = AlpacaConfig()
        assert config.options_feed == "opra"
        assert config.stock_feed == "sip"

    def test_invalid_options_feed_rejected(self):
        with pytest.raises(AlpacaConfigError):
            AlpacaConfig(options_feed="bogus").validate_feeds()

    def test_invalid_stock_feed_rejected(self):
        with pytest.raises(AlpacaConfigError):
            AlpacaConfig(stock_feed="bogus").validate_feeds()

    def test_valid_indicative_feed_accepted(self):
        AlpacaConfig(options_feed="indicative").validate_feeds()


class TestAuthenticationRequired:
    def test_missing_credentials_raises_without_injected_clients(self):
        with pytest.raises(AlpacaAuthenticationError):
            AlpacaMarketDataProvider(AlpacaConfig(api_key=None, api_secret=None))

    def test_injected_clients_bypass_credential_requirement(self):
        # A caller supplying fakes for testing never needs real credentials.
        AlpacaMarketDataProvider(AlpacaConfig(api_key=None, api_secret=None), stock_client=FakeStockClient(), option_client=FakeOptionClient())


class TestGetUnderlyingQuote:
    @pytest.mark.asyncio
    async def test_maps_bid_ask_correctly(self):
        provider = _provider()
        quote = await provider.get_underlying_quote("aapl")
        assert quote.symbol == "AAPL"
        assert quote.bid == 229.5
        assert quote.ask == 229.7
        assert quote.last == pytest.approx(229.6)

    @pytest.mark.asyncio
    async def test_source_reflects_configured_stock_feed(self):
        provider = _provider(stock_feed="iex")
        quote = await provider.get_underlying_quote("aapl")
        assert quote.source == "alpaca_iex"

    @pytest.mark.asyncio
    async def test_naive_timestamp_from_sdk_is_made_timezone_aware(self):
        naive_quote = _make_quote("AAPL", 100.0, 101.0, timestamp=datetime(2026, 9, 22, 15, 0))
        provider = _provider(stock_client=FakeStockClient({"AAPL": naive_quote}))
        quote = await provider.get_underlying_quote("aapl")
        assert quote.timestamp.tzinfo is not None

    @pytest.mark.asyncio
    async def test_zero_bid_and_ask_falls_back_to_zero_last(self):
        zero_quote = _make_quote("AAPL", 0.0, 0.0)
        provider = _provider(stock_client=FakeStockClient({"AAPL": zero_quote}))
        quote = await provider.get_underlying_quote("aapl")
        assert quote.bid == 0.0 and quote.ask == 0.0 and quote.last == 0.0


class TestGetOptionChain:
    @pytest.mark.asyncio
    async def test_maps_a_full_contract_correctly(self):
        provider = _provider()
        chain = await provider.get_option_chain("aapl")
        assert len(chain.contracts) == 1
        c = chain.contracts[0]
        assert c.underlying == "AAPL"
        assert c.option_symbol == "AAPL251219C00230000"
        assert c.expiration == date(2025, 12, 19)
        assert c.right == OptionRight.CALL
        assert c.strike == 230.0
        assert c.bid == 5.0 and c.ask == 5.2
        assert c.last == 5.1
        assert c.iv == pytest.approx(0.28)
        assert c.delta == pytest.approx(0.5)
        assert c.gamma == pytest.approx(0.02)
        assert c.theta == pytest.approx(-0.03)
        assert c.vega == pytest.approx(0.1)
        assert c.underlying_price == pytest.approx(229.6)

    @pytest.mark.asyncio
    async def test_source_reflects_configured_options_feed(self):
        provider = _provider(options_feed="indicative")
        chain = await provider.get_option_chain("aapl")
        assert chain.source == "alpaca_indicative"
        assert chain.contracts[0].source == "alpaca_indicative"

    @pytest.mark.asyncio
    async def test_greeks_absent_maps_to_none(self):
        snap = _make_snapshot("AAPL251219C00230000", delta=None)
        provider = _provider(option_client=FakeOptionClient({"AAPL251219C00230000": snap}))
        chain = await provider.get_option_chain("aapl")
        c = chain.contracts[0]
        assert c.delta is None and c.gamma is None and c.theta is None and c.vega is None

    @pytest.mark.asyncio
    async def test_iv_absent_maps_to_none(self):
        snap = _make_snapshot("AAPL251219C00230000", iv=None)
        provider = _provider(option_client=FakeOptionClient({"AAPL251219C00230000": snap}))
        chain = await provider.get_option_chain("aapl")
        assert chain.contracts[0].iv is None

    @pytest.mark.asyncio
    async def test_open_interest_always_zero_alpaca_snapshot_does_not_report_it(self):
        provider = _provider()
        chain = await provider.get_option_chain("aapl")
        assert chain.contracts[0].open_interest == 0

    @pytest.mark.asyncio
    async def test_missing_quote_on_snapshot_skips_contract_not_fabricated(self):
        from alpaca.data.models.snapshots import OptionsSnapshot

        snap = OptionsSnapshot.model_construct(symbol="AAPL251219C00230000", latest_quote=None, latest_trade=None, implied_volatility=None, greeks=None)
        provider = _provider(option_client=FakeOptionClient({"AAPL251219C00230000": snap}))
        chain = await provider.get_option_chain("aapl")
        assert chain.contracts == []

    @pytest.mark.asyncio
    async def test_crossed_market_skips_contract_not_fabricated(self):
        # bid > ask -- the canonical OptionContract schema itself rejects this.
        snap = _make_snapshot("AAPL251219C00230000", bid=10.0, ask=5.0)
        provider = _provider(option_client=FakeOptionClient({"AAPL251219C00230000": snap}))
        chain = await provider.get_option_chain("aapl")
        assert chain.contracts == []

    @pytest.mark.asyncio
    async def test_zero_bid_zero_ask_contract_still_constructs(self):
        snap = _make_snapshot("AAPL251219C00230000", bid=0.0, ask=0.0, trade_price=0.5)
        provider = _provider(option_client=FakeOptionClient({"AAPL251219C00230000": snap}))
        chain = await provider.get_option_chain("aapl")
        assert len(chain.contracts) == 1
        assert chain.contracts[0].bid == 0.0 and chain.contracts[0].ask == 0.0

    @pytest.mark.asyncio
    async def test_malformed_occ_key_is_skipped_not_fabricated(self):
        good = _make_snapshot("AAPL251219C00230000")
        bad_key_chain = {"AAPL251219C00230000": good, "not-a-valid-occ-symbol": _make_snapshot("not-a-valid-occ-symbol")}
        provider = _provider(option_client=FakeOptionClient(bad_key_chain))
        chain = await provider.get_option_chain("aapl")
        assert len(chain.contracts) == 1
        assert chain.contracts[0].option_symbol == "AAPL251219C00230000"

    @pytest.mark.asyncio
    async def test_mismatched_underlying_root_is_skipped(self):
        # Defense-in-depth: a chain response keyed under a different root symbol never leaks through.
        wrong_root = {"MSFT251219C00230000": _make_snapshot("MSFT251219C00230000")}
        provider = _provider(option_client=FakeOptionClient(wrong_root))
        chain = await provider.get_option_chain("aapl")
        assert chain.contracts == []

    @pytest.mark.asyncio
    async def test_multiple_contracts_multiple_expirations_and_rights(self):
        chain_data = {
            "AAPL251219C00230000": _make_snapshot("AAPL251219C00230000"),
            "AAPL251219P00230000": _make_snapshot("AAPL251219P00230000"),
            "AAPL260116C00235000": _make_snapshot("AAPL260116C00235000"),
        }
        provider = _provider(option_client=FakeOptionClient(chain_data))
        chain = await provider.get_option_chain("aapl")
        assert len(chain.contracts) == 3
        rights = {c.right for c in chain.contracts}
        assert rights == {OptionRight.CALL, OptionRight.PUT}
        expirations = {c.expiration for c in chain.contracts}
        assert len(expirations) == 2

    @pytest.mark.asyncio
    async def test_empty_chain_returns_empty_contracts_list(self):
        provider = _provider(option_client=FakeOptionClient({}))
        chain = await provider.get_option_chain("aapl")
        assert chain.contracts == []

    @pytest.mark.asyncio
    async def test_no_usable_underlying_quote_raises_provider_error(self):
        zero_quote = _make_quote("AAPL", 0.0, 0.0)
        # Force `last` to 0 too by using a trade-less quote: the underlying quote's own `last`
        # falls back to bid/ask, both 0.
        provider = _provider(stock_client=FakeStockClient({"AAPL": zero_quote}))
        with pytest.raises(ProviderError):
            await provider.get_option_chain("aapl")


class TestErrorClassification:
    def test_auth_error_classified_by_status_code(self):
        class FakeExc(Exception):
            status_code = 401

        result = classify_alpaca_error(FakeExc("nope"))
        assert isinstance(result, AlpacaAuthenticationError)

    def test_entitlement_error_classified_by_status_code(self):
        class FakeExc(Exception):
            status_code = 403

        result = classify_alpaca_error(FakeExc("not entitled"))
        assert isinstance(result, AlpacaFeedEntitlementError)

    def test_rate_limit_error_classified_by_status_code(self):
        class FakeExc(Exception):
            status_code = 429

        result = classify_alpaca_error(FakeExc("slow down"))
        assert isinstance(result, AlpacaRateLimitError)

    def test_entitlement_error_classified_by_message_text(self):
        result = classify_alpaca_error(Exception("subscription required for this feed"))
        assert isinstance(result, AlpacaFeedEntitlementError)

    def test_unrecognized_error_classified_as_generic_provider_error(self):
        result = classify_alpaca_error(Exception("connection reset by peer"))
        assert isinstance(result, ProviderError)
        assert not isinstance(result, (AlpacaAuthenticationError, AlpacaFeedEntitlementError, AlpacaRateLimitError))


class TestRetryBehavior:
    @pytest.mark.asyncio
    async def test_entitlement_error_from_sdk_is_never_retried_and_raises_immediately(self):
        class FakeExc(Exception):
            status_code = 403

        provider = _provider(option_client=FakeOptionClient(raise_exc=FakeExc("not entitled to OPRA")))
        with pytest.raises(AlpacaFeedEntitlementError):
            await provider.get_option_chain("aapl")

    @pytest.mark.asyncio
    async def test_auth_error_from_sdk_is_never_retried_and_raises_immediately(self):
        class FakeExc(Exception):
            status_code = 401

        provider = _provider(stock_client=FakeStockClient(raise_exc=FakeExc("bad key")))
        with pytest.raises(AlpacaAuthenticationError):
            await provider.get_underlying_quote("aapl")

    @pytest.mark.asyncio
    async def test_generic_transient_error_retries_up_to_max_then_raises(self):
        class FlakyClient:
            def __init__(self):
                self.calls = 0

            def get_stock_latest_quote(self, request):
                self.calls += 1
                raise TimeoutError("connection timed out")

        flaky = FlakyClient()
        provider = _provider(stock_client=flaky, max_retries=2, retry_base_delay_seconds=0.001)
        with pytest.raises(ProviderError):
            await provider.get_underlying_quote("aapl")
        assert flaky.calls == 2

    @pytest.mark.asyncio
    async def test_transient_error_succeeds_after_one_retry(self):
        class FlakyThenOkClient:
            def __init__(self):
                self.calls = 0

            def get_stock_latest_quote(self, request):
                self.calls += 1
                if self.calls == 1:
                    raise TimeoutError("timeout")
                return {"AAPL": _make_quote("AAPL", 229.5, 229.7)}

        client = FlakyThenOkClient()
        provider = _provider(stock_client=client, max_retries=3, retry_base_delay_seconds=0.001)
        quote = await provider.get_underlying_quote("aapl")
        assert quote.bid == 229.5
        assert client.calls == 2

    @pytest.mark.asyncio
    async def test_rate_limit_exhausting_retries_raises_rate_limit_error(self):
        class FakeExc(Exception):
            status_code = 429

        provider = _provider(stock_client=FakeStockClient(raise_exc=FakeExc("rate limited")), max_retries=2, retry_base_delay_seconds=0.001)
        with pytest.raises(AlpacaRateLimitError):
            await provider.get_underlying_quote("aapl")
