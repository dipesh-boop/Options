"""Tests for `ValidationStore` (InMemory and Sqlite) — durability,
round-tripping every record type, and the helper aggregations."""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src.llm.schemas import StrategyType
from src.validation.session import (
    DailySnapshot,
    InMemoryValidationStore,
    RuleViolationRecord,
    SqliteValidationStore,
    completed_trade_count,
    equity_curve_from_snapshots,
    per_strategy_trades,
)
from src.workflows.rejected_trade_review import HypotheticalOutcome

from .conftest import _trade


def _snapshot(**overrides) -> DailySnapshot:
    defaults = dict(
        snapshot_date=date(2026, 1, 5),
        nav=101_000.0,
        cash=80_000.0,
        capital_deployed_pct=0.2079,
        open_position_count=2,
        drawdown_pct=0.0,
        per_strategy_nav={"cash_secured_put": 50_000.0},
        recorded_at=datetime(2026, 1, 5, 21, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return DailySnapshot(**defaults)


def _violation(**overrides) -> RuleViolationRecord:
    defaults = dict(
        violation_id="v-1", occurred_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
        rule_name="test_rule", description="something went wrong", related_id="t-1",
    )
    defaults.update(overrides)
    return RuleViolationRecord(**defaults)


def _outcome(**overrides) -> HypotheticalOutcome:
    defaults = dict(
        proposal_id="p-1", ticker="XYZ", strategy="cash_secured_put",
        rejected_stage="risk_engine", hypothetical_pnl=150.0, exit_reason="closed",
    )
    defaults.update(overrides)
    return HypotheticalOutcome(**defaults)


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path: Path):
    if request.param == "memory":
        return InMemoryValidationStore()
    return SqliteValidationStore(tmp_path / "validation.db")


class TestValidationStoreRoundtrips:
    def test_empty_store_returns_empty_lists(self, store):
        assert store.trades() == []
        assert store.snapshots() == []
        assert store.rejected_outcomes() == []
        assert store.violations() == []

    def test_record_and_read_trade(self, store):
        t = _trade()
        store.record_trade(t)
        assert store.trades() == [t]

    def test_record_and_read_multiple_trades_preserves_order(self, store):
        t1, t2 = _trade(position_id="a"), _trade(position_id="b")
        store.record_trade(t1)
        store.record_trade(t2)
        assert [t.position_id for t in store.trades()] == ["a", "b"]

    def test_record_and_read_snapshot(self, store):
        s = _snapshot()
        store.record_snapshot(s)
        assert store.snapshots() == [s]

    def test_record_and_read_rejected_outcome(self, store):
        o = _outcome()
        store.record_rejected_outcome(o)
        assert store.rejected_outcomes() == [o]

    def test_record_and_read_violation(self, store):
        v = _violation()
        store.record_violation(v)
        assert store.violations() == [v]


class TestSqliteDurability:
    def test_survives_a_fresh_instance_pointed_at_the_same_file(self, tmp_path: Path):
        """The core durability guarantee: a crash-then-restart must not
        lose trades recorded before the crash."""
        db_path = tmp_path / "validation.db"
        store1 = SqliteValidationStore(db_path)
        store1.record_trade(_trade(position_id="durable-1"))
        store1.record_snapshot(_snapshot())
        store1.record_violation(_violation())
        store1.record_rejected_outcome(_outcome())

        store2 = SqliteValidationStore(db_path)  # a fresh instance, same file
        assert len(store2.trades()) == 1
        assert store2.trades()[0].position_id == "durable-1"
        assert len(store2.snapshots()) == 1
        assert len(store2.violations()) == 1
        assert len(store2.rejected_outcomes()) == 1


class TestHelpers:
    def test_equity_curve_from_snapshots_sorts_by_date(self):
        s1 = _snapshot(snapshot_date=date(2026, 1, 10), nav=105_000.0)
        s2 = _snapshot(snapshot_date=date(2026, 1, 5), nav=101_000.0)
        curve = equity_curve_from_snapshots([s1, s2])
        assert curve == [(date(2026, 1, 5), 101_000.0), (date(2026, 1, 10), 105_000.0)]

    def test_per_strategy_trades_buckets_correctly(self):
        t1 = _trade(position_id="a", strategy=StrategyType.CASH_SECURED_PUT)
        t2 = _trade(position_id="b", strategy=StrategyType.COVERED_CALL)
        t3 = _trade(position_id="c", strategy=StrategyType.CASH_SECURED_PUT)
        buckets = per_strategy_trades([t1, t2, t3])
        assert {t.position_id for t in buckets["cash_secured_put"]} == {"a", "c"}
        assert {t.position_id for t in buckets["covered_call"]} == {"b"}

    def test_completed_trade_count(self):
        store = InMemoryValidationStore()
        store.record_trade(_trade(position_id="a"))
        store.record_trade(_trade(position_id="b"))
        assert completed_trade_count(store) == 2
