"""Durable Wheel storage (Part 21): persists `WheelPosition` state across
application restarts. `WheelPosition` already carries its complete
nested history (every CSP/CC cycle, every state transition, every audit
event) in one Pydantic object, so this module stores it as a single
JSON blob per `wheel_id` — the same "the whole object round-trips through
`model_dump_json`/`model_validate_json`" approach
`src.brokers.base.SqliteIdempotencyStore` already uses for `Order`,
rather than re-deriving `src.validation.session`'s multi-table,
append-only pattern (appropriate there because a `TradeRecord`/
`DailySnapshot` never changes after it's written; a `WheelPosition`
changes on every lifecycle event, so REPLACE-on-save, keyed by
`wheel_id`, is the correct semantics here — mirroring how that module's
own `cohorts` table, its one genuinely mutable record type, is stored).
"""
from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from pathlib import Path

from src.wheel.models import WheelPosition

# Bumped only when WheelPosition's on-disk JSON shape changes in a way
# that would make an older database file structurally incompatible with
# the current schema (a new required field, a renamed field) -- read by
# src.validation.freeze's VALIDATION_MANIFEST.json exactly like
# src.validation.session.DATABASE_SCHEMA_VERSION already is, so a freeze
# can detect "this Wheel database predates a breaking schema change" as
# its own distinct failure mode.
WHEEL_DATABASE_SCHEMA_VERSION = "1.0.0"


class WheelStore(ABC):
    """Every `save` is idempotent in the sense that matters here: saving
    the exact same `WheelPosition` twice leaves the store in the same
    state (not "records it twice" — there is only ever one row per
    `wheel_id`, always reflecting the most recently saved snapshot of
    that Wheel's full history)."""

    @abstractmethod
    def save(self, wheel: WheelPosition) -> None: ...

    @abstractmethod
    def get(self, wheel_id: str) -> WheelPosition | None: ...

    @abstractmethod
    def all(self) -> list[WheelPosition]: ...

    @abstractmethod
    def by_ticker(self, ticker: str) -> list[WheelPosition]: ...


class InMemoryWheelStore(WheelStore):
    """Process-local only — lost on restart. For tests and for any
    caller that doesn't need restart-survival; use `SqliteWheelStore`
    wherever a real Wheel's state must survive a crash."""

    def __init__(self) -> None:
        self._wheels: dict[str, WheelPosition] = {}

    def save(self, wheel: WheelPosition) -> None:
        self._wheels[wheel.wheel_id] = wheel

    def get(self, wheel_id: str) -> WheelPosition | None:
        return self._wheels.get(wheel_id)

    def all(self) -> list[WheelPosition]:
        return list(self._wheels.values())

    def by_ticker(self, ticker: str) -> list[WheelPosition]:
        return [w for w in self._wheels.values() if w.ticker == ticker]


class SqliteWheelStore(WheelStore):
    """A durable `WheelStore` backed by a single sqlite file. Each
    operation opens and closes its own connection — this class holds no
    in-process state a crash could lose, the exact guarantee
    `SqliteIdempotencyStore`/`SqliteValidationStore` already provide for
    their own domains (Step 17B/22's established pattern in this
    codebase)."""

    def __init__(self, db_path: Path | str) -> None:
        self._path = str(db_path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS wheels ("
                "wheel_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, state TEXT NOT NULL, "
                "schema_version TEXT NOT NULL, record_json TEXT NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def save(self, wheel: WheelPosition) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO wheels (wheel_id, ticker, state, schema_version, record_json) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(wheel_id) DO UPDATE SET ticker=excluded.ticker, state=excluded.state, "
                "schema_version=excluded.schema_version, record_json=excluded.record_json",
                (wheel.wheel_id, wheel.ticker, wheel.state.value, WHEEL_DATABASE_SCHEMA_VERSION, wheel.model_dump_json()),
            )

    def get(self, wheel_id: str) -> WheelPosition | None:
        with self._connect() as conn:
            row = conn.execute("SELECT record_json FROM wheels WHERE wheel_id = ?", (wheel_id,)).fetchone()
        return WheelPosition.model_validate_json(row[0]) if row is not None else None

    def all(self) -> list[WheelPosition]:
        with self._connect() as conn:
            rows = conn.execute("SELECT record_json FROM wheels ORDER BY wheel_id").fetchall()
        return [WheelPosition.model_validate_json(r[0]) for r in rows]

    def by_ticker(self, ticker: str) -> list[WheelPosition]:
        with self._connect() as conn:
            rows = conn.execute("SELECT record_json FROM wheels WHERE ticker = ? ORDER BY wheel_id", (ticker,)).fetchall()
        return [WheelPosition.model_validate_json(r[0]) for r in rows]

    def schema_version_on_disk(self) -> str | None:
        """Reads back whatever `schema_version` the most recently saved
        row actually carries — used by a startup check to detect a
        database written by an older, incompatible `SqliteWheelStore`
        before any `WheelPosition` is deserialized from it (a raw string
        compare, so it can never itself throw the way a failed
        `model_validate_json` against a genuinely incompatible shape
        would)."""
        with self._connect() as conn:
            row = conn.execute("SELECT schema_version FROM wheels LIMIT 1").fetchone()
        return row[0] if row is not None else None
