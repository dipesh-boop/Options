from __future__ import annotations

import pytest

from src.research.performance_breakdown import ResearchTradeObservation, TradeContext
from src.validation.regime_analysis import (
    ALL_VALIDATION_REGIMES,
    classify_regime,
    regime_breakdown,
    regime_coverage_summary,
    tag_observation_with_regime,
)

from .conftest import _trade


def _context(**overrides) -> TradeContext:
    defaults = dict(
        entry_delta=-0.20, entry_dte=30, iv_percentile=50.0, market_regime="normal",
        sector="Technology", profit_target_pct=0.5, management_dte=21,
    )
    defaults.update(overrides)
    return TradeContext(**defaults)


class TestClassifyRegime:
    def test_crisis_dominates_regardless_of_trend(self):
        assert classify_regime(spy_trailing_return=0.10, vix_level=35.0) == "crisis_tail_event"
        assert classify_regime(spy_trailing_return=-0.10, vix_level=35.0) == "crisis_tail_event"

    def test_bull_trending(self):
        assert classify_regime(spy_trailing_return=0.05, vix_level=15.0) == "bull_trending"

    def test_bear_trending(self):
        assert classify_regime(spy_trailing_return=-0.05, vix_level=15.0) == "bear_trending"

    def test_range_bound_low_vol(self):
        assert classify_regime(spy_trailing_return=0.005, vix_level=15.0) == "range_bound_low_vol"

    def test_range_bound_high_vol(self):
        assert classify_regime(spy_trailing_return=-0.005, vix_level=25.0) == "range_bound_high_vol"

    def test_negative_vix_rejected(self):
        with pytest.raises(ValueError):
            classify_regime(spy_trailing_return=0.0, vix_level=-1.0)

    def test_every_classify_result_is_a_valid_regime(self):
        for spy_ret in (-0.10, -0.03, -0.005, 0.0, 0.005, 0.03, 0.10):
            for vix in (5.0, 15.0, 22.0, 30.0, 45.0):
                assert classify_regime(spy_trailing_return=spy_ret, vix_level=vix) in ALL_VALIDATION_REGIMES


class TestTagObservationWithRegime:
    def test_returns_new_context_without_mutating_original(self):
        original = _context(market_regime="normal")
        tagged = tag_observation_with_regime(original, "bull_trending")
        assert tagged.market_regime == "bull_trending"
        assert original.market_regime == "normal"
        assert tagged.entry_delta == original.entry_delta


class TestRegimeBreakdownAndCoverage:
    def _observations(self) -> list[ResearchTradeObservation]:
        return [
            ResearchTradeObservation(trade=_trade(position_id="a", realistic_pnl=100.0), context=_context(market_regime="bull_trending")),
            ResearchTradeObservation(trade=_trade(position_id="b", realistic_pnl=-50.0), context=_context(market_regime="bull_trending")),
            ResearchTradeObservation(trade=_trade(position_id="c", realistic_pnl=200.0), context=_context(market_regime="crisis_tail_event")),
        ]

    def test_regime_breakdown_reuses_performance_breakdown(self):
        report = regime_breakdown(self._observations())
        assert report.dimension == "market_regime"
        assert report.total_trades == 3

    def test_coverage_summary_lists_observed_and_missing_regimes(self):
        coverage = regime_coverage_summary(self._observations())
        assert "bull_trending" in coverage.regimes_observed
        assert "crisis_tail_event" in coverage.regimes_observed
        assert "bear_trending" in coverage.regimes_not_observed
        assert coverage.trade_count_by_regime["bull_trending"] == 2

    def test_single_regime_dominant_flag(self):
        observations = [
            ResearchTradeObservation(trade=_trade(position_id=str(i)), context=_context(market_regime="bull_trending"))
            for i in range(9)
        ] + [ResearchTradeObservation(trade=_trade(position_id="x"), context=_context(market_regime="bear_trending"))]
        coverage = regime_coverage_summary(observations)
        assert coverage.single_regime_dominant is True

    def test_not_dominant_when_evenly_spread(self):
        observations = self._observations()  # 2 bull, 1 crisis out of 3 -> 66%, not >80%
        coverage = regime_coverage_summary(observations)
        assert coverage.single_regime_dominant is False

    def test_empty_observations_no_crash(self):
        coverage = regime_coverage_summary([])
        assert coverage.regimes_observed == ()
        assert coverage.single_regime_dominant is False
