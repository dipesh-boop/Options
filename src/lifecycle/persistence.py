"""Durable lifecycle storage: persists `LifecyclePositionRecord` state,
every `LifecycleDecisionSnapshot` ever produced, and every `Alert`
across application restarts.

Three distinct storage semantics, matching `src.wheel.persistence`'s
own reasoning for why `WheelPosition` and this package's records don't
share one shape:

- **`LifecyclePositionRecord`** changes on every lifecycle evaluation
  (current state, excursion, roll chain) — stored REPLACE-on-save,
  keyed by `trade_id`, exactly like `SqliteWheelStore` stores
  `WheelPosition` keyed by `wheel_id`.
- **`LifecycleDecisionSnapshot`** (Part 17) is immutable once produced
  — every evaluation appends a new row, never replaces an old one, so
  the full history "lets anyone reconstruct exactly WHY a position was
  held or closed" (Part 17's own words) is never lost to a later
  overwrite.
- **`Alert`** (Part 23) is mostly immutable but does transition
  unresolved -> resolved exactly once — stored REPLACE-on-save keyed by
  `alert_id`, the same pattern as the position record, since that one
  field-level change is the only mutation an `Alert` ever undergoes.
"""
from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator

from src.lifecycle.alerts import Alert
from src.lifecycle.excursion import ExcursionState
from src.lifecycle.snapshot import LifecycleDecisionSnapshot
from src.lifecycle.state import PositionLifecycleState
from src.strategies.base import StrategyKind

LIFECYCLE_DATABASE_SCHEMA_VERSION = "1.0.0"


