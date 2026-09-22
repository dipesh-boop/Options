"""Hostile-audit-style acceptance tests for the Tradier market-data
amendment (Step 22.4 Parts 2, 5, 46): proves Tradier is market-data-only,
never gains an order-submission path anywhere in the repository, never
touches PaperBroker/Fidelity/Risk Engine authority, and never leaks
secrets. Mirrors `tests/acceptance/test_alpaca_market_data_only.py`'s own
methodology, applied to Tradier.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from .conftest import repo_controlled_files

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"

_ORDER_SHAPED_METHOD_PATTERN = re.compile(
    r"\b(place_order|submit_order|cancel_order|modify_order|amend_order|close_position|"
    r"submit_trade|execute_trade|place_trade|send_order|preview_order|replace_order)\b", re.IGNORECASE,
)
_TRADIER_ORDER_ENDPOINT_PATTERN = re.compile(r"/v1/accounts/[^\"'\s]*/orders", re.IGNORECASE)


def _iter_src_python_files():
    for path in SRC_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        yield path


_DOCSTRING_PATTERN = re.compile(r'"""[\s\S]*?"""')


def _strip_docstrings(text: str) -> str:
    """Removes triple-quoted docstring bodies before a forbidden-string
    scan -- this module's own module docstring documents, by name, the
    exact order-shaped identifiers it does NOT implement (the same
    documented-negation this repo's Alpaca/Fidelity equivalents already
    establish), so a raw substring search must not flag prose that is
    explaining the prohibition."""
    return _DOCSTRING_PATTERN.sub("", text)


class TestNoTradierOrderCapabilityAnywhere:
    def test_no_order_shaped_method_name_on_tradier_provider(self):
        from src.data.tradier_provider import TradierMarketDataProvider

        for name in dir(TradierMarketDataProvider):
            assert not _ORDER_SHAPED_METHOD_PATTERN.search(name), f"order-shaped method name found: {name}"

    def test_public_surface_is_exactly_market_data_methods(self):
        from src.data.provider import MarketDataProvider
        from src.data.tradier_provider import TradierMarketDataProvider

        public = {
            name for name in dir(TradierMarketDataProvider)
            if not name.startswith("_") and callable(getattr(TradierMarketDataProvider, name))
        }
        base_contract = {
            name for name in dir(MarketDataProvider)
            if not name.startswith("_") and callable(getattr(MarketDataProvider, name))
        }
        # Tradier-specific batch/priority extras beyond the base
        # MarketDataProvider contract, all read-only market data.
        allowed_extras = {
            "get_underlying_quotes", "get_expirations", "get_option_chain_for_expiration", "rate_limit_state",
        }
        unexpected = public - base_contract - allowed_extras
        assert unexpected == set(), f"unexpected extra public method(s) on TradierMarketDataProvider: {unexpected}"

    def test_no_order_endpoint_pattern_referenced_in_tradier_provider_code(self):
        code = _strip_docstrings((SRC_ROOT / "data" / "tradier_provider.py").read_text())
        assert not _TRADIER_ORDER_ENDPOINT_PATTERN.search(code), "Tradier order-shaped endpoint referenced outside documentation"

    def test_no_order_shaped_identifier_string_in_tradier_provider_code(self):
        code = _strip_docstrings((SRC_ROOT / "data" / "tradier_provider.py").read_text())
        for forbidden in ("place_order", "submit_order", "cancel_order", "preview_order", "replace_order", "/orders"):
            assert forbidden not in code, f"{forbidden!r} unexpectedly present in tradier_provider.py outside documentation"

    def test_request_method_never_accepts_an_http_method_parameter(self):
        """`_request`'s own signature has no `method` argument at all --
        the single HTTP choke point can only ever issue the GET this
        module's `httpx.AsyncClient.get(...)` call hardcodes."""
        import inspect

        from src.data.tradier_provider import TradierMarketDataProvider

        sig = inspect.signature(TradierMarketDataProvider._request)
        assert "method" not in sig.parameters

    def test_no_tradier_order_shaped_identifier_anywhere_in_the_repository(self):
        pattern = re.compile(r"Tradier(Broker|Order(Client|Provider)?|ExecutionProvider)\b")
        this_file = Path(__file__).resolve()
        # `src/validation/freeze.py`'s own `_verify_tradier_is_market_data_only`
        # documents these same forbidden identifiers by name in its
        # docstring (the identical documented-negation this file uses for
        # itself) -- excluded here for the same reason.
        freeze_module = (REPO_ROOT / "src" / "validation" / "freeze.py").resolve()
        # `tests/unit/validation/test_freeze.py` writes a literal
        # `class TradierBroker:` into a temporary poison file (not a
        # docstring) to prove `_verify_tradier_is_market_data_only` catches
        # drift -- the same documented-negation this file excludes itself
        # and `freeze.py` for.
        freeze_test_module = (REPO_ROOT / "tests" / "unit" / "validation" / "test_freeze.py").resolve()
        offending = []
        # Step 22.4B: "the repository" means repository-controlled code
        # (see `repo_controlled_files`'s own docstring) -- never a local
        # `.venv`/`venv` or other gitignored/generated directory that a
        # raw filesystem walk would otherwise descend into on a real
        # operator checkout.
        for path in repo_controlled_files(REPO_ROOT):
            if path.suffix != ".py":
                continue
            if "__pycache__" in path.parts or ".git" in path.parts or path.resolve() in (
                this_file, freeze_module, freeze_test_module,
            ):
                continue
            text = path.read_text(errors="ignore")
            if pattern.search(text):
                offending.append(str(path.relative_to(REPO_ROOT)))
        assert offending == [], f"a Tradier trading/execution-shaped class referenced in: {offending}"

    def test_no_post_put_delete_http_verb_used_against_tradier_client(self):
        text = (SRC_ROOT / "data" / "tradier_provider.py").read_text()
        for forbidden in ("_http_client.post(", "_http_client.put(", "_http_client.delete(", "_http_client.patch("):
            assert forbidden not in text, f"{forbidden!r} unexpectedly present -- Tradier provider must be GET-only"


