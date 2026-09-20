"""Tests for src.dashboard.risk_state: the RISK PANEL's four-state
Risk Engine state, concentration views, and correlation clusters."""
from __future__ import annotations

from datetime import date, datetime, timezone

import numpy as np
import pytest

from src.dashboard.risk_state import (
    RiskEngineState,
    build_risk_panel,
    compute_risk_engine_state,
    correlation_clusters,
    sector_concentration,
    underlying_concentration,
)
from src.llm.schemas import StrategyType
from src.risk.limits import get_default_limits
from src.risk.portfolio_risk import Portfolio, PortfolioPosition, PortfolioPositionLeg

NOW = datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc)
LIMITS = get_default_limits()


def _position(ticker="XYZ", capital_at_risk=5_000.0, max_loss=5_000.0) -> PortfolioPosition:
    return PortfolioPosition(
        position_id=f"p-{ticker}", ticker=ticker, sector="TECH", strategy=StrategyType.CASH_SECURED_PUT,
        expiration=date(2026, 10, 16), legs=[PortfolioPositionLeg(right="P", side="sell", strike=95.0, entry_price=2.0)],
        contracts=1, capital_at_risk=capital_at_risk, max_loss=max_loss, opened_at=NOW,
    )


def _portfolio(**overrides) -> Portfolio:
    base = dict(as_of=NOW, nav=100_000.0, cash=90_000.0, peak_equity=100_000.0, sector_by_ticker={"XYZ": "TECH"})
    base.update(overrides)
    return Portfolio(**base)


class TestComputeRiskEngineState:
    def test_normal_when_no_drawdown(self):
        state, reason = compute_risk_engine_state(_portfolio(), LIMITS)
        assert state == RiskEngineState.NORMAL
        assert "0.00%" in reason

    def test_halt_on_manual_kill_switch(self):
        portfolio = _portfolio(halted=True, halt_reason="emergency stop")
        state, reason = compute_risk_engine_state(portfolio, LIMITS)
        assert state == RiskEngineState.HALT
        assert "emergency stop" in reason

    def test_halt_on_drawdown_past_halt_threshold(self):
        portfolio = _portfolio(nav=80_000.0, cash=70_000.0, peak_equity=100_000.0)  # 20% drawdown
        state, reason = compute_risk_engine_state(portfolio, LIMITS)
        assert state == RiskEngineState.HALT

    def test_reduce_risk_zone(self):
        # risk_reduction threshold is below halt; find a nav between warning and halt
        dd_reduction = LIMITS.drawdown_risk_reduction_pct + 0.005
        nav = 100_000.0 * (1 - dd_reduction)
        portfolio = _portfolio(nav=nav, cash=nav * 0.9, peak_equity=100_000.0)
        state, reason = compute_risk_engine_state(portfolio, LIMITS)
        assert state == RiskEngineState.REDUCE_RISK
        assert "tightened" in reason

    def test_warning_zone(self):
        dd_warning = LIMITS.drawdown_warning_pct + 0.005
        assert dd_warning < LIMITS.drawdown_risk_reduction_pct  # sanity: still below risk-reduction
        nav = 100_000.0 * (1 - dd_warning)
        portfolio = _portfolio(nav=nav, cash=nav * 0.9, peak_equity=100_000.0)
        state, reason = compute_risk_engine_state(portfolio, LIMITS)
        assert state == RiskEngineState.WARNING

    def test_manual_halt_wins_even_with_zero_drawdown(self):
        portfolio = _portfolio(halted=True, halt_reason="stop")
        state, _ = compute_risk_engine_state(portfolio, LIMITS)
        assert state == RiskEngineState.HALT


class TestConcentration:
    def test_underlying_concentration_lists_each_held_ticker(self):
        portfolio = _portfolio(positions=[_position("XYZ", 5_000.0, 5_000.0)])
        entries = underlying_concentration(portfolio, LIMITS)
        assert len(entries) == 1
        assert entries[0].label == "XYZ"
        assert entries[0].exposure_pct == pytest.approx(0.05)

    def test_no_positions_gives_empty_concentration(self):
        assert underlying_concentration(_portfolio(), LIMITS) == ()

    def test_breach_flag_set_when_exposure_exceeds_limit(self):
        huge = LIMITS.max_underlying_exposure_pct * 100_000.0 + 1_000.0
        portfolio = _portfolio(positions=[_position("XYZ", huge, huge)])
        entries = underlying_concentration(portfolio, LIMITS)
        assert entries[0].breached is True

    def test_sector_concentration_groups_by_sector(self):
        portfolio = _portfolio(
            positions=[_position("XYZ", 3_000.0, 3_000.0)],
            sector_by_ticker={"XYZ": "TECH"},
        )
        entries = sector_concentration(portfolio, LIMITS)
        assert len(entries) == 1
        assert entries[0].label == "TECH"


class TestCorrelationClusters:
    def test_not_tracked_when_price_history_absent(self):
        portfolio = _portfolio(positions=[_position("AAA"), _position("BBB")])
        clusters, tracked = correlation_clusters(portfolio, LIMITS)
        assert tracked is False
        assert clusters == ()

    def test_not_tracked_with_fewer_than_two_positions(self):
        portfolio = _portfolio(positions=[_position("AAA")], price_history={"AAA": [1.0, 2.0, 3.0]})
        clusters, tracked = correlation_clusters(portfolio, LIMITS)
        assert tracked is False

    def test_tracked_and_flags_highly_correlated_pair(self):
        prices = list(np.linspace(100, 110, 30))
        portfolio = _portfolio(
            positions=[_position("AAA"), _position("BBB")],
            price_history={"AAA": prices, "BBB": prices},  # identical series -> correlation 1.0
        )
        clusters, tracked = correlation_clusters(portfolio, LIMITS)
        assert tracked is True
        assert len(clusters) == 1
        assert clusters[0].correlation == pytest.approx(1.0, abs=1e-6)


class TestBuildRiskPanel:
    def test_all_fields_populate(self):
        portfolio = _portfolio(positions=[_position("XYZ")])
        panel = build_risk_panel(portfolio, LIMITS)
        assert panel.state == RiskEngineState.NORMAL
        assert panel.capital_utilization_pct == pytest.approx(0.1)
        assert panel.cash_reserve_pct == pytest.approx(0.9)
        assert len(panel.underlying_concentration) == 1
        assert panel.correlation_tracked is False
