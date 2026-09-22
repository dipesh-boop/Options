"""Tests for `src.portfolio.persistence` (Step 22.4 Parts 30-32)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.portfolio.actions import ControlLoopAction
from src.portfolio.alerts import ControlLoopAlertType, raise_alert_if_new, resolve_alert
from src.portfolio.cycle_record import ControlCycleRecord
from src.portfolio.decision_snapshot import PortfolioControlDecisionSnapshot
from src.portfolio.persistence import InMemoryControlLoopStore, SqliteControlLoopStore

NOW = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)


def _snapshot(cycle_id="c1", position_id="p1", action=ControlLoopAction.HOLD) -> PortfolioControlDecisionSnapshot:
    return PortfolioControlDecisionSnapshot(
        cycle_id=cycle_id, timestamp=NOW, is_trading_day=True, is_market_open=True,
        provider="tradier", provider_health_status="healthy",
        portfolio_nav=100000.0, portfolio_cash=80000.0, portfolio_deployed_pct=0.2, portfolio_drawdown_pct=0.01,
        position_id=position_id, recommended_action=action,
    )


def _cycle(cycle_id="c1", completed=True) -> ControlCycleRecord:
    return ControlCycleRecord(
        cycle_id=cycle_id, started_at=NOW, completed_at=(NOW + timedelta(seconds=30)) if completed else None,
        market_open=True, provider="tradier", provider_health_status="healthy",
        symbols_requested=("SPY", "QQQ"), symbols_successful=("SPY", "QQQ"), positions_evaluated=1,
    )


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        return InMemoryControlLoopStore()
    return SqliteControlLoopStore(tmp_path / "control.db")


class TestDecisionSnapshots:
    def test_append_and_query_by_cycle(self, store):
        store.append_decision_snapshot(_snapshot(position_id="p1"))
        store.append_decision_snapshot(_snapshot(position_id="p2"))
        assert len(store.decision_snapshots_for_cycle("c1")) == 2

    def test_query_by_position(self, store):
        store.append_decision_snapshot(_snapshot(position_id="p1"))
        store.append_decision_snapshot(_snapshot(cycle_id="c2", position_id="p1"))
        store.append_decision_snapshot(_snapshot(position_id="p2"))
        assert len(store.decision_snapshots_for_position("p1")) == 2

    def test_append_only_never_replaces(self, store):
        store.append_decision_snapshot(_snapshot(position_id="p1", action=ControlLoopAction.HOLD))
        store.append_decision_snapshot(_snapshot(position_id="p1", action=ControlLoopAction.EXIT_REQUIRED))
        snaps = store.decision_snapshots_for_position("p1")
        assert len(snaps) == 2  # both preserved -- history is never overwritten


class TestCycleRecords:
    def test_save_and_get(self, store):
        store.save_cycle_record(_cycle())
        rec = store.get_cycle_record("c1")
        assert rec is not None and rec.cycle_id == "c1"

    def test_get_missing_returns_none(self, store):
        assert store.get_cycle_record("nonexistent") is None

    def test_replace_on_save_keeps_one_row_per_cycle(self, store):
        store.save_cycle_record(_cycle(completed=False))
        store.save_cycle_record(_cycle(completed=True))
        assert store.get_cycle_record("c1").is_complete is True
        assert len(store.recent_cycle_records()) == 1

    def test_recent_cycle_records_respects_limit(self, store):
        for i in range(5):
            store.save_cycle_record(_cycle(cycle_id=f"c{i}"))
        assert len(store.recent_cycle_records(limit=3)) == 3


class TestAlerts:
    def test_save_and_query_by_scope(self, store):
        alert = raise_alert_if_new([], scope="p1", alert_type=ControlLoopAlertType.PROFIT_TARGET, reason="x", now=NOW)
        store.save_alert(alert)
        assert len(store.alerts_for_scope("p1")) == 1

    def test_all_unresolved_alerts(self, store):
        a1 = raise_alert_if_new([], scope="p1", alert_type=ControlLoopAlertType.PROFIT_TARGET, reason="x", now=NOW)
        a2 = raise_alert_if_new([], scope="p2", alert_type=ControlLoopAlertType.DTE_EXIT, reason="y", now=NOW)
        store.save_alert(a1)
        store.save_alert(a2)
        assert len(store.all_unresolved_alerts()) == 2

    def test_resolve_replaces_not_duplicates(self, store):
        alert = raise_alert_if_new([], scope="p1", alert_type=ControlLoopAlertType.PROFIT_TARGET, reason="x", now=NOW)
        store.save_alert(alert)
        resolved = resolve_alert(alert, resolved_at=NOW + timedelta(minutes=5))
        store.save_alert(resolved)
        assert len(store.alerts_for_scope("p1")) == 1
        assert store.alerts_for_scope("p1")[0].resolved is True
        assert len(store.all_unresolved_alerts()) == 0


class TestSqliteRestartSurvival:
    def test_survives_restart(self, tmp_path):
        path = tmp_path / "control.db"
        store1 = SqliteControlLoopStore(path)
        store1.append_decision_snapshot(_snapshot())
        store1.save_cycle_record(_cycle())
        alert = raise_alert_if_new([], scope="p1", alert_type=ControlLoopAlertType.PROFIT_TARGET, reason="x", now=NOW)
        store1.save_alert(alert)
        del store1

        store2 = SqliteControlLoopStore(path)
        assert len(store2.decision_snapshots_for_cycle("c1")) == 1
        assert store2.get_cycle_record("c1") is not None
        assert len(store2.alerts_for_scope("p1")) == 1
        assert store2.schema_version_on_disk() == "1.0.0"
