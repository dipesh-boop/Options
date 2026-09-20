"""Durable state for one validation run: closed trades, daily equity
snapshots, rejected-trade outcomes, and rule-violation records.

`TradeRecord` (`src.backtest.simulator`) is reused unmodified as the
closed-trade shape — the same reasoning `src.workflows.performance_review`
already gives: a closed options trade's P&L fields describe a backtest
trade, a live paper trade, and a validation-run trade identically, so a
fourth parallel "validation trade" type would only be a fourth place for
the same shape to drift.

Snapshots are append-only and immutable (`DailySnapshot` is frozen) —
"immutable daily snapshots" is enforced by construction, not by
convention: `ValidationStore.record_snapshot` has no corresponding
update/delete method, mirroring `src.orchestration.pipeline.Database`'s
own append-only contract.
"""
from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

from src.backtest.simulator import TradeRecord
from src.llm.schemas import StrategyType
from src.workflows.rejected_trade_review import HypotheticalOutcome


@dataclass(frozen=True)
class DailySnapshot:
    snapshot_date: date
    nav: float
    cash: float
    capital_deployed_pct: float
    open_position_count: int
    drawdown_pct: float
    per_strategy_nav: dict[str, float]
    recorded_at: datetime


@dataclass(frozen=True)
class RuleViolationRecord:
    """A detected deviation from the platform's own hard rules — e.g. a
    trade recorded in this session whose Risk Engine decision was not
    APPROVE/RESIZE, or a proposal that bypassed the Risk Engine
    entirely. This is an *auditing* record: something this module
    checks for after the fact (`src.validation.consistency
    .check_rule_compliance`), never something the Risk Engine itself
    needs in order to do its job — its own veto is unconditional
    regardless of whether anything reads this log."""

    violation_id: str
    occurred_at: datetime
    rule_name: str
    description: str
    related_id: str | None = None


def _trade_to_dict(t: TradeRecord) -> dict:
    d = asdict(t)
    d["strategy"] = t.strategy.value
    d["opened_at"] = t.opened_at.isoformat()
    d["closed_at"] = t.closed_at.isoformat()
    return d


def _trade_from_dict(d: dict) -> TradeRecord:
    d = dict(d)
    d["strategy"] = StrategyType(d["strategy"])
    d["opened_at"] = date.fromisoformat(d["opened_at"])
    d["closed_at"] = date.fromisoformat(d["closed_at"])
    return TradeRecord(**d)


def _snapshot_to_dict(s: DailySnapshot) -> dict:
    d = asdict(s)
    d["snapshot_date"] = s.snapshot_date.isoformat()
    d["recorded_at"] = s.recorded_at.isoformat()
    return d


def _snapshot_from_dict(d: dict) -> DailySnapshot:
    d = dict(d)
    d["snapshot_date"] = date.fromisoformat(d["snapshot_date"])
    d["recorded_at"] = datetime.fromisoformat(d["recorded_at"])
    return DailySnapshot(**d)


def _violation_to_dict(v: RuleViolationRecord) -> dict:
    d = asdict(v)
    d["occurred_at"] = v.occurred_at.isoformat()
    return d


def _violation_from_dict(d: dict) -> RuleViolationRecord:
    d = dict(d)
    d["occurred_at"] = datetime.fromisoformat(d["occurred_at"])
    return RuleViolationRecord(**d)


def _outcome_to_dict(o: HypotheticalOutcome) -> dict:
    return asdict(o)


def _outcome_from_dict(d: dict) -> HypotheticalOutcome:
    return HypotheticalOutcome(**d)


class ValidationStore(ABC):
    """Append-only interface for one validation run's state. Mirrors
    `src.orchestration.pipeline.Database`: `InMemoryValidationStore` is
    process-local (fine for tests), `SqliteValidationStore` is the
    durable implementation a real 90-day run needs — a crash on day 47
    must not lose the first 46 days of trades."""

    @abstractmethod
    def record_trade(self, trade: TradeRecord) -> None: ...

    @abstractmethod
    def record_snapshot(self, snapshot: DailySnapshot) -> None: ...

    @abstractmethod
    def record_rejected_outcome(self, outcome: HypotheticalOutcome) -> None: ...

    @abstractmethod
    def record_violation(self, violation: RuleViolationRecord) -> None: ...

    @abstractmethod
    def trades(self) -> list[TradeRecord]: ...

    @abstractmethod
    def snapshots(self) -> list[DailySnapshot]: ...

    @abstractmethod
    def rejected_outcomes(self) -> list[HypotheticalOutcome]: ...

    @abstractmethod
    def violations(self) -> list[RuleViolationRecord]: ...


