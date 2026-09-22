from __future__ import annotations

import math

import pytest

from src.quant.black_scholes import Leg, OptionRight, Side
from src.quant.monte_carlo import Position
from src.strategies.base import (
    MANAGEMENT_CONDITION_TYPES,
    STRATEGY_FAMILIES,
    TRADE_PROPOSAL_ELIGIBLE,
    ManagementConditionType,
    StrategyEvaluation,
    StrategyFamily,
    StrategyKind,
    build_strategy_evaluation,
    strategy_type_for,
)

from .conftest import EXPIRATION, contract, limits

from src.data.option_chain import OptionRight as DataOptionRight


class TestStrategyKindCompleteness:
    def test_16_position_producing_strategy_kinds_defined(self):
        """16, not 17: item #17 in the platform's own library list is
        "CASH / NO_TRADE", deliberately NOT a StrategyKind member -- it
        has no legs to price and is represented instead as
        `selected=None` in `src.strategies.selector.SelectionOutcome`, a
        first-class competitor rather than a strategy structure. The
        16th real StrategyKind member is Step 22.2's WHEEL -- like the
        pre-Step-20A LONG_CALL_BUTTERFLY/SHORT_IRON_CONDOR/
        SHORT_IRON_BUTTERFLY trio, it is a permanent, structural member of
        this enum without ever itself becoming a `TradeProposal.strategy`
        value (see `test_15_of_16_kinds_are_trade_proposal_eligible`)."""
        assert len(list(StrategyKind)) == 16

    def test_every_kind_has_a_family_classification(self):
        for kind in StrategyKind:
            assert kind in STRATEGY_FAMILIES
            assert len(STRATEGY_FAMILIES[kind]) >= 1

    def test_15_of_16_kinds_are_trade_proposal_eligible(self):
        """Step 20A reversed the Step 19A Tier1/Tier2 split for the
        original 15 members: `TradeProposal.legs` now caps at 4, and
        `StrategyType` gained the 3 multi-leg members, so every
        StrategyKind from before Step 22.2 is representable as a real
        `TradeProposal`. Step 22.2's WHEEL is a deliberate, permanent
        exception -- it is never itself submitted as an order (every
        order it places is an ordinary CASH_SECURED_PUT/COVERED_CALL
        TradeProposal instead, see src/wheel/__init__.py), so it is
        correctly absent from TRADE_PROPOSAL_ELIGIBLE."""
        eligible = {k.value for k in TRADE_PROPOSAL_ELIGIBLE}
        assert "wheel" not in eligible
        for name in (
            "cash_secured_put", "covered_call", "put_credit_spread", "call_credit_spread", "bull_call_spread",
            "bear_put_spread", "protective_put", "protective_collar", "long_straddle", "long_strangle",
            "long_call", "long_put", "long_call_butterfly", "short_iron_condor", "short_iron_butterfly",
        ):
            assert name in eligible
        assert len(eligible) == 15

    def test_strategy_type_for_returns_matching_type_for_former_tier2(self):
        from src.llm.schemas import StrategyType

        assert strategy_type_for(StrategyKind.SHORT_IRON_CONDOR) == StrategyType.SHORT_IRON_CONDOR
        assert strategy_type_for(StrategyKind.LONG_CALL_BUTTERFLY) == StrategyType.LONG_CALL_BUTTERFLY
        assert strategy_type_for(StrategyKind.SHORT_IRON_BUTTERFLY) == StrategyType.SHORT_IRON_BUTTERFLY

    def test_strategy_type_for_returns_matching_type_for_tier1(self):
        from src.llm.schemas import StrategyType

        assert strategy_type_for(StrategyKind.BULL_CALL_SPREAD) == StrategyType.BULL_CALL_SPREAD


class TestNeverIntroducesExcludedStrategies:
    def test_no_kind_is_named_naked_or_unfunded(self):
        for kind in StrategyKind:
            assert "naked" not in kind.value
            assert "unfunded" not in kind.value


