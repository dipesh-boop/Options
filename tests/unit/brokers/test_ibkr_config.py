"""Tests for IBKRConfig: environment-variable loading and the paper-port
enforcement that is the primary "do not allow live trading" mechanism."""
from __future__ import annotations

import pytest

from src.brokers.base import LiveTradingBlockedError
from src.brokers.ibkr import LIVE_PORTS, PAPER_PORTS, IBKRConfig


class TestDefaults:
    def test_default_port_is_a_paper_port(self):
        assert IBKRConfig().port in PAPER_PORTS

    def test_default_host_is_localhost(self):
        assert IBKRConfig().host == "127.0.0.1"


class TestEnvironmentVariableLoading:
    def test_host_from_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("OPTIONS_AGENT_IBKR_HOST", "10.0.0.5")
        assert IBKRConfig().host == "10.0.0.5"

    def test_port_from_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("OPTIONS_AGENT_IBKR_PORT", "4002")
        assert IBKRConfig().port == 4002

    def test_client_id_from_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("OPTIONS_AGENT_IBKR_CLIENT_ID", "42")
        assert IBKRConfig().client_id == 42

    def test_account_id_from_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("OPTIONS_AGENT_IBKR_ACCOUNT_ID", "DU9999999")
        assert IBKRConfig().account_id == "DU9999999"

    def test_no_credential_fields_exist_on_the_model(self):
        # IBKR's API authenticates via an already-logged-in local
        # TWS/Gateway session, not a credential sent over the wire - so
        # there should be nothing resembling one on this config at all.
        forbidden_substrings = ["password", "secret", "token", "api_key", "apikey"]
        field_names = " ".join(IBKRConfig.model_fields.keys()).lower()
        for forbidden in forbidden_substrings:
            assert forbidden not in field_names


class TestPaperPortEnforcement:
    @pytest.mark.parametrize("port", sorted(PAPER_PORTS))
    def test_known_paper_ports_accepted(self, port: int):
        IBKRConfig(port=port).require_paper_port()  # does not raise

    @pytest.mark.parametrize("port", sorted(LIVE_PORTS))
    def test_known_live_ports_blocked(self, port: int):
        with pytest.raises(LiveTradingBlockedError, match="LIVE"):
            IBKRConfig(port=port).require_paper_port()

    def test_unrecognized_port_blocked(self):
        with pytest.raises(LiveTradingBlockedError, match="not a recognized paper port"):
            IBKRConfig(port=9999).require_paper_port()

    def test_paper_and_live_port_sets_are_disjoint(self):
        assert PAPER_PORTS.isdisjoint(LIVE_PORTS)

    def test_paper_account_prefix_defaults_to_du(self):
        assert IBKRConfig().paper_account_prefix == "DU"
