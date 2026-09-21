from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.llm.context import MarketContext
from src.strategies.volatility_engine import (
    compare_iv_to_rv,
    premium_compensates_for_tail_risk,
    straddle_required_move_comparison,
    strangle_required_move_comparison,
)

NOW = datetime(2026, 9, 22, tzinfo=timezone.utc)


class TestCompareIvToRv:
    def test_positive_spread_when_iv_exceeds_rv(self):
        market = MarketContext(as_of=NOW, vix_level=18.0, realized_volatility=0.15)
        comp = compare_iv_to_rv(implied_volatility=0.22, market=market)
        assert comp.iv_rv_spread == pytest.approx(0.07)

    def test_none_spread_when_realized_vol_unavailable(self):
        market = MarketContext(as_of=NOW, vix_level=18.0)
        comp = compare_iv_to_rv(implied_volatility=0.22, market=market)
        assert comp.iv_rv_spread is None

    def test_term_structure_slope(self):
        market = MarketContext(as_of=NOW, vix_level=18.0, volatility_term_structure=((30, 0.20), (60, 0.24), (90, 0.26)))
        comp = compare_iv_to_rv(implied_volatility=0.20, market=market)
        assert comp.term_structure_slope == pytest.approx(0.06)

    def test_rejects_nonpositive_iv(self):
        with pytest.raises(ValueError):
            compare_iv_to_rv(implied_volatility=0.0, market=MarketContext(as_of=NOW, vix_level=18.0))

    def test_never_invents_iv_percentile_or_rank(self):
        market = MarketContext(as_of=NOW, vix_level=18.0)  # no iv_percentile/iv_rank supplied
        comp = compare_iv_to_rv(implied_volatility=0.20, market=market)
        assert comp.iv_percentile is None
        assert comp.iv_rank is None


class TestRequiredMoveComparisons:
    def test_straddle_required_move_flags_exceeding_implied_expected_move(self):
        comp = straddle_required_move_comparison(
            spot=100, strike=100, call_premium=3.0, put_premium=3.2, implied_expected_move_pct=0.04,
        )
        assert comp.required_move_pct == pytest.approx(0.062)
        assert comp.required_move_exceeds_implied_expected_move is True

    def test_straddle_required_move_not_exceeding(self):
        comp = straddle_required_move_comparison(
            spot=100, strike=100, call_premium=3.0, put_premium=3.2, implied_expected_move_pct=0.10,
        )
        assert comp.required_move_exceeds_implied_expected_move is False

    def test_none_comparison_when_no_reference_supplied(self):
        comp = straddle_required_move_comparison(spot=100, strike=100, call_premium=3.0, put_premium=3.2)
        assert comp.required_move_exceeds_implied_expected_move is None
        assert comp.required_move_exceeds_historical_move is None

    def test_strangle_required_move(self):
        comp = strangle_required_move_comparison(
            spot=100, call_strike=105, put_strike=95, call_premium=1.5, put_premium=1.5, historical_move_pct=0.10,
        )
        assert comp.required_move_pct > 0
        assert comp.required_move_exceeds_historical_move is False


class TestPremiumCompensatesForTailRisk:
    def test_ratio_below_one_when_credit_is_small_relative_to_shortfall(self):
        ratio = premium_compensates_for_tail_risk(credit_received=100.0, expected_shortfall=-800.0)
        assert ratio == pytest.approx(0.125)

    def test_ratio_above_one_when_credit_is_large_relative_to_shortfall(self):
        ratio = premium_compensates_for_tail_risk(credit_received=900.0, expected_shortfall=-800.0)
        assert ratio > 1.0

    def test_rejects_nonnegative_expected_shortfall(self):
        with pytest.raises(ValueError):
            premium_compensates_for_tail_risk(credit_received=100.0, expected_shortfall=50.0)
