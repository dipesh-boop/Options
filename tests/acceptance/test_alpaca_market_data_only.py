"""Hostile-audit-style acceptance tests for the Alpaca market-data
amendment: proves Alpaca is market-data-only, never gains an
order-submission path, never touches PaperBroker/Fidelity/Risk Engine
authority, and never leaks credentials -- the exact guarantees the
governing instruction's Parts 2, 13-15, and 18 require before this
amendment can be considered safe to freeze.

Mirrors the repo-wide-grep style already established by
`tests/acceptance/test_fidelity_manual_only.py` (FS-001/FS-002/FS-003)
and `tests/unit/brokers/test_fidelity_no_execution.py`.
"""
from __future__ import annotations

import inspect
import re
import socket
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"

_ORDER_SHAPED_METHOD_PATTERN = re.compile(
    r"\b(place_order|submit_order|cancel_order|modify_order|amend_order|close_position|"
    r"submit_trade|execute_trade|place_trade|send_order)\b", re.IGNORECASE,
)


def _iter_src_python_files():
    for path in SRC_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        yield path


class TestNoAlpacaTradingClientImportAnywhere:
    """The single most important structural guarantee: this repository
    never imports `alpaca.trading` (the order-submission client) in any
    file, not just the two Alpaca provider modules."""

    def test_no_alpaca_trading_import_in_src(self):
        pattern = re.compile(r"^\s*(from|import)\s+alpaca\.trading\b", re.MULTILINE)
        offending = []
        for path in _iter_src_python_files():
            text = path.read_text(errors="ignore")
            if pattern.search(text):
                offending.append(str(path.relative_to(REPO_ROOT)))
        assert offending == [], f"alpaca.trading imported in: {offending}"

    def test_no_alpaca_trading_import_anywhere_in_the_repository(self):
        pattern = re.compile(r"^\s*(from|import)\s+alpaca\.trading\b", re.MULTILINE)
        offending = []
        for path in REPO_ROOT.rglob("*.py"):
            if "__pycache__" in path.parts or ".git" in path.parts:
                continue
            text = path.read_text(errors="ignore")
            if pattern.search(text):
                offending.append(str(path.relative_to(REPO_ROOT)))
        assert offending == [], f"alpaca.trading imported in: {offending}"

    def test_trading_client_symbol_never_referenced_in_src(self):
        offending = []
        for path in _iter_src_python_files():
            text = path.read_text(errors="ignore")
            if "TradingClient" in text:
                offending.append(str(path.relative_to(REPO_ROOT)))
        assert offending == [], f"TradingClient referenced in: {offending}"


class TestAlpacaProviderHasNoOrderSubmissionCapability:
    def test_market_data_provider_is_not_a_broker_subclass(self):
        from src.brokers.base import Broker
        from src.data.alpaca_provider import AlpacaMarketDataProvider

        assert not issubclass(AlpacaMarketDataProvider, Broker)

    def test_historical_provider_is_not_a_broker_subclass(self):
        from src.brokers.base import Broker
        from src.data.alpaca_historical import AlpacaHistoricalDataProvider

        assert not issubclass(AlpacaHistoricalDataProvider, Broker)

    def test_no_order_shaped_method_name_on_market_data_provider(self):
        from src.data.alpaca_provider import AlpacaMarketDataProvider

        for name in dir(AlpacaMarketDataProvider):
            assert not _ORDER_SHAPED_METHOD_PATTERN.search(name), f"order-shaped method name found: {name}"

    def test_no_order_shaped_method_name_on_historical_provider(self):
        from src.data.alpaca_historical import AlpacaHistoricalDataProvider

        for name in dir(AlpacaHistoricalDataProvider):
            assert not _ORDER_SHAPED_METHOD_PATTERN.search(name), f"order-shaped method name found: {name}"

    def test_public_surface_is_exactly_the_two_market_data_provider_methods(self):
        from src.data.alpaca_provider import AlpacaMarketDataProvider
        from src.data.provider import MarketDataProvider

        public = {
            name for name in dir(AlpacaMarketDataProvider)
            if not name.startswith("_") and callable(getattr(AlpacaMarketDataProvider, name))
        }
        # Exactly MarketDataProvider's own contract (get_option_chain,
        # get_underlying_quote, close) -- nothing extra.
        expected = {
            name for name in dir(MarketDataProvider)
            if not name.startswith("_") and callable(getattr(MarketDataProvider, name))
        }
        assert public == expected, f"unexpected extra public method(s): {public - expected}"

    def test_no_order_shaped_source_identifier_in_either_module(self):
        # "alpaca.trading" itself is deliberately not checked here -- both
        # modules' own docstrings name it once, exactly to document that it
        # is NOT imported (the same documented-negation pattern
        # `test_fidelity_manual_only.py`'s FS-001 already establishes for
        # "fidelity.com"). `TestNoAlpacaTradingClientImportAnywhere` above
        # is the precise, import-statement-only check for that.
        for module_path in ("src/data/alpaca_provider.py", "src/data/alpaca_historical.py"):
            text = (REPO_ROOT / module_path).read_text()
            for forbidden in ("place_order", "submit_order", "cancel_order", "TradingClient"):
                assert forbidden not in text, f"{forbidden!r} unexpectedly present in {module_path}"