def _tz_aware(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("timestamp fields must be timezone-aware")
    return v


class LifecyclePositionRecord(BaseModel):
    """The mutable "current state" record for one position under
    lifecycle management — the analogue of `WheelPosition` for every
    non-Wheel strategy (and, for a Wheel's own CSP/CC legs, a *parallel*
    record alongside `WheelPosition`, never a replacement for it: see
    `src.lifecycle.policies_library.WHEEL_STANDARD`'s own docstring)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trade_id: str
    wheel_id: str | None = None
    ticker: str
    strategy_kind: StrategyKind
    management_policy_name: str
    current_state: PositionLifecycleState
    excursion: ExcursionState
    roll_chain_id: str | None = None
    created_at: datetime
    updated_at: datetime

    _validate_created = field_validator("created_at")(_tz_aware)
    _validate_updated = field_validator("updated_at")(_tz_aware)


class LifecycleStore(ABC):
    @abstractmethod
    def save_position(self, record: LifecyclePositionRecord) -> None: ...

    @abstractmethod
    def get_position(self, trade_id: str) -> LifecyclePositionRecord | None: ...

    @abstractmethod
    def all_positions(self) -> list[LifecyclePositionRecord]: ...

    @abstractmethod
    def positions_by_state(self, state: PositionLifecycleState) -> list[LifecyclePositionRecord]: ...

    @abstractmethod
    def append_snapshot(self, snapshot: LifecycleDecisionSnapshot) -> None: ...

    @abstractmethod
    def snapshots_for_trade(self, trade_id: str) -> list[LifecycleDecisionSnapshot]: ...

    @abstractmethod
    def save_alert(self, alert: Alert) -> None: ...

    @abstractmethod
    def alerts_for_trade(self, trade_id: str) -> list[Alert]: ...

    @abstractmethod
    def all_unresolved_alerts(self) -> list[Alert]: ...


class InMemoryLifecycleStore(LifecycleStore):
    """Process-local only — lost on restart. For tests and any caller
    that doesn't need restart-survival."""

    def __init__(self) -> None:
        self._positions: dict[str, LifecyclePositionRecord] = {}
        self._snapshots: dict[str, list[LifecycleDecisionSnapshot]] = {}
        self._alerts: dict[str, Alert] = {}

    def save_position(self, record: LifecyclePositionRecord) -> None:
        self._positions[record.trade_id] = record

    def get_position(self, trade_id: str) -> LifecyclePositionRecord | None:
        return self._positions.get(trade_id)

    def all_positions(self) -> list[LifecyclePositionRecord]:
        return list(self._positions.values())

    def positions_by_state(self, state: PositionLifecycleState) -> list[LifecyclePositionRecord]:
        return [p for p in self._positions.values() if p.current_state == state]

    def append_snapshot(self, snapshot: LifecycleDecisionSnapshot) -> None:
        self._snapshots.setdefault(snapshot.trade_id, []).append(snapshot)

    def snapshots_for_trade(self, trade_id: str) -> list[LifecycleDecisionSnapshot]:
        return list(self._snapshots.get(trade_id, []))

    def save_alert(self, alert: Alert) -> None:
        self._alerts[alert.alert_id] = alert

    def alerts_for_trade(self, trade_id: str) -> list[Alert]:
        return [a for a in self._alerts.values() if a.trade_id == trade_id]

    def all_unresolved_alerts(self) -> list[Alert]:
        return [a for a in self._alerts.values() if not a.resolved]


class SqliteLifecycleStore(LifecycleStore):
    """A durable `LifecycleStore` backed by a single sqlite file. Each
    operation opens and closes its own connection — this class holds no
    in-process state a crash could lose, matching `SqliteWheelStore`/
    `SqliteIdempotencyStore`'s established pattern in this codebase."""

    def __init__(self, db_path: Path | str) -> None:
        self._path = str(db_path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS lifecycle_positions ("
                "trade_id TEXT PRIMARY KEY, state TEXT NOT NULL, "
                "schema_version TEXT NOT NULL, record_json TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS lifecycle_snapshots ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id TEXT NOT NULL, "
                "timestamp TEXT NOT NULL, schema_version TEXT NOT NULL, snapshot_json TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lifecycle_snapshots_trade_id ON lifecycle_snapshots(trade_id)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS lifecycle_alerts ("
                "alert_id TEXT PRIMARY KEY, trade_id TEXT NOT NULL, resolved INTEGER NOT NULL, "
                "schema_version TEXT NOT NULL, alert_json TEXT NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def save_position(self, record: LifecyclePositionRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO lifecycle_positions (trade_id, state, schema_version, record_json) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(trade_id) DO UPDATE SET state=excluded.state, "
                "schema_version=excluded.schema_version, record_json=excluded.record_json",
                (record.trade_id, record.current_state.value, LIFECYCLE_DATABASE_SCHEMA_VERSION, record.model_dump_json()),
            )

    def get_position(self, trade_id: str) -> LifecyclePositionRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT record_json FROM lifecycle_positions WHERE trade_id = ?", (trade_id,)).fetchone()
        return LifecyclePositionRecord.model_validate_json(row[0]) if row is not None else None

    def all_positions(self) -> list[LifecyclePositionRecord]:
        with self._connect() as conn:
            rows = conn.execute("SELECT record_json FROM lifecycle_positions ORDER BY trade_id").fetchall()
        return [LifecyclePositionRecord.model_validate_json(r[0]) for r in rows]

    def positions_by_state(self, state: PositionLifecycleState) -> list[LifecyclePositionRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT record_json FROM lifecycle_positions WHERE state = ? ORDER BY trade_id", (state.value,)
            ).fetchall()
        return [LifecyclePositionRecord.model_validate_json(r[0]) for r in rows]

    def append_snapshot(self, snapshot: LifecycleDecisionSnapshot) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO lifecycle_snapshots (trade_id, timestamp, schema_version, snapshot_json) VALUES (?, ?, ?, ?)",
                (snapshot.trade_id, snapshot.timestamp.isoformat(), LIFECYCLE_DATABASE_SCHEMA_VERSION, snapshot.model_dump_json()),
            )

    def snapshots_for_trade(self, trade_id: str) -> list[LifecycleDecisionSnapshot]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT snapshot_json FROM lifecycle_snapshots WHERE trade_id = ? ORDER BY id", (trade_id,)
            ).fetchall()
        return [LifecycleDecisionSnapshot.model_validate_json(r[0]) for r in rows]

    def save_alert(self, alert: Alert) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO lifecycle_alerts (alert_id, trade_id, resolved, schema_version, alert_json) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(alert_id) DO UPDATE SET resolved=excluded.resolved, "
                "schema_version=excluded.schema_version, alert_json=excluded.alert_json",
                (alert.alert_id, alert.trade_id, int(alert.resolved), LIFECYCLE_DATABASE_SCHEMA_VERSION, alert.model_dump_json()),
            )

    def alerts_for_trade(self, trade_id: str) -> list[Alert]:
        with self._connect() as conn:
            rows = conn.execute("SELECT alert_json FROM lifecycle_alerts WHERE trade_id = ?", (trade_id,)).fetchall()
        return [Alert.model_validate_json(r[0]) for r in rows]

    def all_unresolved_alerts(self) -> list[Alert]:
        with self._connect() as conn:
            rows = conn.execute("SELECT alert_json FROM lifecycle_alerts WHERE resolved = 0").fetchall()
        return [Alert.model_validate_json(r[0]) for r in rows]

    def schema_version_on_disk(self) -> str | None:
        """Reads back whatever `schema_version` the most recently saved
        position row actually carries — mirrors
        `SqliteWheelStore.schema_version_on_disk`'s own startup-check
        purpose. A raw string compare, so it can never itself throw the
        way a failed `model_validate_json` against a genuinely
        incompatible shape would."""
        with self._connect() as conn:
            row = conn.execute("SELECT schema_version FROM lifecycle_positions LIMIT 1").fetchone()
        return row[0] if row is not None else None
