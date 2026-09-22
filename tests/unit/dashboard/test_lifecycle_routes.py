"""Part 22: read-only Active Positions/Lifecycle dashboard routes --
no execution control anywhere, exact field visibility, 404 for an
unknown trade_id."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import src.dashboard.app as dashboard_app
from src.dashboard.models import DashboardState
from src.lifecycle.excursion import initial_excursion, update_excursion
from src.lifecycle.persistence import LifecyclePositionRecord
from src.lifecycle.precedence import ResolvedAction
from src.lifecycle.snapshot import LifecycleDecisionSnapshot
from src.lifecycle.state import PositionLifecycleState as S
from src.strategies.base import StrategyKind
from tests.unit.risk.conftest import NOW, build_approved_pcs_scenario


@pytest.fixture
def client():
    scenario = build_approved_pcs_scenario()
    state = DashboardState(portfolio=scenario.portfolio, limits=scenario.limits)
    dashboard_app.set_state(state)
    dashboard_app.app.dependency_overrides[dashboard_app.get_now] = lambda: NOW
    c = TestClient(dashboard_app.app)
    yield c, state
    dashboard_app.set_state(None)
    dashboard_app.app.dependency_overrides.clear()


def _seed_position(state: DashboardState, *, trade_id="T1", current_state=S.PROFIT_TARGET_REACHED):
    exc = initial_excursion(10.0, NOW)
    exc = update_excursion(exc, 60.0, NOW)
    rec = LifecyclePositionRecord(
        trade_id=trade_id, ticker="XYZ", strategy_kind=StrategyKind.PUT_CREDIT_SPREAD,
        management_policy_name="PUT_CREDIT_SPREAD_STANDARD", current_state=current_state, excursion=exc,
        created_at=NOW, updated_at=NOW,
    )
    state.lifecycle_positions[trade_id] = rec
    snap = LifecycleDecisionSnapshot(
        trade_id=trade_id, timestamp=NOW, strategy=StrategyKind.PUT_CREDIT_SPREAD, management_policy="PUT_CREDIT_SPREAD_STANDARD",
        current_state=current_state, mfe=60.0, mae=10.0, unrealized_pnl=60.0, realized_pnl=0.0,
        deterministic_action_category="profit_target", deterministic_action_target_state=S.PROFIT_TARGET_REACHED,
        deterministic_action_mandatory=False, deterministic_action_reason="60% captured", risk_status="ok",
        data_is_fresh=True, dte=35, delta=0.2, option_prices={"mid": 0.4},
    )
    state.lifecycle_latest_snapshot[trade_id] = snap
    resolved = ResolvedAction(
        category="profit_target", target_state=S.PROFIT_TARGET_REACHED, mandatory=False, reason="60% captured",
        winning_trigger_names=("profit_target_pct",), all_findings=(),
    )
    state.lifecycle_latest_resolved[trade_id] = resolved
    return rec


class TestListLifecyclePositions:
    def test_empty_state_returns_empty_list(self, client):
        c, _ = client
        r = c.get("/api/lifecycle")
        assert r.status_code == 200
        assert r.json() == []

    def test_populated_position_has_expected_fields(self, client):
        c, state = client
        _seed_position(state)
        r = c.get("/api/lifecycle")
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 1
        assert body[0]["trade_id"] == "T1"
        assert body[0]["ticker"] == "XYZ"
        assert body[0]["strategy"] == "put_credit_spread"
        assert body[0]["management_policy"] == "PUT_CREDIT_SPREAD_STANDARD"
        assert body[0]["current_state"] == "profit_target_reached"
        assert body[0]["status_indicator"] == "profit_target"
        assert body[0]["mfe"] == 60.0
        assert body[0]["mae"] == 10.0
        assert body[0]["unrealized_pnl"] == 60.0
        assert body[0]["risk_status"] == "ok"
        assert body[0]["data_is_fresh"] is True

    def test_no_execution_action_field_anywhere(self, client):
        """Part 22: no live execution controls exposed on this view."""
        c, state = client
        _seed_position(state)
        body = c.get("/api/lifecycle").json()[0]
        forbidden_terms = ("submit", "execute", "cancel_order", "auto_trade", "place_order")
        for key in body:
            assert key.lower() not in forbidden_terms


class TestGetLifecyclePosition:
    def test_returns_the_single_position(self, client):
        c, state = client
        _seed_position(state)
        r = c.get("/api/lifecycle/T1")
        assert r.status_code == 200
        assert r.json()["trade_id"] == "T1"

    def test_unknown_trade_id_is_404(self, client):
        c, _ = client
        r = c.get("/api/lifecycle/does-not-exist")
        assert r.status_code == 404


class TestStatusIndicatorReflectsResolvedCategory:
    def test_risk_exit_indicator(self, client):
        c, state = client
        rec = _seed_position(state, current_state=S.RISK_EXIT_REQUIRED)
        state.lifecycle_latest_resolved["T1"] = ResolvedAction(
            category="risk_halt", target_state=S.RISK_EXIT_REQUIRED, mandatory=True, reason="portfolio halt",
            winning_trigger_names=(), all_findings=(),
        )
        body = c.get("/api/lifecycle/T1").json()
        assert body["status_indicator"] == "risk_exit"

    def test_data_insufficient_indicator(self, client):
        c, state = client
        _seed_position(state, current_state=S.DATA_INSUFFICIENT)
        state.lifecycle_latest_resolved["T1"] = ResolvedAction(
            category="system_data_safety", target_state=S.DATA_INSUFFICIENT, mandatory=True, reason="stale quote",
            winning_trigger_names=(), all_findings=(),
        )
        body = c.get("/api/lifecycle/T1").json()
        assert body["status_indicator"] == "data_insufficient"
