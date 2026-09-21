"""Validation-period framing: config loading, strategy-version freezing
(`VALIDATION_MANIFEST.json`), and sample-size gating.

Same YAML-plus-per-value-env-override pattern `src.risk.limits` already
established for `config/risk_limits.yaml` — application code never
hard-codes one of these numbers.

**Strategy-version freezing** exists so a 90-day validation run measures
one fixed strategy, not a moving target: `build_validation_manifest`
hashes every config file that defines strategy/risk behavior
(`config/risk_limits.yaml`, `config/brokers.yaml`, and this module's own
`config/validation.yaml`) at the moment the validation period starts.
`verify_manifest_integrity` re-hashes those same files later and raises
`ManifestDriftError` — loudly, not silently — if any of them changed
during the run. This is a detection mechanism, not a prevention one:
nothing in this codebase stops someone from editing
`config/risk_limits.yaml` mid-validation; what this module guarantees is
that doing so cannot pass unnoticed by whoever reads the validation
report afterward.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "validation.yaml"

DEFAULT_MANIFEST_CONFIG_PATHS = (
    Path(__file__).resolve().parents[2] / "config" / "risk_limits.yaml",
    Path(__file__).resolve().parents[2] / "config" / "brokers.yaml",
    Path(__file__).resolve().parents[2] / "config" / "validation.yaml",
)


class ValidationConfigError(RuntimeError):
    """Raised when config/validation.yaml is missing, malformed, or
    incomplete. Mirrors `src.risk.limits.RiskLimitsConfigError`: fail
    closed by refusing to start, never fall back to a silent default."""


class ValidationConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    duration_days: int = Field(gt=0)
    checkpoint_days: tuple[int, ...]

    minimum_completed_trades: int = Field(ge=0)
    preferred_completed_trades: int = Field(ge=0)

    default_starting_nav: float = Field(gt=0)

    research_target_annual_return_low_pct: float
    research_target_annual_return_high_pct: float
    research_reference_min_sharpe: float
    research_reference_max_acceptable_drawdown_pct: float

    bootstrap_iterations: int = Field(gt=0)
    bootstrap_confidence_pct: float = Field(gt=0, lt=1)
    monte_carlo_iterations: int = Field(gt=0)
    var_confidence_pct: float = Field(gt=0, lt=1)
    random_seed: int

    min_probability_of_profit: float = Field(ge=0, le=1)
    rejected_trade_min_sample_size: int = Field(ge=0)

    consecutive_loss_alert_count: int = Field(gt=0)
    weekly_loss_alert_pct: float = Field(gt=0)

    # Step 22: where the durable SqliteValidationStore lives (see
    # src.validation.session). A location default only -- reading this
    # value never creates the file or opens a connection.
    db_path: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_orderings(self) -> "ValidationConfig":
        if self.minimum_completed_trades > self.preferred_completed_trades:
            raise ValueError("minimum_completed_trades cannot exceed preferred_completed_trades")
        if self.research_target_annual_return_low_pct > self.research_target_annual_return_high_pct:
            raise ValueError("research_target_annual_return_low_pct cannot exceed the high target")
        for day in self.checkpoint_days:
            if day <= 0 or day >= self.duration_days:
                raise ValueError(f"checkpoint day {day} must be strictly between 0 and duration_days ({self.duration_days})")
        return self


def _resolved(section: dict[str, Any], key: str) -> Any:
    env_key = section.get(f"{key}_env")
    if env_key:
        override = os.environ.get(env_key)
        if override is not None:
            return override
    if key not in section:
        raise ValidationConfigError(f"missing required key {key!r}")
    return section[key]


def load_validation_config(config_path: Path | str | None = None) -> ValidationConfig:
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if not path.is_file():
        raise ValidationConfigError(f"validation config not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    try:
        period = data["validation_period"]
        sample_size = data["sample_size"]
        capital = data["starting_capital"]
        targets = data["research_targets"]
        stats = data["statistics"]
        dq = data["decision_quality"]
        alerts = data["alerts"]
        storage = data.get("storage", {})

        return ValidationConfig(
            duration_days=int(_resolved(period, "duration_days")),
            checkpoint_days=tuple(int(d) for d in period["checkpoint_days"]),
            minimum_completed_trades=int(_resolved(sample_size, "minimum_completed_trades")),
            preferred_completed_trades=int(_resolved(sample_size, "preferred_completed_trades")),
            default_starting_nav=float(_resolved(capital, "default_nav")),
            research_target_annual_return_low_pct=float(targets["annual_return_low_pct"]),
            research_target_annual_return_high_pct=float(targets["annual_return_high_pct"]),
            research_reference_min_sharpe=float(targets["reference_min_sharpe"]),
            research_reference_max_acceptable_drawdown_pct=float(targets["reference_max_acceptable_drawdown_pct"]),
            bootstrap_iterations=int(_resolved(stats, "bootstrap_iterations")),
            bootstrap_confidence_pct=float(stats["bootstrap_confidence_pct"]),
            monte_carlo_iterations=int(_resolved(stats, "monte_carlo_iterations")),
            var_confidence_pct=float(stats["var_confidence_pct"]),
            random_seed=int(stats["random_seed"]),
            min_probability_of_profit=float(dq["min_probability_of_profit"]),
            rejected_trade_min_sample_size=int(dq["rejected_trade_min_sample_size"]),
            consecutive_loss_alert_count=int(alerts["consecutive_loss_alert_count"]),
            weekly_loss_alert_pct=float(alerts["weekly_loss_alert_pct"]),
            db_path=str(_resolved(storage, "db_path")),
        )
    except KeyError as exc:
        raise ValidationConfigError(f"{path} is missing required section {exc}") from exc
    except ValidationError as exc:
        raise ValidationConfigError(f"{path} contains invalid values: {exc}") from exc


_default_config: ValidationConfig | None = None


def get_default_validation_config() -> ValidationConfig:
    global _default_config
    if _default_config is None:
        _default_config = load_validation_config()
    return _default_config


# --------------------------------------------------------- sample-size gate


class SampleSizeStatus(str, Enum):
    INSUFFICIENT_SAMPLE = "insufficient_sample"
    MINIMUM_SAMPLE = "minimum_sample"
    PREFERRED_SAMPLE = "preferred_sample"


def sample_size_status(completed_trades: int, config: ValidationConfig) -> SampleSizeStatus:
    """Below `minimum_completed_trades` (default 50), a validation run at
    any day count — including day 90 — is `INSUFFICIENT_SAMPLE`. This is
    read directly by `src.validation.gates.evaluate_90_day_gate`, which
    can never classify a `PASS_FOR_EXTENDED_VALIDATION` while this status
    is `INSUFFICIENT_SAMPLE`, however good the observed numbers look."""
    if completed_trades < config.minimum_completed_trades:
        return SampleSizeStatus.INSUFFICIENT_SAMPLE
    if completed_trades < config.preferred_completed_trades:
        return SampleSizeStatus.MINIMUM_SAMPLE
    return SampleSizeStatus.PREFERRED_SAMPLE


# --------------------------------------------------------------- period


class ValidationPeriod(BaseModel):
    model_config = ConfigDict(frozen=True)

    start_date: date
    end_date: date
    duration_days: int = Field(gt=0)

    @model_validator(mode="after")
    def _validate(self) -> "ValidationPeriod":
        if self.end_date <= self.start_date:
            raise ValueError("end_date must be after start_date")
        if (self.end_date - self.start_date).days != self.duration_days:
            raise ValueError("end_date - start_date must equal duration_days")
        return self

    def elapsed_calendar_days(self, as_of: date) -> int:
        return max((min(as_of, self.end_date) - self.start_date).days, 0)

    def is_complete(self, as_of: date) -> bool:
        return as_of >= self.end_date

    def is_checkpoint_reached(self, as_of: date, checkpoint_day: int) -> bool:
        return self.elapsed_calendar_days(as_of) >= checkpoint_day


def build_validation_period(start_date: date, config: ValidationConfig) -> ValidationPeriod:
    return ValidationPeriod(
        start_date=start_date,
        end_date=start_date + timedelta(days=config.duration_days),
        duration_days=config.duration_days,
    )


# --------------------------------------------------------- strategy freeze


def compute_file_hash(path: Path | str) -> str:
    data = Path(path).read_bytes()
    return hashlib.sha256(data).hexdigest()


class StrategyVersionManifest(BaseModel):
    """A validation run's frozen identity. Once built and saved, this is
    the single source of truth for "what exact configuration and strategy
    set was this 90-day run actually measuring" — a validation report
    that can't point to one of these is not reporting on a controlled
    experiment."""

    model_config = ConfigDict(frozen=True)

    manifest_id: str = Field(min_length=1)
    frozen_at: datetime
    period: ValidationPeriod
    starting_nav: float = Field(gt=0)
    config_file_hashes: dict[str, str]
    strategy_versions: dict[str, str]
    notes: str = ""
    # Step 19A addition: additive, defaults to "default" so every
    # existing manifest construction/serialization is unaffected. Names
    # which validation cohort this manifest freezes -- see
    # src.validation.cohort for the PRE_EXPANSION_VALIDATION /
    # MULTI_STRATEGY_VALIDATION_V1 (or any later) distinction.
    cohort_label: str = "default"

    @model_validator(mode="after")
    def _tz_aware(self) -> "StrategyVersionManifest":
        if self.frozen_at.tzinfo is None:
            raise ValueError("frozen_at must be timezone-aware")
        return self


def build_validation_manifest(
    *,
    manifest_id: str,
    period: ValidationPeriod,
    frozen_at: datetime,
    starting_nav: float,
    strategy_versions: dict[str, str],
    config_paths: tuple[Path, ...] = DEFAULT_MANIFEST_CONFIG_PATHS,
    notes: str = "",
    cohort_label: str = "default",
) -> StrategyVersionManifest:
    hashes = {str(p): compute_file_hash(p) for p in config_paths}
    return StrategyVersionManifest(
        manifest_id=manifest_id,
        frozen_at=frozen_at,
        period=period,
        starting_nav=starting_nav,
        config_file_hashes=hashes,
        strategy_versions=strategy_versions,
        notes=notes,
        cohort_label=cohort_label,
    )


class ManifestDriftError(RuntimeError):
    """Raised by `verify_manifest_integrity` when one or more of a frozen
    manifest's config files no longer hashes to the value recorded at
    freeze time — i.e. a silent modification occurred during the
    validation run. Fails loudly (an exception, not a boolean a caller
    could ignore), the same enforcement idiom
    `src.data.provider.OptionContract.assert_tradable` uses for stale
    data and `src.backtest.simulator.assert_no_lookahead_options` uses
    for lookahead."""


def verify_manifest_integrity(manifest: StrategyVersionManifest) -> None:
    drifted: list[str] = []
    for path_str, recorded_hash in manifest.config_file_hashes.items():
        path = Path(path_str)
        if not path.is_file():
            drifted.append(f"{path_str}: file no longer exists")
            continue
        current_hash = compute_file_hash(path)
        if current_hash != recorded_hash:
            drifted.append(f"{path_str}: hash changed ({recorded_hash[:12]}... -> {current_hash[:12]}...)")
    if drifted:
        raise ManifestDriftError(
            f"validation manifest {manifest.manifest_id!r} detected {len(drifted)} config change(s) "
            f"since it was frozen at {manifest.frozen_at.isoformat()}: " + "; ".join(drifted)
        )


def save_manifest(manifest: StrategyVersionManifest, path: Path | str) -> None:
    Path(path).write_text(manifest.model_dump_json(indent=2), encoding="utf-8")


def load_manifest(path: Path | str) -> StrategyVersionManifest:
    raw = Path(path).read_text(encoding="utf-8")
    return StrategyVersionManifest.model_validate(json.loads(raw))
