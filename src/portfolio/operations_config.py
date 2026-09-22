"""Step 22.5 (PAPER_TRADING_V1.4.4): loads `config/operations.yaml` --
the cohort/account identity, deterministic market-regime default, durable
store paths, and confirm-time revalidation tolerances the unattended daily
validation-cycle runner (`scripts/run_validation_cycle.py`) and the human
confirm-candidate command (`scripts/confirm_candidate.py`) both need.

Same `_resolved(section, key)` YAML-plus-per-value-env-override idiom
`src.risk.limits`/`src.validation.protocol` already establish. This module
never imports `src.validation.cohort.start_new_cohort` -- it only ever
*reads* a cohort id an operator already started elsewhere; nothing in this
module can create a new cohort.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from src.llm.schemas import MarketRegimeLabel

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "operations.yaml"

_VALID_REGIMES = {"low_vol", "normal", "elevated_vol", "crisis"}


class OperationsConfigError(RuntimeError):
    """Raised when config/operations.yaml is missing, malformed, or
    incomplete. Fails closed -- refuses to start rather than falling back
    to a silent default, matching every other config loader in this
    codebase."""


class OperationsConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    cohort_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    default_market_regime: MarketRegimeLabel
    account_state_db_path: str = Field(min_length=1)
    control_loop_db_path: str = Field(min_length=1)
    lifecycle_db_path: str = Field(min_length=1)
    candidate_review_db_path: str = Field(min_length=1)
    confirmation_ttl_seconds: int = Field(gt=0)
    max_price_drift_pct: float = Field(gt=0, le=1)
    max_capital_required_drift_pct: float = Field(gt=0, le=1)


def _resolved(section: dict[str, Any], key: str) -> Any:
    env_key = section.get(f"{key}_env")
    if env_key:
        override = os.environ.get(env_key)
        if override is not None:
            return override
    if key not in section:
        raise OperationsConfigError(f"missing required key {key!r}")
    return section[key]


def load_operations_config(config_path: Path | str | None = None) -> OperationsConfig:
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if not path.is_file():
        raise OperationsConfigError(f"operations config not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    try:
        cohort = data["cohort"]
        market_regime = data["market_regime"]
        storage = data["storage"]
        review = data["review"]

        regime = str(_resolved(market_regime, "default_regime"))
        if regime not in _VALID_REGIMES:
            raise OperationsConfigError(
                f"market_regime.default_regime {regime!r} is not one of {sorted(_VALID_REGIMES)}"
            )

        return OperationsConfig(
            cohort_id=str(_resolved(cohort, "cohort_id")),
            account_id=str(_resolved(cohort, "account_id")),
            default_market_regime=regime,  # type: ignore[arg-type]
            account_state_db_path=str(_resolved(storage, "account_state_db_path")),
            control_loop_db_path=str(_resolved(storage, "control_loop_db_path")),
            lifecycle_db_path=str(_resolved(storage, "lifecycle_db_path")),
            candidate_review_db_path=str(_resolved(storage, "candidate_review_db_path")),
            confirmation_ttl_seconds=int(_resolved(review, "confirmation_ttl_seconds")),
            max_price_drift_pct=float(_resolved(review, "max_price_drift_pct")),
            max_capital_required_drift_pct=float(_resolved(review, "max_capital_required_drift_pct")),
        )
    except KeyError as exc:
        raise OperationsConfigError(f"{path} is missing required section {exc}") from exc
