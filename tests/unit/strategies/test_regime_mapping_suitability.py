from __future__ import annotations

from src.strategies.base import StrategyKind
from src.strategies.regime_mapping import CANDIDATE_STRATEGIES_BY_VIEW, MarketView, candidate_strategies_for
from src.strategies.suitability import filter_suitable_strategies, is_strategy_suitable, shares_held

from .conftest import portfolio, portfolio_with_shares


class TestRegimeMappingNeverOneToOne:
    def test_every_view_maps_to_multiple_candidates(self):
        for view in MarketView:
            candidates = candidate_strategies_for(view)
            assert len(candidates) >= 2, f"{view} mapped to fewer than 2 candidates"

    def test_all_8_views_covered(self):
        assert len(CANDIDATE_STRATEGIES_BY_VIEW) == 8

    def test_moderately_bullish_matches_step_19a_example(self):
        candidates = candidate_strategies_for(MarketView.MODERATELY_BULLISH)
        for expected in (
            StrategyKind.COVERED_CALL, StrategyKind.CASH_SECURED_PUT, StrategyKind.BULL_CALL_SPREAD,
            StrategyKind.PUT_CREDIT_SPREAD, StrategyKind.LONG_CALL,
        ):
            assert expected in candidates

    def test_large_move_expected_maps_to_straddle_and_strangle(self):
        candidates = candidate_strategies_for(MarketView.LARGE_MOVE_EXPECTED)
        assert StrategyKind.LONG_STRADDLE in candidates
        assert StrategyKind.LONG_STRANGLE in candidates

    def test_portfolio_protection_maps_to_put_and_collar(self):
        candidates = candidate_strategies_for(MarketView.PORTFOLIO_PROTECTION)
        assert StrategyKind.PROTECTIVE_PUT in candidates
        assert StrategyKind.PROTECTIVE_COLLAR in candidates


class TestSuitability:
    def test_covered_call_unsuitable_without_shares(self):
        result = is_strategy_suitable(StrategyKind.COVERED_CALL, ticker="XYZ", portfolio=portfolio(), contracts=1)
        assert result.suitable is False

    def test_covered_call_suitable_with_enough_shares(self):
        result = is_strategy_suitable(StrategyKind.COVERED_CALL, ticker="XYZ", portfolio=portfolio_with_shares(shares=100), contracts=1)
        assert result.suitable is True

    def test_covered_call_unsuitable_with_insufficient_shares_for_contracts_requested(self):
        result = is_strategy_suitable(StrategyKind.COVERED_CALL, ticker="XYZ", portfolio=portfolio_with_shares(shares=100), contracts=2)
        assert result.suitable is False

    def test_cash_secured_put_always_suitable_regardless_of_shares(self):
        result = is_strategy_suitable(StrategyKind.CASH_SECURED_PUT, ticker="XYZ", portfolio=portfolio(), contracts=1)
        assert result.suitable is True

    def test_a_bare_short_call_is_never_offered_as_covered_call_without_shares(self):
        """The structural guarantee behind 'do not incorrectly classify
        a short call as a Covered Call': filter_suitable_strategies
        removes COVERED_CALL entirely from a candidate list when no
        shares are held."""
        candidates = (StrategyKind.COVERED_CALL, StrategyKind.CASH_SECURED_PUT)
        filtered = filter_suitable_strategies(candidates, ticker="XYZ", portfolio=portfolio(), contracts=1)
        assert StrategyKind.COVERED_CALL not in filtered
        assert StrategyKind.CASH_SECURED_PUT in filtered

    def test_protective_put_and_collar_also_require_shares(self):
        for kind in (StrategyKind.PROTECTIVE_PUT, StrategyKind.PROTECTIVE_COLLAR):
            assert is_strategy_suitable(kind, ticker="XYZ", portfolio=portfolio(), contracts=1).suitable is False
            assert is_strategy_suitable(kind, ticker="XYZ", portfolio=portfolio_with_shares(), contracts=1).suitable is True

    def test_shares_held_reads_from_portfolio(self):
        assert shares_held(portfolio(), "XYZ") == 0
        assert shares_held(portfolio_with_shares(shares=250), "XYZ") == 250