class InMemoryValidationStore(ValidationStore):
    def __init__(self) -> None:
        self._trades: list[TradeRecord] = []
        self._snapshots: list[DailySnapshot] = []
        self._rejected: list[HypotheticalOutcome] = []
        self._violations: list[RuleViolationRecord] = []

    def record_trade(self, trade: TradeRecord) -> None:
        self._trades.append(trade)

    def record_snapshot(self, snapshot: DailySnapshot) -> None:
        self._snapshots.append(snapshot)

    def record_rejected_outcome(self, outcome: HypotheticalOutcome) -> None:
        self._rejected.append(outcome)

    def record_violation(self, violation: RuleViolationRecord) -> None:
        self._violations.append(violation)

    def trades(self) -> list[TradeRecord]:
        return list(self._trades)

    def snapshots(self) -> list[DailySnapshot]:
        return list(self._snapshots)

    def rejected_outcomes(self) -> list[HypotheticalOutcome]:
        return list(self._rejected)

    def violations(self) -> list[RuleViolationRecord]:
        return list(self._violations)


_TABLES = ("trades", "snapshots", "rejected_outcomes", "violations")


class SqliteValidationStore(ValidationStore):
    """A durable `ValidationStore` backed by a single sqlite file, the
    same "each operation opens and closes its own connection" pattern
    Step 17B's `SqliteDatabase`/`SqliteIdempotencyStore` established —
    the store itself holds no in-process state a crash could lose."""

    def __init__(self, db_path: Path | str) -> None:
        self._path = str(db_path)
        with self._connect() as conn:
            for table in _TABLES:
                conn.execute(f"CREATE TABLE IF NOT EXISTS {table} (row_id INTEGER PRIMARY KEY AUTOINCREMENT, record_json TEXT NOT NULL)")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def _insert(self, table: str, record_json: str) -> None:
        with self._connect() as conn:
            conn.execute(f"INSERT INTO {table} (record_json) VALUES (?)", (record_json,))

    def _all(self, table: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(f"SELECT record_json FROM {table} ORDER BY row_id").fetchall()
        return [json.loads(r[0]) for r in rows]

    def record_trade(self, trade: TradeRecord) -> None:
        self._insert("trades", json.dumps(_trade_to_dict(trade)))

    def record_snapshot(self, snapshot: DailySnapshot) -> None:
        self._insert("snapshots", json.dumps(_snapshot_to_dict(snapshot)))

    def record_rejected_outcome(self, outcome: HypotheticalOutcome) -> None:
        self._insert("rejected_outcomes", json.dumps(_outcome_to_dict(outcome)))

    def record_violation(self, violation: RuleViolationRecord) -> None:
        self._insert("violations", json.dumps(_violation_to_dict(violation)))

    def trades(self) -> list[TradeRecord]:
        return [_trade_from_dict(d) for d in self._all("trades")]

    def snapshots(self) -> list[DailySnapshot]:
        return [_snapshot_from_dict(d) for d in self._all("snapshots")]

    def rejected_outcomes(self) -> list[HypotheticalOutcome]:
        return [_outcome_from_dict(d) for d in self._all("rejected_outcomes")]

    def violations(self) -> list[RuleViolationRecord]:
        return [_violation_from_dict(d) for d in self._all("violations")]


# ------------------------------------------------------------- helpers


def equity_curve_from_snapshots(snapshots: list[DailySnapshot]) -> list[tuple[date, float]]:
    ordered = sorted(snapshots, key=lambda s: s.snapshot_date)
    return [(s.snapshot_date, s.nav) for s in ordered]


def per_strategy_trades(trades: list[TradeRecord]) -> dict[str, list[TradeRecord]]:
    buckets: dict[str, list[TradeRecord]] = {}
    for t in trades:
        buckets.setdefault(t.strategy.value, []).append(t)
    return buckets


def completed_trade_count(store: ValidationStore) -> int:
    return len(store.trades())
