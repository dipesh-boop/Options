"""Tests for `src.data.alpaca_historical`. As with the market-data
provider tests, a fake stock client (only `get_stock_bars`) stands in
for the real `alpaca` package."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.data.alpaca_historical import AlpacaHistoricalDataProvider
from src.data.alpaca_provider import AlpacaConfig
from src.data.provider import ProviderError


def _make_bar(*, ts, open_, high, low, close, volume):
    from alpaca.data.models.bars import Bar

    return Bar.model_construct(symbol="AAPL", timestamp=ts, open=open_, high=high, low=low, close=close, volume=volume, trade_count=None, vwap=None)


class FakeStockClient:
    def __init__(self, bars_by_symbol: dict | None = None, raise_exc: Exception | None = None):
        self._bars = bars_by_symbol or {}
        self._raise = raise_exc

    def get_stock_bars(self, request):
        if self._raise is not None:
            raise self._raise
        return self._bars


def _provider(*, stock_client=None, **config_kwargs) -> AlpacaHistoricalDataProvider:
    config = AlpacaConfig(api_key="key", api_secret="secret", **config_kwargs)
    return AlpacaHistoricalDataProvider(config, stock_client=stock_client or FakeStockClient())


class TestAlpacaHistoricalDataProvider:
    def test_missing_credentials_raises_without_injected_client(self):
        with pytest.raises(ProviderError):
            AlpacaHistoricalDataProvider(AlpacaConfig(api_key=None, api_secret=None))

    @pytest.mark.asyncio
    async def test_maps_bars_correctly(self):
        bars = {"AAPL": [_make_bar(ts=datetime(2026, 9, 1, tzinfo=timezone.utc), open_=100, high=105, low=99, close=104, volume=1_000_000)]}
        provider = _provider(stock_client=FakeStockClient(bars))
        result = await provider.get_bars("aapl", date(2026, 9, 1), date(2026, 9, 2))
        assert len(result) == 1
        bar = result[0]
        assert bar.symbol == "AAPL"
        assert bar.bar_date == date(2026, 9, 1)
        assert bar.open == 100 and bar.high == 105 and bar.low == 99 and bar.close == 104
        assert bar.volume == 1_000_000
        assert bar.source == "alpaca_sip"

    @pytest.mark.asyncio
    async def test_empty_result_returns_empty_list(self):
        provider = _provider(stock_client=FakeStockClient({"AAPL": []}))
        result = await provider.get_bars("aapl", date(2026, 9, 1), date(2026, 9, 2))
        assert result == []

    @pytest.mark.asyncio
    async def test_malformed_bar_is_skipped_not_fabricated(self):
        # high < low -- HistoricalBar's own validator rejects this.
        bad_bar = _make_bar(ts=datetime(2026, 9, 1, tzinfo=timezone.utc), open_=100, high=90, low=99, close=95, volume=100)
        provider = _provider(stock_client=FakeStockClient({"AAPL": [bad_bar]}))
        result = await provider.get_bars("aapl", date(2026, 9, 1), date(2026, 9, 2))
        assert result == []

    @pytest.mark.asyncio
    async def test_provider_error_propagates_classified(self):
        class FakeExc(Exception):
            status_code = 401

        provider = _provider(stock_client=FakeStockClient(raise_exc=FakeExc("bad creds")))
        with pytest.raises(ProviderError):
            await provider.get_bars("aapl", date(2026, 9, 1), date(2026, 9, 2))

    @pytest.mark.asyncio
    async def test_source_reflects_configured_stock_feed(self):
        bars = {"AAPL": [_make_bar(ts=datetime(2026, 9, 1, tzinfo=timezone.utc), open_=100, high=105, low=99, close=104, volume=1000)]}
        provider = _provider(stock_client=FakeStockClient(bars), stock_feed="iex")
        result = await provider.get_bars("aapl", date(2026, 9, 1), date(2026, 9, 2))
        assert result[0].source == "alpaca_iex"