class TestAlpacaCanonicalBoundary:
    """Proves the Risk/Quant Engines see exactly the same canonical
    types from Alpaca as from any other provider -- no special-casing,
    no raw Alpaca object ever crosses the boundary."""

    @pytest.mark.asyncio
    async def test_option_chain_output_passes_ensure_canonical(self):
        from alpaca.data.models.quotes import Quote
        from alpaca.data.models.snapshots import OptionsGreeks, OptionsSnapshot
        from alpaca.data.models.trades import Trade

        from src.data.alpaca_provider import AlpacaConfig, AlpacaMarketDataProvider
        from src.data.option_chain import OptionChain
        from src.data.provider import ensure_canonical
        from src.data.quotes import UnderlyingQuote

        NOW_TS = __import__("datetime").datetime(2026, 9, 22, 15, 0, tzinfo=__import__("datetime").timezone.utc)

        def make_quote(symbol, bid, ask):
            return Quote.model_construct(symbol=symbol, timestamp=NOW_TS, bid_price=bid, bid_size=1.0, ask_price=ask, ask_size=1.0, bid_exchange=None, ask_exchange=None, conditions=None, tape=None)

        class FakeStock:
            def get_stock_latest_quote(self, request):
                return {"AAPL": make_quote("AAPL", 229.5, 229.7)}

        class FakeOption:
            def get_option_chain(self, request):
                occ = "AAPL251219C00230000"
                snap = OptionsSnapshot.model_construct(
                    symbol=occ, latest_quote=make_quote(occ, 5.0, 5.2),
                    latest_trade=Trade.model_construct(symbol=occ, timestamp=NOW_TS, exchange="X", price=5.1, size=1.0, id=1, conditions=None, tape=None),
                    implied_volatility=0.28, greeks=OptionsGreeks.model_construct(delta=0.5, gamma=0.02, rho=0.01, theta=-0.03, vega=0.1),
                )
                return {occ: snap}

        provider = AlpacaMarketDataProvider(AlpacaConfig(api_key="k", api_secret="s"), stock_client=FakeStock(), option_client=FakeOption())
        quote = await provider.get_underlying_quote("AAPL")
        ensure_canonical(quote, UnderlyingQuote)  # raises TypeError if not an exact canonical instance
        chain = await provider.get_option_chain("AAPL")
        ensure_canonical(chain, OptionChain)


