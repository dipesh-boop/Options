"""Tests for the shared backtest data model: `HistoricalOptionQuote`
validation and the options-chain look-ahead guard."""
from __future__ import annotations

from datetime import date

import pytest

from src.backtest.simulator import (
    HistoricalOptionQuote,
    LookaheadViolationError,
    assert_no_lookahead_options,
)
from src.data.option_chain import OptionRight
from tests.unit.backtest.conftest import make_quote


class TestHistoricalOptionQuoteValidation:
    def test_valid_quote_constructs(self):
        q = make_quote(strike=100.0, right=OptionRight.PUT, bid=1.0, ask=1.2)
        assert q.bid == 1.0 and q.ask == 1.2

    def test_bid_greater_than_ask_rejected(self):
        with pytest.raises(ValueError, match="bid.*ask"):
            make_quote(strike=100.0, right=OptionRight.PUT, bid=1.5, ask=1.0)

    def test_negative_bid_rejected(self):
        with pytest.raises(ValueError):
            make_quote(strike=100.0, right=OptionRight.PUT, bid=-0.1, ask=1.0)

    def test_negative_ask_rejected(self):
        with pytest.raises(ValueError):
            make_quote(strike=100.0, right=OptionRight.PUT, bid=0.0, ask=-1.0)

    def test_non_positive_strike_rejected(self):
        with pytest.raises(ValueError, match="strike"):
            make_quote(strike=0.0, right=OptionRight.PUT, bid=1.0, ask=1.2)

    def test_non_positive_underlying_price_rejected(self):
        with pytest.raises(ValueError, match="underlying_price"):
            make_quote(strike=100.0, right=OptionRight.PUT, bid=1.0, ask=1.2, underlying_price=0.0)

    def test_mid_property(self):
        q = make_quote(strike=100.0, right=OptionRight.PUT, bid=1.0, ask=1.2)
        assert q.mid == pytest.approx(1.1)

    def test_mid_property_zero_when_both_sides_non_positive(self):
        q = make_quote(strike=100.0, right=OptionRight.PUT, bid=0.0, ask=0.0)
        assert q.mid == 0.0


class TestAssertNoLookaheadOptions:
    def test_passes_when_all_quotes_on_or_before_as_of(self):
        quotes = [
            make_quote(strike=100.0, right=OptionRight.PUT, bid=1.0, ask=1.2, quote_date=date(2024, 1, 2)),
            make_quote(strike=95.0, right=OptionRight.PUT, bid=0.5, ask=0.7, quote_date=date(2024, 1, 2)),
        ]
        result = assert_no_lookahead_options(quotes, date(2024, 1, 2))
        assert result == quotes

    def test_raises_on_a_single_future_dated_quote(self):
        quotes = [make_quote(strike=100.0, right=OptionRight.PUT, bid=1.0, ask=1.2, quote_date=date(2024, 1, 3))]
        with pytest.raises(LookaheadViolationError, match="lookahead violation"):
            assert_no_lookahead_options(quotes, date(2024, 1, 2))

    def test_raises_when_only_one_of_several_quotes_is_future_dated(self):
        quotes = [
            make_quote(strike=100.0, right=OptionRight.PUT, bid=1.0, ask=1.2, quote_date=date(2024, 1, 2)),
            make_quote(strike=95.0, right=OptionRight.PUT, bid=0.5, ask=0.7, quote_date=date(2024, 1, 5)),
        ]
        with pytest.raises(LookaheadViolationError):
            assert_no_lookahead_options(quotes, date(2024, 1, 2))

    def test_empty_quote_list_never_raises(self):
        assert assert_no_lookahead_options([], date(2024, 1, 2)) == []
