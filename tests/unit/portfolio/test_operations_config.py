"""Tests for src.portfolio.operations_config: the config/operations.yaml
loader."""
from __future__ import annotations

import pytest
import yaml

from src.portfolio.operations_config import OperationsConfigError, load_operations_config


def _valid_data(**overrides) -> dict:
    base = {
        "cohort": {"cohort_id": "test-cohort", "account_id": "test-account"},
        "market_regime": {"default_regime": "normal"},
        "storage": {
            "account_state_db_path": "data/test.db", "control_loop_db_path": "data/test.db",
            "lifecycle_db_path": "data/test.db", "candidate_review_db_path": "data/test.db",
        },
        "review": {"confirmation_ttl_seconds": 900, "max_price_drift_pct": 0.05, "max_capital_required_drift_pct": 0.05},
    }
    base.update(overrides)
    return base


def test_default_config_loads_successfully():
    cfg = load_operations_config()
    assert cfg.cohort_id
    assert cfg.account_id
    assert cfg.default_market_regime in ("low_vol", "normal", "elevated_vol", "crisis")
    assert cfg.confirmation_ttl_seconds > 0


def test_missing_file_raises(tmp_path):
    with pytest.raises(OperationsConfigError):
        load_operations_config(tmp_path / "does_not_exist.yaml")


def test_missing_section_raises(tmp_path):
    p = tmp_path / "operations.yaml"
    p.write_text(yaml.safe_dump({"cohort": _valid_data()["cohort"]}))
    with pytest.raises(OperationsConfigError):
        load_operations_config(p)


def test_invalid_market_regime_raises(tmp_path):
    p = tmp_path / "operations.yaml"
    p.write_text(yaml.safe_dump(_valid_data(market_regime={"default_regime": "not_a_real_regime"})))
    with pytest.raises(OperationsConfigError):
        load_operations_config(p)


def test_cohort_id_env_override(tmp_path, monkeypatch):
    data = _valid_data()
    data["cohort"]["cohort_id_env"] = "OPTIONS_AGENT_TEST_COHORT_ID"
    p = tmp_path / "operations.yaml"
    p.write_text(yaml.safe_dump(data))
    monkeypatch.setenv("OPTIONS_AGENT_TEST_COHORT_ID", "overridden-cohort")
    cfg = load_operations_config(p)
    assert cfg.cohort_id == "overridden-cohort"


def test_blank_env_override_is_treated_as_unset(tmp_path, monkeypatch):
    """Step 22.8 (PAPER_TRADING_V1.4.7): a blank-but-present env var
    (the shipped .env template's own convention) falls through to the
    YAML value, never attempting int()/float() on an empty string."""
    data = _valid_data()
    data["cohort"]["cohort_id_env"] = "OPTIONS_AGENT_TEST_COHORT_ID_BLANK"
    data["review"]["confirmation_ttl_seconds_env"] = "OPTIONS_AGENT_TEST_TTL_BLANK"
    p = tmp_path / "operations.yaml"
    p.write_text(yaml.safe_dump(data))
    monkeypatch.setenv("OPTIONS_AGENT_TEST_COHORT_ID_BLANK", "")
    monkeypatch.setenv("OPTIONS_AGENT_TEST_TTL_BLANK", "")
    cfg = load_operations_config(p)
    assert cfg.cohort_id == "test-cohort"
    assert cfg.confirmation_ttl_seconds == 900


def test_valid_config_round_trips_every_field(tmp_path):
    p = tmp_path / "operations.yaml"
    p.write_text(yaml.safe_dump(_valid_data()))
    cfg = load_operations_config(p)
    assert cfg.cohort_id == "test-cohort"
    assert cfg.account_id == "test-account"
    assert cfg.default_market_regime == "normal"
    assert cfg.confirmation_ttl_seconds == 900
    assert cfg.max_price_drift_pct == 0.05
    assert cfg.max_capital_required_drift_pct == 0.05
