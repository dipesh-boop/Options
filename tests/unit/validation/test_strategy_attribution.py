"""Tests for src.validation.strategy_attribution -- per-strategy
performance tracking ("do NOT judge only the combined portfolio") and
the six named attribution questions, each a deterministic threshold
rule over Python-computed numbers, never an LLM judgment."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.backtest.simulator import TradeRecord
from src.data.option_chain import OptionRight as DataOptionRight
from src.llm.schemas import StrategyType
from src.research.performance_breakdown import ResearchTradeObservation, TradeContext
from src.strategies.bull_call_spread import evaluate_bull_call_spread
from src.strategies.put_credit_spread import evaluate_put_credit_spread
from src.validation.counterfactual import StrategyAlternativeRecord
from src.validation.strategy_attribution import (
    answer_attribution_questions,
    funnel_counts_by_strategy,
    per_strategy_performance,
)

from tests.unit.strategies.conftest import EXPIRATION as STRATEGY_EXPIRATION
from tests.unit.strategies.conftest import contract, limits

_STRATEGY_COMMON = dict(
    ticker="XYZ", expiration=STRATEGY_EXPIRATION, spot=100.0, sigma=0.25, t=24 / 365, rate=0.04,
    days_to_expiry=24, num_contracts=1, limits=limits(),
)


def _pcs_evaluation():
    return evaluate_put_credit_spread(
        short_put_contract=contract(95, DataOptionRight.PUT, 2.8, 3.0),
        long_put_contract=contract(90, DataOptionRight.PUT, 1.4, 1.6), **_STRATEGY_COMMON,
    )


def _bcs_evaluation():
    return evaluate_bull_call_spread(
        long_call_contract=contract(95, DataOptionRight.CALL, 6.8, 7.0),
        short_call_contract=contract(105, DataOptionRight.CALL, 2.3, 2.5), **_STRATEGY_COMMON,
    )


def _trade(pnl: float, *, strategy: StrategyType, capital_at_risk: float = 500.0, day_offset: int = 0, holding_days: int = 10, commission: float = 1.0, theoretical_pnl: float | None = None) -> TradeRecord:
    opened = date(2024, 1, 1) + timedelta(days=day_offset)
    return TradeRecord(
        position_id=f"bt-{strategy.value}-{day_offset}", ticker="XYZ", strategy=strategy, contracts=1,
        opened_at=opened, closed_at=opened + timedelta(days=holding_days), close_reason="profit_target",
        capital_at_risk=capital_at_risk, entry_spread_pct=0.05, realistic_entry_credit=100.0,
        realistic_exit_debit=-(100.0 - pnl), theoretical_entry_credit=100.0,
        theoretical_exit_debit=-(100.0 - (theoretical_pnl if theoretical_pnl is not None else pnl)),
        commission_paid=commission, realistic_pnl=pnl,
        theoretical_pnl=theoretical_pnl if theoretical_pnl is not None else pnl,
    )


def _obs(pnl: float, *, strategy: StrategyType, regime: str = "bull_trending", **kwargs) -> ResearchTradeObservation:
    context = TradeContext(
        entry_delta=-0.20, entry_dte=30, iv_percentile=50.0, market_regime=regime, sector="tech",
        profit_target_pct=0.5, management_dte=7,
    )
    return ResearchTradeObservation(trade=_trade(pnl, strategy=strategy, **kwargs), context=context)


class TestPerStrategyPerformance:
    def test_tracks_each_strategy_separately_not_rolled_up(self):
        observations = [
            _obs(100.0, strategy=StrategyType.PUT_CREDIT_SPREAD),
            _obs(-50.0, strategy=StrategyType.BULL_CALL_SPREAD),
        ]
        summaries = per_strategy_performance(observations)
        assert set(summaries) == {"put_credit_spread", "bull_call_spread"}
        assert summaries["put_credit_spread"].net_pnl == pytest.approx(100.0)
        assert summaries["bull_call_spread"].net_pnl == pytest.approx(-50.0)

    def test_win_loss_counts_and_win_rate(self):
        observations = [
            _obs(100.0, strategy=StrategyType.PUT_CREDIT_SPREAD, day_offset=0),
            _obs(-40.0, strategy=StrategyType.PUT_CREDIT_SPREAD, day_offset=1),
            _obs(60.0, strategy=StrategyType.PUT_CREDIT_SPREAD, day_offset=2),
        ]
        summary = per_strategy_performance(observations)["put_credit_spread"]
        assert summary.trades == 3
        assert summary.wins == 2
        assert summary.losses == 1
        assert summary.win_rate == pytest.approx(2 / 3)

    def test_return_on_capital_is_dollar_weighted(self):
        observations = [
            _obs(100.0, strategy=StrategyType.PUT_CREDIT_SPREAD, capital_at_risk=500.0, day_offset=0),
            _obs(-50.0, strategy=StrategyType.PUT_CREDIT_SPREAD, capital_at_risk=1000.0, day_offset=1),
        ]
        summary = per_strategy_performance(observations)["put_credit_spread"]
        assert summary.return_on_capital == pytest.approx(50.0 / 1500.0)

    def test_expectancy_is_average_pnl(self):
        observations = [_obs(100.0, strategy=StrategyType.PUT_CREDIT_SPREAD, day_offset=0), _obs(-40.0, strategy=StrategyType.PUT_CREDIT_SPREAD, day_offset=1)]
        summary = per_strategy_performance(observations)["put_credit_spread"]
        assert summary.expectancy == pytest.approx(30.0)

    def test_profit_factor_is_gross_profit_over_gross_loss(self):
        observations = [
            _obs(100.0, strategy=StrategyType.PUT_CREDIT_SPREAD, day_offset=0),
            _obs(60.0, strategy=StrategyType.PUT_CREDIT_SPREAD, day_offset=1),
            _obs(-40.0, strategy=StrategyType.PUT_CREDIT_SPREAD, day_offset=2),
        ]
        summary = per_strategy_performance(observations)["put_credit_spread"]
        assert summary.profit_factor == pytest.approx(160.0 / 40.0)

    def test_profit_factor_is_none_with_no_losing_trades(self):
        observations = [_obs(100.0, strategy=StrategyType.PUT_CREDIT_SPREAD)]
        summary = per_strategy_performance(observations)["put_credit_spread"]
        assert summary.profit_factor is None

    def test_sharpe_is_none_below_the_minimum_sample_size(self):
        observations = [_obs(100.0, strategy=StrategyType.PUT_CREDIT_SPREAD, day_offset=i) for i in range(5)]
        summary = per_strategy_performance(observations, min_sample_for_sharpe=20)["put_credit_spread"]
        assert summary.sharpe is None

    def test_sharpe_is_computed_at_or_above_the_minimum_sample_size(self):
        observations = [
            _obs(100.0 if i % 2 == 0 else -30.0, strategy=StrategyType.PUT_CREDIT_SPREAD, day_offset=i) for i in range(20)
        ]
        summary = per_strategy_performance(observations, min_sample_for_sharpe=20)["put_credit_spread"]
        assert summary.sharpe is not None

    def test_max_drawdown_contribution_from_chronological_cumulative_pnl(self):
        # +100, -150, +20 chronologically -> peak 100, trough -50 -> drawdown 150
        observations = [
            _obs(100.0, strategy=StrategyType.PUT_CREDIT_SPREAD, day_offset=0),
            _obs(-150.0, strategy=StrategyType.PUT_CREDIT_SPREAD, day_offset=1),
            _obs(20.0, strategy=StrategyType.PUT_CREDIT_SPREAD, day_offset=2),
        ]
        summary = per_strategy_performance(observations)["put_credit_spread"]
        assert summary.max_drawdown_contribution == pytest.approx(150.0)

    def test_worst_trade_pnl_and_average_loss_magnitude(self):
        observations = [
            _obs(100.0, strategy=StrategyType.PUT_CREDIT_SPREAD, day_offset=0),
            _obs(-30.0, strategy=StrategyType.PUT_CREDIT_SPREAD, day_offset=1),
            _obs(-90.0, strategy=StrategyType.PUT_CREDIT_SPREAD, day_offset=2),
        ]
        summary = per_strategy_performance(observations)["put_credit_spread"]
        assert summary.worst_trade_pnl == pytest.approx(-90.0)
        assert summary.average_loss_magnitude == pytest.approx(60.0)

    def test_average_loss_magnitude_is_none_with_no_losses(self):
        observations = [_obs(100.0, strategy=StrategyType.PUT_CREDIT_SPREAD)]
        summary = per_strategy_performance(observations)["put_credit_spread"]
        assert summary.average_loss_magnitude is None

    def test_avg_slippage_matches_build_backtest_results_own_definition(self):
        # theoretical - realistic - commission, same formula src.backtest.engine.build_backtest_result uses
        observations = [_obs(80.0, strategy=StrategyType.PUT_CREDIT_SPREAD, theoretical_pnl=100.0, commission=1.0)]
        summary = per_strategy_performance(observations)["put_credit_spread"]
        assert summary.avg_slippage == pytest.approx(100.0 - 80.0 - 1.0)

    def test_performance_by_regime_buckets_independently_per_strategy(self):
        observations = [
            _obs(100.0, strategy=StrategyType.PUT_CREDIT_SPREAD, regime="bull_trending", day_offset=0),
            _obs(-50.0, strategy=StrategyType.PUT_CREDIT_SPREAD, regime="bear_trending", day_offset=1),
        ]
        summary = per_strategy_performance(observations)["put_credit_spread"]
        assert set(summary.performance_by_regime) == {"bull_trending", "bear_trending"}
        assert summary.performance_by_regime["bull_trending"].total_pnl == pytest.approx(100.0)
        assert summary.performance_by_regime["bear_trending"].total_pnl == pytest.approx(-50.0)


class TestAnswerAttributionQuestions:
    def test_generated_profit_flags_positive_net_pnl_strategies(self):
        observations = [
            _obs(100.0, strategy=StrategyType.PUT_CREDIT_SPREAD),
            _obs(-50.0, strategy=StrategyType.BULL_CALL_SPREAD),
        ]
        report = answer_attribution_questions(per_strategy_performance(observations))
        assert report.generated_profit == ("put_credit_spread",)

    def test_hedge_family_strategies_flagged_as_reduced_losses_regardless_of_pnl_sign(self):
        observations = [_obs(-20.0, strategy=StrategyType.PROTECTIVE_PUT)]  # a hedge costs a premium -- expected
        report = answer_attribution_questions(per_strategy_performance(observations))
        assert report.reduced_losses == ("protective_put",)
        assert report.improved_drawdown == ("protective_put",)
        # A hedge's own negative P&L must never make it "consumed capital without value" --
        # Step 19A's rule that hedges are judged by drawdown/tail reduction, never standalone P&L.
        assert "protective_put" not in report.consumed_capital_without_value

    def test_non_hedge_losing_strategy_flagged_as_consumed_capital_without_value(self):
        observations = [_obs(-50.0, strategy=StrategyType.BULL_CALL_SPREAD)]
        report = answer_attribution_questions(per_strategy_performance(observations))
        assert report.consumed_capital_without_value == ("bull_call_spread",)

    def test_regime_specific_flags_concentration_above_80_percent(self):
        observations = [_obs(50.0, strategy=StrategyType.PUT_CREDIT_SPREAD, regime="bull_trending", day_offset=i) for i in range(9)]
        observations.append(_obs(50.0, strategy=StrategyType.PUT_CREDIT_SPREAD, regime="bear_trending", day_offset=9))
        report = answer_attribution_questions(per_strategy_performance(observations))
        assert report.regime_specific == ("put_credit_spread",)

    def test_regime_specific_not_flagged_when_spread_across_regimes(self):
        observations = [
            _obs(50.0, strategy=StrategyType.PUT_CREDIT_SPREAD, regime="bull_trending", day_offset=0),
            _obs(50.0, strategy=StrategyType.PUT_CREDIT_SPREAD, regime="bear_trending", day_offset=1),
        ]
        report = answer_attribution_questions(per_strategy_performance(observations))
        assert report.regime_specific == ()

    def test_increased_tail_risk_flags_a_worst_loss_far_beyond_the_average_loss(self):
        observations = [_obs(30.0, strategy=StrategyType.PUT_CREDIT_SPREAD, day_offset=0)]
        observations += [_obs(-10.0, strategy=StrategyType.PUT_CREDIT_SPREAD, day_offset=i) for i in range(1, 6)]
        # One catastrophic loss dwarfing the five typical -10 losses --
        # even with the outlier pulling the average up, the worst single
        # loss still clears 3x the resulting average loss magnitude.
        observations.append(_obs(-500.0, strategy=StrategyType.PUT_CREDIT_SPREAD, day_offset=6))
        report = answer_attribution_questions(per_strategy_performance(observations))
        assert report.increased_tail_risk == ("put_credit_spread",)

    def test_no_tail_risk_flag_when_losses_are_uniform(self):
        observations = [
            _obs(30.0, strategy=StrategyType.PUT_CREDIT_SPREAD, day_offset=0),
            _obs(-10.0, strategy=StrategyType.PUT_CREDIT_SPREAD, day_offset=1),
            _obs(-12.0, strategy=StrategyType.PUT_CREDIT_SPREAD, day_offset=2),
        ]
        report = answer_attribution_questions(per_strategy_performance(observations))
        assert report.increased_tail_risk == ()

    def test_excessive_trading_costs_flags_slippage_dominating_expectancy(self):
        observations = [_obs(40.0, strategy=StrategyType.PUT_CREDIT_SPREAD, theoretical_pnl=100.0, commission=1.0)]
        report = answer_attribution_questions(per_strategy_performance(observations))
        assert report.generated_excessive_trading_costs == ("put_credit_spread",)

    def test_no_excessive_cost_flag_when_slippage_is_small(self):
        observations = [_obs(95.0, strategy=StrategyType.PUT_CREDIT_SPREAD, theoretical_pnl=100.0, commission=1.0)]
        report = answer_attribution_questions(per_strategy_performance(observations))
        assert report.generated_excessive_trading_costs == ()

    def test_summaries_are_always_carried_alongside_never_the_only_place_a_number_lives(self):
        observations = [_obs(100.0, strategy=StrategyType.PUT_CREDIT_SPREAD)]
        report = answer_attribution_questions(per_strategy_performance(observations))
        assert "put_credit_spread" in report.summaries
        assert report.summaries["put_credit_spread"].net_pnl == pytest.approx(100.0)


class TestFunnelCountsByStrategy:
    """opportunities_considered / trades_proposed / trades_rejected /
    trades_entered, sourced from src.validation.counterfactual
    .StrategyAlternativeRecord -- the decision-time funnel, distinct
    from per_strategy_performance's completed-trade stats."""

    def test_opportunities_considered_counts_distinct_opportunities(self):
        records = [
            StrategyAlternativeRecord(opportunity_id="opp-1", evaluation=_pcs_evaluation(), was_selected=True, risk_decision="approve", selection_or_rejection_reason="best score"),
            StrategyAlternativeRecord(opportunity_id="opp-2", evaluation=_pcs_evaluation(), was_selected=False, risk_decision="approve", selection_or_rejection_reason="outranked"),
        ]
        counts = funnel_counts_by_strategy(records)
        assert counts["put_credit_spread"].opportunities_considered == 2

    def test_trades_proposed_counts_only_selected_records(self):
        records = [
            StrategyAlternativeRecord(opportunity_id="opp-1", evaluation=_pcs_evaluation(), was_selected=True, risk_decision="approve", selection_or_rejection_reason="best score"),
            StrategyAlternativeRecord(opportunity_id="opp-2", evaluation=_pcs_evaluation(), was_selected=False, risk_decision="approve", selection_or_rejection_reason="outranked by a better candidate"),
        ]
        counts = funnel_counts_by_strategy(records)
        assert counts["put_credit_spread"].trades_proposed == 1

    def test_trades_rejected_counts_non_approval_risk_decisions_regardless_of_selection(self):
        records = [
            StrategyAlternativeRecord(opportunity_id="opp-1", evaluation=_pcs_evaluation(), was_selected=False, risk_decision="reject", selection_or_rejection_reason="exceeds concentration limit"),
            StrategyAlternativeRecord(opportunity_id="opp-2", evaluation=_pcs_evaluation(), was_selected=False, risk_decision="approve", selection_or_rejection_reason="outranked"),
        ]
        counts = funnel_counts_by_strategy(records)
        assert counts["put_credit_spread"].trades_rejected == 1

    def test_trades_entered_requires_both_selected_and_approved(self):
        records = [
            StrategyAlternativeRecord(opportunity_id="opp-1", evaluation=_pcs_evaluation(), was_selected=True, risk_decision="approve", selection_or_rejection_reason="best score"),
            StrategyAlternativeRecord(opportunity_id="opp-2", evaluation=_pcs_evaluation(), was_selected=True, risk_decision="resize", selection_or_rejection_reason="best score, resized"),
        ]
        counts = funnel_counts_by_strategy(records)
        assert counts["put_credit_spread"].trades_entered == 2

    def test_strategies_tracked_independently(self):
        records = [
            StrategyAlternativeRecord(opportunity_id="opp-1", evaluation=_pcs_evaluation(), was_selected=True, risk_decision="approve", selection_or_rejection_reason="best score"),
            StrategyAlternativeRecord(opportunity_id="opp-1", evaluation=_bcs_evaluation(), was_selected=False, risk_decision="approve", selection_or_rejection_reason="outranked"),
        ]
        counts = funnel_counts_by_strategy(records)
        assert set(counts) == {"put_credit_spread", "bull_call_spread"}
        assert counts["put_credit_spread"].trades_proposed == 1
        assert counts["bull_call_spread"].trades_proposed == 0

    def test_resize_counts_as_a_risk_approval_not_a_rejection(self):
        records = [
            StrategyAlternativeRecord(opportunity_id="opp-1", evaluation=_pcs_evaluation(), was_selected=False, risk_decision="resize", selection_or_rejection_reason="approved with a smaller size"),
        ]
        counts = funnel_counts_by_strategy(records)
        assert counts["put_credit_spread"].trades_rejected == 0


