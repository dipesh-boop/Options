"""Durable state for one validation run: closed trades, daily equity
snapshots, rejected-trade outcomes, rule-violation records, and (Step
22) the cohort's own identity/status and the full per-opportunity
decision trail (`src.validation.records.CohortRecord`/
`OpportunityRecord`), plus reconciliation-failure records for a fill
whose portfolio update did not complete.

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

**Step 22 idempotency**: every `record_*` method on `SqliteValidationStore`
now writes through a per-table UNIQUE natural key (a trade's
`position_id`, a snapshot's `(cohort_id, snapshot_date)`, a rejected
outcome's `proposal_id`, a violation's `violation_id`, a cohort's
`cohort_id`, an opportunity's `opportunity_id`) via `INSERT OR IGNORE`
— a caller that retries the exact same record after a crash (never
knowing whether the first attempt's write actually committed before
the process died) gets exactly one row, never a silent duplicate. Each
`_insert` call is also its own sqlite transaction (commit on success,
rollback on any exception) via the same `with self._connect() as conn`
pattern `SqliteDatabase`/`SqliteIdempotencyStore` already established
in Step 17B — a crash mid-write can never leave a half-written row.
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
from src.validation.records import CohortRecord, OpportunityRecord, from_jsonable, to_jsonable
from src.workflows.rejected_trade_review import HypotheticalOutcome

DEFAULT_COHORT_ID = "default"

# Step 22: bumped only when the on-disk table shapes below change in a
# way that would make an older database file structurally incompatible
# with the current `SqliteValidationStore` (a new required column, a
# renamed table, a changed key). Read by `src.validation.freeze`'s
# VALIDATION_MANIFEST.json builder/verifier so a freeze can detect
# "this database was created by an incompatible schema version" as a
# distinct failure from ordinary config drift.
DATABASE_SCHEMA_VERSION = "1.0.0"


@dataclass(frozen=True)
class ReconciliationFailureRecord:
    """Durable evidence that a fill was recorded at the broker but the
    portfolio-accounting update for it did not complete (the same
    condition `src.orchestration.pipeline.run_order_pipeline`'s own
    SY-003 fix already detects in-process, via
    `PipelineOutcome.rejected_stage == "portfolio_update"` — see
    `tests/acceptance/test_accounting_reconciliation.py`). This is the
    durable counterpart: a 90-day unattended run must not lose that
    signal the moment the in-memory `PipelineOutcome` goes out of scope
    -- it must survive to be found and resolved, never silently
    dropped or, worse, mistaken for a clean fill."""

    failure_id: str
    cohort_id: str
    proposal_id: str
    detected_at: datetime
    detail: str
    resolved: bool = False
    resolved_at: datetime | None = None
    resolution_note: str | None = None


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
    must not lose the first 46 days of trades.

    Every `record_*` method is idempotent: calling it twice with a
    record carrying the same natural key (a trade's `position_id`, a
    snapshot's date, etc.) records it once, never twice — safe for a
    caller to retry after a crash without knowing whether the first
    attempt already committed."""

    @abstractmethod
    def record_trade(self, trade: TradeRecord, *, cohort_id: str = DEFAULT_COHORT_ID) -> None: ...

    @abstractmethod
    def record_snapshot(self, snapshot: DailySnapshot, *, cohort_id: str = DEFAULT_COHORT_ID) -> None: ...

    @abstractmethod
    def record_rejected_outcome(self, outcome: HypotheticalOutcome, *, cohort_id: str = DEFAULT_COHORT_ID) -> None: ...

    @abstractmethod
    def record_violation(self, violation: RuleViolationRecord, *, cohort_id: str = DEFAULT_COHORT_ID) -> None: ...

    @abstractmethod
    def record_cohort(self, cohort: CohortRecord) -> None: ...

    @abstractmethod
    def record_opportunity(self, opportunity: OpportunityRecord) -> None: ...

    @abstractmethod
    def record_reconciliation_failure(self, failure: ReconciliationFailureRecord) -> None: ...

    @abstractmethod
    def trades(self, *, cohort_id: str | None = None) -> list[TradeRecord]: ...

    @abstractmethod
    def snapshots(self, *, cohort_id: str | None = None) -> list[DailySnapshot]: ...

    @abstractmethod
    def rejected_outcomes(self, *, cohort_id: str | None = None) -> list[HypotheticalOutcome]: ...

    @abstractmethod
    def violations(self, *, cohort_id: str | None = None) -> list[RuleViolationRecord]: ...

    @abstractmethod
    def cohorts(self) -> list[CohortRecord]: ...

    @abstractmethod
    def get_cohort(self, cohort_id: str) -> CohortRecord | None: ...

    @abstractmethod
    def opportunities(self, *, cohort_id: str | None = None) -> list[OpportunityRecord]: ...

    @abstractmethod
    def reconciliation_failures(self, *, unresolved_only: bool = False) -> list[ReconciliationFailureRecord]: ...


