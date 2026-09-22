"""Part 24: Post-Trade Analysis -- actual outcome figures plus
counterfactual "what if" exits that never alter the recorded actual."""
from __future__ import annotations

from datetime import date

import pytest

from src.backtest.commissions import CommissionSchedule
from src.backtest.execution import execute_entry, execute_exit
from src.backtest.simulator import BacktestLeg, HistoricalOptionQuote
from src.brokers.paper import FillModel, PaperBrokerConfig
from src.data.option_chain import OptionRight as DataRight
from src.strategies.base import StrategyKind
from src.validation.post_trade_analysis import (
    build_closed_position_analysis,
    counterfactual_exit_at_quotes,
    counterfactual_hold_to_expiration,
)

EXPIRATION = date(2026, 10, 16)
ZERO_FRICTION = PaperBrokerConfig(fill_model=FillModel.MID, commission_per_contract=0.0, slippage_bps=0.0, spread_capture_fraction=0.0)
ZERO_COMMISSION = CommissionSchedule(per_contract=0.0)


def _quote(strike, bid, ask, qdate=date(2026, 9, 20), spot=100.0) -> HistoricalOptionQuote:
    return HistoricalOptionQuote(
        underlying="XYZ", quote_date=qdate, expiration=EXPIRATION, strike=strike, right=DataRight.PUT, bid=bid, ask=ask,
        volume=500, open_interest=1000, underlying_price=spot,
    )


@pytest.fixture
def real_entry():
    legs = [BacktestLeg(right=DataRight.PUT, strike=95.0, side="sell")]
    entry = execute_entry(
        legs=legs, expiration=EXPIRATION, quotes=[_quote(95.0, 1.95, 2.05)], requested_contracts=1,
        limit_price=-10_000.0, fill_config=ZERO_FRICTION, commission_schedule=ZERO_COMMISSION,
    )
    return legs, entry


class TestCounterfactualExitAtQuotes:
    def test_more_favorable_exit_quote_yields_higher_pnl(self, real_entry):
        legs, entry = real_entry
        cf = counterfactual_exit_at_quotes(
            legs=legs, expiration=EXPIRATION, entry_realistic_price=entry.realistic_price,
            entry_filled_contracts=entry.filled_contracts, entry_commission=entry.commission,
            exit_quotes=[_quote(95.0, 0.15, 0.25, qdate=date(2026, 10, 10))], label="exit_at_50pct",
            fill_config=ZERO_FRICTION, commission_schedule=ZERO_COMMISSION,
        )
        assert cf.label == "exit_at_50pct"
        assert cf.exit_reason == "closed"
        assert cf.hypothetical_pnl == pytest.approx(180.0)

    def test_entry_leg_is_held_fixed(self, real_entry):
        """The real entry (already known) is never re-priced --
        changing only the exit quotes changes the outcome."""
        legs, entry = real_entry
        cf1 = counterfactual_exit_at_quotes(
            legs=legs, expiration=EXPIRATION, entry_realistic_price=entry.realistic_price,
            entry_filled_contracts=entry.filled_contracts, entry_commission=entry.commission,
            exit_quotes=[_quote(95.0, 0.75, 0.85, qdate=date(2026, 9, 28))], label="early_exit",
            fill_config=ZERO_FRICTION, commission_schedule=ZERO_COMMISSION,
        )
        assert cf1.hypothetical_pnl == pytest.approx(120.0)


class TestCounterfactualHoldToExpiration:
    def test_otm_settlement_keeps_full_credit(self, real_entry):
        legs, entry = real_entry
        cf = counterfactual_hold_to_expiration(
            legs=legs, entry_realistic_price=entry.realistic_price, entry_filled_contracts=entry.filled_contracts,
            entry_commission=entry.commission, settlement_price=110.0,
        )
        assert cf.exit_reason == "expiration_otm"
        assert cf.hypothetical_pnl == pytest.approx(200.0)

    def test_itm_settlement_is_assignment(self, real_entry):
        legs, entry = real_entry
        cf = counterfactual_hold_to_expiration(
            legs=legs, entry_realistic_price=entry.realistic_price, entry_filled_contracts=entry.filled_contracts,
            entry_commission=entry.commission, settlement_price=80.0,
        )
        assert cf.exit_reason == "assignment"


