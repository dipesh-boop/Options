"""Durable control-loop storage: persists every
`PortfolioControlDecisionSnapshot` and every `ControlCycleRecord` across
application restarts. Same pattern as `src.lifecycle.persistence`
(itself matching `src.wheel.persistence`): an `ABC` interface, an
`InMemory*` implementation for tests/callers that don't need
restart-survival, and a `Sqlite*` implementation that opens and closes
its own connection per operation so this class holds no in-process state
a crash could lose.

Both record types are append-only (Part 30/31 both say so explicitly --
"immutable/never rewrite history") -- unlike `LifecycleStore`'s
`LifecyclePositionRecord`/`WheelPosition`, there is no mutable
"current state" row in this store at all. `ControlCycleRecord` is the
one partial exception: a cycle that's still running is saved once with
`completed_at=None` and then re-saved (REPLACE, keyed by `cycle_id`)
when it finishes -- never a second, separate "completed" row for the
same cycle.

Also persists `src.portfolio.alerts.ControlLoopAlert` (Part 32),
REPLACE-on-save keyed by `alert_id` -- the same one-mutation-ever
pattern (`resolved`/`resolved_at` flip exactly once)
`src.lifecycle.persistence.SqliteLifecycleStore.save_alert` already
uses for `Alert`.
"""
from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from pathlib import Path

from src.portfolio.alerts import ControlLoopAlert
from src.portfolio.cycle_record import ControlCycleRecord
from src.portfolio.decision_snapshot import PortfolioControlDecisionSnapshot

CONTROL_LOOP_DATABASE_SCHEMA_VERSION = "1.0.0"


class ControlLoopStore(ABC):
    @abstractmethod
    def append_decision_snapshot(self, snapshot: PortfolioControlDecisionSnapshot) -> None: ...

    @abstractmethod
    def decision_snapshots_for_cycle(self, cycle_id: str) -> list[PortfolioControlDecisionSnapshot]: ...

    @abstractmethod
    def decision_snapshots_for_position(self, position_id: str) -> list[PortfolioControlDecisionSnapshot]: ...

    @abstractmethod
    def save_cycle_record(self, record: ControlCycleRecord) -> None: ...

    @abstractmethod
    def get_cycle_record(self, cycle_id: str) -> ControlCycleRecord | None: ...

    @abstractmethod
    def recent_cycle_records(self, limit: int = 50) -> list[ControlCycleRecord]: ...

    @abstractmethod
    def save_alert(self, alert: ControlLoopAlert) -> None: ...

    @abstractmethod
    def alerts_for_scope(self, scope: str) -> list[ControlLoopAlert]: ...

    @abstractmethod
    def all_unresolved_alerts(self) -> list[ControlLoopAlert]: ...


class InMemoryControlLoopStore(ControlLoopStore):
    """Process-local only -- lost on restart. For tests and any caller
    that doesn't need restart-survival."""

    def __init__(self) -> None:
        self._snapshots: list[PortfolioControlDecisionSnapshot] = []
        self._cycles: dict[str, ControlCycleRecord] = {}
        self._cycle_order: list[str] = []
        self._alerts: dict[str, ControlLoopAlert] = {}

    def append_decision_snapshot(self, snapshot: PortfolioControlDecisionSnapshot) -> None:
        self._snapshots.append(snapshot)

    def decision_snapshots_for_cycle(self, cycle_id: str) -> list[PortfolioControlDecisionSnapshot]:
        return [s for s in self._snapshots if s.cycle_id == cycle_id]

    def decision_snapshots_for_position(self, position_id: str) -> list[PortfolioControlDecisionSnapshot]:
        return [s for s in self._snapshots if s.position_id == position_id]

    def save_cycle_record(self, record: ControlCycleRecord) -> None:
        if record.cycle_id not in self._cycles:
            self._cycle_order.append(record.cycle_id)
        self._cycles[record.cycle_id] = record

    def get_cycle_record(self, cycle_id: str) -> ControlCycleRecord | None:
        return self._cycles.get(cycle_id)

    def recent_cycle_records(self, limit: int = 50) -> list[ControlCycleRecord]:
        ordered = [self._cycles[cid] for cid in reversed(self._cycle_order)]
        return ordered[:limit]

    def save_alert(self, alert: ControlLoopAlert) -> None:
        self._alerts[alert.alert_id] = alert

    def alerts_for_scope(self, scope: str) -> list[ControlLoopAlert]:
        return [a for a in self._alerts.values() if a.scope == scope]

    def all_unresolved_alerts(self) -> list[ControlLoopAlert]:
        return [a for a in self._alerts.values() if not a.resolved]


