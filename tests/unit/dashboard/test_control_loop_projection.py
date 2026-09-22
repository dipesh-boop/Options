"""Step 22.4A Part 5/6/11: the control-loop dashboard projection/loading
mechanism and the three honestly-distinguished states -- NO DASHBOARD
SESSION, NO CONTROL LOOP CYCLE YET, CONTROL LOOP DATA AVAILABLE."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import src.dashboard.app as dashboard_app
from src.dashboard.control_loop_projection import load_latest_control_loop_state
from src.dashboard.models import DashboardState
from src.portfolio.alerts import AlertSeverity, ControlLoopAlert, ControlLoopAlertType
from src.portfolio.cycle_record import ControlCycleRecord
from src.portfolio.exposure import PortfolioExposureSnapshot
from src.portfolio.persistence import InMemoryControlLoopStore
from tests.unit.risk.conftest import build_approved_pcs_scenario

NOW = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)


def _cycle_record(cycle_id="cyc-1") -> ControlCycleRecord:
    return ControlCycleRecord(
        cycle_id=cycle_id, started_at=NOW, completed_at=NOW, market_open=True, provider="tradier",
        provider_health_status="healthy", symbols_requested=("SPY",), symbols_successful=("SPY",),
        positions_evaluated=1, recommendations_created=1,
    )


def _exposure() -> PortfolioExposureSnapshot:
    return PortfolioExposureSnapshot(
        as_of=NOW, underlying_exposure_pct={"SPY": 0.1}, sector_exposure_pct={}, strategy_exposure_pct={},
        expiration_concentration_pct={}, directional_exposure="neutral", portfolio_delta=None,
        volatility_exposure="neutral", portfolio_vega=None, short_option_capital_pct=0.0,
        assignment_risk_position_ids=(), wheel_cash_commitment_pct=0.0, owned_share_exposure_pct=0.0,
        owned_share_prices_missing=(), covered_call_encumbered_shares={}, correlated_pairs=(),
    )


def _alert(alert_id="a1") -> ControlLoopAlert:
    return ControlLoopAlert(
        alert_id=alert_id, scope="portfolio", alert_type=ControlLoopAlertType.RISK_HALT,
        severity=AlertSeverity.CRITICAL, reason="test", created_at=NOW,
    )


class TestLoadLatestControlLoopStateProjection:
    def test_empty_store_leaves_state_honestly_empty(self):
        scenario = build_approved_pcs_scenario()
        state = DashboardState(portfolio=scenario.portfolio, limits=scenario.limits)
        store = InMemoryControlLoopStore()
        load_latest_control_loop_state(state, control_loop_store=store)
        assert state.latest_cycle_record is None
        assert state.latest_exposure is None
        assert state.control_loop_alerts == {}

    def test_persisted_cycle_and_exposure_are_reloaded(self):
        scenario = build_approved_pcs_scenario()
        state = DashboardState(portfolio=scenario.portfolio, limits=scenario.limits)
        store = InMemoryControlLoopStore()
        store.save_cycle_record(_cycle_record())
        store.save_exposure_snapshot("cyc-1", _exposure())
        store.save_alert(_alert())

        load_latest_control_loop_state(state, control_loop_store=store)

        assert state.latest_cycle_record is not None
        assert state.latest_cycle_record.cycle_id == "cyc-1"
        assert state.latest_exposure is not None
        assert state.latest_exposure.underlying_exposure_pct == {"SPY": 0.1}
        assert "a1" in state.control_loop_alerts

    def test_only_most_recent_cycle_is_reloaded(self):
        scenario = build_approved_pcs_scenario()
        state = DashboardState(portfolio=scenario.portfolio, limits=scenario.limits)
        store = InMemoryControlLoopStore()
        store.save_cycle_record(_cycle_record("cyc-1"))
        store.save_cycle_record(_cycle_record("cyc-2"))
        load_latest_control_loop_state(state, control_loop_store=store)
        assert state.latest_cycle_record.cycle_id == "cyc-2"

    def test_cycle_with_no_saved_exposure_leaves_exposure_honestly_none(self):
        scenario = build_approved_pcs_scenario()
        state = DashboardState(portfolio=scenario.portfolio, limits=scenario.limits)
        store = InMemoryControlLoopStore()
        store.save_cycle_record(_cycle_record())
        load_latest_control_loop_state(state, control_loop_store=store)
        assert state.latest_cycle_record is not None
        assert state.latest_exposure is None

    def test_never_fabricates_a_cycle_when_none_exists(self):
        scenario = build_approved_pcs_scenario()
        state = DashboardState(portfolio=scenario.portfolio, limits=scenario.limits)
        store = InMemoryControlLoopStore()
        result = load_latest_control_loop_state(state, control_loop_store=store)
        assert result is state
        assert result.latest_cycle_record is None


@pytest.fixture
def client():
    c = TestClient(dashboard_app.app)
    yield c
    dashboard_app.set_state(None)


class TestThreeHonestDashboardStates:
    """Part 6/11: NO DASHBOARD SESSION (503) vs NO CONTROL LOOP CYCLE YET
    (404) vs CONTROL LOOP DATA AVAILABLE (200) must never collapse into
    each other or a misleading placeholder."""

    def test_state_1_no_dashboard_session_is_503(self, client):
        dashboard_app.set_state(None)
        r = client.get("/api/control-loop/status")
        assert r.status_code == 503
        assert "no loaded portfolio" in r.json()["detail"]

    def test_state_2_session_without_a_cycle_is_404(self, client):
        scenario = build_approved_pcs_scenario()
        state = DashboardState(portfolio=scenario.portfolio, limits=scenario.limits)
        store = InMemoryControlLoopStore()
        dashboard_app.set_state(state, control_loop_store=store)
        r = client.get("/api/control-loop/status")
        assert r.status_code == 404
        assert "no control-loop cycle has run yet" in r.json()["detail"]
        # exposure/alerts must be independently honest too, not collapsed
        r_exp = client.get("/api/control-loop/exposure")
        assert r_exp.status_code == 404
        r_alerts = client.get("/api/control-loop/alerts")
        assert r_alerts.status_code == 200
        assert r_alerts.json() == []

    def test_state_3_session_with_a_persisted_cycle_is_200_with_real_data(self, client):
        scenario = build_approved_pcs_scenario()
        state = DashboardState(portfolio=scenario.portfolio, limits=scenario.limits)
        store = InMemoryControlLoopStore()
        store.save_cycle_record(_cycle_record())
        store.save_exposure_snapshot("cyc-1", _exposure())
        store.save_alert(_alert())
        dashboard_app.set_state(state, control_loop_store=store)

        r = client.get("/api/control-loop/status")
        assert r.status_code == 200
        assert r.json()["cycle_id"] == "cyc-1"

        r_exp = client.get("/api/control-loop/exposure")
        assert r_exp.status_code == 200

        r_alerts = client.get("/api/control-loop/alerts")
        assert r_alerts.status_code == 200
        assert len(r_alerts.json()) == 1

    def test_set_state_without_control_loop_store_stays_state_2(self, client):
        # Omitting `control_loop_store` (the default) must never silently
        # fabricate control-loop visibility -- a caller must opt in.
        scenario = build_approved_pcs_scenario()
        state = DashboardState(portfolio=scenario.portfolio, limits=scenario.limits)
        dashboard_app.set_state(state)
        r = client.get("/api/control-loop/status")
        assert r.status_code == 404


class TestControlLoopRoutesReadOnly:
    """Part 11.D / Part 12: the dashboard cannot execute a trade, start
    validation, or alter a Risk/Lifecycle decision through the
    control-loop routes -- there is no POST/PUT/DELETE/PATCH route for
    any of them, and a GET never mutates anything."""

    _PATHS = ("/api/control-loop/status", "/api/control-loop/exposure", "/api/control-loop/alerts")

    def test_no_mutating_method_registered_for_any_control_loop_path(self):
        for route in dashboard_app.app.routes:
            path = getattr(route, "path", None)
            if path in self._PATHS:
                methods = getattr(route, "methods", set())
                assert methods <= {"GET", "HEAD"}, f"{path} exposes non-read-only method(s): {methods}"

    def test_repeated_get_never_mutates_the_persisted_cycle(self, client):
        scenario = build_approved_pcs_scenario()
        state = DashboardState(portfolio=scenario.portfolio, limits=scenario.limits)
        store = InMemoryControlLoopStore()
        store.save_cycle_record(_cycle_record())
        store.save_exposure_snapshot("cyc-1", _exposure())
        dashboard_app.set_state(state, control_loop_store=store)

        first = client.get("/api/control-loop/status").json()
        client.get("/api/control-loop/status")
        client.get("/api/control-loop/exposure")
        third = client.get("/api/control-loop/status").json()
        assert first == third
        assert store.get_cycle_record("cyc-1") is not None  # never deleted/altered by reads
