from __future__ import annotations

from datetime import date

import pytest

from src.data.historical import HistoricalBar
from src.validation.benchmarks import compare_against_benchmarks


def _bars() -> list[HistoricalBar]:
    return [
        HistoricalBar(
            symbol="SPY", bar_date=date(2026, 1, 1), open=500.0, high=505.0, low=498.0, close=500.0,
            volume=1_000_000, source="test",
        ),
        HistoricalBar(
            symbol="SPY", bar_date=date(2026, 4, 1), open=515.0, high=520.0, low=510.0, close=515.0,
            volume=1_000_000, source="test",
        ),
    ]


class TestCompareAgainstBenchmarks:
    def test_all_four_figures_present(self):
        result = compare_against_benchmarks(
            strategy_return=0.05, spy_bars=_bars(), risk_free_annual_rate=0.04,
            start=date(2026, 1, 1), end=date(2026, 4, 1),
        )
        assert result.strategy_return == 0.05
        assert result.spy_return == pytest.approx(515.0 / 500.0 - 1.0)
        assert result.cash_return == 0.0
        assert result.risk_free_return > 0

    def test_excess_figures_are_correctly_signed(self):
        result = compare_against_benchmarks(
            strategy_return=0.05, spy_bars=_bars(), risk_free_annual_rate=0.04,
            start=date(2026, 1, 1), end=date(2026, 4, 1),
        )
        assert result.excess_vs_spy == pytest.approx(result.strategy_return - result.spy_return)
        assert result.excess_vs_risk_free == pytest.approx(result.strategy_return - result.risk_free_return)
        assert result.excess_vs_cash == pytest.approx(result.strategy_return)  # cash_return is 0

    def test_negative_strategy_return_still_produces_a_cash_comparison(self):
        result = compare_against_benchmarks(
            strategy_return=-0.02, spy_bars=_bars(), risk_free_annual_rate=0.04,
            start=date(2026, 1, 1), end=date(2026, 4, 1),
        )
        assert result.excess_vs_cash == pytest.approx(-0.02)
