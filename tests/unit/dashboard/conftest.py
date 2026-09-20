"""Shared fixtures for the dashboard test suite. Reuses
`tests.unit.risk.conftest`'s scenario builders directly rather than
building a second, parallel set of Portfolio/TradeProposal/OptionChain
fixtures -- a registered dashboard opportunity is exactly "a Risk-Engine-
approved scenario with a MANUAL broker capability.\""""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.risk.engine import evaluate_trade_proposal
from src.dashboard.models import DashboardState
from src.dashboard.service import register_opportunity
from tests.unit.risk.conftest import (
    EXPIRATION,
    MD_TS,
    NOW,
    build_approved_covered_call_scenario,
    build_approved_csp_scenario,
    build_approved_pcs_scenario,
    fidelity_capabilities,
    make_contract,
    make_portfolio,
    make_underlying,
    pcs_proposal,
)

LATER = NOW + timedelta(hours=1)


@pytest.fixture
def manual_caps():
    return fidelity_capabilities()


@pytest.fixture
def pcs_scenario():
    return build_approved_pcs_scenario()


@pytest.fixture
def dashboard_state(pcs_scenario):
    return DashboardState(portfolio=pcs_scenario.portfolio, limits=pcs_scenario.limits)


@pytest.fixture
def registered_opportunity(dashboard_state, pcs_scenario, manual_caps):
    """A dashboard state with exactly one tracked opportunity, already
    at AWAITING_HUMAN (the ticket the Risk Engine itself produced)."""
    result = evaluate_trade_proposal(
        pcs_scenario.proposal, pcs_scenario.portfolio, pcs_scenario.quantitative_analysis,
        pcs_scenario.market_data, manual_caps, limits=pcs_scenario.limits, now=NOW,
    )
    assert result.fidelity_ticket is not None
    record = register_opportunity(
        dashboard_state, proposal=pcs_scenario.proposal, market_data=pcs_scenario.market_data,
        quantitative_analysis=pcs_scenario.quantitative_analysis, devils_advocate_review=None,
        risk_decision=result, now=NOW,
    )
    return dashboard_state, record
