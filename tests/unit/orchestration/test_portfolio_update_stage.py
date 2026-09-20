"""SY-005 regression: `default_portfolio_update_stage` must update
`Portfolio.cash` alongside `positions` after a fill. Before this fix,
`cash` was bit-for-bit identical to the input regardless of the new
position's capital committed -- every cash-dependent Risk Engine check
(buying power, minimum cash reserve, capital-deployed cap) was
evaluated against a number that never reflected any prior fill."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.brokers.base import Order, OrderAction, OrderLeg, OrderStatus, OrderType
from src.data.option_chain import OptionRight
from src.orchestration.pipeline import default_portfolio_update_stage
from src.risk.trade_risk import QuantitativeAnalysis
from tests.unit.orchestration.conftest import EXPIRATION, NOW, make_portfolio, make_proposal

SHORT_SYMBOL = "SPY261016P00620000"
LONG_SYMBOL = "SPY261016P00615000"


def _qa(**overrides) -> QuantitativeAnalysis:
    base = dict(
        proposal_id="prop-1", generated_at=NOW, max_profit=150.0, max_loss=850.0, breakeven=619.15,
        capital_required=850.0, return_on_capital=0.176, annualized_roc=1.5, probability_of_profit=0.7,
        expected_value=80.0, net_delta=-0.2, net_vega=-1.5,
    )
    base.update(overrides)
    return QuantitativeAnalysis(**base)


def _order(**overrides) -> Order:
    base = dict(
        client_order_id="prop-1",
        broker_order_id="bo-1",
        legs=[
            OrderLeg(symbol=SHORT_SYMBOL, right=OptionRight.PUT, strike=620.0, expiration=EXPIRATION, action=OrderAction.SELL, quantity=2),
            OrderLeg(symbol=LONG_SYMBOL, right=OptionRight.PUT, strike=615.0, expiration=EXPIRATION, action=OrderAction.BUY, quantity=2),
        ],
        order_type=OrderType.LIMIT,
        limit_price=0.70,
        status=OrderStatus.FILLED,
        filled_quantity=2,
        avg_fill_price=0.75,
        timestamp=NOW,
        source="paper",
    )
    base.update(overrides)
    return Order(**base)


class TestCashIsUpdatedAfterAFill:
    def test_cash_decreases_by_the_new_positions_capital_at_risk(self):
        portfolio = make_portfolio(cash=90_000.0, nav=100_000.0)
        proposal = make_proposal(contracts_requested=2)
        qa = _qa(max_loss=850.0)
        order = _order()

        updated = default_portfolio_update_stage(portfolio, proposal, qa, order)

        # capital_at_risk on the new position is qa.max_loss * (filled/requested) = 850 * (2/2) = 850
        assert updated.cash == pytest.approx(90_000.0 - 850.0)
        assert updated.cash != portfolio.cash  # the SY-005 bug's old (wrong) behavior: bit-for-bit unchanged

    def test_partial_fill_scales_the_cash_reduction(self):
        portfolio = make_portfolio(cash=90_000.0, nav=100_000.0)
        proposal = make_proposal(contracts_requested=4)
        qa = _qa(max_loss=1_700.0)  # for the full 4-contract size
        order = _order(filled_quantity=2, status=OrderStatus.PARTIALLY_FILLED)

        updated = default_portfolio_update_stage(portfolio, proposal, qa, order)

        # capital_at_risk = 1700 * (2/4) = 850, same as the position's own capital_at_risk field
        assert updated.positions[0].capital_at_risk == pytest.approx(850.0)
        assert updated.cash == pytest.approx(90_000.0 - 850.0)

    def test_nav_is_unchanged_by_opening_a_position(self):
        """Opening a position moves cash into capital-at-risk -- it does
        not change net worth (NAV), only how much of it is free."""
        portfolio = make_portfolio(cash=90_000.0, nav=100_000.0)
        updated = default_portfolio_update_stage(portfolio, make_proposal(contracts_requested=2), _qa(max_loss=850.0), _order())
        assert updated.nav == portfolio.nav

    def test_cash_and_positions_are_both_updated_in_the_same_new_snapshot(self):
        portfolio = make_portfolio(cash=90_000.0, nav=100_000.0)
        updated = default_portfolio_update_stage(portfolio, make_proposal(contracts_requested=2), _qa(max_loss=850.0), _order())
        assert len(updated.positions) == 1
        assert updated.cash == pytest.approx(90_000.0 - 850.0)

    def test_capital_at_risk_exceeding_available_cash_fails_closed_not_silently(self):
        """Fails loud (a caught, recorded exception via SY-003's fix),
        never a silently-invalid negative-cash Portfolio -- `model_copy`
        does not re-run Portfolio's own Field(ge=0) validator, so this
        must be checked explicitly."""
        portfolio = make_portfolio(cash=500.0, nav=100_000.0)
        with pytest.raises(ValueError, match="cash negative"):
            default_portfolio_update_stage(portfolio, make_proposal(contracts_requested=2), _qa(max_loss=850.0), _order())
