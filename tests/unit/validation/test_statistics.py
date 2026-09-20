from __future__ import annotations

from datetime import date

import pytest

from src.validation.statistics import (
    annualize_return,
    bootstrap_confidence_interval,
    conditional_value_at_risk,
    label_for_sample,
    labeled_from_sample,
    monte_carlo_forward_projection,
    value_at_risk,
)


class TestConfidenceLabeling:
    def test_at_or_above_minimum_is_observed(self):
        assert label_for_sample(50, minimum=50) == "OBSERVED"
        assert label_for_sample(100, minimum=50) == "OBSERVED"

    def test_below_minimum_is_estimated(self):
        assert label_for_sample(49, minimum=50) == "ESTIMATED"
        assert label_for_sample(0, minimum=50) == "ESTIMATED"

    def test_labeled_from_sample_carries_basis_text(self):
        lv = labeled_from_sample(0.12, 30, minimum=50, description="win rate")
        assert lv.value == 0.12
        assert lv.label == "ESTIMATED"
        assert "n=30" in lv.basis

    def test_annualize_return_is_always_projected(self):
        lv = annualize_return(0.05, period_days=90)
        assert lv.label == "PROJECTED"
        # (1.05)^(365/90) - 1
        assert lv.value == pytest.approx((1.05) ** (365 / 90) - 1.0)

    def test_annualize_return_rejects_nonpositive_days(self):
        with pytest.raises(ValueError):
            annualize_return(0.05, period_days=0)


class TestBootstrapConfidenceInterval:
    def test_empty_values_returns_zeros(self):
        assert bootstrap_confidence_interval([], iterations=100, confidence_pct=0.9, seed=1) == (0.0, 0.0, 0.0)

    def test_single_value_returns_degenerate_interval(self):
        result = bootstrap_confidence_interval([42.0], iterations=100, confidence_pct=0.9, seed=1)
        assert result == (42.0, 42.0, 42.0)

    def test_point_estimate_is_the_sample_mean(self):
        values = [10.0, 20.0, 30.0, -5.0, 15.0]
        point, lower, upper = bootstrap_confidence_interval(values, iterations=500, confidence_pct=0.9, seed=7)
        assert point == pytest.approx(sum(values) / len(values))
        assert lower <= point <= upper

    def test_deterministic_given_same_seed(self):
        values = [10.0, -5.0, 8.0, 12.0, -2.0, 6.0]
        r1 = bootstrap_confidence_interval(values, iterations=300, confidence_pct=0.9, seed=99)
        r2 = bootstrap_confidence_interval(values, iterations=300, confidence_pct=0.9, seed=99)
        assert r1 == r2

    def test_interval_narrows_as_confidence_decreases(self):
        values = [10.0, -30.0, 25.0, -15.0, 5.0, 40.0, -20.0]
        _, lower90, upper90 = bootstrap_confidence_interval(values, iterations=2000, confidence_pct=0.90, seed=3)
        _, lower50, upper50 = bootstrap_confidence_interval(values, iterations=2000, confidence_pct=0.50, seed=3)
        assert (upper50 - lower50) <= (upper90 - lower90)


class TestMonteCarloForwardProjection:
    def test_no_trade_history_returns_starting_nav_unchanged(self):
        result = monte_carlo_forward_projection(
            [], starting_nav=100_000.0, num_future_trades=50, iterations=500, seed=1
        )
        assert result.median_terminal_nav == 100_000.0
        assert result.basis_trade_count == 0

    def test_all_positive_pnls_never_projects_a_loss(self):
        result = monte_carlo_forward_projection(
            [100.0, 200.0, 150.0], starting_nav=100_000.0, num_future_trades=20, iterations=500, seed=5,
        )
        assert result.probability_of_loss == 0.0
        assert result.median_terminal_nav > 100_000.0

    def test_all_negative_pnls_always_projects_a_loss(self):
        result = monte_carlo_forward_projection(
            [-100.0, -200.0, -150.0], starting_nav=100_000.0, num_future_trades=20, iterations=500, seed=5,
        )
        assert result.probability_of_loss == 1.0
        assert result.median_terminal_nav < 100_000.0

    def test_p5_never_exceeds_p95(self):
        result = monte_carlo_forward_projection(
            [-500.0, 300.0, -100.0, 900.0, -50.0], starting_nav=100_000.0, num_future_trades=30, iterations=1000, seed=11,
        )
        assert result.p5_terminal_nav <= result.median_terminal_nav <= result.p95_terminal_nav

    def test_rejects_nonpositive_starting_nav(self):
        with pytest.raises(ValueError):
            monte_carlo_forward_projection([1.0], starting_nav=0.0, num_future_trades=10, iterations=10, seed=1)

    def test_deterministic_given_same_seed(self):
        pnls = [50.0, -30.0, 20.0, -10.0]
        r1 = monte_carlo_forward_projection(pnls, starting_nav=100_000.0, num_future_trades=40, iterations=300, seed=77)
        r2 = monte_carlo_forward_projection(pnls, starting_nav=100_000.0, num_future_trades=40, iterations=300, seed=77)
        assert r1 == r2


class TestVarCvar:
    def test_var_and_cvar_reuse_backtest_metrics(self):
        curve = [(date(2026, 1, 1), 100.0), (date(2026, 1, 2), 95.0), (date(2026, 1, 3), 90.0), (date(2026, 1, 4), 110.0)]
        var95 = value_at_risk(curve, confidence_pct=0.95)
        cvar95 = conditional_value_at_risk(curve, confidence_pct=0.95)
        assert var95 >= 0.0
        assert cvar95 >= var95
