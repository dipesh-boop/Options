"""Tests for config loading, ValidationPeriod, sample-size gating, and
strategy-version-freeze/drift detection."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.validation.protocol import (
    ManifestDriftError,
    SampleSizeStatus,
    ValidationConfigError,
    ValidationPeriod,
    build_validation_manifest,
    build_validation_period,
    compute_file_hash,
    load_manifest,
    load_validation_config,
    sample_size_status,
    save_manifest,
    verify_manifest_integrity,
)

from .conftest import _validation_config


class TestLoadValidationConfig:
    def test_loads_real_config_file(self):
        cfg = load_validation_config()
        assert cfg.duration_days == 90
        assert cfg.minimum_completed_trades == 50
        assert cfg.preferred_completed_trades == 100
        assert cfg.default_starting_nav == 100_000.0

    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(ValidationConfigError):
            load_validation_config(tmp_path / "nope.yaml")

    def test_env_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        real = Path(__file__).resolve().parents[3] / "config" / "validation.yaml"
        monkeypatch.setenv("OPTIONS_AGENT_VALIDATION_MIN_TRADES", "5")
        cfg = load_validation_config(real)
        assert cfg.minimum_completed_trades == 5

    def test_blank_env_override_is_treated_as_unset(self, monkeypatch: pytest.MonkeyPatch):
        """Step 22.8 (PAPER_TRADING_V1.4.7): the shipped .env template
        leaves OPTIONS_AGENT_VALIDATION_* blank by convention -- a
        blank-but-present override must fall through to the YAML
        default, never attempt int('')."""
        real = Path(__file__).resolve().parents[3] / "config" / "validation.yaml"
        monkeypatch.setenv("OPTIONS_AGENT_VALIDATION_MIN_TRADES", "")
        monkeypatch.setenv("OPTIONS_AGENT_VALIDATION_DURATION_DAYS", "")
        cfg = load_validation_config(real)
        assert cfg.minimum_completed_trades == 50
        assert cfg.duration_days == 90

    def test_inconsistent_thresholds_rejected(self, tmp_path: Path):
        bad = tmp_path / "validation.yaml"
        bad.write_text(
            """
validation_period:
  duration_days: 90
  checkpoint_days: [30, 60]
sample_size:
  minimum_completed_trades: 100
  preferred_completed_trades: 50
starting_capital:
  default_nav: 100000.0
research_targets:
  annual_return_low_pct: 0.12
  annual_return_high_pct: 0.15
  reference_min_sharpe: 1.0
  reference_max_acceptable_drawdown_pct: 0.15
statistics:
  bootstrap_iterations: 100
  bootstrap_confidence_pct: 0.9
  monte_carlo_iterations: 100
  var_confidence_pct: 0.95
  random_seed: 1
decision_quality:
  min_probability_of_profit: 0.5
  rejected_trade_min_sample_size: 20
alerts:
  consecutive_loss_alert_count: 5
  weekly_loss_alert_pct: 0.05
