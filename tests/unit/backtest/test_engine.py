"""Tests for the day-by-day backtest engine loop: look-ahead bias
enforcement, correct contract-multiplier scaling, cash accounting
correctness across close/expiration/assignment, the profit-target
slippage-direction regression, commissions, and the
`build_backtest_result`/`evaluate_target` reporting layer."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.backtest.commissions import CommissionSchedule
from src.backtest.engine import (
    BacktestConfig,
    DEFAULT_TARGET_HIGH,
    DEFAULT_TARGET_LOW,
    _estimate_capital_at_risk,
    _match_quotes_for_legs,
    build_backtest_result,
    evaluate_target,
    run_backtest,
)
from src.backtest.simulator import BacktestLeg, EntrySignal, HistoricalOptionQuote, LookaheadViolationError
from src.brokers.paper import FillModel, PaperBrokerConfig
from src.data.option_chain import OptionRight
from src.llm.schemas import StrategyType
from tests.unit.backtest.conftest import (
    EXPIRATION,
    UNDERLYING,
    cash_secured_put_legs,
    covered_call_legs,
    make_quote,
    put_credit_spread_legs,
)

_MULT = 100


def _csp_entry(**overrides) -> EntrySignal:
    defaults = dict(
        ticker=UNDERLYING, strategy=StrategyType.CASH_SECURED_PUT, legs=cash_secured_put_legs(),
        expiration=EXPIRATION, entry_date=date(2024, 1, 2), contracts_requested=1, limit_price=0.0,
        management_dte=7, profit_target_pct=0.5,
    )
    defaults.update(overrides)
    return EntrySignal(**defaults)


class TestLookaheadBiasIsEnforcedDuringSimulation:
    def test_a_quote_lookup_returning_a_future_dated_quote_raises(self):
        entry = _csp_entry()

        def bad_quote_lookup(ticker, as_of):
            # deliberately returns a quote dated *after* as_of
            return [make_quote(strike=95.0, right=OptionRight.PUT, bid=1.9, ask=2.1, quote_date=as_of + timedelta(days=1))]

        trading_days = [date(2024, 1, 2)]
        with pytest.raises(LookaheadViolationError):
            run_backtest([entry], bad_quote_lookup, trading_days, _config())

    def test_trading_days_must_be_pre_sorted(self):
        with pytest.raises(ValueError, match="sorted"):
            run_backtest([], lambda t, d: [], [date(2024, 1, 5), date(2024, 1, 2)], _config())


def _config(fill_model: FillModel = FillModel.MID, slippage_bps: float = 0.0, commission_per_contract: float = 0.65, spread_capture_fraction: float = 0.0) -> BacktestConfig:
    return BacktestConfig(
        initial_cash=100_000.0,
        fill_config=PaperBrokerConfig(fill_model=fill_model, commission_per_contract=commission_per_contract, slippage_bps=slippage_bps, spread_capture_fraction=spread_capture_fraction),
        commission_schedule=CommissionSchedule(per_contract=commission_per_contract),
    )


class TestContractMultiplierScaling:
    def test_entry_credit_is_scaled_by_100_and_by_contract_count(self):
        entry = _csp_entry(contracts_requested=3)
        quotes = {date(2024, 1, 2): [make_quote(strike=95.0, right=OptionRight.PUT, bid=1.90, ask=2.10, quote_date=date(2024, 1, 2))]}
        state = run_backtest([entry], lambda t, d: quotes.get(d, []), [date(2024, 1, 2)], _config(commission_per_contract=0.0))
        assert len(state.open_positions) == 1
        position = state.open_positions[0]
        # mid = 2.00 credit per share * 100 multiplier * 3 contracts = 600
        assert position.realistic_entry_credit_total == pytest.approx(600.0)
        assert position.theoretical_entry_credit_total == pytest.approx(600.0)

    def test_cash_increases_by_the_scaled_credit_minus_commission(self):
        entry = _csp_entry(contracts_requested=2)
        quotes = {date(2024, 1, 2): [make_quote(strike=95.0, right=OptionRight.PUT, bid=1.90, ask=2.10, quote_date=date(2024, 1, 2))]}
        config = _config(commission_per_contract=0.65)
        state = run_backtest([entry], lambda t, d: quotes.get(d, []), [date(2024, 1, 2)], config)
        # credit = 2.00 * 100 * 2 = 400; commission = 2 contracts * 1 leg * 0.65 = 1.30
        assert state.realistic_cash == pytest.approx(100_000.0 + 400.0 - 1.30)
        assert state.theoretical_cash == pytest.approx(100_000.0 + 400.0)  # theoretical track pays no commission


class TestProfitTargetCloseRoundTrip:
    def test_credit_spread_closes_at_profit_target_and_cash_reconciles(self):
        entry = EntrySignal(
            ticker=UNDERLYING, strategy=StrategyType.PUT_CREDIT_SPREAD, legs=put_credit_spread_legs(),
            expiration=EXPIRATION, entry_date=date(2024, 1, 2), contracts_requested=1, limit_price=0.0,
            management_dte=7, profit_target_pct=0.5,
        )
        quotes = {
            date(2024, 1, 2): [
                make_quote(strike=95.0, right=OptionRight.PUT, bid=1.90, ask=2.10, quote_date=date(2024, 1, 2)),
                make_quote(strike=90.0, right=OptionRight.PUT, bid=0.90, ask=1.10, quote_date=date(2024, 1, 2)),
            ],
            date(2024, 1, 15): [
                make_quote(strike=95.0, right=OptionRight.PUT, bid=0.40, ask=0.60, quote_date=date(2024, 1, 15)),
                make_quote(strike=90.0, right=OptionRight.PUT, bid=0.10, ask=0.20, quote_date=date(2024, 1, 15)),
            ],
        }
        config = _config(commission_per_contract=0.0)
        state = run_backtest([entry], lambda t, d: quotes.get(d, []), [date(2024, 1, 2), date(2024, 1, 15)], config)

        assert len(state.closed_trades) == 1
        trade = state.closed_trades[0]
        assert trade.close_reason == "profit_target"
        # entry credit 1.00 * 100, exit debit -0.35 * 100 -> pnl = 100 - 35 = 65
        assert trade.realistic_pnl == pytest.approx(65.0)
        assert trade.theoretical_pnl == pytest.approx(65.0)
        # cash must exactly reflect the entry credit plus exit debit (zero commission here)
        assert state.realistic_cash == pytest.approx(100_000.0 + trade.realistic_pnl)
        assert state.theoretical_cash == pytest.approx(100_000.0 + trade.theoretical_pnl)
        assert state.open_positions == []

    def test_slippage_model_makes_the_realistic_close_strictly_worse_than_mid(self):
        """Regression test for the flip_legs/mark_to_market sign bug:
        under a slippage-aware fill model, the *realistic* close must
        cost more (lower realistic pnl) than the frictionless
        theoretical close -- if the sign were still wrong, slippage
        would instead make the realistic close artificially cheaper."""
        entry = EntrySignal(
            ticker=UNDERLYING, strategy=StrategyType.PUT_CREDIT_SPREAD, legs=put_credit_spread_legs(),
            expiration=EXPIRATION, entry_date=date(2024, 1, 2), contracts_requested=1, limit_price=-10.0,
            management_dte=7, profit_target_pct=0.5,
        )
        quotes = {
            date(2024, 1, 2): [
                make_quote(strike=95.0, right=OptionRight.PUT, bid=1.90, ask=2.10, quote_date=date(2024, 1, 2)),
                make_quote(strike=90.0, right=OptionRight.PUT, bid=0.90, ask=1.10, quote_date=date(2024, 1, 2)),
            ],
            date(2024, 1, 15): [
                make_quote(strike=95.0, right=OptionRight.PUT, bid=0.40, ask=0.60, quote_date=date(2024, 1, 15)),
                make_quote(strike=90.0, right=OptionRight.PUT, bid=0.10, ask=0.20, quote_date=date(2024, 1, 15)),
            ],
        }
        config = _config(fill_model=FillModel.MID_WITH_SLIPPAGE, slippage_bps=100.0, spread_capture_fraction=0.0, commission_per_contract=0.0)
        state = run_backtest([entry], lambda t, d: quotes.get(d, []), [date(2024, 1, 2), date(2024, 1, 15)], config)

        assert len(state.closed_trades) == 1
        trade = state.closed_trades[0]
        assert trade.realistic_pnl < trade.theoretical_pnl


class TestExpirationAndAssignment:
    def test_position_expiring_otm_settles_worthless_with_full_credit_kept(self):
        entry = _csp_entry(contracts_requested=1)
        entry_quote = make_quote(strike=95.0, right=OptionRight.PUT, bid=1.90, ask=2.10, quote_date=date(2024, 1, 2))
        # settlement quote on expiration day: underlying above strike -> OTM
        settle_quote = make_quote(strike=95.0, right=OptionRight.PUT, bid=0.0, ask=0.0, quote_date=EXPIRATION, underlying_price=110.0)
        quotes = {date(2024, 1, 2): [entry_quote], EXPIRATION: [settle_quote]}
        config = _config(commission_per_contract=0.0)
        state = run_backtest([entry], lambda t, d: quotes.get(d, []), [date(2024, 1, 2), EXPIRATION], config)

        assert len(state.closed_trades) == 1
        trade = state.closed_trades[0]
        assert trade.close_reason == "expiration_otm"
        assert trade.realistic_pnl == pytest.approx(200.0)  # full credit, no assignment cost
        assert state.realistic_cash == pytest.approx(100_000.0 + 200.0)

    def test_short_put_assigned_itm_at_expiration_reduces_pnl_by_intrinsic_value(self):
        entry = _csp_entry(contracts_requested=1)
        entry_quote = make_quote(strike=95.0, right=OptionRight.PUT, bid=1.90, ask=2.10, quote_date=date(2024, 1, 2))
        # underlying settles below the strike -> assigned
        settle_quote = make_quote(strike=95.0, right=OptionRight.PUT, bid=5.0, ask=5.5, quote_date=EXPIRATION, underlying_price=90.0)
        quotes = {date(2024, 1, 2): [entry_quote], EXPIRATION: [settle_quote]}
        config = _config(commission_per_contract=0.0)
        state = run_backtest([entry], lambda t, d: quotes.get(d, []), [date(2024, 1, 2), EXPIRATION], config)

        trade = state.closed_trades[0]
        assert trade.close_reason == "assignment"
        # credit 200 collected; the newly-acquired shares are immediately
        # marked at the settlement price (this engine has no ongoing
        # share ledger), so the loss is the intrinsic value only
        # (95 - 90 = 5/share), not the full 95*100 strike notional.
        assert trade.realistic_pnl == pytest.approx(200.0 - 5.0 * _MULT)
        assert state.realistic_cash == pytest.approx(100_000.0 + trade.realistic_pnl)

    def test_covered_call_assigned_itm_settles_via_the_short_call_leg_only(self):
        entry = EntrySignal(
            ticker=UNDERLYING, strategy=StrategyType.COVERED_CALL, legs=covered_call_legs(), expiration=EXPIRATION,
            entry_date=date(2024, 1, 2), contracts_requested=1, limit_price=0.0, management_dte=7, profit_target_pct=0.5,
            underlying_shares_held=100, underlying_cost_basis=100.0,
        )
        entry_quote = make_quote(strike=105.0, right=OptionRight.CALL, bid=1.90, ask=2.10, quote_date=date(2024, 1, 2))
        settle_quote = make_quote(strike=105.0, right=OptionRight.CALL, bid=10.0, ask=10.5, quote_date=EXPIRATION, underlying_price=115.0)
        quotes = {date(2024, 1, 2): [entry_quote], EXPIRATION: [settle_quote]}
        config = _config(commission_per_contract=0.0)
        state = run_backtest([entry], lambda t, d: quotes.get(d, []), [date(2024, 1, 2), EXPIRATION], config)

        trade = state.closed_trades[0]
        assert trade.close_reason == "assignment"
        # credit 200 collected at entry; the shares delivered were
        # already held at cost basis 100 (underlying_cost_basis), so the
        # realized gain is (strike - cost basis) = 5/share, not the full
        # 105*100 strike notional the option settlement alone pays.
        assert trade.realistic_pnl == pytest.approx(200.0 + (105.0 - 100.0) * _MULT)


class TestMatchQuotesForLegsChecksUnderlyingRegressionMD003:
    """MD-003: matching used to be (expiration, strike, right) only --
    a quote for a completely different underlying at a coincidentally
    matching strike/expiration/right would silently match. Now the
    underlying must match too."""

    def test_correct_underlying_matches(self):
        legs = cash_secured_put_legs()
        quotes = [make_quote(strike=95.0, right=OptionRight.PUT, bid=1.9, ask=2.1, underlying=UNDERLYING)]
        matched = _match_quotes_for_legs(quotes, legs, EXPIRATION, UNDERLYING)
        assert matched is not None and len(matched) == 1

    def test_wrong_underlying_at_matching_strike_and_expiration_is_never_matched(self):
        legs = cash_secured_put_legs()
        quotes = [make_quote(strike=95.0, right=OptionRight.PUT, bid=1.9, ask=2.1, underlying="WRONG_TICKER")]
        matched = _match_quotes_for_legs(quotes, legs, EXPIRATION, UNDERLYING)
        assert matched is None

    def test_correct_underlying_quote_still_matches_when_a_wrong_underlying_quote_is_also_present(self):
        legs = cash_secured_put_legs()
        quotes = [
            make_quote(strike=95.0, right=OptionRight.PUT, bid=9.9, ask=10.1, underlying="WRONG_TICKER"),
            make_quote(strike=95.0, right=OptionRight.PUT, bid=1.9, ask=2.1, underlying=UNDERLYING),
        ]
        matched = _match_quotes_for_legs(quotes, legs, EXPIRATION, UNDERLYING)
        assert matched is not None
        assert matched[0].bid == pytest.approx(1.9)  # the correct-underlying quote, not the wrong-ticker one


class TestManagementDteClose:
    def test_position_closes_when_management_dte_reached_even_without_profit_target(self):
        entry = _csp_entry(contracts_requested=1, management_dte=20, profit_target_pct=0.99)
        entry_quote = make_quote(strike=95.0, right=OptionRight.PUT, bid=1.90, ask=2.10, quote_date=date(2024, 1, 2))
        # near-DTE day: value barely moved, nowhere near 99% profit target
        near_dte_quote = make_quote(strike=95.0, right=OptionRight.PUT, bid=1.80, ask=2.00, quote_date=date(2024, 1, 30))
        quotes = {date(2024, 1, 2): [entry_quote], date(2024, 1, 30): [near_dte_quote]}
        config = _config(commission_per_contract=0.0)
        state = run_backtest([entry], lambda t, d: quotes.get(d, []), [date(2024, 1, 2), date(2024, 1, 30)], config)

        assert len(state.closed_trades) == 1
        assert state.closed_trades[0].close_reason == "dte_management"


class TestEvaluateTarget:
    def test_exceeds_above_high_threshold(self):
        assert evaluate_target(DEFAULT_TARGET_HIGH + 0.01) == "exceeds"

    def test_meets_between_low_and_high(self):
        assert evaluate_target((DEFAULT_TARGET_LOW + DEFAULT_TARGET_HIGH) / 2) == "meets"

    def test_approaches_just_below_low(self):
        assert evaluate_target(DEFAULT_TARGET_LOW - 0.01) == "approaches"

    def test_falls_below_far_under_target(self):
        assert evaluate_target(0.0) == "falls_below"

    def test_boundary_exactly_at_target_low_is_meets(self):
        assert evaluate_target(DEFAULT_TARGET_LOW) == "meets"

    def test_boundary_exactly_at_target_high_is_exceeds(self):
        assert evaluate_target(DEFAULT_TARGET_HIGH) == "exceeds"


class TestBuildBacktestResultWiring:
    def test_slippage_and_commission_are_isolated_from_each_other(self):
        """`total_slippage` must isolate pure fill-price gap from
        commission (`theoretical_pnl - realistic_pnl - commission_paid`)
        -- a backtest with real commission but zero fill-price slippage
        (plain MID) must report ~zero slippage cost despite realistic
        pnl being lower than theoretical."""
        entry = _csp_entry(contracts_requested=1)
        entry_quote = make_quote(strike=95.0, right=OptionRight.PUT, bid=1.90, ask=2.10, quote_date=date(2024, 1, 2))
        settle_quote = make_quote(strike=95.0, right=OptionRight.PUT, bid=0.0, ask=0.0, quote_date=EXPIRATION, underlying_price=110.0)
        quotes = {date(2024, 1, 2): [entry_quote], EXPIRATION: [settle_quote]}
        config = _config(fill_model=FillModel.MID, commission_per_contract=0.65)
        state = run_backtest([entry], lambda t, d: quotes.get(d, []), [date(2024, 1, 2), EXPIRATION], config)

        result = build_backtest_result(state, config)
        assert result.slippage_cost_per_year == pytest.approx(0.0, abs=1e-6)
        assert result.trades[0].commission_paid > 0

    def test_target_category_reflects_realistic_not_theoretical_cagr(self):
        entry = _csp_entry(contracts_requested=1)
        entry_quote = make_quote(strike=95.0, right=OptionRight.PUT, bid=1.90, ask=2.10, quote_date=date(2024, 1, 2))
        settle_quote = make_quote(strike=95.0, right=OptionRight.PUT, bid=0.0, ask=0.0, quote_date=EXPIRATION, underlying_price=110.0)
        quotes = {date(2024, 1, 2): [entry_quote], EXPIRATION: [settle_quote]}
        config = _config(commission_per_contract=0.0)
        state = run_backtest([entry], lambda t, d: quotes.get(d, []), [date(2024, 1, 2), EXPIRATION], config)
        result = build_backtest_result(state, config)
        assert result.target_category == evaluate_target(result.realistic_metrics.cagr)

    def test_benchmark_is_none_when_no_spy_bars_supplied(self):
        entry = _csp_entry(contracts_requested=1)
        entry_quote = make_quote(strike=95.0, right=OptionRight.PUT, bid=1.90, ask=2.10, quote_date=date(2024, 1, 2))
        settle_quote = make_quote(strike=95.0, right=OptionRight.PUT, bid=0.0, ask=0.0, quote_date=EXPIRATION, underlying_price=110.0)
        quotes = {date(2024, 1, 2): [entry_quote], EXPIRATION: [settle_quote]}
        config = _config(commission_per_contract=0.0)
        state = run_backtest([entry], lambda t, d: quotes.get(d, []), [date(2024, 1, 2), EXPIRATION], config)
        result = build_backtest_result(state, config, spy_bars=None)
        assert result.benchmark is None

    def test_no_trades_produces_zero_valued_execution_stats_without_dividing_by_zero(self):
        config = _config(commission_per_contract=0.0)
        state = run_backtest([], lambda t, d: [], [date(2024, 1, 2)], config)
        result = build_backtest_result(state, config)
        assert result.slippage_cost_per_year == 0.0
        assert result.slippage_pct_of_gross_profit == 0.0
        assert result.average_bid_ask_spread_pct == 0.0


class TestEstimateCapitalAtRiskAllStrategyShapes:
    """Step 14B regression tests: `_estimate_capital_at_risk` previously
    (a) charged a debit vertical (bull call/bear put spread) the full
    strike width -- the same figure a credit spread of that width needs
    -- and (b) returned exactly 0 for any position with no short legs
    at all (long call/put, long straddle/strangle, a fresh protective
    put), silently under-reporting capital at risk for every
    long-premium Step 19A strategy. Both are fixed via the same
    `_is_credit_pairing` rule `src.brokers.paper` uses and a new
    `entry_credit_total` parameter."""

    def _leg(self, right: OptionRight, strike: float, side: str) -> BacktestLeg:
        return BacktestLeg(right=right, strike=strike, side=side)

    def test_put_credit_spread_still_uses_the_strike_width(self):
        legs = [self._leg(OptionRight.PUT, 95, "sell"), self._leg(OptionRight.PUT, 90, "buy")]
        assert _estimate_capital_at_risk(legs, 1, 0.0, entry_credit_total=140.0) == pytest.approx(500.0)

    def test_bull_call_spread_uses_only_the_debit_paid_not_the_width(self):
        legs = [self._leg(OptionRight.CALL, 95, "buy"), self._leg(OptionRight.CALL, 105, "sell")]
        # A $10-wide debit spread paid for at a $450 net debit.
        assert _estimate_capital_at_risk(legs, 1, 0.0, entry_credit_total=-450.0) == pytest.approx(450.0)

    def test_bear_put_spread_uses_only_the_debit_paid(self):
        legs = [self._leg(OptionRight.PUT, 100, "buy"), self._leg(OptionRight.PUT, 90, "sell")]
        assert _estimate_capital_at_risk(legs, 1, 0.0, entry_credit_total=-400.0) == pytest.approx(400.0)

    def test_cash_secured_put_unchanged(self):
        legs = [self._leg(OptionRight.PUT, 95, "sell")]
        assert _estimate_capital_at_risk(legs, 1, 0.0, entry_credit_total=280.0) == pytest.approx(9500.0)

    def test_covered_call_unchanged_uses_cost_basis(self):
        legs = [self._leg(OptionRight.CALL, 105, "sell")]
        assert _estimate_capital_at_risk(legs, 1, 90.0, entry_credit_total=230.0) == pytest.approx(9000.0)

    def test_long_call_uses_the_debit_paid_never_zero(self):
        legs = [self._leg(OptionRight.CALL, 100, "buy")]
        assert _estimate_capital_at_risk(legs, 1, 0.0, entry_credit_total=-350.0) == pytest.approx(350.0)

    def test_long_straddle_uses_the_total_debit_paid_never_zero(self):
        legs = [self._leg(OptionRight.CALL, 100, "buy"), self._leg(OptionRight.PUT, 100, "buy")]
        assert _estimate_capital_at_risk(legs, 1, 0.0, entry_credit_total=-620.0) == pytest.approx(620.0)

    def test_protective_put_bought_fresh_uses_the_put_debit(self):
        legs = [self._leg(OptionRight.PUT, 90, "buy")]
        assert _estimate_capital_at_risk(legs, 1, 0.0, entry_credit_total=-150.0) == pytest.approx(150.0)

    def test_protective_put_against_held_shares_uses_cost_basis_plus_debit(self):
        legs = [self._leg(OptionRight.PUT, 90, "buy")]
        assert _estimate_capital_at_risk(legs, 1, 100.0, entry_credit_total=-150.0) == pytest.approx(10150.0)

    def test_protective_collar_uses_cost_basis_not_the_short_calls_strike(self):
        legs = [self._leg(OptionRight.CALL, 110, "sell"), self._leg(OptionRight.PUT, 90, "buy")]
        assert _estimate_capital_at_risk(legs, 1, 100.0, entry_credit_total=20.0) == pytest.approx(10000.0)

    def test_default_entry_credit_total_is_backward_compatible_zero_for_credit_spreads(self):
        # Existing callers that never pass entry_credit_total still work
        # unchanged for the two shapes that don't need it.
        legs = [self._leg(OptionRight.PUT, 95, "sell"), self._leg(OptionRight.PUT, 90, "buy")]
        assert _estimate_capital_at_risk(legs, 1, 0.0) == pytest.approx(500.0)
