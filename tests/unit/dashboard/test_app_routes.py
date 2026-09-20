"""FastAPI TestClient tests for src.dashboard.app: every allowed route
behaves correctly end to end, through the real HTTP layer (request
validation, dependency injection, error mapping) rather than calling
the service layer directly."""
from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from src.brokers.fidelity import TicketStatus
import src.dashboard.app as dashboard_app
from src.dashboard.models import DashboardState
from src.dashboard.service import register_opportunity
from src.risk.engine import evaluate_trade_proposal
from tests.unit.risk.conftest import NOW, build_approved_pcs_scenario, fidelity_capabilities, make_underlying


@pytest.fixture
def client_and_trade_id():
    scenario = build_approved_pcs_scenario()
    manual_caps = fidelity_capabilities()
    result = evaluate_trade_proposal(
        scenario.proposal, scenario.portfolio, scenario.quantitative_analysis,
        scenario.market_data, manual_caps, limits=scenario.limits, now=NOW,
    )
    state = DashboardState(portfolio=scenario.portfolio, limits=scenario.limits)
    record = register_opportunity(
        state, proposal=scenario.proposal, market_data=scenario.market_data,
        quantitative_analysis=scenario.quantitative_analysis, devils_advocate_review=None,
        risk_decision=result, now=NOW,
    )
    dashboard_app.set_state(state)
    dashboard_app.app.dependency_overrides[dashboard_app.get_now] = lambda: NOW
    client = TestClient(dashboard_app.app)
    yield client, record.trade_id
    dashboard_app.set_state(None)
    dashboard_app.app.dependency_overrides.clear()


class TestReadRoutes:
    def test_portfolio_header(self, client_and_trade_id):
        client, _ = client_and_trade_id
        r = client.get("/api/portfolio-header")
        assert r.status_code == 200
        assert r.json()["nav"] == 100_000.0

    def test_risk_panel(self, client_and_trade_id):
        client, _ = client_and_trade_id
        r = client.get("/api/risk-panel")
        assert r.status_code == 200
        assert r.json()["state"] == "NORMAL"

    def test_list_opportunities(self, client_and_trade_id):
        client, trade_id = client_and_trade_id
        r = client.get("/api/opportunities")
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 1
        assert body[0]["trade_id"] == trade_id
        assert body[0]["status"] == "awaiting_human"

    def test_get_single_opportunity(self, client_and_trade_id):
        client, trade_id = client_and_trade_id
        r = client.get(f"/api/opportunities/{trade_id}")
        assert r.status_code == 200
        assert r.json()["ticker"] == "SPY"

    def test_get_unknown_opportunity_is_404(self, client_and_trade_id):
        client, _ = client_and_trade_id
        r = client.get("/api/opportunities/does-not-exist")
        assert r.status_code == 404

    def test_audit_log_empty_route(self, client_and_trade_id):
        client, _ = client_and_trade_id
        r = client.get("/api/audit")
        assert r.status_code == 200
        assert len(r.json()) == 1  # the initial "approval" event
        assert r.json()[0]["event_type"] == "approval"

    def test_audit_log_for_trade(self, client_and_trade_id):
        client, trade_id = client_and_trade_id
        r = client.get(f"/api/audit/{trade_id}")
        assert r.status_code == 200
        assert len(r.json()) == 1

    def test_no_loaded_state_returns_503(self):
        dashboard_app.set_state(None)
        client = TestClient(dashboard_app.app)
        r = client.get("/api/portfolio-header")
        assert r.status_code == 503


class TestCopyRoute:
    def test_copy_returns_ticket_text(self, client_and_trade_id):
        client, trade_id = client_and_trade_id
        r = client.post(f"/api/opportunities/{trade_id}/copy")
        assert r.status_code == 200
        assert r.json()["text"].startswith("FIDELITY TRADER+ ORDER")

    def test_copy_disabled_once_stale_returns_400(self, client_and_trade_id):
        client, trade_id = client_and_trade_id
        dashboard_app.app.dependency_overrides[dashboard_app.get_now] = lambda: NOW + timedelta(hours=2)
        r = client.post(f"/api/opportunities/{trade_id}/copy")
        assert r.status_code == 400