class TestManagementConditionTypes:
    """The closed, Python-determined set of management-condition
    categories -- "LLMs may interpret conditions, may NOT improvise
    risk rules." Distinct from entry_rules/exit_rules/etc (free-text
    prose, unchanged) -- this is the enum those rules are drawn from."""

    def test_9_condition_types_defined(self):
        assert len(list(ManagementConditionType)) == 9

    def test_every_kind_has_a_mapping_with_the_universal_baseline(self):
        baseline = {
            ManagementConditionType.PROFIT_TARGET, ManagementConditionType.MAX_LOSS,
            ManagementConditionType.DTE_EXIT, ManagementConditionType.THESIS_INVALIDATION,
            ManagementConditionType.EXPIRATION_MANAGEMENT,
        }
        for kind in StrategyKind:
            assert kind in MANAGEMENT_CONDITION_TYPES
            assert baseline.issubset(set(MANAGEMENT_CONDITION_TYPES[kind]))

    def test_pure_long_premium_strategies_carry_no_assignment_management(self):
        # A long call/put/straddle/strangle has no short leg -- nothing
        # for the holder to be assigned on.
        for kind in (
            StrategyKind.LONG_CALL, StrategyKind.LONG_PUT, StrategyKind.LONG_STRADDLE, StrategyKind.LONG_STRANGLE,
        ):
            assert ManagementConditionType.ASSIGNMENT_MANAGEMENT not in MANAGEMENT_CONDITION_TYPES[kind]

    def test_short_leg_strategies_carry_assignment_management(self):
        for kind in (
            StrategyKind.CASH_SECURED_PUT, StrategyKind.COVERED_CALL, StrategyKind.PUT_CREDIT_SPREAD,
            StrategyKind.CALL_CREDIT_SPREAD, StrategyKind.SHORT_IRON_CONDOR, StrategyKind.SHORT_IRON_BUTTERFLY,
        ):
            assert ManagementConditionType.ASSIGNMENT_MANAGEMENT in MANAGEMENT_CONDITION_TYPES[kind]

    def test_no_type_is_invented_outside_the_enum(self):
        for kind in StrategyKind:
            for t in MANAGEMENT_CONDITION_TYPES[kind]:
                assert isinstance(t, ManagementConditionType)


