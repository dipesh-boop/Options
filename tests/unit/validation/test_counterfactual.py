"""Tests for src.validation.counterfactual -- selection-effectiveness
tracking, never judged from a single trade."""
from __future__ import annotations

import pytest

from src.strategies.base import StrategyKind
from src.validation.counterfactual import (
    CounterfactualOutcome,
    StrategyAlternativeRecord,
    dynamic_vs_fixed_strategy_comparison,
    summarize_by_regime,
    summarize_selection_effectiveness,
)

from .conftest import _trade


def _outcome(opportunity_id: str, kind: StrategyKind, *, selected: bool, pnl: float, regime: str = "bull_trending") -> CounterfactualOutcome:
    return CounterfactualOutcome(
        opportunity_id=opportunity_id, strategy_kind=kind, was_selected=selected, hypothetical_pnl=pnl,
        market_regime=regime, volatility_regime="low_vol",
    )


class TestStrategyAlternativeRecord:
    def test_stores_the_evaluation_directly_no_field_duplication(self):
        from src.strategies.put_credit_spread import evaluate_put_credit_spread
        from src.data.option_chain import OptionRight as DataOptionRight
        from tests.unit.strategies.conftest import EXPIRATION, contract, limits

        ev = evaluate_put_credit_spread(
            ticker="XYZ", expiration=EXPIRATION, short_put_contract=contract(95, DataOptionRight.PUT, 2.8, 3.0),
            long_put_contract=contract(90, DataOptionRight.PUT, 1.4, 1.6), spot=100.0, sigma=0.25, t=24 / 365,
            rate=0.04, days_to_expiry=24, num_contracts=1, limits=limits(),
        )
        record = StrategyAlternativeRecord(
            opportunity_id="opp-1", evaluation=ev, was_selected=True, risk_decision="approve",
            selection_or_rejection_reason="best risk-adjusted score",
        )
        assert record.evaluation.maximum_loss == ev.maximum_loss  # same object, nothing duplicated/drifted


class TestSummarizeSelectionEffectiveness:
    def test_below_threshold_is_not_meaningful(self):
        outcomes = [_outcome(f"opp-{i}", StrategyKind.PUT_CREDIT_SPREAD, selected=True, pnl=100.0) for i in range(5)]
        summary = summarize_selection_effectiveness(outcomes, min_sample_size=20)
        assert summary.meaningful_sample is False
        assert summary.warning is not None

    def test_at_threshold_is_meaningful(self):
        outcomes = []
        for i in range(20):
            outcomes.append(_outcome(f"opp-{i}", StrategyKind.PUT_CREDIT_SPREAD, selected=True, pnl=100.0))
        summary = summarize_selection_effectiveness(outcomes, min_sample_size=20)
        assert summary.meaningful_sample is True
        assert summary.warning is None

    def test_positive_regret_means_alternatives_outperformed_selection(self):
        outcomes = [
            _outcome("opp-1", StrategyKind.PUT_CREDIT_SPREAD, selected=True, pnl=50.0),
            _outcome("opp-1", StrategyKind.BULL_CALL_SPREAD, selected=False, pnl=200.0),
        ]
        summary = summarize_selection_effectiveness(outcomes)
        assert summary.selection_regret > 0

    def test_negative_regret_means_selection_outperformed_alternatives(self):
        outcomes = [
            _outcome("opp-1", StrategyKind.PUT_CREDIT_SPREAD, selected=True, pnl=200.0),
            _outcome("opp-1", StrategyKind.BULL_CALL_SPREAD, selected=False, pnl=50.0),
        ]
        summary = summarize_selection_effectiveness(outcomes)
        assert summary.selection_regret < 0

    def test_no_trade_expectancy_passed_through_never_computed_here(self):
        outcomes = [_outcome("opp-1", StrategyKind.PUT_CREDIT_SPREAD, selected=True, pnl=100.0)]
        summary = summarize_selection_effectiveness(outcomes, no_trade_pnl_per_opportunity=0.0)
        assert summary.no_trade_expectancy == 0.0

    def test_sample_size_counts_opportunities_not_individual_alternative_rows(self):
        # 2 opportunities, 3 alternative rows total.
        outcomes = [
            _outcome("opp-1", StrategyKind.PUT_CREDIT_SPREAD, selected=True, pnl=100.0),
            _outcome("opp-1", StrategyKind.BULL_CALL_SPREAD, selected=False, pnl=50.0),
            _outcome("opp-2", StrategyKind.PUT_CREDIT_SPREAD, selected=True, pnl=80.0),
        ]
        summary = summarize_selection_effectiveness(outcomes)
        assert summary.sample_size == 2


