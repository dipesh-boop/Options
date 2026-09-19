"""Tests for read-only context serialization."""
from __future__ import annotations

import json
from datetime import date, datetime

from src.llm.context import (
    CandidateContext,
    MarketContext,
    PortfolioStateContext,
    build_agent_context,
)


def test_empty_context_serializes_to_empty_object():
    assert json.loads(build_agent_context()) == {}


def test_only_requested_sections_are_included():
    market = MarketContext(as_of=datetime(2026, 1, 1, 9, 30), vix_level=14.2)
    payload = json.loads(build_agent_context(market=market))
    assert "market" in payload
    assert "portfolio" not in payload
    assert "candidates" not in payload


def test_dates_serialize_as_iso_strings():
    candidate = CandidateContext(
        symbol="SPY",
        strategy_type="cash_secured_put",
        expiry=date(2026, 3, 20),
        strike=560.0,
        right="P",
        mid_price=4.25,
        implied_vol=0.14,
        delta=-0.3,
        open_interest=5000,
        volume=1200,
    )
    payload = json.loads(build_agent_context(candidates=[candidate]))
    assert payload["candidates"][0]["expiry"] == "2026-03-20"


def test_portfolio_and_extra_included():
    portfolio = PortfolioStateContext(
        nav=100_000.0,
        net_delta=-12.5,
        net_theta=45.0,
        net_vega=-8.0,
        current_drawdown_pct=0.02,
        open_position_count=3,
    )
    payload = json.loads(build_agent_context(portfolio=portfolio, extra={"note": "test"}))
    assert payload["portfolio"]["nav"] == 100_000.0
    assert payload["extra"]["note"] == "test"