class TestBuildStrategyEvaluation:
    def _bull_call_spread_position(self) -> Position:
        return Position(legs=[
            Leg(right=OptionRight.CALL, strike=95, side=Side.BUY, entry_price=6.9, quantity=1),
            Leg(right=OptionRight.CALL, strike=105, side=Side.SELL, entry_price=2.4, quantity=1),
        ])

    def test_assembles_every_contract_field(self):
        c1 = contract(95, DataOptionRight.CALL, 6.8, 7.0)
        c2 = contract(105, DataOptionRight.CALL, 2.3, 2.5)
        ev = build_strategy_evaluation(
            kind=StrategyKind.BULL_CALL_SPREAD, ticker="XYZ", expiration=EXPIRATION,
            position=self._bull_call_spread_position(), contracts=[c1, c2], limits=limits(),
            spot=100.0, sigma=0.25, t=24 / 365, rate=0.04, days_to_expiry=24, num_contracts=1,
            market_outlook="moderately_bullish", volatility_outlook="neutral", required_positions="none",
            capital_requirement=450.0, buying_power_requirement=450.0,
            entry_rules=("enter",), exit_rules=("exit",), adjustment_rules=("adjust",), invalidation_rules=("invalidate",),
        )
        assert ev.strategy_kind == StrategyKind.BULL_CALL_SPREAD
        assert ev.strategy_family == STRATEGY_FAMILIES[StrategyKind.BULL_CALL_SPREAD]
        assert ev.number_of_legs == 2
        assert ev.execution_complexity == "two_leg"
        assert math.isfinite(ev.maximum_profit)
        assert math.isfinite(ev.maximum_loss)
        assert len(ev.breakeven_points) == 1
        assert ev.probability_metrics.probability_of_profit > 0
        assert ev.fidelity_compatible is True
        assert ev.fidelity_incompatibility_reason is None
        assert ev.management_condition_types == MANAGEMENT_CONDITION_TYPES[StrategyKind.BULL_CALL_SPREAD]

    def test_payoff_at_uses_the_stored_position(self):
        ev = build_strategy_evaluation(
            kind=StrategyKind.BULL_CALL_SPREAD, ticker="XYZ", expiration=EXPIRATION,
            position=self._bull_call_spread_position(),
            contracts=[contract(95, DataOptionRight.CALL, 6.8, 7.0), contract(105, DataOptionRight.CALL, 2.3, 2.5)],
            limits=limits(), spot=100.0, sigma=0.25, t=24 / 365, rate=0.04, days_to_expiry=24, num_contracts=1,
            market_outlook="moderately_bullish", volatility_outlook="neutral", required_positions="none",
            capital_requirement=450.0, buying_power_requirement=450.0,
            entry_rules=(), exit_rules=(), adjustment_rules=(), invalidation_rules=(),
        )
        assert ev.payoff_at(110) == pytest.approx(ev.maximum_profit)

    def test_liquidity_score_is_bounded(self):
        ev = build_strategy_evaluation(
            kind=StrategyKind.BULL_CALL_SPREAD, ticker="XYZ", expiration=EXPIRATION,
            position=self._bull_call_spread_position(),
            contracts=[contract(95, DataOptionRight.CALL, 6.8, 7.0), contract(105, DataOptionRight.CALL, 2.3, 2.5)],
            limits=limits(), spot=100.0, sigma=0.25, t=24 / 365, rate=0.04, days_to_expiry=24, num_contracts=1,
            market_outlook="moderately_bullish", volatility_outlook="neutral", required_positions="none",
            capital_requirement=450.0, buying_power_requirement=450.0,
            entry_rules=(), exit_rules=(), adjustment_rules=(), invalidation_rules=(),
        )
        assert 0.0 <= ev.liquidity_score <= 1.0

    def test_assignment_risk_none_for_pure_long_strategy(self):
        pos = Position(legs=[Leg(right=OptionRight.CALL, strike=100, side=Side.BUY, entry_price=3.0, quantity=1)])
        ev = build_strategy_evaluation(
            kind=StrategyKind.LONG_CALL, ticker="XYZ", expiration=EXPIRATION, position=pos,
            contracts=[contract(100, DataOptionRight.CALL, 2.9, 3.1)], limits=limits(),
            spot=100.0, sigma=0.25, t=24 / 365, rate=0.04, days_to_expiry=24, num_contracts=1,
            market_outlook="bullish", volatility_outlook="neutral", required_positions="none",
            capital_requirement=300.0, buying_power_requirement=300.0,
            entry_rules=(), exit_rules=(), adjustment_rules=(), invalidation_rules=(),
        )
        assert ev.assignment_risk == "none"
        assert ev.early_exercise_risk == "none"

    def test_assignment_risk_high_when_short_leg_itm(self):
        pos = Position(legs=[Leg(right=OptionRight.PUT, strike=110, side=Side.SELL, entry_price=12.0, quantity=1)])
        ev = build_strategy_evaluation(
            kind=StrategyKind.CASH_SECURED_PUT, ticker="XYZ", expiration=EXPIRATION, position=pos,
            contracts=[contract(110, DataOptionRight.PUT, 11.8, 12.2)], limits=limits(),
            spot=100.0, sigma=0.25, t=24 / 365, rate=0.04, days_to_expiry=24, num_contracts=1,
            market_outlook="moderately_bullish", volatility_outlook="neutral", required_positions="none",
            capital_requirement=11_000.0, buying_power_requirement=11_000.0,
            entry_rules=(), exit_rules=(), adjustment_rules=(), invalidation_rules=(),
        )
        assert ev.assignment_risk == "high"

    def test_fidelity_incompatible_flag_propagates(self):
        pos = Position(legs=[
            Leg(right=OptionRight.CALL, strike=95, side=Side.BUY, entry_price=6.5, quantity=1),
            Leg(right=OptionRight.CALL, strike=100, side=Side.SELL, entry_price=3.5, quantity=2),
            Leg(right=OptionRight.CALL, strike=105, side=Side.BUY, entry_price=1.5, quantity=1),
        ])
        ev = build_strategy_evaluation(
            kind=StrategyKind.LONG_CALL_BUTTERFLY, ticker="XYZ", expiration=EXPIRATION, position=pos,
            contracts=[contract(95, DataOptionRight.CALL, 6.4, 6.6), contract(100, DataOptionRight.CALL, 3.4, 3.6), contract(105, DataOptionRight.CALL, 1.4, 1.6)],
            limits=limits(), spot=100.0, sigma=0.25, t=24 / 365, rate=0.04, days_to_expiry=24, num_contracts=1,
            market_outlook="neutral", volatility_outlook="contraction", required_positions="none",
            capital_requirement=100.0, buying_power_requirement=100.0,
            entry_rules=(), exit_rules=(), adjustment_rules=(), invalidation_rules=(),
            fidelity_compatible=False, fidelity_incompatibility_reason="4-leg",
        )
        assert ev.fidelity_compatible is False
        assert ev.fidelity_incompatibility_reason == "4-leg"
