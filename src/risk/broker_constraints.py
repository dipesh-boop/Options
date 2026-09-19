"""Per-broker execution capabilities, loaded from `config/brokers.yaml`.

The Risk Engine must never infer what a broker/account can do — a
strategy is only ever approvable for a given broker if that broker's
entry in `config/brokers.yaml` explicitly lists it. A broker missing
from the file entirely, or a strategy missing from its
`allowed_strategies`, resolves to `None` here, which `src.risk.engine`
treats as `ReasonCode.REJECT_ACCOUNT_CAPABILITY` — never as "probably
fine."

This module may import `src.llm.schemas.StrategyType` (a plain data
enum) to translate between config-file strategy names and the
platform's canonical strategy vocabulary. It has no other dependency on
`src.llm` and, like every other module in `src.risk`, makes no LLM
calls and imports no LLM-calling machinery (`src.llm.client`,
`src.llm.router`) — see `tests/unit/risk/test_architecture_boundary.py`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from src.llm.schemas import StrategyType

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "brokers.yaml"

ExecutionMode = Literal["MANUAL", "AUTOMATED"]


class BrokerConfigError(RuntimeError):
    """Raised for a malformed config/brokers.yaml. A malformed file is
    treated the same as a missing broker entry by every caller: fail
    closed, never guess."""


class BrokerCapabilities(BaseModel):
    """What one broker/account is explicitly configured to allow. Every
    field here is something a human operator declared, never something
    this platform observed or assumed."""

    model_config = ConfigDict(frozen=True)

    broker_name: str = Field(min_length=1)
    execution_mode: ExecutionMode
    account_alias: str = Field(min_length=1)
    options_enabled: bool
    allowed_strategies: frozenset[StrategyType]

    @field_validator("allowed_strategies", mode="before")
    @classmethod
    def _parse_strategy_names(cls, v: object) -> frozenset[StrategyType]:
        if not isinstance(v, list) or not v:
            raise ValueError("allowed_strategies must be a non-empty list")
        parsed: set[StrategyType] = set()
        for name in v:
            if not isinstance(name, str) or name not in StrategyType.__members__:
                raise ValueError(
                    f"{name!r} is not a recognized strategy name; must be one of "
                    f"{sorted(StrategyType.__members__)}"
                )
            parsed.add(StrategyType[name])
        return frozenset(parsed)

    def allows(self, strategy: StrategyType) -> bool:
        return self.options_enabled and strategy in self.allowed_strategies


def load_broker_capabilities(
    broker_name: str, config_path: Path | str | None = None
) -> BrokerCapabilities | None:
    """Returns `None` — never raises — for a broker that is absent from
    the config file, so every caller is forced to handle "not
    configured" as a first-class outcome (REJECT_ACCOUNT_CAPABILITY)
    rather than an exception that might accidentally be swallowed."""
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if not path.is_file():
        raise BrokerConfigError(f"broker config not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    entry = data.get(broker_name)
    if entry is None:
        return None
    if not isinstance(entry, dict):
        raise BrokerConfigError(f"brokers.yaml entry for {broker_name!r} must be a mapping")

    try:
        return BrokerCapabilities(
            broker_name=broker_name,
            execution_mode=entry["execution_mode"],
            account_alias=entry["account_alias"],
            options_enabled=bool(entry["options_enabled"]),
            allowed_strategies=entry["allowed_strategies"],
        )
    except KeyError as exc:
        raise BrokerConfigError(f"brokers.yaml entry for {broker_name!r} is missing {exc}") from exc
    except ValidationError as exc:
        raise BrokerConfigError(f"brokers.yaml entry for {broker_name!r} is invalid: {exc}") from exc
