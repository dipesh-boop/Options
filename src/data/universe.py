"""Step 22.5 (PAPER_TRADING_V1.4.4): loads `config/universe.yaml` into the
frozen-universe ticker list and strategy set the unattended validation-cycle
runner (`scripts/run_validation_cycle.py`) scans every day.

Same YAML-plus-per-value-env-override pattern `src.risk.limits`/
`src.validation.protocol` already establish. This module contains no
tickers of its own -- every entry lives in `config/universe.yaml`, an
operator-maintained trading decision, not a technical one.

`load_universe_strategies` returns every strategy name the config file
lists, unfiltered, as plain strings (e.g. `"CASH_SECURED_PUT"`) -- never
`StrategyType` members. `src.data` must never import `src.llm`
(`tests/unit/data/test_architecture_boundary.py`: the Market Data Layer
sits strictly beneath the Multi-Agent Layer, ARCHITECTURE.md §3/§9), and
`StrategyType` lives in `src.llm.schemas`, so resolving these names to
real enum members -- and narrowing that list to the strategies
`src.workflows.candidate_generation.generate_candidates` actually
implements (`candidate_eligible_strategies`, which lives there, not here)
-- is the caller's job, not this loader's.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from src.workflows.candidate_generation import UniverseEntry

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "universe.yaml"

_TICKER_PATTERN = re.compile(r"^[A-Z]{1,10}$")  # matches TradeProposal.ticker's own pattern
_STRATEGY_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")  # a plausible Python Enum member name, nothing more


class UniverseConfigError(RuntimeError):
    """Raised when config/universe.yaml is missing, malformed, or
    incomplete. Fails closed by refusing to start, never falls back to a
    silent in-code default -- the same discipline
    `src.risk.limits.RiskLimitsConfigError` already enforces."""


def _resolved(section: dict[str, Any], key: str) -> Any:
    env_key = section.get(f"{key}_env")
    if env_key:
        override = os.environ.get(env_key)
        # Step 22.8: blank-but-present means UNSET, matching
        # src.risk.limits._resolved -- see that function's own comment.
        if override:
            return override
    if key not in section:
        raise UniverseConfigError(f"missing required key {key!r}")
    return section[key]


def _load_raw(config_path: Path | str | None) -> dict:
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if not path.is_file():
        raise UniverseConfigError(f"universe config not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if "tickers" not in data:
        raise UniverseConfigError(f"{path} is missing required key 'tickers'")
    if "strategies" not in data:
        raise UniverseConfigError(f"{path} is missing required key 'strategies'")
    return data


def load_universe(config_path: Path | str | None = None) -> tuple[UniverseEntry, ...]:
    """Returns the configured `UniverseEntry` list, ordered exactly as the
    YAML file lists it. Fails closed on a malformed ticker/sector entry or
    a ticker that doesn't match `TradeProposal.ticker`'s own pattern --
    never silently skips a bad entry, since an operator-authored universe
    file with a typo should be caught immediately, not scan one fewer
    ticker than intended without anyone noticing."""
    data = _load_raw(config_path)
    entries: list[UniverseEntry] = []
    for i, raw in enumerate(data["tickers"]):
        if "ticker" not in raw or "sector" not in raw:
            raise UniverseConfigError(f"tickers[{i}] is missing 'ticker' or 'sector': {raw!r}")
        ticker = str(raw["ticker"]).strip().upper()
        sector = str(raw["sector"]).strip()
        if not _TICKER_PATTERN.match(ticker):
            raise UniverseConfigError(f"tickers[{i}].ticker {ticker!r} does not match ^[A-Z]{{1,10}}$")
        if not sector:
            raise UniverseConfigError(f"tickers[{i}].sector must not be empty")
        entries.append(UniverseEntry(ticker=ticker, sector=sector))
    if not entries:
        raise UniverseConfigError("config/universe.yaml's 'tickers' list must not be empty")
    return tuple(entries)


def load_universe_strategies(config_path: Path | str | None = None) -> tuple[str, ...]:
    """Returns every strategy NAME `config/universe.yaml` lists, unfiltered,
    normalized to upper-case-stripped strings (e.g. `"CASH_SECURED_PUT"") --
    matches `config/brokers.yaml`'s own `allowed_strategies` convention
    (`src.risk.broker_constraints`): strategy NAMES in YAML, resolved by
    NAME (`StrategyType[name]`), never by `StrategyType`'s lowercase enum
    VALUES. This loader only validates that each entry is a plausible
    identifier -- resolving it to a real `StrategyType` member (and
    catching an unknown name) is the caller's job, since that requires
    importing `src.llm.schemas`, which this module must not do (see
    module docstring)."""
    data = _load_raw(config_path)
    raw_strategies = _resolved(data, "strategies")
    if isinstance(raw_strategies, str):
        raw_strategies = [s.strip() for s in raw_strategies.split(",") if s.strip()]
    if not raw_strategies:
        raise UniverseConfigError("config/universe.yaml's 'strategies' list must not be empty")
    names: list[str] = []
    for s in raw_strategies:
        name = str(s).strip().upper()
        if not _STRATEGY_NAME_PATTERN.match(name):
            raise UniverseConfigError(f"config/universe.yaml lists a malformed strategy name: {s!r}")
        names.append(name)
    return tuple(names)
