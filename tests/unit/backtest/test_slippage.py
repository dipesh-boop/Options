"""Tests for the `HistoricalOptionQuote` -> `src.brokers.paper` fill
adapter: realistic vs. theoretical fills, and that theoretical fills
never assume midpoint by *coincidence* but by explicit, fixed
`_THEORETICAL_CONFIG`."""
from __future__ import annotations

from datetime import date

import pytest

from src.backtest.simulator import BacktestLeg
from src.backtest.slippage import NoFillError, fill_realistic, fill_theoretical, mark_to_market
from src.brokers.paper import FillModel, PaperBrokerConfig
from src.data.option_chain import OptionRight
from tests.unit.backtest.conftest import EXPIRATION, make_quote

SINGLE_SHORT_PUT = (BacktestLeg(right=OptionRight.PUT, strike=95.0, side="sell"),)


class TestFillRealisticMidModel:
    def test_single_short_leg_mid_price_is_net_credit(self, zero_friction_config):
        quotes = [make_quote(strike=95.0, right=OptionRight.PUT, bid=1.90, ask=2.10)]
        fill = fill_realistic(list(SINGLE_SHORT_PUT), EXPIRATION, quotes, 1, limit_price=0.0, config=zero_friction_config)
        assert fill.net_price == pytest.approx(2.00)
        assert fill.filled_contracts == 1

    def test_limit_price_not_satisfied_raises_no_fill(self, zero_friction_config):
        quotes = [make_quote(strike=95.0, right=OptionRight.PUT, bid=1.90, ask=2.10)]
        with pytest.raises(NoFillError):
            fill_realistic(list(SINGLE_SHORT_PUT), EXPIRATION, quotes, 1, limit_price=2.50, config=zero_friction_config)

    def test_insufficient_liquidity_raises_no_fill(self, zero_friction_config):
        quotes = [make_quote(strike=95.0, right=OptionRight.PUT, bid=1.90, ask=2.10, volume=0, open_interest=0)]
        with pytest.raises(NoFillError):
            fill_realistic(list(SINGLE_SHORT_PUT), EXPIRATION, quotes, 1, limit_price=0.0, config=zero_friction_config)


class TestFillTheoreticalIsAlwaysPlainMidRegardlessOfConfig:
    def test_theoretical_ignores_caller_slippage_config(self, slippage_config):
        """`fill_theoretical` takes no `config` argument at all — it is
        pinned to `_THEORETICAL_CONFIG` (MID, zero commission, zero
        slippage) no matter what fill model the realistic side uses, so
        the theoretical track can never accidentally inherit realistic
        friction."""
        quotes = [make_quote(strike=95.0, right=OptionRight.PUT, bid=1.90, ask=2.10)]
        price = fill_theoretical(list(SINGLE_SHORT_PUT), EXPIRATION, quotes, 1)
        assert price == pytest.approx(2.00)

    def test_theoretical_always_fully_filled_even_with_zero_volume(self):
        quotes = [make_quote(strike=95.0, right=OptionRight.PUT, bid=1.90, ask=2.10, volume=0, open_interest=0)]
        # fill_theoretical has no NoFillError path at all -- no liquidity gate exists
        price = fill_theoretical(list(SINGLE_SHORT_PUT), EXPIRATION, quotes, 100)
        assert price == pytest.approx(2.00)


class TestMarkToMarket:
    def test_mid_model_no_limit_gate_even_on_illiquid_quote(self, zero_friction_config):
        quotes = [make_quote(strike=95.0, right=OptionRight.PUT, bid=1.90, ask=2.10, volume=0, open_interest=0)]
        # mark_to_market answers "what would it cost," never "did it fill" -- no NoFillError possible
        price = mark_to_market(list(SINGLE_SHORT_PUT), EXPIRATION, quotes, zero_friction_config)
        assert price == pytest.approx(2.00)

    def test_slippage_model_moves_price_against_whichever_direction_is_traded(self):
        """Regression test for the sign bug fixed in `engine.py`: pricing
        a *closing* (buy-to-close) order under a slippage-aware fill
        model must come back worse (more negative / larger debit) than
        pricing the same legs unflipped (as if selling fresh)."""
        config = PaperBrokerConfig(fill_model=FillModel.MID_WITH_SLIPPAGE, commission_per_contract=0.0, slippage_bps=200.0, spread_capture_fraction=0.0)
        quotes = [make_quote(strike=95.0, right=OptionRight.PUT, bid=1.90, ask=2.10)]

        opening_side = SINGLE_SHORT_PUT  # sell to open
        closing_side = (BacktestLeg(right=OptionRight.PUT, strike=95.0, side="buy"),)  # buy to close

        opening_price = mark_to_market(list(opening_side), EXPIRATION, quotes, config)
        closing_price = mark_to_market(list(closing_side), EXPIRATION, quotes, config)

        # opening (sell) valuation: mid - slippage -> a smaller credit
        assert opening_price == pytest.approx(2.00 - 0.04)
        # closing (buy) valuation: -mid - slippage -> a larger debit
        assert closing_price == pytest.approx(-2.00 - 0.04)
        # the true cost to close is strictly worse than the naive unflipped price would suggest
        assert -closing_price > opening_price
