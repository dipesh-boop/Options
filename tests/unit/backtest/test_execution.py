"""Tests for `execute_entry`/`execute_exit`/`flip_legs`, including the
put-credit-spread payoff at max profit and max loss, and that
theoretical/realistic results diverge only through friction, never
through the strike/right shape of the trade."""
from __future__ import annotations

import pytest

from src.backtest.execution import execute_entry, execute_exit, flip_legs
from src.backtest.simulator import BacktestLeg
from src.data.option_chain import OptionRight
from tests.unit.backtest.conftest import EXPIRATION, make_quote, put_credit_spread_legs


class TestFlipLegs:
    def test_flips_side_keeps_strike_and_right(self):
        legs = put_credit_spread_legs()
        flipped = flip_legs(list(legs))
        assert [l.side for l in flipped] == ["buy", "sell"]
        assert [(l.strike, l.right) for l in flipped] == [(l.strike, l.right) for l in legs]

    def test_double_flip_is_identity(self):
        legs = list(put_credit_spread_legs())
        assert flip_legs(flip_legs(legs)) == legs


class TestExecuteEntryPutCreditSpread:
    def test_entry_credit_matches_mid_spread_width_under_zero_friction(self, zero_friction_config, zero_commission_schedule):
        quotes = [
            make_quote(strike=95.0, right=OptionRight.PUT, bid=1.90, ask=2.10),
            make_quote(strike=90.0, right=OptionRight.PUT, bid=0.90, ask=1.10),
        ]
        result = execute_entry(
            legs=list(put_credit_spread_legs()), expiration=EXPIRATION, quotes=quotes,
            requested_contracts=2, limit_price=0.0, fill_config=zero_friction_config, commission_schedule=zero_commission_schedule,
        )
        # short 95 mid 2.00, long 90 mid 1.00 -> net credit 1.00
        assert result.realistic_price == pytest.approx(1.00)
        assert result.theoretical_price == pytest.approx(1.00)
        assert result.filled_contracts == 2
        assert result.commission == pytest.approx(0.0)

    def test_commission_scales_with_contracts_and_legs(self, zero_friction_config, standard_commission_schedule):
        quotes = [
            make_quote(strike=95.0, right=OptionRight.PUT, bid=1.90, ask=2.10),
            make_quote(strike=90.0, right=OptionRight.PUT, bid=0.90, ask=1.10),
        ]
        result = execute_entry(
            legs=list(put_credit_spread_legs()), expiration=EXPIRATION, quotes=quotes,
            requested_contracts=3, limit_price=0.0, fill_config=zero_friction_config, commission_schedule=standard_commission_schedule,
        )
        # 3 contracts * 2 legs * 0.65
        assert result.commission == pytest.approx(3.90)


class TestExecuteExitClosesOppositeSide:
    def test_exit_builds_the_closing_order_from_opening_legs(self, zero_friction_config, zero_commission_schedule):
        """A credit spread closed at the exact same prices it was opened
        at nets to a zero round-trip P&L (theoretical price is a plain
        mirror image with zero friction)."""
        entry_quotes = [
            make_quote(strike=95.0, right=OptionRight.PUT, bid=1.90, ask=2.10),
            make_quote(strike=90.0, right=OptionRight.PUT, bid=0.90, ask=1.10),
        ]
        entry = execute_entry(
            legs=list(put_credit_spread_legs()), expiration=EXPIRATION, quotes=entry_quotes,
            requested_contracts=1, limit_price=0.0, fill_config=zero_friction_config, commission_schedule=zero_commission_schedule,
        )
        exit_result = execute_exit(
            legs=list(put_credit_spread_legs()), expiration=EXPIRATION, quotes=entry_quotes, contracts=1,
            limit_price=10_000.0, fill_config=zero_friction_config, commission_schedule=zero_commission_schedule,
        )
        # entry collected +1.00 credit; closing at the same market pays exactly -1.00 back
        assert entry.realistic_price == pytest.approx(1.00)
        assert exit_result.realistic_price == pytest.approx(-1.00)
        assert entry.realistic_price + exit_result.realistic_price == pytest.approx(0.0)

    def test_max_profit_payoff_when_spread_fully_decays_to_worthless(self, zero_friction_config, zero_commission_schedule):
        """Closing a credit spread when both legs are quoted at (0, 0)
        (fully OTM/worthless) costs nothing -> the full entry credit is
        realized profit."""
        entry_quotes = [
            make_quote(strike=95.0, right=OptionRight.PUT, bid=1.90, ask=2.10),
            make_quote(strike=90.0, right=OptionRight.PUT, bid=0.90, ask=1.10),
        ]
        worthless_quotes = [
            make_quote(strike=95.0, right=OptionRight.PUT, bid=0.0, ask=0.0, quote_date=EXPIRATION.replace(day=1)),
            make_quote(strike=90.0, right=OptionRight.PUT, bid=0.0, ask=0.0, quote_date=EXPIRATION.replace(day=1)),
        ]
        entry = execute_entry(
            legs=list(put_credit_spread_legs()), expiration=EXPIRATION, quotes=entry_quotes,
            requested_contracts=1, limit_price=0.0, fill_config=zero_friction_config, commission_schedule=zero_commission_schedule,
        )
        exit_result = execute_exit(
            legs=list(put_credit_spread_legs()), expiration=EXPIRATION, quotes=worthless_quotes, contracts=1,
            limit_price=10_000.0, fill_config=zero_friction_config, commission_schedule=zero_commission_schedule,
        )
        assert exit_result.realistic_price == pytest.approx(0.0)
        round_trip_pnl = entry.realistic_price + exit_result.realistic_price
        assert round_trip_pnl == pytest.approx(1.00)  # full max profit realized
