"""Tests for src.risk.broker_constraints: config/brokers.yaml loading
and the "never infer capability" guarantee."""
from __future__ import annotations

import pytest
import yaml

from src.risk.broker_constraints import BrokerConfigError, load_broker_capabilities


def test_fidelity_loads_from_the_real_config():
    caps = load_broker_capabilities("fidelity")
    assert caps is not None
    assert caps.execution_mode == "MANUAL"
    assert caps.options_enabled is True


def test_unconfigured_broker_returns_none_not_a_default():
    assert load_broker_capabilities("robinhood") is None


def test_missing_config_file_raises(tmp_path):
    with pytest.raises(BrokerConfigError):
        load_broker_capabilities("fidelity", tmp_path / "missing.yaml")


def test_unrecognized_strategy_name_is_rejected(tmp_path):
    p = tmp_path / "brokers.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "fidelity": {
                    "execution_mode": "MANUAL",
                    "account_alias": "OPTIONS_ACCOUNT",
                    "options_enabled": True,
                    "allowed_strategies": ["NAKED_CALL"],  # not a real StrategyType
                }
            }
        )
    )
    with pytest.raises(BrokerConfigError):
        load_broker_capabilities("fidelity", p)


def test_empty_allowed_strategies_is_rejected(tmp_path):
    p = tmp_path / "brokers.yaml"
    p.write_text(
        yaml.safe_dump(
            {"fidelity": {"execution_mode": "MANUAL", "account_alias": "OPTIONS_ACCOUNT", "options_enabled": True, "allowed_strategies": []}}
        )
    )
    with pytest.raises(BrokerConfigError):
        load_broker_capabilities("fidelity", p)


def test_missing_required_key_is_rejected(tmp_path):
    p = tmp_path / "brokers.yaml"
    p.write_text(yaml.safe_dump({"fidelity": {"execution_mode": "MANUAL", "options_enabled": True}}))
    with pytest.raises(BrokerConfigError):
        load_broker_capabilities("fidelity", p)


def test_capabilities_are_frozen():
    caps = load_broker_capabilities("fidelity")
    with pytest.raises(Exception):
        caps.options_enabled = False