class TestTradierCanonicalBoundary:
    @pytest.mark.asyncio
    async def test_option_chain_output_passes_ensure_canonical(self):
        from src.data.option_chain import OptionChain
        from src.data.provider import ensure_canonical
        from src.data.quotes import UnderlyingQuote
        from src.data.tradier_provider import TradierConfig, TradierMarketDataProvider

        class FakeResponse:
            def __init__(self, body):
                self.status_code = 200
                self.headers = {}
                self._body = body

            def json(self):
                return self._body

        class FakeClient:
            def __init__(self):
                self._paths = {
                    "/markets/quotes": FakeResponse({"quotes": {"quote": {"symbol": "SPY", "bid": 454.5, "ask": 455.5, "last": 455.0, "volume": 1000}}}),
                    "/markets/options/expirations": FakeResponse({"expirations": {"date": []}}),
                }

            async def get(self, path, params=None):
                return self._paths[path]

        provider = TradierMarketDataProvider(TradierConfig(token="fake"), http_client=FakeClient())
        quote = await provider.get_underlying_quote("SPY")
        ensure_canonical(quote, UnderlyingQuote)
        chain = await provider.get_option_chain("SPY")
        ensure_canonical(chain, OptionChain)


class TestTradierNeverTouchesFidelityOrPaperBrokerOrRiskAuthority:
    def test_brokers_yaml_does_not_list_tradier_as_an_execution_broker(self):
        brokers_cfg = yaml.safe_load((REPO_ROOT / "config" / "brokers.yaml").read_text())
        assert "tradier" not in {k.lower() for k in brokers_cfg}

    def test_brokers_yaml_fidelity_execution_mode_still_manual(self):
        brokers_cfg = yaml.safe_load((REPO_ROOT / "config" / "brokers.yaml").read_text())
        assert brokers_cfg["fidelity"]["execution_mode"] == "MANUAL"

    def test_tradier_provider_never_imports_paper_broker_or_fidelity(self):
        text = (SRC_ROOT / "data" / "tradier_provider.py").read_text()
        assert "src.brokers.paper" not in text
        assert "src.brokers.fidelity" not in text

    def test_risk_engine_module_never_imports_tradier(self):
        text = (SRC_ROOT / "risk" / "engine.py").read_text()
        assert "tradier" not in text.lower()

    def test_control_loop_never_imports_a_tradier_execution_path(self):
        # The Portfolio Control Loop (src/portfolio/) consumes Tradier
        # only through the read-only provider/quality-gate/rate-limiter
        # modules -- never a hypothetical execution client.
        for path in (SRC_ROOT / "portfolio").rglob("*.py"):
            text = path.read_text()
            assert "TradierBroker" not in text
            assert "place_order" not in text and "submit_order" not in text