"""
        )
        with pytest.raises(ValidationConfigError):
            load_validation_config(bad)


class TestSampleSizeStatus:
    def test_below_minimum_is_insufficient(self):
        cfg = _validation_config()
        assert sample_size_status(0, cfg) == SampleSizeStatus.INSUFFICIENT_SAMPLE
        assert sample_size_status(49, cfg) == SampleSizeStatus.INSUFFICIENT_SAMPLE

    def test_at_minimum_below_preferred_is_minimum_sample(self):
        cfg = _validation_config()
        assert sample_size_status(50, cfg) == SampleSizeStatus.MINIMUM_SAMPLE
        assert sample_size_status(99, cfg) == SampleSizeStatus.MINIMUM_SAMPLE

    def test_at_or_above_preferred_is_preferred_sample(self):
        cfg = _validation_config()
        assert sample_size_status(100, cfg) == SampleSizeStatus.PREFERRED_SAMPLE
        assert sample_size_status(500, cfg) == SampleSizeStatus.PREFERRED_SAMPLE


class TestValidationPeriod:
    def test_build_from_config(self):
        cfg = _validation_config()
        period = build_validation_period(date(2026, 1, 1), cfg)
        assert period.start_date == date(2026, 1, 1)
        assert period.end_date == date(2026, 4, 1)
        assert period.duration_days == 90

    def test_end_before_start_rejected(self):
        with pytest.raises(ValueError):
            ValidationPeriod(start_date=date(2026, 1, 1), end_date=date(2025, 1, 1), duration_days=90)

    def test_duration_mismatch_rejected(self):
        with pytest.raises(ValueError):
            ValidationPeriod(start_date=date(2026, 1, 1), end_date=date(2026, 1, 10), duration_days=90)

    def test_elapsed_calendar_days_clamped_to_period(self):
        period = ValidationPeriod(start_date=date(2026, 1, 1), end_date=date(2026, 4, 1), duration_days=90)
        assert period.elapsed_calendar_days(date(2025, 12, 1)) == 0
        assert period.elapsed_calendar_days(date(2026, 1, 31)) == 30
        assert period.elapsed_calendar_days(date(2027, 1, 1)) == 90

    def test_is_complete(self):
        period = ValidationPeriod(start_date=date(2026, 1, 1), end_date=date(2026, 4, 1), duration_days=90)
        assert period.is_complete(date(2026, 3, 1)) is False
        assert period.is_complete(date(2026, 4, 1)) is True

    def test_is_checkpoint_reached(self):
        period = ValidationPeriod(start_date=date(2026, 1, 1), end_date=date(2026, 4, 1), duration_days=90)
        assert period.is_checkpoint_reached(date(2026, 1, 31), 30) is True
        assert period.is_checkpoint_reached(date(2026, 1, 20), 30) is False


class TestManifestFreezeAndDrift:
    def _period(self) -> ValidationPeriod:
        return ValidationPeriod(start_date=date(2026, 1, 1), end_date=date(2026, 4, 1), duration_days=90)

    def test_build_manifest_hashes_real_config_files(self):
        manifest = build_validation_manifest(
            manifest_id="run-1",
            period=self._period(),
            frozen_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            starting_nav=100_000.0,
            strategy_versions={"cash_secured_put": "v1", "covered_call": "v1", "put_credit_spread": "v1"},
        )
        assert len(manifest.config_file_hashes) == 3
        for path_str in manifest.config_file_hashes:
            assert Path(path_str).is_file()

    def test_compute_file_hash_is_deterministic(self, tmp_path: Path):
        f = tmp_path / "x.txt"
        f.write_text("hello")
        assert compute_file_hash(f) == compute_file_hash(f)

    def test_verify_integrity_passes_when_unchanged(self, tmp_path: Path):
        f = tmp_path / "config.yaml"
        f.write_text("a: 1\n")
        manifest = build_validation_manifest(
            manifest_id="run-2", period=self._period(), frozen_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            starting_nav=100_000.0, strategy_versions={}, config_paths=(f,),
        )
        verify_manifest_integrity(manifest)  # does not raise

    def test_verify_integrity_raises_on_file_modification(self, tmp_path: Path):
        """The core version-change/drift-detection scenario Step 19
        explicitly asks tests to cover."""
        f = tmp_path / "config.yaml"
        f.write_text("a: 1\n")
        manifest = build_validation_manifest(
            manifest_id="run-3", period=self._period(), frozen_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            starting_nav=100_000.0, strategy_versions={}, config_paths=(f,),
        )
        f.write_text("a: 2\n")  # silent modification after freeze
        with pytest.raises(ManifestDriftError, match="hash changed"):
            verify_manifest_integrity(manifest)

    def test_verify_integrity_raises_on_deleted_file(self, tmp_path: Path):
        f = tmp_path / "config.yaml"
        f.write_text("a: 1\n")
        manifest = build_validation_manifest(
            manifest_id="run-4", period=self._period(), frozen_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            starting_nav=100_000.0, strategy_versions={}, config_paths=(f,),
        )
        f.unlink()
        with pytest.raises(ManifestDriftError, match="no longer exists"):
            verify_manifest_integrity(manifest)

    def test_frozen_at_requires_timezone(self):
        with pytest.raises(Exception):
            build_validation_manifest(
                manifest_id="run-5", period=self._period(), frozen_at=datetime(2026, 1, 1),
                starting_nav=100_000.0, strategy_versions={}, config_paths=(),
            )

    def test_save_and_load_manifest_roundtrip(self, tmp_path: Path):
        f = tmp_path / "config.yaml"
        f.write_text("a: 1\n")
        manifest = build_validation_manifest(
            manifest_id="run-6", period=self._period(), frozen_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            starting_nav=100_000.0, strategy_versions={"csp": "v1"}, config_paths=(f,), notes="first run",
        )
        out = tmp_path / "VALIDATION_MANIFEST.json"
        save_manifest(manifest, out)
        loaded = load_manifest(out)
        assert loaded == manifest
        assert json.loads(out.read_text())["manifest_id"] == "run-6"
