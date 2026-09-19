"""Tests for SPY/risk-free benchmark comparison, including that
`spy_total_return` reuses `src.data.historical.assert_no_lookahead`
rather than re-implementing its own bias check."""
from __future__ import annotations

from datetime import date

import pytest

from src.backtest.benchmark import compare_to_benchmarks, risk_free_return, spy_total_return
from src.data.historical import HistoricalBar


def _bar(bar_date: date, close: float, symbol: str = "SPY") -> HistoricalBar:
    return HistoricalBar(
        symbol=symbol, bar_date=bar_date, open=close, high=close, low=close, close=close,
        volume=1_000_000, source="test-fixture",
    )


class TestSpyTotalReturn:
    def test_simple_close_to_close_return(self):
        bars = [_bar(date(2022, 1, 1), 400.0), _bar(date(2023, 1, 1), 440.0)]
        assert spy_total_return(bars, date(2022, 1, 1), date(2023, 1, 1)) == pytest.approx(0.10)

    def test_uses_only_bars_within_the_requested_range(self):
        bars = [
            _bar(date(2021, 1, 1), 300.0),  # before range
            _bar(date(2022, 1, 1), 400.0),
            _bar(date(2023, 1, 1), 440.0),
        ]
        assert spy_total_return(bars, date(2022, 1, 1), date(2023, 1, 1)) == pytest.approx(0.10)

    def test_raises_lookahead_violation_for_bars_after_end(self):
        bars = [_bar(date(2022, 1, 1), 400.0), _bar(date(2023, 6, 1), 500.0)]
        with pytest.raises(ValueError, match="lookahead violation"):
            spy_total_return(bars, date(2022, 1, 1), date(2023, 1, 1))

    def test_fewer_than_two_bars_in_range_raises(self):
        bars = [_bar(date(2022, 1, 1), 400.0)]
        with pytest.raises(ValueError, match="need at least two bars"):
            spy_total_return(bars, date(2022, 1, 1), date(2023, 1, 1))


class TestRiskFreeReturn:
    def test_simple_accrual_over_one_year(self):
        assert risk_free_return(0.05, date(2022, 1, 1), date(2023, 1, 1)) == pytest.approx(0.05, abs=1e-3)

    def test_compounds_over_two_years(self):
        expected = 1.05 ** 2 - 1.0
        assert risk_free_return(0.05, date(2022, 1, 1), date(2024, 1, 1)) == pytest.approx(expected, abs=1e-3)

    def test_negative_rate_rejected(self):
        with pytest.raises(ValueError):
            risk_free_return(-0.01, date(2022, 1, 1), date(2023, 1, 1))

    def test_end_before_start_rejected(self):
        with pytest.raises(ValueError):
            risk_free_return(0.05, date(2023, 1, 1), date(2022, 1, 1))


class TestCompareToBenchmarks:
    def test_computes_excess_return_over_spy_and_risk_free(self):
        bars = [_bar(date(2022, 1, 1), 400.0), _bar(date(2023, 1, 1), 440.0)]  # SPY +10%
        result = compare_to_benchmarks(
            strategy_realistic_return=0.15,
            strategy_theoretical_return=0.18,
            spy_bars=bars,
            risk_free_annual_rate=0.04,
            start=date(2022, 1, 1),
            end=date(2023, 1, 1),
        )
        assert result.spy_total_return == pytest.approx(0.10)
        assert result.excess_return_vs_spy_realistic == pytest.approx(0.05)
        assert result.excess_return_vs_risk_free_realistic == pytest.approx(0.15 - result.risk_free_return)
        assert result.strategy_theoretical_return == pytest.approx(0.18)
