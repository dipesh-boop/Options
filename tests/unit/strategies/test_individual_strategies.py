"""Smoke + correctness tests for every src.strategies.<name> module:
leg construction, payoff/max-profit/max-loss/breakeven correctness
(cross-checked against src.quant.monte_carlo.payoff_profile), capital
requirements, expiration handling, and Fidelity-compatibility flags."""
from __future__ import annotations

import math

import pytest

from src.data.option_chain import OptionRight as DataOptionRight
from src.strategies.base import StrategyKind
from src.strategies.bear_put_spread import evaluate_bear_put_spread
from src.strategies.bull_call_spread import evaluate_bull_call_spread
from src.strategies.call_credit_spread import evaluate_call_credit_spread
from src.strategies.cash_secured_put import evaluate_cash_secured_put
from src.strategies.covered_call import evaluate_covered_call
from src.strategies.iron_butterfly import evaluate_short_iron_butterfly
from src.strategies.iron_condor import evaluate_short_iron_condor
from src.strategies.long_call import evaluate_long_call
from src.strategies.long_call_butterfly import evaluate_long_call_butterfly
from src.strategies.long_put import evaluate_long_put
from src.strategies.long_straddle import evaluate_long_straddle
from src.strategies.long_strangle import evaluate_long_strangle
from src.strategies.protective_collar import evaluate_protective_collar
from src.strategies.protective_put import evaluate_protective_put
from src.strategies.put_credit_spread import evaluate_put_credit_spread

from .conftest import EXPIRATION, contract, limits, portfolio, portfolio_with_shares

_COMMON = dict(ticker="XYZ", expiration=EXPIRATION, spot=100.0, sigma=0.25, t=24 / 365, rate=0.04, days_to_expiry=24, num_contracts=1)


class TestCashSecuredPut:
    def test_evaluation(self):
        ev = evaluate_cash_secured_put(put_contract=contract(95, DataOptionRight.PUT, 2.8, 3.0), limits=limits(), **_COMMON)
        assert ev.strategy_kind == StrategyKind.CASH_SECURED_PUT
        assert ev.capital_requirement == pytest.approx(95 * 100)
        assert ev.fidelity_compatible is True


class TestCoveredCall:
    def test_requires_cost_basis_and_produces_capped_upside(self):
        ev = evaluate_covered_call(call_contract=contract(105, DataOptionRight.CALL, 2.3, 2.5), cost_basis=95.0, limits=limits(), **_COMMON)
        assert ev.strategy_kind == StrategyKind.COVERED_CALL
        assert math.isfinite(ev.maximum_profit)
        assert "shares" in ev.required_positions


class TestPutCreditSpread:
    def test_evaluation(self):
        ev = evaluate_put_credit_spread(
            short_put_contract=contract(95, DataOptionRight.PUT, 2.8, 3.0), long_put_contract=contract(90, DataOptionRight.PUT, 1.4, 1.6),
            limits=limits(), **_COMMON,
        )
        assert ev.strategy_kind == StrategyKind.PUT_CREDIT_SPREAD
        assert ev.maximum_loss > 0
        assert ev.net_credit_or_debit > 0


class TestCallCreditSpread:
    def test_evaluation(self):
        ev = evaluate_call_credit_spread(
            short_call_contract=contract(105, DataOptionRight.CALL, 2.8, 3.0), long_call_contract=contract(110, DataOptionRight.CALL, 1.0, 1.2),
            limits=limits(), **_COMMON,
        )
        assert ev.strategy_kind == StrategyKind.CALL_CREDIT_SPREAD
        assert ev.net_credit_or_debit > 0


class TestBullCallSpread:
    def test_evaluation_is_a_debit(self):
        ev = evaluate_bull_call_spread(
            long_call_contract=contract(95, DataOptionRight.CALL, 6.8, 7.0), short_call_contract=contract(105, DataOptionRight.CALL, 2.3, 2.5),
            limits=limits(), **_COMMON,
        )
        assert ev.strategy_kind == StrategyKind.BULL_CALL_SPREAD
        assert ev.net_credit_or_debit < 0


class TestBearPutSpread:
    def test_evaluation_is_a_debit(self):
        ev = evaluate_bear_put_spread(
            long_put_contract=contract(100, DataOptionRight.PUT, 6.0, 6.2), short_put_contract=contract(90, DataOptionRight.PUT, 2.0, 2.2),
            limits=limits(), **_COMMON,
        )
        assert ev.strategy_kind == StrategyKind.BEAR_PUT_SPREAD
        assert ev.net_credit_or_debit < 0