class TestSummarizeByRegime:
    def test_buckets_independently_by_market_regime(self):
        outcomes = [
            _outcome("opp-1", StrategyKind.PUT_CREDIT_SPREAD, selected=True, pnl=100.0, regime="bull_trending"),
            _outcome("opp-2", StrategyKind.PUT_CREDIT_SPREAD, selected=True, pnl=-50.0, regime="bear_trending"),
        ]
        result = summarize_by_regime(outcomes, key="market_regime")
        assert "bull_trending" in result
        assert "bear_trending" in result
        assert result["bull_trending"].selected_expectancy == 100.0
        assert result["bear_trending"].selected_expectancy == -50.0

    def test_each_bucket_has_its_own_meaningful_sample_flag(self):
        outcomes = [_outcome(f"opp-{i}", StrategyKind.PUT_CREDIT_SPREAD, selected=True, pnl=100.0, regime="bull_trending") for i in range(25)]
        outcomes.append(_outcome("opp-x", StrategyKind.PUT_CREDIT_SPREAD, selected=True, pnl=100.0, regime="crisis_tail_event"))
        result = summarize_by_regime(outcomes, key="market_regime", min_sample_size=20)
        assert result["bull_trending"].meaningful_sample is True
        assert result["crisis_tail_event"].meaningful_sample is False

    def test_rejects_invalid_key(self):
        import pytest

        with pytest.raises(ValueError):
            summarize_by_regime([], key="not_a_real_key")

    def test_unspecified_regime_bucketed_honestly(self):
        outcome = CounterfactualOutcome(
            opportunity_id="opp-1", strategy_kind=StrategyKind.PUT_CREDIT_SPREAD, was_selected=True,
            hypothetical_pnl=100.0, market_regime=None, volatility_regime=None,
        )
        result = summarize_by_regime([outcome], key="market_regime")
        assert "unspecified" in result


class TestDynamicVsFixedStrategyComparison:
    def test_insufficient_sample_when_too_few_selections(self):
        outcomes = [_outcome(f"opp-{i}", StrategyKind.PUT_CREDIT_SPREAD, selected=True, pnl=100.0) for i in range(5)]
        comp = dynamic_vs_fixed_strategy_comparison(outcomes, min_sample_size=20)
        assert comp.dynamic_meaningful_sample is False
        assert comp.dynamic_outperforms_best_fixed is None

    def test_dynamic_outperforms_when_it_always_picks_whichever_candidate_actually_did_best(self):
        # Both strategies are considered every opportunity; each wins
        # big on alternating opportunities and loses small the rest of
        # the time. Dynamic selection always picks the actual winner
        # (pnl=200 every time), so its expectancy beats either
        # strategy's own always-use-me fixed-baseline average (105).
        outcomes = []
        for i in range(20):
            pcs_wins = i % 2 == 0
            outcomes.append(_outcome(f"opp-{i}", StrategyKind.PUT_CREDIT_SPREAD, selected=pcs_wins, pnl=200.0 if pcs_wins else 10.0))
            outcomes.append(_outcome(f"opp-{i}", StrategyKind.BULL_CALL_SPREAD, selected=not pcs_wins, pnl=10.0 if pcs_wins else 200.0))
        comp = dynamic_vs_fixed_strategy_comparison(outcomes, min_sample_size=20)
        assert comp.dynamic_meaningful_sample is True
        assert comp.dynamic_expectancy == pytest.approx(200.0)
        assert comp.fixed_strategy_baselines["put_credit_spread"].expectancy == pytest.approx(105.0)
        assert comp.dynamic_outperforms_best_fixed is True

    def test_dynamic_does_not_outperform_when_a_fixed_strategy_would_have_done_better(self):
        outcomes = []
        for i in range(20):
            # Dynamic selection alternates picks and does mediocre on average;
            # bull_call_spread was considered every time and always did great.
            selected_kind = StrategyKind.PUT_CREDIT_SPREAD if i % 2 == 0 else StrategyKind.BULL_CALL_SPREAD
            outcomes.append(_outcome(f"opp-{i}", selected_kind, selected=True, pnl=10.0))
            outcomes.append(_outcome(f"opp-{i}", StrategyKind.BULL_CALL_SPREAD, selected=(selected_kind == StrategyKind.BULL_CALL_SPREAD), pnl=200.0))
        comp = dynamic_vs_fixed_strategy_comparison(outcomes, min_sample_size=20)
        assert comp.best_fixed_strategy == "bull_call_spread"
        assert comp.dynamic_outperforms_best_fixed is False

    def test_fixed_baseline_only_computed_over_opportunities_the_strategy_was_actually_considered_for(self):
        outcomes = [
            _outcome("opp-1", StrategyKind.PUT_CREDIT_SPREAD, selected=True, pnl=100.0),
            _outcome("opp-1", StrategyKind.LONG_CALL, selected=False, pnl=-50.0),
        ]
        comp = dynamic_vs_fixed_strategy_comparison(outcomes, min_sample_size=1)
        assert comp.fixed_strategy_baselines["long_call"].sample_size == 1
        assert comp.fixed_strategy_baselines["long_call"].expectancy == -50.0

    def test_no_best_fixed_strategy_when_none_reach_meaningful_sample(self):
        outcomes = [_outcome("opp-1", StrategyKind.PUT_CREDIT_SPREAD, selected=True, pnl=100.0)]
        comp = dynamic_vs_fixed_strategy_comparison(outcomes, min_sample_size=20)
        assert comp.best_fixed_strategy is None
        assert comp.best_fixed_strategy_expectancy is None
