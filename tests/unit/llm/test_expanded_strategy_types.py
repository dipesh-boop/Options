"""Step 19A: leg-shape validators for the 9 new StrategyType members.
Mirrors the existing test_trade_proposal.py fixture/assertion style."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.llm.schemas import Conviction, LegSide, OptionLeg, OptionRight, StrategyType, TradeAction, TradeDirection, TradeProposal

NOW = datetime(2026, 9, 22, 14, 0, tzinfo=timezone.utc)
DATA_TS = NOW - timedelta(minutes=5)
EXP = date(2026, 10, 16)


def _proposal(**overrides) -> TradeProposal:
    base = dict(
        proposal_id="p-1", timestamp=NOW, ticker="XYZ", strategy=StrategyType.LONG_CALL, market_regime="normal",
        expiration=EXP, legs=[OptionLeg(right=OptionRight.CALL, strike=100, side=LegSide.BUY)],
        direction=TradeDirection.BULLISH, contracts_requested=1, target_entry=3.0, profit_target=0.5,
        management_dte=15, thesis="t", risk_thesis="r", confidence=Conviction.MEDIUM, data_sources=["test"],
        data_timestamp=DATA_TS, invalidation_conditions=["invalidated"],
    )
    base.update(overrides)
    return TradeProposal(**base)


class TestCallCreditSpread:
    def test_valid(self):
        p = _proposal(strategy=StrategyType.CALL_CREDIT_SPREAD, legs=[
            OptionLeg(right=OptionRight.CALL, strike=100, side=LegSide.SELL),
            OptionLeg(right=OptionRight.CALL, strike=110, side=LegSide.BUY),
        ])
        assert p.strategy == StrategyType.CALL_CREDIT_SPREAD

    def test_wrong_strike_order_rejected(self):
        with pytest.raises(ValidationError, match="short call strike must be lower"):
            _proposal(strategy=StrategyType.CALL_CREDIT_SPREAD, legs=[
                OptionLeg(right=OptionRight.CALL, strike=110, side=LegSide.SELL),
                OptionLeg(right=OptionRight.CALL, strike=100, side=LegSide.BUY),
            ])

    def test_put_leg_rejected(self):
        with pytest.raises(ValidationError, match="must both be calls"):
            _proposal(strategy=StrategyType.CALL_CREDIT_SPREAD, legs=[
                OptionLeg(right=OptionRight.PUT, strike=100, side=LegSide.SELL),
                OptionLeg(right=OptionRight.CALL, strike=110, side=LegSide.BUY),
            ])


class TestBullCallSpread:
    def test_valid(self):
        p = _proposal(strategy=StrategyType.BULL_CALL_SPREAD, legs=[
            OptionLeg(right=OptionRight.CALL, strike=95, side=LegSide.BUY),
            OptionLeg(right=OptionRight.CALL, strike=105, side=LegSide.SELL),
        ])
        assert p.strategy == StrategyType.BULL_CALL_SPREAD

    def test_wrong_strike_order_rejected(self):
        with pytest.raises(ValidationError, match="long call strike must be lower"):
            _proposal(strategy=StrategyType.BULL_CALL_SPREAD, legs=[
                OptionLeg(right=OptionRight.CALL, strike=105, side=LegSide.BUY),
                OptionLeg(right=OptionRight.CALL, strike=95, side=LegSide.SELL),
            ])


class TestBearPutSpread:
    def test_valid(self):
        p = _proposal(strategy=StrategyType.BEAR_PUT_SPREAD, legs=[
            OptionLeg(right=OptionRight.PUT, strike=100, side=LegSide.BUY),
            OptionLeg(right=OptionRight.PUT, strike=90, side=LegSide.SELL),
        ])
        assert p.strategy == StrategyType.BEAR_PUT_SPREAD

    def test_wrong_strike_order_rejected(self):
        with pytest.raises(ValidationError, match="long put strike must be higher"):
            _proposal(strategy=StrategyType.BEAR_PUT_SPREAD, legs=[
                OptionLeg(right=OptionRight.PUT, strike=90, side=LegSide.BUY),
                OptionLeg(right=OptionRight.PUT, strike=100, side=LegSide.SELL),
            ])


class TestProtectivePut:
    def test_valid(self):
        p = _proposal(strategy=StrategyType.PROTECTIVE_PUT, legs=[OptionLeg(right=OptionRight.PUT, strike=90, side=LegSide.BUY)])
        assert p.strategy == StrategyType.PROTECTIVE_PUT

    def test_short_put_rejected(self):
        with pytest.raises(ValidationError, match="requires exactly one long put leg"):
            _proposal(strategy=StrategyType.PROTECTIVE_PUT, legs=[OptionLeg(right=OptionRight.PUT, strike=90, side=LegSide.SELL)])

    def test_call_rejected(self):
        with pytest.raises(ValidationError, match="requires exactly one long put leg"):
            _proposal(strategy=StrategyType.PROTECTIVE_PUT, legs=[OptionLeg(right=OptionRight.CALL, strike=90, side=LegSide.BUY)])


class TestProtectiveCollar:
    def test_valid(self):
        p = _proposal(strategy=StrategyType.PROTECTIVE_COLLAR, legs=[
            OptionLeg(right=OptionRight.CALL, strike=110, side=LegSide.SELL),
            OptionLeg(right=OptionRight.PUT, strike=90, side=LegSide.BUY),
        ])
        assert p.strategy == StrategyType.PROTECTIVE_COLLAR

    def test_inverted_strikes_rejected(self):
        with pytest.raises(ValidationError, match="ceiling.*must be above"):
            _proposal(strategy=StrategyType.PROTECTIVE_COLLAR, legs=[
                OptionLeg(right=OptionRight.CALL, strike=90, side=LegSide.SELL),
                OptionLeg(right=OptionRight.PUT, strike=110, side=LegSide.BUY),
            ])

    def test_both_calls_rejected(self):
        with pytest.raises(ValidationError, match="one call leg and one put leg"):
            _proposal(strategy=StrategyType.PROTECTIVE_COLLAR, legs=[
                OptionLeg(right=OptionRight.CALL, strike=110, side=LegSide.SELL),
                OptionLeg(right=OptionRight.CALL, strike=90, side=LegSide.BUY),
            ])


class TestLongStraddle:
    def test_valid(self):
        p = _proposal(strategy=StrategyType.LONG_STRADDLE, legs=[
            OptionLeg(right=OptionRight.CALL, strike=100, side=LegSide.BUY),
            OptionLeg(right=OptionRight.PUT, strike=100, side=LegSide.BUY),
        ])
        assert p.strategy == StrategyType.LONG_STRADDLE

    def test_different_strikes_rejected(self):
        with pytest.raises(ValidationError, match="must share the same strike"):
            _proposal(strategy=StrategyType.LONG_STRADDLE, legs=[
                OptionLeg(right=OptionRight.CALL, strike=100, side=LegSide.BUY),
                OptionLeg(right=OptionRight.PUT, strike=95, side=LegSide.BUY),
            ])

    def test_short_leg_rejected(self):
        with pytest.raises(ValidationError, match="must both be long"):
            _proposal(strategy=StrategyType.LONG_STRADDLE, legs=[
                OptionLeg(right=OptionRight.CALL, strike=100, side=LegSide.SELL),
                OptionLeg(right=OptionRight.PUT, strike=100, side=LegSide.BUY),
            ])


class TestLongStrangle:
    def test_valid(self):
        p = _proposal(strategy=StrategyType.LONG_STRANGLE, legs=[
            OptionLeg(right=OptionRight.CALL, strike=110, side=LegSide.BUY),
            OptionLeg(right=OptionRight.PUT, strike=90, side=LegSide.BUY),
        ])
        assert p.strategy == StrategyType.LONG_STRANGLE

    def test_equal_strikes_rejected(self):
        with pytest.raises(ValidationError, match="else it is a straddle"):
            _proposal(strategy=StrategyType.LONG_STRANGLE, legs=[
                OptionLeg(right=OptionRight.CALL, strike=100, side=LegSide.BUY),
                OptionLeg(right=OptionRight.PUT, strike=100, side=LegSide.BUY),
            ])


class TestLongCall:
    def test_valid(self):
        p = _proposal(strategy=StrategyType.LONG_CALL, legs=[OptionLeg(right=OptionRight.CALL, strike=100, side=LegSide.BUY)])
        assert p.strategy == StrategyType.LONG_CALL

    def test_short_call_rejected(self):
        with pytest.raises(ValidationError, match="requires exactly one long call leg"):
            _proposal(strategy=StrategyType.LONG_CALL, legs=[OptionLeg(right=OptionRight.CALL, strike=100, side=LegSide.SELL)])


class TestLongPut:
    def test_valid(self):
        p = _proposal(strategy=StrategyType.LONG_PUT, legs=[OptionLeg(right=OptionRight.PUT, strike=100, side=LegSide.BUY)])
        assert p.strategy == StrategyType.LONG_PUT

    def test_short_put_rejected(self):
        with pytest.raises(ValidationError, match="requires exactly one long put leg"):
            _proposal(strategy=StrategyType.LONG_PUT, legs=[OptionLeg(right=OptionRight.PUT, strike=100, side=LegSide.SELL)])


class TestStrategyTypeStillHasNoLiveMode:
    def test_no_naked_or_unlimited_risk_member_named(self):
        """Structural guard: none of the 9 new members should ever be
        confused with a naked/unfunded structure by name."""
        forbidden_substrings = ("naked", "unfunded", "live")
        for member in StrategyType:
            for forbidden in forbidden_substrings:
                assert forbidden not in member.value

    def test_max_two_legs_still_enforced_by_schema(self):
        from pydantic import ValidationError as VE

        with pytest.raises(VE):
            _proposal(
                strategy=StrategyType.LONG_STRADDLE,
                legs=[
                    OptionLeg(right=OptionRight.CALL, strike=100, side=LegSide.BUY),
                    OptionLeg(right=OptionRight.PUT, strike=100, side=LegSide.BUY),
                    OptionLeg(right=OptionRight.CALL, strike=105, side=LegSide.BUY),
                ],
            )