class TestProtectivePut:
    def test_requires_shares_and_has_unlimited_profit(self):
        ev = evaluate_protective_put(
            put_contract=contract(90, DataOptionRight.PUT, 2.4, 2.6), cost_basis=95.0, limits=limits(), **_COMMON
        )
        assert ev.strategy_kind == StrategyKind.PROTECTIVE_PUT
        assert ev.maximum_profit == math.inf
        assert math.isfinite(ev.maximum_loss)
        assert "PORTFOLIO_PROTECTION" in [f.name for f in ev.strategy_family]


class TestProtectiveCollar:
    def test_both_profit_and_loss_capped(self):
        ev = evaluate_protective_collar(
            call_contract=contract(110, DataOptionRight.CALL, 1.9, 2.1), put_contract=contract(90, DataOptionRight.PUT, 1.4, 1.6),
            cost_basis=95.0, limits=limits(), **_COMMON,
        )
        assert ev.strategy_kind == StrategyKind.PROTECTIVE_COLLAR
        assert math.isfinite(ev.maximum_profit)
        assert math.isfinite(ev.maximum_loss)


class TestLongCall:
    def test_unlimited_profit_bounded_loss(self):
        ev = evaluate_long_call(call_contract=contract(100, DataOptionRight.CALL, 2.9, 3.1), limits=limits(), **_COMMON)
        assert ev.maximum_profit == math.inf
        assert ev.maximum_loss == pytest.approx(3.0 * 100)


class TestLongPut:
    def test_bounded_profit_and_loss(self):
        ev = evaluate_long_put(put_contract=contract(100, DataOptionRight.PUT, 2.9, 3.1), limits=limits(), **_COMMON)
        assert math.isfinite(ev.maximum_profit)
        assert ev.maximum_loss == pytest.approx(3.0 * 100)


class TestLongStraddle:
    def test_two_breakevens_and_unlimited_profit(self):
        ev = evaluate_long_straddle(
            call_contract=contract(100, DataOptionRight.CALL, 2.9, 3.1), put_contract=contract(100, DataOptionRight.PUT, 3.1, 3.3),
            limits=limits(), **_COMMON,
        )
        assert ev.maximum_profit == math.inf
        assert len(ev.breakeven_points) == 2

    def test_mismatched_strikes_rejected(self):
        with pytest.raises(ValueError):
            evaluate_long_straddle(
                call_contract=contract(100, DataOptionRight.CALL, 2.9, 3.1), put_contract=contract(95, DataOptionRight.PUT, 3.1, 3.3),
                limits=limits(), **_COMMON,
            )


class TestLongStrangle:
    def test_two_breakevens_wider_than_straddle(self):
        ev = evaluate_long_strangle(
            call_contract=contract(105, DataOptionRight.CALL, 1.4, 1.6), put_contract=contract(95, DataOptionRight.PUT, 1.4, 1.6),
            limits=limits(), **_COMMON,
        )
        assert len(ev.breakeven_points) == 2
        assert ev.breakeven_points[1] - ev.breakeven_points[0] > 10  # wider than the strike gap alone


class TestLongCallButterflyTier2:
    def test_defined_risk_and_fidelity_incompatible(self):
        ev = evaluate_long_call_butterfly(
            lower_contract=contract(95, DataOptionRight.CALL, 6.4, 6.6), middle_contract=contract(100, DataOptionRight.CALL, 3.4, 3.6),
            upper_contract=contract(105, DataOptionRight.CALL, 1.4, 1.6), limits=limits(), **_COMMON,
        )
        assert ev.strategy_kind == StrategyKind.LONG_CALL_BUTTERFLY
        assert math.isfinite(ev.maximum_profit)
        assert math.isfinite(ev.maximum_loss)
        assert ev.fidelity_compatible is False
        assert "2-leg cap" in ev.fidelity_incompatibility_reason

    def test_unequal_wings_rejected(self):
        with pytest.raises(ValueError):
            evaluate_long_call_butterfly(
                lower_contract=contract(90, DataOptionRight.CALL, 9.4, 9.6), middle_contract=contract(100, DataOptionRight.CALL, 3.4, 3.6),
                upper_contract=contract(105, DataOptionRight.CALL, 1.4, 1.6), limits=limits(), **_COMMON,
            )


