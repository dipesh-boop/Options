"""Tests for src.data.universe: the config/universe.yaml loader.

`load_universe_strategies` returns plain strategy NAME strings (e.g.
"CASH_SECURED_PUT"), never `StrategyType` members -- src.data must never
import src.llm (see tests/unit/data/test_architecture_boundary.py).
Resolving those names to real `StrategyType` members, and narrowing that
list to the ones `generate_candidates` can actually produce a candidate
for, is the caller's job -- see
tests/unit/workflows/test_candidate_generation.py's
`TestCandidateEligibleStrategies` for the latter."""
from __future__ import annotations

import pytest
import yaml

from src.data.universe import (
    UniverseConfigError,
    load_universe,
    load_universe_strategies,
)
from src.workflows.candidate_generation import UniverseEntry


def test_default_config_loads_successfully():
    universe = load_universe()
    assert len(universe) >= 1
    assert all(isinstance(e, UniverseEntry) for e in universe)
    strategies = load_universe_strategies()
    assert len(strategies) >= 1
    assert all(isinstance(s, str) for s in strategies)


def test_missing_file_raises(tmp_path):
    with pytest.raises(UniverseConfigError):
        load_universe(tmp_path / "does_not_exist.yaml")
    with pytest.raises(UniverseConfigError):
        load_universe_strategies(tmp_path / "does_not_exist.yaml")


def test_missing_tickers_key_raises(tmp_path):
    p = tmp_path / "universe.yaml"
    p.write_text(yaml.safe_dump({"strategies": ["CASH_SECURED_PUT"]}))
    with pytest.raises(UniverseConfigError):
        load_universe(p)


def test_missing_strategies_key_raises(tmp_path):
    p = tmp_path / "universe.yaml"
    p.write_text(yaml.safe_dump({"tickers": [{"ticker": "SPY", "sector": "ETF"}]}))
    with pytest.raises(UniverseConfigError):
        load_universe_strategies(p)


def test_empty_tickers_list_raises(tmp_path):
    p = tmp_path / "universe.yaml"
    p.write_text(yaml.safe_dump({"tickers": [], "strategies": ["CASH_SECURED_PUT"]}))
    with pytest.raises(UniverseConfigError):
        load_universe(p)


def test_malformed_ticker_entry_raises(tmp_path):
    p = tmp_path / "universe.yaml"
    p.write_text(yaml.safe_dump({"tickers": [{"ticker": "spy123"}], "strategies": ["CASH_SECURED_PUT"]}))
    with pytest.raises(UniverseConfigError):
        load_universe(p)


def test_malformed_strategy_name_raises(tmp_path):
    p = tmp_path / "universe.yaml"
    p.write_text(yaml.safe_dump({"tickers": [{"ticker": "SPY", "sector": "ETF"}], "strategies": ["not valid!"]}))
    with pytest.raises(UniverseConfigError):
        load_universe_strategies(p)


def test_ticker_env_case_normalized_and_stripped(tmp_path):
    p = tmp_path / "universe.yaml"
    p.write_text(yaml.safe_dump({"tickers": [{"ticker": " spy ", "sector": "ETF"}], "strategies": ["CASH_SECURED_PUT"]}))
    universe = load_universe(p)
    assert universe[0].ticker == "SPY"


def test_strategy_names_normalized_case_and_stripped(tmp_path):
    p = tmp_path / "universe.yaml"
    p.write_text(yaml.safe_dump({"tickers": [{"ticker": "SPY", "sector": "ETF"}], "strategies": [" cash_secured_put "]}))
    assert load_universe_strategies(p) == ("CASH_SECURED_PUT",)


def test_strategies_env_override(tmp_path, monkeypatch):
    p = tmp_path / "universe.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "tickers": [{"ticker": "SPY", "sector": "ETF"}],
                "strategies": ["CASH_SECURED_PUT"],
                "strategies_env": "OPTIONS_AGENT_TEST_UNIVERSE_STRATEGIES",
            }
        )
    )
    monkeypatch.setenv("OPTIONS_AGENT_TEST_UNIVERSE_STRATEGIES", "COVERED_CALL,PUT_CREDIT_SPREAD")
    strategies = load_universe_strategies(p)
    assert strategies == ("COVERED_CALL", "PUT_CREDIT_SPREAD")


def test_blank_strategies_env_override_is_treated_as_unset(tmp_path, monkeypatch):
    """Step 22.8 (PAPER_TRADING_V1.4.7): a blank-but-present override
    falls through to the YAML default, never treated as 'override with
    an empty strategy list.'"""
    p = tmp_path / "universe.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "tickers": [{"ticker": "SPY", "sector": "ETF"}],
                "strategies": ["CASH_SECURED_PUT"],
                "strategies_env": "OPTIONS_AGENT_TEST_UNIVERSE_STRATEGIES_BLANK",
            }
        )
    )
    monkeypatch.setenv("OPTIONS_AGENT_TEST_UNIVERSE_STRATEGIES_BLANK", "")
    strategies = load_universe_strategies(p)
    assert strategies == ("CASH_SECURED_PUT",)
