"""Tests for src.risk.limits: the config/risk_limits.yaml loader."""
from __future__ import annotations

import os

import pytest
import yaml

from src.risk.limits import RiskLimitsConfigError, load_risk_limits


def test_default_config_loads_successfully():
    cfg = load_risk_limits()
    assert cfg.target_risk_per_trade_pct == 0.01
    assert cfg.absolute_max_risk_per_trade_pct == 0.02
    assert cfg.drawdown_halt_pct == 0.15


def test_missing_file_raises(tmp_path):
    with pytest.raises(RiskLimitsConfigError):
        load_risk_limits(tmp_path / "does_not_exist.yaml")


def test_missing_section_raises(tmp_path):
    p = tmp_path / "risk_limits.yaml"
    p.write_text(yaml.safe_dump({"position_risk": {"target_risk_per_trade_pct": 0.01, "absolute_max_risk_per_trade_pct": 0.02}}))
    with pytest.raises(RiskLimitsConfigError):
        load_risk_limits(p)


def _full_valid_config(**overrides) -> dict:
    base = {
        "position_risk": {"target_risk_per_trade_pct": 0.01, "absolute_max_risk_per_trade_pct": 0.02},
        "concentration": {"max_underlying_exposure_pct": 0.10, "max_sector_exposure_pct": 0.25},
        "capital": {
            "min_cash_reserve_pct": 0.20,
            "normal_max_capital_deployed_pct": 0.60,
            "absolute_max_capital_deployed_pct": 0.70,
        },
        "drawdown": {"warning_pct": 0.08, "risk_reduction_pct": 0.10, "halt_pct": 0.15},
        "risk_reduction_sizing_multiplier": 0.5,
        "liquidity": {"min_open_interest": 100, "min_volume": 10, "max_bid_ask_spread_pct": 0.15},
        "correlation": {"high_correlation_threshold": 0.70},
        "data_freshness": {"max_market_data_age_minutes": 15},
        "pricing": {"risk_free_rate": 0.04},
        "quant_cross_check_tolerance_pct": 0.02,
        "stress_testing": {"max_stress_loss_pct_of_nav": 0.30},
    }
    for section, values in overrides.items():
        base[section] = {**base.get(section, {}), **values} if isinstance(values, dict) else values
    return base


def test_target_risk_exceeding_absolute_max_is_rejected(tmp_path):
    p = tmp_path / "risk_limits.yaml"
    data = _full_valid_config(position_risk={"target_risk_per_trade_pct": 0.05, "absolute_max_risk_per_trade_pct": 0.02})
    p.write_text(yaml.safe_dump(data))
    with pytest.raises(RiskLimitsConfigError):
        load_risk_limits(p)


def test_non_increasing_drawdown_thresholds_rejected(tmp_path):
    p = tmp_path / "risk_limits.yaml"
    data = _full_valid_config(drawdown={"warning_pct": 0.15, "risk_reduction_pct": 0.10, "halt_pct": 0.08})
    p.write_text(yaml.safe_dump(data))
    with pytest.raises(RiskLimitsConfigError):
        load_risk_limits(p)


def test_env_override_takes_precedence(tmp_path, monkeypatch: pytest.MonkeyPatch):
    p = tmp_path / "risk_limits.yaml"
    data = _full_valid_config()
    data["position_risk"]["target_risk_per_trade_pct_env"] = "OPTIONS_AGENT_TEST_TARGET_RISK"
    p.write_text(yaml.safe_dump(data))
    monkeypatch.setenv("OPTIONS_AGENT_TEST_TARGET_RISK", "0.005")
    cfg = load_risk_limits(p)
    assert cfg.target_risk_per_trade_pct == 0.005


def test_no_hardcoded_policy_numbers_in_limits_module():
    """src/risk/limits.py must never hard-code a policy value; every
    number in it should be a Field constraint bound, not a default."""
    import re
    from pathlib import Path

    source = Path(__import__("src.risk.limits", fromlist=["x"]).__file__).read_text(encoding="utf-8")
    # Only the DEFAULT_CONFIG_PATH computation should reference literal
    # path components; no bare policy-shaped float literal (e.g. 0.01,
    # 0.15) should appear as a default value.
    suspicious = re.findall(r"=\s*0\.\d+\b", source)
    assert suspicious == [], f"found suspicious hard-coded numeric literals: {suspicious}"
