"""Step 22.2 Part 15/23: stateful Wheel backtest tests -- cross-cycle
statefulness (CSP assignment feeds directly into the CC phase),
BACKTEST_DATA_INSUFFICIENT on missing settlement data, no look-ahead,
and sorted trading_days enforcement."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.backtest.commissions import CommissionSchedule
from src.backtest.simulator import HistoricalOptionQuote, LookaheadViolationError
from src.brokers.paper import FillModel, PaperBrokerConfig
from src.data.option_chain import OptionRight
from src.wheel.backtest_engine import (
    WheelBacktestConfig,
    WheelBacktestDataInsufficientError,
    WheelStrikeSelectionRule,
    run_wheel_backtest,
)
from src.wheel.state import WheelState

TICKER = "WHL"
START = date(2026, 1, 5)
CSP_EXP = START + timedelta(days=35)
CC_EXP = CSP_EXP + timedelta(days=35)


def _config() -> WheelBacktestConfig:
    return WheelBacktestConfig(
        initial_cash=100_000.0,
        fill_config=PaperBrokerConfig(fill_model=FillModel.MID),
        commission_schedule=CommissionSchedule(),
        csp_rule=WheelStrikeSelectionRule(min_dte=30, max_dte=45, target_delta_low=0.10, target_delta_high=0.35, contracts=1),
        cc_rule=WheelStrikeSelectionRule(min_dte=30, max_dte=45, target_delta_low=0.10, target_delta_high=0.35, contracts=1),
    )


def _quote(**overrides) -> HistoricalOptionQuote:
    base = dict(underlying=TICKER, quote_date=START, expiration=CSP_EXP, strike=48.0, right=OptionRight.PUT, bid=0.95, ask=1.05, volume=500, open_interest=1000, underlying_price=50.0, iv=0.30)
    base.update(overrides)
    return HistoricalOptionQuote(**base)


class TestFullCycleStatefulness:
    def test_assignment_feeds_into_a_covered_call_same_run(self):
        trading_days = [START + timedelta(days=i) for i in range((CC_EXP - START).days + 3)]
        quotes = {
            (TICKER, START): [_quote()],
            (TICKER, CSP_EXP): [
                _quote(quote_date=CSP_EXP, underlying_price=45.0, bid=3.0, ask=3.2),  # ITM -> assigned
                HistoricalOptionQuote(underlying=TICKER, quote_date=CSP_EXP, expiration=CC_EXP, strike=50.0, right=OptionRight.CALL, bid=1.0, ask=1.2, volume=500, open_interest=1000, underlying_price=45.0, iv=0.30),
            ],
            (TICKER, CC_EXP): [
                HistoricalOptionQuote(underlying=TICKER, quote_date=CC_EXP, expiration=CC_EXP, strike=50.0, right=OptionRight.CALL, bid=5.0, ask=5.2, volume=200, open_interest=800, underlying_price=55.0, iv=0.30),
            ],
        }

        def lookup(ticker, as_of):
            return quotes.get((ticker, as_of), [])

        state = run_wheel_backtest(wheel_id="bt-1", ticker=TICKER, start=START, trading_days=trading_days, quote_lookup=lookup, config=_config())
        assert state.wheel.state == WheelState.WHEEL_COMPLETE
        assert len(state.wheel.csp_cycles) == 1
        assert len(state.wheel.cc_cycles) == 1
        assert state.wheel.csp_cycles[0].close_reason.value == "assigned"
        assert state.wheel.cc_cycles[0].close_reason.value == "assigned_called_away"
        assert state.wheel.accounting.realized_stock_pnl == 200.0  # (50-48)*100
        # cash should have increased overall: premiums collected + gain on shares
        assert state.cash > 100_000.0

    def test_no_assignment_path_expires_worthless_and_stops(self):
        trading_days = [START + timedelta(days=i) for i in range((CSP_EXP - START).days + 3)]
        quotes = {
            (TICKER, START): [_quote()],
            (TICKER, CSP_EXP): [_quote(quote_date=CSP_EXP, underlying_price=60.0, bid=0.0, ask=0.05)],  # OTM
        }

        def lookup(ticker, as_of):
            return quotes.get((ticker, as_of), [])

        state = run_wheel_backtest(wheel_id="bt-2", ticker=TICKER, start=START, trading_days=trading_days, quote_lookup=lookup, config=_config())
        assert state.wheel.state == WheelState.CSP_EXPIRED
        assert state.wheel.accounting.shares_owned == 0


class TestDataInsufficiency:
    def test_missing_settlement_quote_raises_not_silently_skips(self):
        trading_days = [START + timedelta(days=i) for i in range((CSP_EXP - START).days + 3)]
        quotes = {(TICKER, START): [_quote()]}  # no quote at all on the expiration day

        def lookup(ticker, as_of):
            return quotes.get((ticker, as_of), [])

        with pytest.raises(WheelBacktestDataInsufficientError):
            run_wheel_backtest(wheel_id="bt-3", ticker=TICKER, start=START, trading_days=trading_days, quote_lookup=lookup, config=_config())


class TestNoLookahead:
    def test_a_future_dated_quote_in_the_lookup_result_raises(self):
        future_dated = _quote(quote_date=START + timedelta(days=1))  # dated tomorrow, looked up "today"

        def lookup(ticker, as_of):
            return [future_dated]

        with pytest.raises(LookaheadViolationError):
            run_wheel_backtest(wheel_id="bt-4", ticker=TICKER, start=START, trading_days=[START], quote_lookup=lookup, config=_config())


class TestTradingDaysOrdering:
    def test_unsorted_trading_days_rejected(self):
        with pytest.raises(ValueError):
            run_wheel_backtest(wheel_id="bt-5", ticker=TICKER, start=START, trading_days=[START + timedelta(days=1), START], quote_lookup=lambda t, d: [], config=_config())


class TestCashDiscipline:
    def test_never_opens_a_csp_beyond_available_cash(self):
        trading_days = [START]
        # A strike so large that the required collateral would exceed initial_cash.
        expensive_quote = _quote(strike=4800.0, underlying_price=5000.0, bid=95.0, ask=105.0)

        def lookup(ticker, as_of):
            return [expensive_quote]

        config = WheelBacktestConfig(
            initial_cash=10_000.0,  # far less than strike*100 = $480,000
            fill_config=PaperBrokerConfig(fill_model=FillModel.MID),
            commission_schedule=CommissionSchedule(),
            csp_rule=WheelStrikeSelectionRule(min_dte=1, max_dte=60, target_delta_low=0.01, target_delta_high=0.99, contracts=1),
            cc_rule=WheelStrikeSelectionRule(min_dte=1, max_dte=60, target_delta_low=0.01, target_delta_high=0.99, contracts=1),
        )
        state = run_wheel_backtest(wheel_id="bt-6", ticker=TICKER, start=START, trading_days=trading_days, quote_lookup=lookup, config=config)
        assert state.wheel.state == WheelState.WHEEL_CANDIDATE  # never opened
        assert state.cash == 10_000.0
