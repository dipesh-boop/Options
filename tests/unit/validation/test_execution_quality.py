from __future__ import annotations

import pytest

from src.validation.execution_quality import compute_options_execution_metrics, summarize_validation_execution_quality

from .conftest import _trade


class TestSummarizeValidationExecutionQuality:
    def test_reuses_workflows_summarize_slippage(self):
        # No slippage records -> empty summary, same shape as the
        # underlying src.workflows.execution_quality function returns.
        summary = summarize_validation_execution_quality([])
        assert summary.count == 0
        assert summary.average_slippage == 0.0


class TestOptionsExecutionMetrics:
    def test_empty_trades_returns_zeros_and_none_fill_rate(self):
        metrics = compute_options_execution_metrics([])
        assert metrics.total_closed_trades == 0
        assert metrics.fill_rate is None

    def test_classifies_close_reasons_correctly(self):
        trades = [
            _trade(position_id="a", close_reason="profit_target"),
            _trade(position_id="b", close_reason="dte_management"),
            _trade(position_id="c", close_reason="expiration_otm"),
            _trade(position_id="d", close_reason="assignment"),
            _trade(position_id="e", close_reason="exercise"),
        ]
        metrics = compute_options_execution_metrics(trades)
        assert metrics.total_closed_trades == 5
        assert metrics.early_close_count == 2
        assert metrics.assignment_count == 2
        assert metrics.expiration_otm_count == 1
        assert metrics.early_close_rate == pytest.approx(2 / 5)
        assert metrics.assignment_rate == pytest.approx(2 / 5)

    def test_fill_rate_computed_only_when_attempted_supplied(self):
        trades = [_trade(position_id="a")]
        no_attempted = compute_options_execution_metrics(trades)
        assert no_attempted.fill_rate is None

        with_attempted = compute_options_execution_metrics(trades, attempted_trades=4)
        assert with_attempted.fill_rate == pytest.approx(1 / 4)

    def test_fill_rate_zero_attempted_stays_none_not_divide_by_zero(self):
        metrics = compute_options_execution_metrics([_trade()], attempted_trades=0)
        assert metrics.fill_rate is None