class SqliteControlLoopStore(ControlLoopStore):
    """A durable `ControlLoopStore` backed by a single sqlite file."""

    def __init__(self, db_path: Path | str) -> None:
        self._path = str(db_path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS control_decision_snapshots ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id TEXT NOT NULL, "
                "position_id TEXT, timestamp TEXT NOT NULL, "
                "schema_version TEXT NOT NULL, snapshot_json TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_control_snapshots_cycle_id "
                "ON control_decision_snapshots(cycle_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_control_snapshots_position_id "
                "ON control_decision_snapshots(position_id)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS control_cycle_records ("
                "cycle_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, "
                "schema_version TEXT NOT NULL, record_json TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_control_cycles_started_at "
                "ON control_cycle_records(started_at)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS control_loop_alerts ("
                "alert_id TEXT PRIMARY KEY, scope TEXT NOT NULL, resolved INTEGER NOT NULL, "
                "schema_version TEXT NOT NULL, alert_json TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_control_loop_alerts_scope ON control_loop_alerts(scope)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def append_decision_snapshot(self, snapshot: PortfolioControlDecisionSnapshot) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO control_decision_snapshots "
                "(cycle_id, position_id, timestamp, schema_version, snapshot_json) VALUES (?, ?, ?, ?, ?)",
                (
                    snapshot.cycle_id, snapshot.position_id, snapshot.timestamp.isoformat(),
                    CONTROL_LOOP_DATABASE_SCHEMA_VERSION, snapshot.model_dump_json(),
                ),
            )

    def decision_snapshots_for_cycle(self, cycle_id: str) -> list[PortfolioControlDecisionSnapshot]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT snapshot_json FROM control_decision_snapshots WHERE cycle_id = ? ORDER BY id",
                (cycle_id,),
            ).fetchall()
        return [PortfolioControlDecisionSnapshot.model_validate_json(r[0]) for r in rows]

    def decision_snapshots_for_position(self, position_id: str) -> list[PortfolioControlDecisionSnapshot]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT snapshot_json FROM control_decision_snapshots WHERE position_id = ? ORDER BY id",
                (position_id,),
            ).fetchall()
        return [PortfolioControlDecisionSnapshot.model_validate_json(r[0]) for r in rows]

    def save_cycle_record(self, record: ControlCycleRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO control_cycle_records (cycle_id, started_at, schema_version, record_json) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(cycle_id) DO UPDATE SET "
                "started_at=excluded.started_at, schema_version=excluded.schema_version, "
                "record_json=excluded.record_json",
                (record.cycle_id, record.started_at.isoformat(), CONTROL_LOOP_DATABASE_SCHEMA_VERSION, record.model_dump_json()),
            )

    def get_cycle_record(self, cycle_id: str) -> ControlCycleRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT record_json FROM control_cycle_records WHERE cycle_id = ?", (cycle_id,)
            ).fetchone()
        return ControlCycleRecord.model_validate_json(row[0]) if row is not None else None

    def recent_cycle_records(self, limit: int = 50) -> list[ControlCycleRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT record_json FROM control_cycle_records ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [ControlCycleRecord.model_validate_json(r[0]) for r in rows]

    def schema_version_on_disk(self) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT schema_version FROM control_cycle_records LIMIT 1").fetchone()
        return row[0] if row is not None else None

    def save_alert(self, alert: ControlLoopAlert) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO control_loop_alerts (alert_id, scope, resolved, schema_version, alert_json) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(alert_id) DO UPDATE SET "
                "resolved=excluded.resolved, schema_version=excluded.schema_version, alert_json=excluded.alert_json",
                (alert.alert_id, alert.scope, int(alert.resolved), CONTROL_LOOP_DATABASE_SCHEMA_VERSION, alert.model_dump_json()),
            )

    def alerts_for_scope(self, scope: str) -> list[ControlLoopAlert]:
        with self._connect() as conn:
            rows = conn.execute("SELECT alert_json FROM control_loop_alerts WHERE scope = ?", (scope,)).fetchall()
        return [ControlLoopAlert.model_validate_json(r[0]) for r in rows]

    def all_unresolved_alerts(self) -> list[ControlLoopAlert]:
        with self._connect() as conn:
            rows = conn.execute("SELECT alert_json FROM control_loop_alerts WHERE resolved = 0").fetchall()
        return [ControlLoopAlert.model_validate_json(r[0]) for r in rows]
