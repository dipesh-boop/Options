"""Tests for `src.data.factory` -- the one place `OPTIONS_AGENT_DATA_
PROVIDER` gets turned into an actual provider instance."""
from __future__ import annotations

import pytest

from src.data.factory import (
    DataProviderConfigError,
    DataProviderSelection,
    _MockMarketDataProvider,
    get_configured_market_data_provider,
)


class TestDataProviderSelection:
    def test_defaults_to_mock(self):
        assert DataProviderSelection().data_provider == "mock"


class TestGetConfiguredProvider:
    def test_mock_returns_mock_provider(self):
        provider = get_configured_market_data_provider(DataProviderSelection(data_provider="mock"))
        assert isinstance(provider, _MockMarketDataProvider)

    def test_case_insensitive(self):
        provider = get_configured_market_data_provider(DataProviderSelection(data_provider="MOCK"))
        assert isinstance(provider, _MockMarketDataProvider)

    def test_unknown_provider_name_raises_fails_closed(self):
        with pytest.raises(DataProviderConfigError):
            get_configured_market_data_provider(DataProviderSelection(data_provider="robinhood"))

    def test_alpaca_without_credentials_raises_never_silently_falls_back_to_mock(self):
        with pytest.raises(DataProviderConfigError):
            get_configured_market_data_provider(DataProviderSelection(data_provider="alpaca"))

    def test_tradier_without_token_raises_never_silently_falls_back_to_mock(self, monkeypatch):
        monkeypatch.delenv("OPTIONS_AGENT_TRADIER_TOKEN", raising=False)
        with pytest.raises(DataProviderConfigError):
            get_configured_market_data_provider(DataProviderSelection(data_provider="tradier"))

    def test_tradier_with_token_constructs_a_real_provider(self, monkeypatch):
        from src.data.tradier_provider import TradierMarketDataProvider

        monkeypatch.setenv("OPTIONS_AGENT_TRADIER_TOKEN", "fake-token-for-construction-only")
        provider = get_configured_market_data_provider(DataProviderSelection(data_provider="tradier"))
        assert isinstance(provider, TradierMarketDataProvider)

    def test_ibkr_either_constructs_or_fails_closed_with_a_clear_error(self):
        # IBKRBroker's __init__ doesn't connect -- only require_paper_port()
        # validation runs, plus building a default ib_insync client (which
        # may not be installed in every environment, e.g. this test sandbox).
        # Either a real provider comes back, or a clear, typed config error
        # does -- never a silent substitution and never an unhandled crash.
        try:
            provider = get_configured_market_data_provider(DataProviderSelection(data_provider="ibkr"))
        except DataProviderConfigError:
            return
        assert hasattr(provider, "get_option_chain")
        assert hasattr(provider, "get_underlying_quote")


class TestMockMarketDataProvider:
    @pytest.mark.asyncio
    async def test_get_underlying_quote_returns_canonical_quote(self):
        provider = _MockMarketDataProvider()
        quote = await provider.get_underlying_quote("aapl")
        assert quote.symbol == "AAPL"
        assert quote.source == "mock"

    @pytest.mark.asyncio
    async def test_get_option_chain_returns_empty_contracts(self):
        provider = _MockMarketDataProvider()
        chain = await provider.get_option_chain("aapl")
        assert chain.contracts == []
        assert chain.source == "mock"