class TestClosedPositionAnalysis:
    def test_derived_fields(self):
        analysis = build_closed_position_analysis(
            trade_id="T1", strategy_kind=StrategyKind.CASH_SECURED_PUT, management_policy_name="CSP_STANDARD",
            entry_date=date(2026, 9, 20), exit_date=date(2026, 9, 28), realized_pnl=120.0,
            capital_at_risk=9500.0, capital_committed=9500.0, mfe=140.0, mae=-5.0, commissions=1.3,
            exit_reason="profit_target_reached",
        )
        assert analysis.days_held == 8
        assert analysis.return_on_risk == pytest.approx(120.0 / 9500.0)
        assert analysis.return_on_committed_capital == pytest.approx(120.0 / 9500.0)
        assert analysis.exit_efficiency == pytest.approx(120.0 / 140.0)

    def test_none_capital_figures_yield_none_ratios_not_zero(self):
        analysis = build_closed_position_analysis(
            trade_id="T1", strategy_kind=StrategyKind.CASH_SECURED_PUT, management_policy_name="CSP_STANDARD",
            entry_date=date(2026, 9, 20), exit_date=date(2026, 9, 28), realized_pnl=120.0,
            capital_at_risk=None, capital_committed=None, mfe=0.0, mae=0.0, commissions=0.0, exit_reason="x",
        )
        assert analysis.return_on_risk is None
        assert analysis.return_on_committed_capital is None

    def test_never_profitable_position_has_none_exit_efficiency(self):
        analysis = build_closed_position_analysis(
            trade_id="T1", strategy_kind=StrategyKind.CASH_SECURED_PUT, management_policy_name="CSP_STANDARD",
            entry_date=date(2026, 9, 20), exit_date=date(2026, 9, 28), realized_pnl=-100.0,
            capital_at_risk=9500.0, capital_committed=9500.0, mfe=-10.0, mae=-100.0, commissions=0.0,
            exit_reason="loss_threshold_reached",
        )
        assert analysis.exit_efficiency is None

    def test_exit_before_entry_rejected(self):
        with pytest.raises(ValueError):
            build_closed_position_analysis(
                trade_id="T1", strategy_kind=StrategyKind.CASH_SECURED_PUT, management_policy_name="CSP_STANDARD",
                entry_date=date(2026, 9, 28), exit_date=date(2026, 9, 20), realized_pnl=0.0,
                capital_at_risk=None, capital_committed=None, mfe=0.0, mae=0.0, commissions=0.0, exit_reason="x",
            )

    def test_counterfactuals_never_alter_the_actual_realized_pnl(self, real_entry):
        legs, entry = real_entry
        cf = counterfactual_hold_to_expiration(
            legs=legs, entry_realistic_price=entry.realistic_price, entry_filled_contracts=entry.filled_contracts,
            entry_commission=entry.commission, settlement_price=110.0,
        )
        analysis = build_closed_position_analysis(
            trade_id="T1", strategy_kind=StrategyKind.CASH_SECURED_PUT, management_policy_name="CSP_STANDARD",
            entry_date=date(2026, 9, 20), exit_date=date(2026, 9, 28), realized_pnl=120.0,
            capital_at_risk=9500.0, capital_committed=9500.0, mfe=140.0, mae=-5.0, commissions=1.3,
            exit_reason="profit_target_reached", counterfactuals=[cf],
        )
        assert analysis.realized_pnl == 120.0  # unaffected by a much larger counterfactual
        assert len(analysis.counterfactuals) == 1
        assert analysis.counterfactuals[0].hypothetical_pnl == pytest.approx(200.0)
