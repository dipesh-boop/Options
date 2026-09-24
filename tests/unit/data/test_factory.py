"""Tests for `src.data.factory` -- the one place `OPTIONS_AGENT_DATA_
PROVIDER` gets turned into an actual provider instance."""
from __future__ import annotations

import pytest

from src.data.factory import (
    DataProviderConfigError,
    DataProviderSelection,
    OfficialProviderPreflightError,
    _MockMarketDataProvider,
    get_configured_market_data_provider,
    verify_official_provider_is_tradier_production,
)


class TestDataProviderSelection:
    def test_defaults_to_mock(self):
        assert DataProviderSelection().data_provider == "mock"

    def test_blank_env_value_is_treated_as_unset(self, monkeypatch):
        """Step 22.8 (PAPER_TRADING_V1.4.7): the shipped .env template
        leaves OPTIONS_AGENT_DATA_PROVIDER blank by convention -- a
        blank-but-present override must still resolve to "mock", never
        an empty-string provider name that would fail resolution."""
        monkeypatch.setenv("OPTIONS_AGENT_DATA_PROVIDER", "")
        assert DataProviderSelection().data_provider == "mock"

    def test_nonblank_env_override_still_works(self, monkeypatch):
        monkeypatch.setenv("OPTIONS_AGENT_DATA_PROVIDER", "tradier")
        assert DataProviderSelection().data_provider == "tradier"


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


class TestVerifyOfficialProviderIsTradierProduction:
    """Step 22.6 (PAPER_TRADING_V1.4.5): the policy check the official,
    state-mutating validation cycle runs before its first mutation.
    Distinct from `DataProviderConfigError` (a provider that's simply
    broken) -- this is "the provider is fine, but it isn't the one
    official validation is allowed to run against"."""

    def _tradier_config(self, **overrides):
        from src.data.tradier_provider import TradierConfig

        defaults = dict(token="fake-token", base_url="https://api.tradier.com/v1")
        defaults.update(overrides)
        return TradierConfig(**defaults)

    def test_mock_is_rejected(self):
        with pytest.raises(OfficialProviderPreflightError, match="tradier"):
            verify_official_provider_is_tradier_production(DataProviderSelection(data_provider="mock"))

    def test_alpaca_is_rejected(self):
        with pytest.raises(OfficialProviderPreflightError, match="tradier"):
            verify_official_provider_is_tradier_production(DataProviderSelection(data_provider="alpaca"))

    def test_ibkr_is_rejected(self):
        with pytest.raises(OfficialProviderPreflightError, match="tradier"):
            verify_official_provider_is_tradier_production(DataProviderSelection(data_provider="ibkr"))

    def test_unknown_provider_name_is_rejected_with_the_same_error_type(self):
        # DataProviderSelection itself never validates data_provider's
        # value -- get_configured_market_data_provider does that -- so
        # an unknown name must still fail closed here, never be treated
        # as "not tradier, but otherwise fine."
        with pytest.raises(OfficialProviderPreflightError, match="tradier"):
            verify_official_provider_is_tradier_production(DataProviderSelection(data_provider="robinhood"))

    def test_provider_name_is_case_insensitive(self):
        # Matches get_configured_market_data_provider's own .lower() --
        # "TRADIER" must be accepted exactly like "tradier".
        verify_official_provider_is_tradier_production(
            DataProviderSelection(data_provider="TRADIER"), self._tradier_config(),
        )

    def test_tradier_without_token_is_rejected(self):
        with pytest.raises(OfficialProviderPreflightError, match="token"):
            verify_official_provider_is_tradier_production(
                DataProviderSelection(data_provider="tradier"), self._tradier_config(token=None),
            )

    def test_tradier_with_empty_string_token_is_rejected(self):
        with pytest.raises(OfficialProviderPreflightError, match="token"):
            verify_official_provider_is_tradier_production(
                DataProviderSelection(data_provider="tradier"), self._tradier_config(token=""),
            )

    def test_tradier_sandbox_host_is_rejected(self):
        with pytest.raises(OfficialProviderPreflightError, match="production"):
            verify_official_provider_is_tradier_production(
                DataProviderSelection(data_provider="tradier"),
                self._tradier_config(base_url="https://sandbox.tradier.com/v1"),
            )

    def test_tradier_arbitrary_other_host_is_rejected(self):
        with pytest.raises(OfficialProviderPreflightError, match="production"):
            verify_official_provider_is_tradier_production(
                DataProviderSelection(data_provider="tradier"),
                self._tradier_config(base_url="https://example.com/v1"),
            )

    def test_tradier_production_url_with_trailing_slash_is_accepted(self):
        verify_official_provider_is_tradier_production(
            DataProviderSelection(data_provider="tradier"),
            self._tradier_config(base_url="https://api.tradier.com/v1/"),
        )

    def test_tradier_production_is_accepted(self):
        # No exception -- this is the pass case the official cycle and
        # --preflight both rely on to proceed.
        verify_official_provider_is_tradier_production(
            DataProviderSelection(data_provider="tradier"), self._tradier_config(),
        )

    def test_default_selection_reads_real_environment(self, monkeypatch):
        # Omitting the `selection`/`tradier_config` arguments (as the
        # runner scripts do) must read OPTIONS_AGENT_DATA_PROVIDER/
        # OPTIONS_AGENT_TRADIER_* from the real environment, exactly
        # like DataProviderSelection()/TradierConfig() do everywhere
        # else in this codebase.
        monkeypatch.setenv("OPTIONS_AGENT_DATA_PROVIDER", "mock")
        with pytest.raises(OfficialProviderPreflightError, match="tradier"):
            verify_official_provider_is_tradier_production()

    def test_error_message_never_contains_the_token(self):
        try:
            verify_official_provider_is_tradier_production(
                DataProviderSelection(data_provider="tradier"),
                self._tradier_config(token=None),
            )
        except OfficialProviderPreflightError as exc:
            assert "fake-token" not in str(exc)
