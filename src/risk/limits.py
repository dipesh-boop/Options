"""Loads `config/risk_limits.yaml` into a frozen, typed config object.

Same pattern as `src.llm.router.ModelRouter`: application code never
hard-codes a limit — it asks this module, which reads the YAML file
(with an optional per-value environment variable override for ops-time
changes without a deploy). This file intentionally contains no policy
numbers of its own; every default lives in config/risk_limits.yaml.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "risk_limits.yaml"


class RiskLimitsConfigError(RuntimeError):
    """Raised when config/risk_limits.yaml is missing, malformed, or
    incomplete. A Risk Engine that cannot resolve its own limits must
    fail closed by refusing to start, not by falling back to a silent
    in-code default."""


class RiskLimitsConfig(BaseModel):
    """A fully resolved set of risk limits. Frozen: nothing downstream
    can mutate a loaded config into something it wasn't."""

    model_config = ConfigDict(frozen=True)

    target_risk_per_trade_pct: float = Field(gt=0, le=1)
    absolute_max_risk_per_trade_pct: float = Field(gt=0, le=1)

    max_underlying_exposure_pct: float = Field(gt=0, le=1)
    max_sector_exposure_pct: float = Field(gt=0, le=1)

    min_cash_reserve_pct: float = Field(ge=0, le=1)
    normal_max_capital_deployed_pct: float = Field(gt=0, le=1)
    absolute_max_capital_deployed_pct: float = Field(gt=0, le=1)

    drawdown_warning_pct: float = Field(gt=0, le=1)
    drawdown_risk_reduction_pct: float = Field(gt=0, le=1)
    drawdown_halt_pct: float = Field(gt=0, le=1)
    risk_reduction_sizing_multiplier: float = Field(gt=0, le=1)

    min_open_interest: int = Field(ge=0)
    min_volume: int = Field(ge=0)
    max_bid_ask_spread_pct: float = Field(gt=0, le=1)

    high_correlation_threshold: float = Field(ge=0, le=1)

    max_market_data_age_minutes: float = Field(gt=0)

    risk_free_rate: float = Field(ge=0, le=1)

    quant_cross_check_tolerance_pct: float = Field(gt=0, le=1)

    max_stress_loss_pct_of_nav: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _validate_orderings(self) -> "RiskLimitsConfig":
        if self.target_risk_per_trade_pct > self.absolute_max_risk_per_trade_pct:
            raise ValueError("target_risk_per_trade_pct cannot exceed absolute_max_risk_per_trade_pct")
        if self.normal_max_capital_deployed_pct > self.absolute_max_capital_deployed_pct:
            raise ValueError("normal_max_capital_deployed_pct cannot exceed absolute_max_capital_deployed_pct")
        if not (self.drawdown_warning_pct < self.drawdown_risk_reduction_pct < self.drawdown_halt_pct):
            raise ValueError("drawdown thresholds must be strictly increasing: warning < risk_reduction < halt")
        return self


def _resolved(section: dict[str, Any], key: str) -> Any:
    env_key = section.get(f"{key}_env")
    if env_key:
        override = os.environ.get(env_key)
        if override is not None:
            return override
    if key not in section:
        raise RiskLimitsConfigError(f"missing required key {key!r}")
    return section[key]


def load_risk_limits(config_path: Path | str | None = None) -> RiskLimitsConfig:
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if not path.is_file():
        raise RiskLimitsConfigError(f"risk limits config not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    try:
        position_risk = data["position_risk"]
        concentration = data["concentration"]
        capital = data["capital"]
        drawdown = data["drawdown"]
        liquidity = data["liquidity"]
        correlation = data["correlation"]
        data_freshness = data["data_freshness"]
        pricing = data["pricing"]

        return RiskLimitsConfig(
            target_risk_per_trade_pct=float(_resolved(position_risk, "target_risk_per_trade_pct")),
            absolute_max_risk_per_trade_pct=float(_resolved(position_risk, "absolute_max_risk_per_trade_pct")),
            max_underlying_exposure_pct=float(_resolved(concentration, "max_underlying_exposure_pct")),
            max_sector_exposure_pct=float(_resolved(concentration, "max_sector_exposure_pct")),
            min_cash_reserve_pct=float(_resolved(capital, "min_cash_reserve_pct")),
            normal_max_capital_deployed_pct=float(_resolved(capital, "normal_max_capital_deployed_pct")),
            absolute_max_capital_deployed_pct=float(_resolved(capital, "absolute_max_capital_deployed_pct")),
            drawdown_warning_pct=float(_resolved(drawdown, "warning_pct")),
            drawdown_risk_reduction_pct=float(_resolved(drawdown, "risk_reduction_pct")),
            drawdown_halt_pct=float(_resolved(drawdown, "halt_pct")),
            risk_reduction_sizing_multiplier=float(
                _resolved(data, "risk_reduction_sizing_multiplier")
            ),
            min_open_interest=int(_resolved(liquidity, "min_open_interest")),
            min_volume=int(_resolved(liquidity, "min_volume")),
            max_bid_ask_spread_pct=float(_resolved(liquidity, "max_bid_ask_spread_pct")),
            high_correlation_threshold=float(_resolved(correlation, "high_correlation_threshold")),
            max_market_data_age_minutes=float(_resolved(data_freshness, "max_market_data_age_minutes")),
            risk_free_rate=float(_resolved(pricing, "risk_free_rate")),
            quant_cross_check_tolerance_pct=float(_resolved(data, "quant_cross_check_tolerance_pct")),
            max_stress_loss_pct_of_nav=float(
                _resolved(data["stress_testing"], "max_stress_loss_pct_of_nav")
            ),
        )
    except KeyError as exc:
        raise RiskLimitsConfigError(f"{path} is missing required section {exc}") from exc
    except ValidationError as exc:
        raise RiskLimitsConfigError(f"{path} contains invalid limits: {exc}") from exc


_default_limits: RiskLimitsConfig | None = None


def get_default_limits() -> RiskLimitsConfig:
    """Process-wide default limits, lazily loaded so importing this
    module never touches the filesystem by itself."""
    global _default_limits
    if _default_limits is None:
        _default_limits = load_risk_limits()
    return _default_limits