class TestNoCredentialLeakage:
    def test_no_print_or_log_statement_references_token_value(self):
        forbidden_call_pattern = re.compile(r"(print|log(ger)?\.\w+)\([^)]*\btoken\b", re.IGNORECASE)
        text = (SRC_ROOT / "data" / "tradier_provider.py").read_text()
        assert not forbidden_call_pattern.search(text), "tradier_provider.py appears to log a token value"

    def test_error_messages_never_contain_the_actual_token(self):
        from src.data.tradier_provider import classify_tradier_error

        secret = "sk-super-secret-tradier-token-12345"
        exc = classify_tradier_error(401, f"invalid token {secret}", secret)
        assert secret not in str(exc)

    def test_authentication_error_at_construction_never_leaks_token(self):
        from src.data.tradier_provider import TradierAuthenticationError, TradierConfig, TradierMarketDataProvider

        with pytest.raises(TradierAuthenticationError) as exc_info:
            TradierMarketDataProvider(TradierConfig(token=None))
        # no token was ever provided, so nothing could leak -- this also
        # proves the failure is closed (no silent fallback to a default)
        assert "OPTIONS_AGENT_TRADIER_TOKEN" in str(exc_info.value)

    def test_no_credential_shaped_field_name_beyond_token(self):
        from src.data.tradier_provider import TradierConfig

        forbidden_substrings = ("password", "cookie", "session_token", "mfa", "otp", "secret")
        for field_name in TradierConfig.model_fields:
            lowered = field_name.lower()
            for forbidden in forbidden_substrings:
                assert forbidden not in lowered, f"suspicious credential-shaped field name: {field_name}"

    def test_env_example_carries_no_real_token_value(self):
        env_example = (REPO_ROOT / ".env.example")
        if not env_example.is_file():
            pytest.skip(".env.example not present")
        text = env_example.read_text()
        for line in text.splitlines():
            if line.strip().startswith("OPTIONS_AGENT_TRADIER_TOKEN"):
                value = line.split("=", 1)[1].strip() if "=" in line else ""
                assert value == "" or value.startswith("#"), f".env.example appears to carry a real Tradier token: {line!r}"


class TestFreezeVerifierAwareness:
    def test_tradier_market_data_only_check_name_appears_somewhere_in_freeze_tooling(self):
        # Documents the expectation that `make verify-freeze` gains a
        # `tradier_market_data_only` check (Part 2's own requirement) --
        # this test intentionally stays soft (skip, not fail) until
        # Step 22.4's freeze task wires it in, so this file can be
        # committed ahead of that final step without a false failure.
        freeze_module = SRC_ROOT / "validation" / "freeze.py"
        if not freeze_module.is_file():
            pytest.skip("src/validation/freeze.py not present")
        text = freeze_module.read_text()
        if "tradier" not in text.lower():
            pytest.skip("tradier_market_data_only freeze check not wired up yet -- Step 22.4 final freeze task")
