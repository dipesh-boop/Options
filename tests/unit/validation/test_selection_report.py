"""Tests for src.validation.selection_report -- the dedicated report
answering whether the Strategy Selector itself is adding value."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.backtest.simulator import TradeRecord
from src.llm.schemas import StrategyType
from src.research.performance_breakdown import ResearchTradeObservation, TradeContext
from src.strategies.base import StrategyKind
from src.validation.counterfactual import CounterfactualOutcome
from src.validation.selection_report import build_strategy_selection_report, render_strategy_selection_report
from src.validation.strategy_attribution import per_strategy_performance


def _outcome(opportunity_id: str, kind: StrategyKind, *, selected: bool, pnl: float, regime: str = "bull_trending") -> CounterfactualOutcome:
    return CounterfactualOutcome(
        opportunity_id=opportunity_id, strategy_kind=kind, was_selected=selected, hypothetical_pnl=pnl,
        market_regime=regime, volatility_regime="low_vol",
    )


def _trade(pnl: float, *, strategy: StrategyType, capital_at_risk: float = 500.0, day_offset: int = 0) -> TradeRecord:
    opened = date(2024, 1, 1) + timedelta(days=day_offset)
    return TradeRecord(
        position_id=f"bt-{strategy.value}-{day_offset}", ticker="XYZ", strategy=strategy, contracts=1,
        opened_at=opened, closed_at=opened + timedelta(days=10), close_reason="profit_target",
        capital_at_risk=capital_at_risk, entry_spread_pct=0.05, realistic_entry_credit=100.0,
        realistic_exit_debit=-(100.0 - pnl), theoretical_entry_credit=100.0, theoretical_exit_debit=-(100.0 - pnl),
        commission_paid=1.0, realistic_pnl=pnl, theoretical_pnl=pnl,
    )


def _obs(pnl: float, *, strategy: StrategyType, day_offset: int = 0) -> ResearchTradeObservation:
    context = TradeContext(
        entry_delta=-0.20, entry_dte=30, iv_percentile=50.0, market_regime="bull_trending", sector="tech",
        profit_target_pct=0.5, management_dte=7,
    )
    return ResearchTradeObservation(trade=_trade(pnl, strategy=strategy, day_offset=day_offset), context=context)


class TestSelectionFrequency:
    def test_counts_only_selected_records_per_strategy(self):
        outcomes = [
            _outcome("opp-1", StrategyKind.PUT_CREDIT_SPREAD, selected=True, pnl=100.0),
            _outcome("opp-1", StrategyKind.BULL_CALL_SPREAD, selected=False, pnl=50.0),
            _outcome("opp-2", StrategyKind.PUT_CREDIT_SPREAD, selected=True, pnl=80.0),
        ]
        report = build_strategy_selection_report(outcomes, {})
        assert report.selection_frequency["put_credit_spread"].times_selected == 2
        assert "bull_call_spread" not in report.selection_frequency

    def test_selection_share_sums_to_one_across_strategies(self):
        outcomes = [
            _outcome("opp-1", StrategyKind.PUT_CREDIT_SPREAD, selected=True, pnl=100.0),
            _outcome("opp-2", StrategyKind.BULL_CALL_SPREAD, selected=True, pnl=50.0),
        ]
        report = build_strategy_selection_report(outcomes, {})
        shares = [f.selection_share for f in report.selection_frequency.values()]
        assert sum(shares) == pytest.approx(1.0)


class TestRiskAdjustedValue:
    def test_expectancy_per_dollar_of_capital_deployed(self):
        observations = [_obs(100.0, strategy=StrategyType.PUT_CREDIT_SPREAD, day_offset=0)]
        summaries = per_strategy_performance(observations)
        report = build_strategy_selection_report([], summaries)
        # capital_at_risk defaults to 500.0 in the fixture -> 100/500
        assert report.risk_adjusted_value["put_credit_spread"] == pytest.approx(100.0 / 500.0)


class TestReusesAttributionQuestions:
    """Four of the six named questions overlap exactly with
    src.validation.strategy_attribution's own six -- reused, never
    re-answered by a second implementation."""

    def test_hedge_family_strategy_flagged_as_reducing_portfolio_risk(self):
        observations = [_obs(-20.0, strategy=StrategyType.PROTECTIVE_PUT)]
        summaries = per_strategy_performance(observations)
        report = build_strategy_selection_report([], summaries)
        assert report.reduces_portfolio_risk == ("protective_put",)

    def test_non_hedge_losing_strategy_flagged_as_consuming_capital_without_benefit(self):
        observations = [_obs(-50.0, strategy=StrategyType.BULL_CALL_SPREAD)]
        summaries = per_strategy_performance(observations)
        report = build_strategy_selection_report([], summaries)
        assert report.consumes_capital_without_benefit == ("bull_call_spread",)


class TestBuildStrategySelectionReportIncludesEverything:
    def test_overall_effectiveness_and_regime_breakdowns_present(self):
        outcomes = [
            _outcome("opp-1", StrategyKind.PUT_CREDIT_SPREAD, selected=True, pnl=100.0, regime="bull_trending"),
            _outcome("opp-2", StrategyKind.PUT_CREDIT_SPREAD, selected=True, pnl=-50.0, regime="bear_trending"),
        ]
        report = build_strategy_selection_report(outcomes, {})
        assert report.overall_effectiveness.sample_size == 2
        assert "bull_trending" in report.effectiveness_by_market_regime
        assert "low_vol" in report.effectiveness_by_volatility_regime

    def test_dynamic_vs_fixed_comparison_is_included(self):
        report = build_strategy_selection_report([], {})
        assert report.dynamic_vs_fixed.dynamic_sample_size == 0

    def test_attribution_report_carried_alongside_never_discarded(self):
        observations = [_obs(100.0, strategy=StrategyType.PUT_CREDIT_SPREAD)]
        summaries = per_strategy_performance(observations)
        report = build_strategy_selection_report([], summaries)
        assert "put_credit_spread" in report.attribution.summaries


class TestRenderStrategySelectionReport:
    def test_renders_without_error_on_empty_input(self):
        report = build_strategy_selection_report([], {})
        text = render_strategy_selection_report(report)
        assert "STRATEGY SELECTION REPORT" in text
        assert "INSUFFICIENT SAMPLE" in text

    def test_renders_selection_frequency_and_verdict(self):
        outcomes = []
        for i in range(20):
            pcs_wins = i % 2 == 0
            outcomes.append(_outcome(f"opp-{i}", StrategyKind.PUT_CREDIT_SPREAD, selected=pcs_wins, pnl=200.0 if pcs_wins else 10.0))
            outcomes.append(_outcome(f"opp-{i}", StrategyKind.BULL_CALL_SPREAD, selected=not pcs_wins, pnl=10.0 if pcs_wins else 200.0))
        report = build_strategy_selection_report(outcomes, {})
        text = render_strategy_selection_report(report)
        assert "put_credit_spread: 10 time(s)" in text
        assert "Verdict:" in text