class TestMultiStrategyAttributionReport:
    """The dedicated multi-strategy attribution report: one row per all
    15 StrategyKind values (Tier1 and Tier2 alike), funnel counts and
    completed-trade performance combined."""

    def test_covers_all_15_strategies_even_absent_ones(self):
        from src.strategies.base import StrategyKind
        from src.validation.strategy_attribution import build_multi_strategy_attribution_report

        report = build_multi_strategy_attribution_report([], [])
        assert set(report.rows) == {k.value for k in StrategyKind}
        assert len(report.rows) == 15

    def test_absent_strategy_row_is_zeroed_and_insufficient_sample(self):
        from src.validation.protocol import SampleSizeStatus
        from src.validation.strategy_attribution import build_multi_strategy_attribution_report

        report = build_multi_strategy_attribution_report([], [])
        row = report.rows["long_call_butterfly"]
        assert row.opportunities_identified == 0
        assert row.completed_trades == 0
        assert row.sample_status == SampleSizeStatus.INSUFFICIENT_SAMPLE
        assert row.performance is None

    def test_funnel_and_performance_merged_for_a_present_strategy(self):
        from src.validation.strategy_attribution import build_multi_strategy_attribution_report

        observations = [_obs(100.0, strategy=StrategyType.PUT_CREDIT_SPREAD)]
        records = [
            StrategyAlternativeRecord(opportunity_id="opp-1", evaluation=_pcs_evaluation(), was_selected=True, risk_decision="approve", selection_or_rejection_reason="best score"),
        ]
        report = build_multi_strategy_attribution_report(observations, records)
        row = report.rows["put_credit_spread"]
        assert row.opportunities_identified == 1
        assert row.trades_proposed == 1
        assert row.trades_approved == 1
        assert row.completed_trades == 1
        assert row.performance is not None
        assert row.performance.net_pnl == pytest.approx(100.0)

    def test_meets_preferred_sample_threshold_at_50_completed_trades(self):
        from src.validation.protocol import SampleSizeStatus
        from src.validation.strategy_attribution import build_multi_strategy_attribution_report

        observations = [_obs(10.0, strategy=StrategyType.PUT_CREDIT_SPREAD, day_offset=i) for i in range(50)]
        report = build_multi_strategy_attribution_report(observations, [])
        assert report.rows["put_credit_spread"].sample_status == SampleSizeStatus.PREFERRED_SAMPLE


class TestRenderMultiStrategyAttributionReport:
    def test_renders_insufficient_sample_marker(self):
        from src.validation.strategy_attribution import build_multi_strategy_attribution_report, render_multi_strategy_attribution_report

        report = build_multi_strategy_attribution_report([], [])
        text = render_multi_strategy_attribution_report(report)
        assert "INSUFFICIENT SAMPLE" in text
        assert "LONG_CALL_BUTTERFLY" in text

    def test_renders_both_average_pnl_and_expectancy_labels_for_the_same_number(self):
        from src.validation.strategy_attribution import build_multi_strategy_attribution_report, render_multi_strategy_attribution_report

        observations = [_obs(100.0, strategy=StrategyType.PUT_CREDIT_SPREAD)]
        report = build_multi_strategy_attribution_report(observations, [])
        text = render_multi_strategy_attribution_report(report)
        assert "Average P&L / Expectancy: $100.00" in text
