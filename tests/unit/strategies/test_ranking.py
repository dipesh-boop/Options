"""Tests for src.strategies.ranking, split out of comparison.py per Step
14B so the ranking rule is independently testable from the metrics
table. TestDoesNotOptimizeForMaximumProfit in
test_comparison_portfolio_fit_selector.py already covers the worked
example end to end through the selector; these tests exercise
ranking.py directly."""
from __future__ import annotations

from src.data.option_chain import OptionRight as DataOptionRight
from src.strategies.bull_call_spread import evaluate_bull_call_spread
from src.strategies.comparison import rank_candidates as comparison_rank_candidates
from src.strategies.comparison import risk_adjusted_score as comparison_risk_adjusted_score
from src.strategies.put_credit_spread import evaluate_put_credit_spread
from src.strategies.ranking import rank_candidates, risk_adjusted_score

from .conftest import EXPIRATION, contract, limits

_COMMON = dict(ticker="XYZ", expiration=EXPIRATION, spot=100.0, sigma=0.25, t=24 / 365, rate=0.04, days_to_expiry=24, num_contracts=1)


def _pcs():
    return evaluate_put_credit_spread(
        short_put_contract=contract(95, DataOptionRight.PUT, 2.8, 3.0), long_put_contract=contract(90, DataOptionRight.PUT, 1.4, 1.6),
        limits=limits(), **_COMMON,
    )


def _bcs():
    return evaluate_bull_call_spread(
        long_call_contract=contract(95, DataOptionRight.CALL, 6.8, 7.0), short_call_contract=contract(105, DataOptionRight.CALL, 2.3, 2.5),
        limits=limits(), **_COMMON,
    )


class TestRiskAdjustedScore:
    def test_is_expected_value_over_maximum_loss(self):
        ev = _pcs()
        assert risk_adjusted_score(ev) == ev.expected_value / ev.maximum_loss

    def test_zero_or_negative_max_loss_falls_back_to_raw_ev(self):
        ev = _pcs()
        zero_loss = ev.__class__(**{**ev.__dict__, "maximum_loss": 0.0})
        assert risk_adjusted_score(zero_loss) == zero_loss.expected_value


class TestRankCandidates:
    def test_sorted_highest_score_first(self):
        candidates = [_pcs(), _bcs()]
        ranked = rank_candidates(candidates)
        scores = [risk_adjusted_score(c) for c in ranked]
        assert scores == sorted(scores, reverse=True)

    def test_never_mutates_input_order_in_place(self):
        candidates = [_pcs(), _bcs()]
        original_ids = [id(c) for c in candidates]
        rank_candidates(candidates)
        assert [id(c) for c in candidates] == original_ids


class TestComparisonModuleReExportsIdenticalFunctions:
    """comparison.py re-exports ranking.py's functions for backward
    compatibility (selector.py itself now imports directly from
    ranking.py) -- both names must resolve to the exact same function
    object, never a second, possibly-drifting implementation."""

    def test_rank_candidates_is_the_same_function(self):
        assert comparison_rank_candidates is rank_candidates

    def test_risk_adjusted_score_is_the_same_function(self):
        assert comparison_risk_adjusted_score is risk_adjusted_score