class InMemoryValidationStore(ValidationStore):
    def __init__(self) -> None:
        self._trades: dict[str, TradeRecord] = {}
        self._snapshots: dict[tuple[str, str], DailySnapshot] = {}
        self._rejected: dict[str, HypotheticalOutcome] = {}
        self._violations: dict[str, RuleViolationRecord] = {}
        self._cohorts: dict[str, CohortRecord] = {}
        self._opportunities: dict[str, OpportunityRecord] = {}
        self._reconciliation_failures: dict[str, ReconciliationFailureRecord] = {}

    def record_trade(self, trade: TradeRecord, *, cohort_id: str = DEFAULT_COHORT_ID) -> None:
        self._trades.setdefault(trade.position_id, trade)

    def record_snapshot(self, snapshot: DailySnapshot, *, cohort_id: str = DEFAULT_COHORT_ID) -> None:
        self._snapshots.setdefault((cohort_id, snapshot.snapshot_date.isoformat()), snapshot)

    def record_rejected_outcome(self, outcome: HypotheticalOutcome, *, cohort_id: str = DEFAULT_COHORT_ID) -> None:
        self._rejected.setdefault(outcome.proposal_id, outcome)

    def record_violation(self, violation: RuleViolationRecord, *, cohort_id: str = DEFAULT_COHORT_ID) -> None:
        self._violations.setdefault(violation.violation_id, violation)

    def record_cohort(self, cohort: CohortRecord) -> None:
        self._cohorts[cohort.cohort_id] = cohort  # cohorts DO update (status changes) -- last write wins, unlike append-only records

    def record_opportunity(self, opportunity: OpportunityRecord) -> None:
        self._opportunities.setdefault(opportunity.opportunity_id, opportunity)

    def record_reconciliation_failure(self, failure: ReconciliationFailureRecord) -> None:
        self._reconciliation_failures[failure.failure_id] = failure  # can transition resolved False->True

    def trades(self, *, cohort_id: str | None = None) -> list[TradeRecord]:
        return list(self._trades.values())

    def snapshots(self, *, cohort_id: str | None = None) -> list[DailySnapshot]:
        if cohort_id is None:
            return [s for (_, _), s in self._snapshots.items()]
        return [s for (cid, _), s in self._snapshots.items() if cid == cohort_id]

    def rejected_outcomes(self, *, cohort_id: str | None = None) -> list[HypotheticalOutcome]:
        return list(self._rejected.values())

    def violations(self, *, cohort_id: str | None = None) -> list[RuleViolationRecord]:
        return list(self._violations.values())

    def cohorts(self) -> list[CohortRecord]:
        return list(self._cohorts.values())

    def get_cohort(self, cohort_id: str) -> CohortRecord | None:
        return self._cohorts.get(cohort_id)

    def opportunities(self, *, cohort_id: str | None = None) -> list[OpportunityRecord]:
        if cohort_id is None:
            return list(self._opportunities.values())
        return [o for o in self._opportunities.values() if o.cohort_id == cohort_id]

    def reconciliation_failures(self, *, unresolved_only: bool = False) -> list[ReconciliationFailureRecord]:
        values = list(self._reconciliation_failures.values())
        return [f for f in values if not f.resolved] if unresolved_only else values


# Each entry: (table_name, natural_key_columns). `record_json` always
# holds the full record; natural-key columns are duplicated out of the
# JSON purely so sqlite can enforce uniqueness on them directly.
_APPEND_ONLY_TABLES = {
    "trades": ("position_id",),
    "snapshots": ("cohort_id", "snapshot_date"),
    "rejected_outcomes": ("proposal_id",),
    "violations": ("violation_id",),
    "opportunities": ("opportunity_id",),
}


