from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src.validation.alerts import AlertCode, generate_alerts, insufficient_sample_alert
from src.validation.protocol import ValidationPeriod, build_validation_manifest
from src.validation.session import DailySnapshot, RuleViolationRecord

NOW = datetime(2026, 2, 1, 21, 0, tzinfo=timezone.utc)


def _snapshot(**overrides) -> DailySnapshot:
    defaults = dict(
        snapshot_date=date(2026, 2, 1), nav=100_000.0, cash=80_000.0, capital_deployed_pct=0.20,
        open_position_count=2, drawdown_pct=0.0, per_strategy_nav={}, recorded_at=NOW,
    )
    defaults.update(overrides)
    return DailySnapshot(**defaults)


def _call(**overrides):
    defaults = dict(
        snapshot=_snapshot(), limits=None, recent_trade_pnls=[], consecutive_loss_alert_count=5,
        weekly_pnl=None, starting_nav=100_000.0, weekly_loss_alert_pct=0.05, new_violations=[],
        manifest=None, now=NOW,
    )
    defaults.update(overrides)
    return generate_alerts(**defaults)


class TestDrawdownAlerts:
    def test_no_alert_below_warning(self, limits):
        alerts = _call(limits=limits, snapshot=_snapshot(drawdown_pct=0.03))
        assert not any(a.code in (AlertCode.DRAWDOWN_WARNING, AlertCode.DRAWDOWN_CRITICAL) for a in alerts)

    def test_warning_alert_at_warning_threshold(self, limits):
        alerts = _call(limits=limits, snapshot=_snapshot(drawdown_pct=0.09))
        assert any(a.code == AlertCode.DRAWDOWN_WARNING and a.severity == "warning" for a in alerts)

    def test_critical_alert_at_halt_threshold(self, limits):
        alerts = _call(limits=limits, snapshot=_snapshot(drawdown_pct=0.16))
        codes = [a.code for a in alerts]
        assert AlertCode.DRAWDOWN_CRITICAL in codes
        assert AlertCode.DRAWDOWN_WARNING not in codes  # only the more severe one fires


class TestConsecutiveLossAlert:
    def test_no_alert_below_threshold_count(self, limits):
        alerts = _call(limits=limits, recent_trade_pnls=[-10.0, -20.0, -30.0], consecutive_loss_alert_count=5)
        assert not any(a.code == AlertCode.CONSECUTIVE_LOSSES for a in alerts)

    def test_alert_fires_at_exact_threshold(self, limits):
        alerts = _call(limits=limits, recent_trade_pnls=[-10.0] * 5, consecutive_loss_alert_count=5)
        assert any(a.code == AlertCode.CONSECUTIVE_LOSSES for a in alerts)

    def test_a_single_winner_in_the_trailing_window_resets_the_streak(self, limits):
        alerts = _call(limits=limits, recent_trade_pnls=[-10.0, -10.0, 5.0, -10.0, -10.0], consecutive_loss_alert_count=5)
        assert not any(a.code == AlertCode.CONSECUTIVE_LOSSES for a in alerts)

    def test_only_trailing_window_is_checked(self, limits):
        """5 losses followed by a winner, then only 3 more losses --
        should not fire, since the most recent 5 aren't all losses."""
        pnls = [-10.0] * 5 + [50.0] + [-10.0] * 3
        alerts = _call(limits=limits, recent_trade_pnls=pnls, consecutive_loss_alert_count=5)
        assert not any(a.code == AlertCode.CONSECUTIVE_LOSSES for a in alerts)


class TestWeeklyLossAlert:
    def test_no_alert_when_weekly_pnl_not_supplied(self, limits):
        alerts = _call(limits=limits, weekly_pnl=None)
        assert not any(a.code == AlertCode.WEEKLY_LOSS_THRESHOLD for a in alerts)

    def test_no_alert_below_threshold(self, limits):
        alerts = _call(limits=limits, weekly_pnl=-1000.0, starting_nav=100_000.0, weekly_loss_alert_pct=0.05)
        assert not any(a.code == AlertCode.WEEKLY_LOSS_THRESHOLD for a in alerts)

    def test_alert_fires_at_threshold(self, limits):
        alerts = _call(limits=limits, weekly_pnl=-5000.0, starting_nav=100_000.0, weekly_loss_alert_pct=0.05)
        assert any(a.code == AlertCode.WEEKLY_LOSS_THRESHOLD for a in alerts)

    def test_rejects_nonpositive_starting_nav(self, limits):
        with pytest.raises(ValueError):
            _call(limits=limits, weekly_pnl=-100.0, starting_nav=0.0)


class TestViolationAlerts:
    def test_each_violation_produces_a_critical_alert(self, limits):
        violations = [
            RuleViolationRecord(violation_id="v1", occurred_at=NOW, rule_name="r1", description="d1"),
            RuleViolationRecord(violation_id="v2", occurred_at=NOW, rule_name="r2", description="d2"),
        ]
        alerts = _call(limits=limits, new_violations=violations)
        violation_alerts = [a for a in alerts if a.code == AlertCode.RULE_VIOLATION]
        assert len(violation_alerts) == 2
        assert all(a.severity == "critical" for a in violation_alerts)


class TestManifestDriftAlert:
    def _period(self) -> ValidationPeriod:
        return ValidationPeriod(start_date=date(2026, 1, 1), end_date=date(2026, 4, 1), duration_days=90)

    def test_no_alert_when_manifest_is_none(self, limits):
        alerts = _call(limits=limits, manifest=None)
        assert not any(a.code == AlertCode.MANIFEST_DRIFT for a in alerts)

    def test_no_alert_when_manifest_unchanged(self, limits, tmp_path: Path):
        f = tmp_path / "config.yaml"
        f.write_text("a: 1\n")
        manifest = build_validation_manifest(
            manifest_id="run-1", period=self._period(), frozen_at=NOW,
            starting_nav=100_000.0, strategy_versions={}, config_paths=(f,),
        )
        alerts = _call(limits=limits, manifest=manifest)
        assert not any(a.code == AlertCode.MANIFEST_DRIFT for a in alerts)

    def test_critical_alert_when_manifest_config_changed(self, limits, tmp_path: Path):
        """The core version-change/drift scenario at the alerting layer."""
        f = tmp_path / "config.yaml"
        f.write_text("a: 1\n")
        manifest = build_validation_manifest(
            manifest_id="run-2", period=self._period(), frozen_at=NOW,
            starting_nav=100_000.0, strategy_versions={}, config_paths=(f,),
        )
        f.write_text("a: 2\n")
        alerts = _call(limits=limits, manifest=manifest)
        drift_alerts = [a for a in alerts if a.code == AlertCode.MANIFEST_DRIFT]
        assert len(drift_alerts) == 1
        assert drift_alerts[0].severity == "critical"


class TestInsufficientSampleAlert:
    def test_returns_a_warning_alert(self):
        alert = insufficient_sample_alert(NOW)
        assert alert.code == AlertCode.INSUFFICIENT_SAMPLE_AT_90
        assert alert.severity == "warning"