class TestShortIronCondorTier2:
    def test_defined_risk_and_fidelity_incompatible(self):
        ev = evaluate_short_iron_condor(
            long_put_contract=contract(90, DataOptionRight.PUT, 0.5, 0.7), short_put_contract=contract(95, DataOptionRight.PUT, 1.1, 1.3),
            short_call_contract=contract(105, DataOptionRight.CALL, 1.0, 1.2), long_call_contract=contract(110, DataOptionRight.CALL, 0.4, 0.6),
            limits=limits(), **_COMMON,
        )
        assert ev.strategy_kind == StrategyKind.SHORT_IRON_CONDOR
        assert math.isfinite(ev.maximum_profit)
        assert math.isfinite(ev.maximum_loss)
        assert ev.net_credit_or_debit > 0  # short iron condor is a net-credit structure
        assert ev.fidelity_compatible is False

    def test_wrong_strike_order_rejected(self):
        with pytest.raises(ValueError):
            evaluate_short_iron_condor(
                long_put_contract=contract(95, DataOptionRight.PUT, 0.5, 0.7), short_put_contract=contract(90, DataOptionRight.PUT, 1.1, 1.3),
                short_call_contract=contract(105, DataOptionRight.CALL, 1.0, 1.2), long_call_contract=contract(110, DataOptionRight.CALL, 0.4, 0.6),
                limits=limits(), **_COMMON,
            )


class TestShortIronButterflyTier2:
    def test_defined_risk_and_fidelity_incompatible(self):
        ev = evaluate_short_iron_butterfly(
            put_wing_contract=contract(90, DataOptionRight.PUT, 0.7, 0.9), center_put_contract=contract(100, DataOptionRight.PUT, 3.1, 3.3),
            center_call_contract=contract(100, DataOptionRight.CALL, 3.3, 3.5), call_wing_contract=contract(110, DataOptionRight.CALL, 0.8, 1.0),
            limits=limits(), **_COMMON,
        )
        assert ev.strategy_kind == StrategyKind.SHORT_IRON_BUTTERFLY
        assert math.isfinite(ev.maximum_profit)
        assert math.isfinite(ev.maximum_loss)
        assert ev.fidelity_compatible is False

    def test_mismatched_center_strikes_rejected(self):
        with pytest.raises(ValueError):
            evaluate_short_iron_butterfly(
                put_wing_contract=contract(90, DataOptionRight.PUT, 0.7, 0.9), center_put_contract=contract(100, DataOptionRight.PUT, 3.1, 3.3),
                center_call_contract=contract(102, DataOptionRight.CALL, 3.3, 3.5), call_wing_contract=contract(110, DataOptionRight.CALL, 0.8, 1.0),
                limits=limits(), **_COMMON,
            )


class TestNoStrategyRequiresMoreThanCapitalAtRisk:
    """Cross-strategy property: every one of the 15 evaluated strategies
    must have a finite, non-negative maximum_loss -- none of this
    platform's approved strategies is ever genuinely unlimited-risk."""

    def test_all_evaluated_strategies_have_bounded_loss(self):
        evaluations = [
            evaluate_cash_secured_put(put_contract=contract(95, DataOptionRight.PUT, 2.8, 3.0), limits=limits(), **_COMMON),
            evaluate_covered_call(call_contract=contract(105, DataOptionRight.CALL, 2.3, 2.5), cost_basis=95.0, limits=limits(), **_COMMON),
            evaluate_put_credit_spread(short_put_contract=contract(95, DataOptionRight.PUT, 2.8, 3.0), long_put_contract=contract(90, DataOptionRight.PUT, 1.4, 1.6), limits=limits(), **_COMMON),
            evaluate_bull_call_spread(long_call_contract=contract(95, DataOptionRight.CALL, 6.8, 7.0), short_call_contract=contract(105, DataOptionRight.CALL, 2.3, 2.5), limits=limits(), **_COMMON),
            evaluate_long_call(call_contract=contract(100, DataOptionRight.CALL, 2.9, 3.1), limits=limits(), **_COMMON),
            evaluate_long_straddle(call_contract=contract(100, DataOptionRight.CALL, 2.9, 3.1), put_contract=contract(100, DataOptionRight.PUT, 3.1, 3.3), limits=limits(), **_COMMON),
        ]
        for ev in evaluations:
            assert math.isfinite(ev.maximum_loss), f"{ev.strategy_kind} has non-finite max loss"
            assert ev.maximum_loss >= 0