class SqliteValidationStore(ValidationStore):
    """A durable `ValidationStore` backed by a single sqlite file, the
    same "each operation opens and closes its own connection" pattern
    Step 17B's `SqliteDatabase`/`SqliteIdempotencyStore` established —
    the store itself holds no in-process state a crash could lose."""

    def __init__(self, db_path: Path | str) -> None:
        self._path = str(db_path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            for table, key_cols in _APPEND_ONLY_TABLES.items():
                key_cols_sql = ", ".join(f"{c} TEXT NOT NULL" for c in key_cols)
                unique_sql = ", ".join(key_cols)
                conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {table} ("
                    f"row_id INTEGER PRIMARY KEY AUTOINCREMENT, {key_cols_sql}, "
                    f"record_json TEXT NOT NULL, UNIQUE({unique_sql}))"
                )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS cohorts ("
                "cohort_id TEXT PRIMARY KEY, record_json TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS reconciliation_failures ("
                "failure_id TEXT PRIMARY KEY, record_json TEXT NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _insert(self, table: str, key_values: tuple[str, ...], record_json: str) -> None:
        key_cols = _APPEND_ONLY_TABLES[table]
        placeholders = ", ".join(["?"] * (len(key_cols) + 1))
        cols = ", ".join([*key_cols, "record_json"])
        with self._connect() as conn:
            conn.execute(f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({placeholders})", (*key_values, record_json))

    def _all(self, table: str, *, where: str | None = None, params: tuple = ()) -> list[dict]:
        query = f"SELECT record_json FROM {table}"
        if where:
            query += f" WHERE {where}"
        query += " ORDER BY row_id" if table in _APPEND_ONLY_TABLES else ""
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [json.loads(r[0]) for r in rows]

    def record_trade(self, trade: TradeRecord, *, cohort_id: str = DEFAULT_COHORT_ID) -> None:
        self._insert("trades", (trade.position_id,), json.dumps(_trade_to_dict(trade)))

    def record_snapshot(self, snapshot: DailySnapshot, *, cohort_id: str = DEFAULT_COHORT_ID) -> None:
        self._insert("snapshots", (cohort_id, snapshot.snapshot_date.isoformat()), json.dumps(_snapshot_to_dict(snapshot)))

    def record_rejected_outcome(self, outcome: HypotheticalOutcome, *, cohort_id: str = DEFAULT_COHORT_ID) -> None:
        self._insert("rejected_outcomes", (outcome.proposal_id,), json.dumps(_outcome_to_dict(outcome)))

    def record_violation(self, violation: RuleViolationRecord, *, cohort_id: str = DEFAULT_COHORT_ID) -> None:
        self._insert("violations", (violation.violation_id,), json.dumps(_violation_to_dict(violation)))

    def record_cohort(self, cohort: CohortRecord) -> None:
        # Cohorts are the one mutable record (status/started_at change
        # over the cohort's life) -- REPLACE, not INSERT OR IGNORE, is
        # correct here; still one atomic, idempotent write (replaying
        # the identical record is a no-op).
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO cohorts (cohort_id, record_json) VALUES (?, ?) "
                "ON CONFLICT(cohort_id) DO UPDATE SET record_json = excluded.record_json",
                (cohort.cohort_id, json.dumps(to_jsonable(cohort, CohortRecord))),
            )

    def record_opportunity(self, opportunity: OpportunityRecord) -> None:
        self._insert("opportunities", (opportunity.opportunity_id,), json.dumps(to_jsonable(opportunity, OpportunityRecord)))

    def record_reconciliation_failure(self, failure: ReconciliationFailureRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO reconciliation_failures (failure_id, record_json) VALUES (?, ?) "
                "ON CONFLICT(failure_id) DO UPDATE SET record_json = excluded.record_json",
                (failure.failure_id, json.dumps(to_jsonable(failure, ReconciliationFailureRecord))),
            )

    def trades(self, *, cohort_id: str | None = None) -> list[TradeRecord]:
        return [_trade_from_dict(d) for d in self._all("trades")]

    def snapshots(self, *, cohort_id: str | None = None) -> list[DailySnapshot]:
        if cohort_id is None:
            return [_snapshot_from_dict(d) for d in self._all("snapshots")]
        return [_snapshot_from_dict(d) for d in self._all("snapshots", where="cohort_id = ?", params=(cohort_id,))]

    def rejected_outcomes(self, *, cohort_id: str | None = None) -> list[HypotheticalOutcome]:
        return [_outcome_from_dict(d) for d in self._all("rejected_outcomes")]

    def violations(self, *, cohort_id: str | None = None) -> list[RuleViolationRecord]:
        return [_violation_from_dict(d) for d in self._all("violations")]

    def cohorts(self) -> list[CohortRecord]:
        return [from_jsonable(d, CohortRecord) for d in self._all("cohorts")]

    def get_cohort(self, cohort_id: str) -> CohortRecord | None:
        rows = self._all("cohorts", where="cohort_id = ?", params=(cohort_id,))
        return from_jsonable(rows[0], CohortRecord) if rows else None

    def opportunities(self, *, cohort_id: str | None = None) -> list[OpportunityRecord]:
        # cohort_id lives inside record_json (not its own indexed
        # column, unlike snapshots) -- filtered in Python after fetch,
        # a fine tradeoff at 90-day-validation scale (at most a few
        # hundred opportunities).
        all_records = [from_jsonable(d, OpportunityRecord) for d in self._all("opportunities")]
        if cohort_id is None:
            return all_records
        return [o for o in all_records if o.cohort_id == cohort_id]

    def reconciliation_failures(self, *, unresolved_only: bool = False) -> list[ReconciliationFailureRecord]:
        records = [from_jsonable(d, ReconciliationFailureRecord) for d in self._all("reconciliation_failures")]
        return [r for r in records if not r.resolved] if unresolved_only else records


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