class TestMarkOrderEnteredRoute:
    def test_happy_path(self, client_and_trade_id):
        client, trade_id = client_and_trade_id
        r = client.post(
            f"/api/opportunities/{trade_id}/mark-order-entered",
            json={"actual_limit_entered": 0.74, "contracts": 2, "entered_by": "dipesh"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "order_entered"

    def test_missing_required_field_is_422(self, client_and_trade_id):
        client, trade_id = client_and_trade_id
        r = client.post(f"/api/opportunities/{trade_id}/mark-order-entered", json={"contracts": 2})
        assert r.status_code == 422

    def test_negative_limit_is_422(self, client_and_trade_id):
        client, trade_id = client_and_trade_id
        r = client.post(
            f"/api/opportunities/{trade_id}/mark-order-entered",
            json={"actual_limit_entered": -1.0, "contracts": 2, "entered_by": "dipesh"},
        )
        assert r.status_code == 422


class TestFillRoute:
    def test_full_flow_entered_then_filled(self, client_and_trade_id):
        client, trade_id = client_and_trade_id
        client.post(f"/api/opportunities/{trade_id}/mark-order-entered", json={"actual_limit_entered": 0.75, "contracts": 2, "entered_by": "dipesh"})
        r = client.post(
            f"/api/opportunities/{trade_id}/fill",
            json={"status": "FILLED", "fill_price": 0.76, "contracts_filled": 2, "confirmed_by": "dipesh"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "filled"

    def test_invalid_status_is_422(self, client_and_trade_id):
        client, trade_id = client_and_trade_id
        client.post(f"/api/opportunities/{trade_id}/mark-order-entered", json={"actual_limit_entered": 0.75, "contracts": 2, "entered_by": "dipesh"})
        r = client.post(
            f"/api/opportunities/{trade_id}/fill",
            json={"status": "EXECUTED", "fill_price": 0.76, "contracts_filled": 2, "confirmed_by": "dipesh"},
        )
        assert r.status_code == 422

    def test_fill_before_order_entered_is_400(self, client_and_trade_id):
        client, trade_id = client_and_trade_id
        r = client.post(
            f"/api/opportunities/{trade_id}/fill",
            json={"status": "FILLED", "fill_price": 0.76, "contracts_filled": 2, "confirmed_by": "dipesh"},
        )
        assert r.status_code == 400


class TestCancelRoute:
    def test_cancel_after_entry(self, client_and_trade_id):
        client, trade_id = client_and_trade_id
        client.post(f"/api/opportunities/{trade_id}/mark-order-entered", json={"actual_limit_entered": 0.75, "contracts": 2, "entered_by": "dipesh"})
        r = client.post(f"/api/opportunities/{trade_id}/cancel", json={"reason": "changed mind", "actor": "dipesh"})
        assert r.status_code == 200
        assert r.json()["status"] == "cancelled"


class TestRejectRoute:
    def test_reject_from_awaiting_human(self, client_and_trade_id):
        client, trade_id = client_and_trade_id
        r = client.post(f"/api/opportunities/{trade_id}/reject", json={"reason": "pass", "actor": "dipesh"})
        assert r.status_code == 200
        assert r.json()["status"] == "rejected"

    def test_reject_twice_is_400(self, client_and_trade_id):
        client, trade_id = client_and_trade_id
        client.post(f"/api/opportunities/{trade_id}/reject", json={"reason": "pass", "actor": "dipesh"})
        r = client.post(f"/api/opportunities/{trade_id}/reject", json={"reason": "again", "actor": "dipesh"})
        assert r.status_code == 400


class TestRefreshRoute:
    def test_refresh_with_fresh_quotes_re_approves(self, client_and_trade_id):
        client, trade_id = client_and_trade_id
        later = NOW + timedelta(hours=2)
        dashboard_app.app.dependency_overrides[dashboard_app.get_now] = lambda: later
        r = client.post(
            f"/api/opportunities/{trade_id}/refresh",
            json={
                "underlying_price": 628.5, "underlying_bid": 628.4, "underlying_ask": 628.6,
                "quote_timestamp": (later - timedelta(minutes=1)).isoformat(),
                "leg_quotes": [
                    {"strike": 620.0, "right": "P", "bid": 1.32, "ask": 1.38, "volume": 500, "open_interest": 1000, "iv": 0.18},
                    {"strike": 615.0, "right": "P", "bid": 0.58, "ask": 0.62, "volume": 400, "open_interest": 800, "iv": 0.19},
                ],
            },
        )
        assert r.status_code == 200
        assert r.json()["status"] == "awaiting_human"

    def test_refresh_missing_leg_quotes_is_422(self, client_and_trade_id):
        client, trade_id = client_and_trade_id
        r = client.post(
            f"/api/opportunities/{trade_id}/refresh",
            json={"underlying_price": 628.5, "underlying_bid": 628.4, "underlying_ask": 628.6, "quote_timestamp": NOW.isoformat(), "leg_quotes": []},
        )
        assert r.status_code == 422
