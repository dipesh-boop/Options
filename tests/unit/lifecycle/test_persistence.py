"""Persistence (Part 27): position records (REPLACE semantics),
append-only snapshots, alerts, and restart recovery via a fresh
connection against the same sqlite file."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.lifecycle.alerts import Alert, AlertType, resolve_alert
from src.lifecycle.excursion import initial_excursion
from src.lifecycle.persistence import (
    InMemoryLifecycleStore,
    LifecyclePositionRecord,
    LIFECYCLE_DATABASE_SCHEMA_VERSION,
    SqliteLifecycleStore,
)
from src.lifecycle.snapshot import LifecycleDecisionSnapshot
from src.lifecycle.state import PositionLifecycleState as S
from src.strategies.base import StrategyKind

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _record(**overrides) -> LifecyclePositionRecord:
    base = dict(
        trade_id="T1", wheel_id=None, ticker="XYZ", strategy_kind=StrategyKind.PUT_CREDIT_SPREAD,
        management_policy_name="PUT_CREDIT_SPREAD_STANDARD", current_state=S.ACTIVE,
        excursion=initial_excursion(10.0, T0), roll_chain_id=None, created_at=T0, updated_at=T0,
    )
    base.update(overrides)
    return LifecyclePositionRecord(**base)


def _snapshot(**overrides) -> LifecycleDecisionSnapshot:
    base = dict(
        trade_id="T1", timestamp=T0, strategy=StrategyKind.PUT_CREDIT_SPREAD, management_policy="PUT_CREDIT_SPREAD_STANDARD",
        current_state=S.ACTIVE, mfe=10.0, mae=10.0, unrealized_pnl=10.0, realized_pnl=0.0,
        deterministic_action_category="hold", deterministic_action_target_state=S.ACTIVE,
        deterministic_action_mandatory=False, deterministic_action_reason="no trigger", risk_status="ok", data_is_fresh=True,
    )
    base.update(overrides)
    return LifecycleDecisionSnapshot(**base)


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        return InMemoryLifecycleStore()
    return SqliteLifecycleStore(tmp_path / "lifecycle.db")


class TestPositionRecords:
    def test_save_and_get(self, store):
        store.save_position(_record())
        got = store.get_position("T1")
        assert got is not None and got.current_state == S.ACTIVE

    def test_missing_trade_id_returns_none(self, store):
        assert store.get_position("NOPE") is None

    def test_save_replaces_not_appends(self, store):
        store.save_position(_record())
        store.save_position(_record(current_state=S.PROFIT_TARGET_REACHED, updated_at=T0 + timedelta(days=1)))
        assert len(store.all_positions()) == 1
        assert store.get_position("T1").current_state == S.PROFIT_TARGET_REACHED

    def test_positions_by_state(self, store):
        store.save_position(_record(trade_id="T1", current_state=S.ACTIVE))
        store.save_position(_record(trade_id="T2", current_state=S.PROFIT_TARGET_REACHED))
        assert len(store.positions_by_state(S.ACTIVE)) == 1
        assert len(store.positions_by_state(S.PROFIT_TARGET_REACHED)) == 1


class TestSnapshotsAreAppendOnly:
    def test_multiple_snapshots_for_same_trade_are_all_kept(self, store):
        store.append_snapshot(_snapshot(current_state=S.ACTIVE))
        store.append_snapshot(_snapshot(current_state=S.PROFIT_TARGET_REACHED, timestamp=T0 + timedelta(days=1)))
        snaps = store.snapshots_for_trade("T1")
        assert len(snaps) == 2

    def test_snapshots_returned_in_chronological_order(self, store):
        store.append_snapshot(_snapshot(current_state=S.ACTIVE, timestamp=T0))
        store.append_snapshot(_snapshot(current_state=S.PROFIT_TARGET_REACHED, timestamp=T0 + timedelta(days=1)))
        snaps = store.snapshots_for_trade("T1")
        assert snaps[0].current_state == S.ACTIVE
        assert snaps[1].current_state == S.PROFIT_TARGET_REACHED

    def test_no_snapshots_returns_empty_list(self, store):
        assert store.snapshots_for_trade("NOPE") == []


class TestAlerts:
    def test_save_and_resolve(self, store):
        alert = Alert(alert_id="A1", trade_id="T1", alert_type=AlertType.PROFIT_TARGET_REACHED, reason="x", created_at=T0)
        store.save_alert(alert)
        assert len(store.all_unresolved_alerts()) == 1
        resolved = resolve_alert(alert, resolved_at=T0 + timedelta(days=1))
        store.save_alert(resolved)
        assert len(store.all_unresolved_alerts()) == 0
        assert len(store.alerts_for_trade("T1")) == 1


class TestRestartRecovery:
    def test_full_state_survives_a_fresh_connection(self, tmp_path):
        path = tmp_path / "lifecycle.db"
        store1 = SqliteLifecycleStore(path)
        store1.save_position(_record(current_state=S.PROFIT_TARGET_REACHED))
        store1.append_snapshot(_snapshot())
        store1.append_snapshot(_snapshot(timestamp=T0 + timedelta(days=1)))
        store1.save_alert(Alert(alert_id="A1", trade_id="T1", alert_type=AlertType.PROFIT_TARGET_REACHED, reason="x", created_at=T0, resolved=True, resolved_at=T0))
        del store1

        store2 = SqliteLifecycleStore(path)
        assert store2.get_position("T1").current_state == S.PROFIT_TARGET_REACHED
        assert len(store2.snapshots_for_trade("T1")) == 2
        assert store2.alerts_for_trade("T1")[0].resolved is True
        assert store2.schema_version_on_disk() == LIFECYCLE_DATABASE_SCHEMA_VERSION
