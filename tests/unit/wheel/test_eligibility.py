"""Step 22.2 Part 23: Wheel underlying-eligibility screening tests --
universe, liquidity, DTE/delta range, earnings fail-closed behavior,
concentration/correlation reuse, cash sufficiency."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from src.data.option_chain import OptionContract, OptionRight
from src.data.quotes import UnderlyingQuote
from src.risk.limits import get_default_limits
from src.risk.portfolio_risk import Portfolio
from src.wheel.eligibility import (
    EarningsProximityStatus,
    WheelEligibilityConfig,
    check_wheel_eligibility,
    resolve_put_delta,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
EXP = NOW.date() + timedelta(days=35)


def _underlying(**overrides) -> UnderlyingQuote:
    base = dict(symbol="SPY", bid=99.9, ask=100.1, last=100.0, volume=5_000_000, timestamp=NOW, source="test")
    base.update(overrides)
    return UnderlyingQuote(**base)


def _put(**overrides) -> OptionContract:
    base = dict(
        underlying="SPY", option_symbol="SPY-put", expiration=EXP, strike=95.0, right=OptionRight.PUT,
        bid=1.0, ask=1.05, last=1.02, volume=500, open_interest=1000, iv=0.20, underlying_price=100.0,
        timestamp=NOW, source="test",
    )
    base.update(overrides)
    return OptionContract(**base)


def _portfolio(**overrides) -> Portfolio:
    base = dict(as_of=NOW, nav=100_000.0, cash=90_000.0, peak_equity=100_000.0, sector_by_ticker={"SPY": "ETF"})
    base.update(overrides)
    return Portfolio(**base)


def _check(**overrides):
    base = dict(
        ticker="SPY", sector="ETF", is_etf=True, approved_universe=frozenset({"SPY"}),
        underlying_quote=_underlying(), put_contract=_put(), put_contracts_requested=1,
        portfolio=_portfolio(), limits=get_default_limits(), config=WheelEligibilityConfig(),
        earnings_status=EarningsProximityStatus.NOT_APPLICABLE_ETF, now=NOW,
    )
    base.update(overrides)
    return check_wheel_eligibility(**base)


class TestHappyPath:
    def test_fully_qualifying_candidate_is_eligible(self):
        result = _check()
        assert result.eligible is True
        assert not result.blocking_reasons


class TestUniverseAndLiquidity:
    def test_ticker_outside_approved_universe_blocks(self):
        result = _check(approved_universe=frozenset({"QQQ"}))
        assert not result.eligible
        assert any(c.name == "approved_universe" and not c.passed for c in result.checks)

    def test_thin_open_interest_blocks(self):
        result = _check(put_contract=_put(open_interest=5))
        assert not result.eligible
        assert any(c.name == "option_open_interest" and not c.passed for c in result.checks)

    def test_thin_volume_blocks(self):
        result = _check(put_contract=_put(volume=1))
        assert not result.eligible
        assert any(c.name == "option_volume" and not c.passed for c in result.checks)

    def test_wide_bid_ask_spread_blocks(self):
        result = _check(put_contract=_put(bid=0.5, ask=2.0))
        assert not result.eligible
        assert any(c.name == "option_bid_ask_spread" and not c.passed for c in result.checks)

    def test_zero_bid_and_ask_is_data_insufficient_and_blocks(self):
        result = _check(put_contract=_put(bid=0.0, ask=0.0, last=0.0))
        assert any(c.name == "option_bid_ask_spread" and not c.passed and "DATA_INSUFFICIENT" in c.detail for c in result.checks)

    def test_low_priced_underlying_below_minimum_blocks(self):
        result = _check(underlying_quote=_underlying(bid=2.0, ask=2.1, last=2.05))
        assert any(c.name == "underlying_price" and not c.passed for c in result.checks)

    def test_thin_underlying_volume_blocks(self):
        result = _check(underlying_quote=_underlying(volume=500))
        assert any(c.name == "underlying_liquidity" and not c.passed for c in result.checks)


class TestDteAndDelta:
    def test_dte_outside_permitted_range_blocks(self):
        far_exp = NOW.date() + timedelta(days=200)
        result = _check(put_contract=_put(expiration=far_exp))
        assert any(c.name == "dte_in_permitted_range" and not c.passed for c in result.checks)

    def test_delta_outside_permitted_range_blocks(self):
        # A deep ITM put has |delta| near 1.0, well outside [0.10, 0.30].
        result = _check(put_contract=_put(strike=150.0, iv=0.20))
        assert any(c.name == "delta_in_permitted_range" and not c.passed for c in result.checks)

    def test_missing_iv_is_data_insufficient_for_delta(self):
        result = _check(put_contract=_put(iv=None, delta=None))
        assert any(
            c.name == "delta_in_permitted_range" and not c.passed and "DATA_INSUFFICIENT" in c.detail
            for c in result.checks
        )
        assert result.computed_put_delta is None

    def test_provider_supplied_delta_is_preferred_over_computed(self):
        contract = _put(delta=-0.20)
        delta = resolve_put_delta(contract, spot=100.0, t=35 / 365, risk_free_rate=0.04)
        assert delta == -0.20


class TestEarningsFailClosed:
    def test_no_earnings_in_window_passes(self):
        result = _check(is_etf=False, earnings_status=EarningsProximityStatus.NO_EARNINGS_IN_WINDOW)
        assert any(c.name == "earnings_proximity" and c.passed for c in result.checks)

    def test_earnings_in_window_blocks(self):
        result = _check(is_etf=False, earnings_status=EarningsProximityStatus.EARNINGS_IN_WINDOW)
        assert any(c.name == "earnings_proximity" and not c.passed for c in result.checks)

    def test_missing_earnings_data_blocks_never_treated_as_no_earnings(self):
        result = _check(is_etf=False, earnings_status=EarningsProximityStatus.DATA_UNAVAILABLE)
        check = next(c for c in result.checks if c.name == "earnings_proximity")
        assert not check.passed
        assert "DATA_INSUFFICIENT" in check.detail

    def test_etf_earnings_check_is_informational_only(self):
        result = _check(is_etf=True, earnings_status=EarningsProximityStatus.NOT_APPLICABLE_ETF)
        assert any(c.name == "earnings_proximity" and c.passed for c in result.checks)

    def test_avoid_earnings_disabled_bypasses_the_check(self):
        result = _check(
            is_etf=False, earnings_status=EarningsProximityStatus.EARNINGS_IN_WINDOW,
            config=WheelEligibilityConfig(avoid_earnings=False),
        )
        assert any(c.name == "earnings_proximity" and c.passed for c in result.checks)


class TestCashAndConcentration:
    def test_insufficient_cash_if_assigned_blocks(self):
        result = _check(portfolio=_portfolio(cash=1000.0, nav=1000.0 / 0.79))
        assert any(c.name == "cash_available_if_assigned" and not c.passed for c in result.checks)

    def test_underlying_concentration_breach_blocks(self):
        from src.risk.portfolio_risk import PortfolioPosition, PortfolioPositionLeg

        big_position = PortfolioPosition(
            position_id="pos-1", ticker="SPY", sector="ETF", strategy="cash_secured_put",
            expiration=EXP, legs=[PortfolioPositionLeg(right="P", side="sell", strike=90.0, entry_price=1.0)],
            contracts=1, capital_at_risk=15_000.0, max_loss=15_000.0, opened_at=NOW,
        )
        pf = _portfolio(positions=[big_position])
        result = _check(portfolio=pf)
        assert any(c.name == "underlying_concentration" and not c.passed for c in result.checks)


class TestEligibilityConfigValidation:
    def test_invalid_delta_bounds_raise(self):
        with pytest.raises(ValueError):
            WheelEligibilityConfig(csp_short_delta_low=0.5, csp_short_delta_high=0.2)

    def test_invalid_dte_bounds_raise(self):
        with pytest.raises(ValueError):
            WheelEligibilityConfig(csp_min_dte=60, csp_max_dte=25)