class TestAlpacaNeverTouchesFidelityOrPaperBrokerOrRiskAuthority:
    def test_brokers_yaml_does_not_list_alpaca_as_an_execution_broker(self):
        brokers_cfg = yaml.safe_load((REPO_ROOT / "config" / "brokers.yaml").read_text())
        assert "alpaca" not in {k.lower() for k in brokers_cfg}

    def test_brokers_yaml_fidelity_execution_mode_still_manual(self):
        brokers_cfg = yaml.safe_load((REPO_ROOT / "config" / "brokers.yaml").read_text())
        assert brokers_cfg["fidelity"]["execution_mode"] == "MANUAL"

    def test_broker_environment_still_has_exactly_one_paper_member(self):
        from src.brokers.base import BrokerEnvironment

        assert list(BrokerEnvironment) == [BrokerEnvironment.PAPER]

    def test_alpaca_modules_never_import_paper_broker_or_fidelity(self):
        for module_path in ("src/data/alpaca_provider.py", "src/data/alpaca_historical.py", "src/data/provider_health.py", "src/data/factory.py"):
            text = (REPO_ROOT / module_path).read_text()
            assert "src.brokers.paper" not in text, f"{module_path} unexpectedly imports src.brokers.paper"
            assert "src.brokers.fidelity" not in text, f"{module_path} unexpectedly imports src.brokers.fidelity"

    def test_risk_engine_module_never_imports_alpaca(self):
        text = (REPO_ROOT / "src" / "risk" / "engine.py").read_text()
        assert "alpaca" not in text.lower()

    def test_alpaca_config_selection_does_not_alter_risk_limits_file(self):
        # config/risk_limits.yaml has no data-provider-conditional logic of
        # any kind -- confirms the Risk Engine's limits are wholly
        # independent of which market-data provider is selected.
        text = (REPO_ROOT / "config" / "risk_limits.yaml").read_text()
        assert "alpaca" not in text.lower()


class TestNoCredentialLeakage:
    def test_no_print_or_log_statement_references_api_key_or_secret_value(self):
        forbidden_call_pattern = re.compile(r"(print|log(ger)?\.\w+)\([^)]*\b(api_key|api_secret)\b", re.IGNORECASE)
        for module_path in ("src/data/alpaca_provider.py", "src/data/alpaca_historical.py"):
            text = (REPO_ROOT / module_path).read_text()
            assert not forbidden_call_pattern.search(text), f"{module_path} appears to log a credential value"

    def test_error_messages_never_format_in_the_actual_key_or_secret_value(self):
        from src.data.alpaca_provider import AlpacaAuthenticationError, AlpacaConfig, AlpacaMarketDataProvider

        secret_value = "sk-super-secret-value-12345"
        with pytest.raises(AlpacaAuthenticationError) as exc_info:
            AlpacaMarketDataProvider(AlpacaConfig(api_key=None, api_secret=secret_value))
        assert secret_value not in str(exc_info.value)

    def test_no_credential_shaped_field_name_beyond_api_key_and_api_secret(self):
        from src.data.alpaca_provider import AlpacaConfig

        forbidden_substrings = ("password", "cookie", "session_token", "mfa", "otp")
        for field_name in AlpacaConfig.model_fields:
            lowered = field_name.lower()
            assert not any(f in lowered for f in forbidden_substrings), f"suspicious field name: {field_name}"


class TestNoNetworkActivityFromFakeClients:
    """Mirrors `test_fidelity_no_execution.py`'s strongest proof: with a
    fake client injected (the only way these tests ever run), the
    provider genuinely never opens a socket -- the real `alpaca`
    package is imported (module-level classes exist) but never
    instantiated or called when a fake is supplied."""

    @pytest.mark.asyncio
    async def test_get_option_chain_with_injected_fakes_never_opens_a_socket(self):
        from alpaca.data.models.quotes import Quote
        from alpaca.data.models.snapshots import OptionsSnapshot
        from alpaca.data.models.trades import Trade

        from src.data.alpaca_provider import AlpacaConfig, AlpacaMarketDataProvider

        NOW_TS = __import__("datetime").datetime(2026, 9, 22, 15, 0, tzinfo=__import__("datetime").timezone.utc)

        def make_quote(symbol, bid, ask):
            return Quote.model_construct(symbol=symbol, timestamp=NOW_TS, bid_price=bid, bid_size=1.0, ask_price=ask, ask_size=1.0, bid_exchange=None, ask_exchange=None, conditions=None, tape=None)

        class FakeStock:
            def get_stock_latest_quote(self, request):
                return {"AAPL": make_quote("AAPL", 100.0, 101.0)}

        class FakeOption:
            def get_option_chain(self, request):
                return {}

        provider = AlpacaMarketDataProvider(AlpacaConfig(api_key="k", api_secret="s"), stock_client=FakeStock(), option_client=FakeOption())

        original_socket = socket.socket

        def _raise_if_socket_opened(*args, **kwargs):
            raise AssertionError("a real socket was opened during a fake-client call")

        socket.socket = _raise_if_socket_opened
        try:
            await provider.get_option_chain("AAPL")
        finally:
            socket.socket = original_socket
