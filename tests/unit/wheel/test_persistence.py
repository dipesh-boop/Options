"""Step 22.2 Part 21/23: Wheel persistence tests, including the
explicit restart-recovery proof Part 23 requires: create a Wheel,
persist it, restart (drop the in-process store, build a fresh one
pointed at the same file), reload it, and prove state/accounting are
reconstructed exactly."""
from __future__ import annotations

from datetime import date, datetime, timezone

from src.wheel import lifecycle
from src.wheel.persistence import InMemoryWheelStore, SqliteWheelStore, WHEEL_DATABASE_SCHEMA_VERSION

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _wheel_with_history():
    w = lifecycle.open_wheel_candidate(wheel_id="w-persist", ticker="SPY", now=NOW)
    w = lifecycle.open_csp(w, strike=50.0, expiration=date(2026, 2, 1), contracts=1, premium_per_share=1.2, commission=0.65, proposal_id="p1", position_id="pos1", now=NOW)
    w = lifecycle.csp_assigned(w, now=NOW)
    w = lifecycle.mark_cc_eligible(w, now=NOW)
    w = lifecycle.open_cc(w, strike=53.0, expiration=date(2026, 3, 1), contracts=1, premium_per_share=0.8, commission=0.65, proposal_id="p2", position_id="pos2", now=NOW, below_acquisition_basis=False, below_economic_basis=False, max_loss_if_called_away=None)
    return w


class TestInMemoryWheelStore:
    def test_save_and_get_round_trip(self):
        store = InMemoryWheelStore()
        w = _wheel_with_history()
        store.save(w)
        assert store.get(w.wheel_id) == w

    def test_get_missing_returns_none(self):
        assert InMemoryWheelStore().get("nope") is None

    def test_by_ticker_filters(self):
        store = InMemoryWheelStore()
        w1 = lifecycle.open_wheel_candidate(wheel_id="a", ticker="SPY", now=NOW)
        w2 = lifecycle.open_wheel_candidate(wheel_id="b", ticker="QQQ", now=NOW)
        store.save(w1)
        store.save(w2)
        assert [w.wheel_id for w in store.by_ticker("SPY")] == ["a"]


class TestSqliteWheelStoreRestartRecovery:
    def test_full_restart_recovery_reconstructs_exact_state_and_accounting(self, tmp_path):
        db_path = tmp_path / "wheels.db"
        w = _wheel_with_history()

        store1 = SqliteWheelStore(db_path)
        store1.save(w)
        del store1  # simulate the process ending -- no in-process state to lose

        store2 = SqliteWheelStore(db_path)  # simulate a fresh process starting up
        reloaded = store2.get(w.wheel_id)

        assert reloaded == w  # exact structural equality: every cycle, event, and accounting figure
        assert reloaded.state == w.state
        assert reloaded.accounting.acquisition_basis_per_share == w.accounting.acquisition_basis_per_share
        assert reloaded.accounting.economic_basis_per_share == w.accounting.economic_basis_per_share
        assert len(reloaded.csp_cycles) == 1
        assert len(reloaded.cc_cycles) == 1
        assert len(reloaded.events) == len(w.events)
        assert len(reloaded.state_history) == len(w.state_history)

    def test_all_and_by_ticker_after_restart(self, tmp_path):
        db_path = tmp_path / "wheels.db"
        w1 = lifecycle.open_wheel_candidate(wheel_id="a", ticker="SPY", now=NOW)
        w2 = lifecycle.open_wheel_candidate(wheel_id="b", ticker="QQQ", now=NOW)
        store1 = SqliteWheelStore(db_path)
        store1.save(w1)
        store1.save(w2)
        del store1

        store2 = SqliteWheelStore(db_path)
        assert {w.wheel_id for w in store2.all()} == {"a", "b"}
        assert [w.wheel_id for w in store2.by_ticker("QQQ")] == ["b"]

    def test_save_is_idempotent_and_replaces_on_conflict(self, tmp_path):
        db_path = tmp_path / "wheels.db"
        store = SqliteWheelStore(db_path)
        w = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="SPY", now=NOW)
        store.save(w)
        w2 = lifecycle.open_csp(w, strike=50.0, expiration=date(2026, 2, 1), contracts=1, premium_per_share=1.0, commission=0.65, proposal_id="p1", position_id=None, now=NOW)
        store.save(w2)
        reloaded = store.get("w1")
        assert reloaded.state.value == "csp_open"
        assert len(store.all()) == 1  # updated in place, not duplicated

    def test_schema_version_recorded_and_readable(self, tmp_path):
        db_path = tmp_path / "wheels.db"
        store = SqliteWheelStore(db_path)
        store.save(lifecycle.open_wheel_candidate(wheel_id="w1", ticker="SPY", now=NOW))
        assert store.schema_version_on_disk() == WHEEL_DATABASE_SCHEMA_VERSION

    def test_empty_database_schema_version_is_none(self, tmp_path):
        store = SqliteWheelStore(tmp_path / "wheels.db")
        assert store.schema_version_on_disk() is None
