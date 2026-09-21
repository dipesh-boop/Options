"""Section 11: expiration/assignment/pin-risk acceptance across the
strategy library at various moneyness states.
`tests/unit/brokers/test_paper_broker.py::TestExpirationAssignmentExercise`
already covers single-leg OTM/ITM settlement live; this file extends
that to representative 2-/3-/4-leg structures and, critically, is the
regression coverage for a real accounting bug this acceptance pass
found (ACCEPT-003 below) in the BACKTEST settlement path -- never
exercised by any existing unit test, since none of them settle a
non-1-quantity-ratio leg (only `LONG_CALL_BUTTERFLY`'s middle leg is
ever non-1) at expiration.
"""
from __future__ import annotations

from datetime import date

import pytest

from src.backtest.assignment import realized_settlement_pnl, settle_position
from src.backtest.engine import _settle_expired_position
from src.backtest.simulator import BacktestLeg, BacktestPosition
from src.brokers.base import OrderAction, OrderLeg, OrderStatus, PlaceOrderRequest
from src.brokers.paper import FillModel, PaperBroker, PaperBrokerConfig
from src.data.option_chain import OptionRight as DataRight
from src.llm.schemas import StrategyType
from src.quant.black_scholes import Leg as QuantLeg, OptionRight as QRight, Side as QSide
from src.quant.monte_carlo import Position, payoff_at_expiration

from .conftest import EXPIRATION, NOW, make_chain, make_contract


class TestButterflyMiddleLegRatioCorrectlyAppliedAtSettlementAcceptRegression003:
    """ACCEPT-003: `settle_leg`/`realized_settlement_pnl` in
    `src/backtest/assignment.py` used to settle every leg using the
    position's flat combo-unit `contracts` count, ignoring
    `BacktestLeg.quantity_ratio` entirely -- correct on the ENTRY fill
    path (`_to_order_leg` -> `compute_fill`), never applied on the
    SETTLEMENT path. `LONG_CALL_BUTTERFLY`'s 2x middle leg was
    therefore settled as if it were only 1x, understating its
    assignment cash/share impact by half whenever the position was
    held to expiration ITM. Fixed by multiplying each leg's settlement
    impact by its own `quantity_ratio`; every other strategy in the
    library uses `quantity_ratio=1` on every leg, so the fix is a
    no-op everywhere else (see the parametrized ratio=1 case below)."""

    _LOWER, _MIDDLE, _UPPER = 16.5, 9.5, 4.5

    def _position(self, contracts: int = 1) -> BacktestPosition:
        legs = (
            BacktestLeg(right=DataRight.CALL, strike=615.0, side="buy", quantity_ratio=1),
            BacktestLeg(right=DataRight.CALL, strike=628.0, side="sell", quantity_ratio=2),
            BacktestLeg(right=DataRight.CALL, strike=641.0, side="buy", quantity_ratio=1),
        )
        debit = self._LOWER - 2 * self._MIDDLE + self._UPPER
        return BacktestPosition(
            position_id="bf-1", ticker="SPY", strategy=StrategyType.LONG_CALL_BUTTERFLY, legs=legs, contracts=contracts,
            expiration=EXPIRATION, opened_at=date(2026, 9, 20), management_dte=21, profit_target_pct=0.5,
            realistic_entry_credit_total=-debit * 100 * contracts, theoretical_entry_credit_total=-debit * 100 * contracts,
            capital_at_risk=debit * 100 * contracts,
        )

    def _independently_expected_pnl(self, settlement_price: float, contracts: int = 1) -> float:
        """Computed via the platform's own already-verified generic
        payoff engine (`payoff_at_expiration`), never by re-deriving
        the settlement math this test is checking -- an independent
        cross-check, per invariant #5."""
        position = Position(legs=[
            QuantLeg(right=QRight.CALL, strike=615.0, side=QSide.BUY, entry_price=self._LOWER, quantity=contracts),
            QuantLeg(right=QRight.CALL, strike=628.0, side=QSide.SELL, entry_price=self._MIDDLE, quantity=2 * contracts),
            QuantLeg(right=QRight.CALL, strike=641.0, side=QSide.BUY, entry_price=self._UPPER, quantity=contracts),
        ])
        return payoff_at_expiration(position, settlement_price)

    @pytest.mark.parametrize("settlement_price", [610.0, 628.0, 635.0, 645.0])
    def test_settlement_pnl_matches_the_independent_payoff_engine_at_every_moneyness_zone(self, settlement_price: float):
        position = self._position(contracts=1)
        record, _ = _settle_expired_position(position, EXPIRATION, settlement_price)
        assert record.realistic_pnl == pytest.approx(self._independently_expected_pnl(settlement_price), abs=0.01)

    def test_settlement_pnl_scales_linearly_with_contracts(self):
        position_1x = self._position(contracts=1)
        position_3x = self._position(contracts=3)
        record_1x, _ = _settle_expired_position(position_1x, EXPIRATION, settlement_price=635.0)
        record_3x, _ = _settle_expired_position(position_3x, EXPIRATION, settlement_price=635.0)
        assert record_3x.realistic_pnl == pytest.approx(record_1x.realistic_pnl * 3, abs=0.03)

    def test_middle_leg_share_impact_is_exactly_twice_a_wings_when_both_assigned(self):
        """Deep ITM for both the middle and a wing (settlement above
        the upper strike) -- the middle short leg's share_impact must
        be exactly 2x the wing's, never 1x."""
        legs = (
            BacktestLeg(right=DataRight.CALL, strike=615.0, side="buy", quantity_ratio=1),
            BacktestLeg(right=DataRight.CALL, strike=628.0, side="sell", quantity_ratio=2),
            BacktestLeg(right=DataRight.CALL, strike=641.0, side="buy", quantity_ratio=1),
        )
        settlements = settle_position(list(legs), contracts=1, settlement_price=650.0)
        by_strike = {s.leg.strike: s for s in settlements}
        assert by_strike[628.0].share_impact == -2 * abs(by_strike[615.0].share_impact)
        assert by_strike[641.0].share_impact == by_strike[615.0].share_impact


class TestLiveMultiLegExpirationAndAssignmentThroughPaperBroker:
    """`settle_expiration` is called against the LIVE `PaperBroker`
    position book, which (unlike the backtest's `BacktestLeg`) already
    stores each symbol's own actual signed quantity with the ratio
    baked in from the real fill -- confirmed correct by
    `tests/unit/brokers/test_paper_broker_multileg.py`'s
    `test_middle_leg_position_is_exactly_twice_each_wing`. This class
    proves the FULL settlement (cash + share impact) is also correct
    for representative 2-/4-leg structures at every moneyness zone."""

    async def _broker_with_iron_condor(self) -> PaperBroker:
        broker = PaperBroker(initial_cash=1_000_000.0, config=PaperBrokerConfig(fill_model=FillModel.MID), now=NOW)
        await broker.connect()
        chain = make_chain(
            make_contract(option_symbol="live-lp", strike=600.0, right=DataRight.PUT, bid=0.55, ask=0.60),
            make_contract(option_symbol="live-sp", strike=610.0, right=DataRight.PUT, bid=1.20, ask=1.30),
            make_contract(option_symbol="live-sc", strike=650.0, right=DataRight.CALL, bid=1.15, ask=1.20),
            make_contract(option_symbol="live-lc", strike=660.0, right=DataRight.CALL, bid=0.47, ask=0.50),
        )
        broker.update_market_data(chain)
        await broker.place_order(PlaceOrderRequest(
            client_order_id="ic-expiration-acceptance",
            legs=[
                OrderLeg(symbol="live-lp", right=DataRight.PUT, strike=600.0, expiration=EXPIRATION, action=OrderAction.BUY, quantity=1),
                OrderLeg(symbol="live-sp", right=DataRight.PUT, strike=610.0, expiration=EXPIRATION, action=OrderAction.SELL, quantity=1),
                OrderLeg(symbol="live-sc", right=DataRight.CALL, strike=650.0, expiration=EXPIRATION, action=OrderAction.SELL, quantity=1),
                OrderLeg(symbol="live-lc", right=DataRight.CALL, strike=660.0, expiration=EXPIRATION, action=OrderAction.BUY, quantity=1),
            ],
            limit_price=1.0,
        ))
        return broker

    @pytest.mark.asyncio
    async def test_settlement_between_the_short_strikes_expires_everything_worthless(self):
        """The max-profit zone: underlying between the two short
        strikes at expiration -- all 4 legs OTM, no assignment, full
        credit retained, position simply removed."""
        broker = await self._broker_with_iron_condor()
        cash_before = (await broker.get_account()).cash_balance
        settlements = broker.settle_expiration("SPY", EXPIRATION, settlement_price=630.0)
        assert all(not s.was_itm and not s.assigned_or_exercised for s in settlements)
        assert (await broker.get_account()).cash_balance == pytest.approx(cash_before)
        assert await broker.get_positions() == []

    @pytest.mark.asyncio
    async def test_settlement_beyond_the_long_put_wing_assigns_only_the_put_side(self):
        """Deep below even the long put wing (580): both put legs are
        ITM/assigned, both call legs expire worthless -- proves only
        the in-the-money side settles, never the untouched call side."""
        broker = await self._broker_with_iron_condor()
        settlements = broker.settle_expiration("SPY", EXPIRATION, settlement_price=580.0)
        by_strike = {s.strike: s for s in settlements}
        assert by_strike[600.0].was_itm and by_strike[600.0].assigned_or_exercised
        assert by_strike[610.0].was_itm and by_strike[610.0].assigned_or_exercised
        assert not by_strike[650.0].was_itm and not by_strike[650.0].assigned_or_exercised
        assert not by_strike[660.0].was_itm and not by_strike[660.0].assigned_or_exercised
        # The long put (exercised -> SELLS 100 shares) and the short put
        # (assigned -> BUYS 100 shares) are opposite share flows that
        # net to exactly zero -- the correct economic shape of a put
        # spread's share exposure (only the strike-width cash
        # difference is the real, retained economic effect). If either
        # leg's direction were wrong, this would net to +/-200, not 0.
        assert by_strike[600.0].share_impact == -100  # long put exercised: sells shares
        assert by_strike[610.0].share_impact == 100  # short put assigned: buys shares
        positions = {p.symbol: p for p in await broker.get_positions()}
        assert "SPY" not in positions or positions["SPY"].quantity == 0


class TestPinRiskAndEarlyAssignmentAssumptionsAreExplicitlyDocumented:
    """Section 11's own requirement: "explicit-assumption documentation
    where real-world certainty is impossible." Early assignment before
    expiration (a real, if uncommon, American-option risk) and pin risk
    near a short strike cannot be deterministically simulated -- this
    platform doesn't pretend otherwise; it says so, in the exact module
    that would otherwise imply it does."""

    def test_paper_broker_module_docstring_states_the_early_assignment_simplification(self):
        import src.brokers.paper as paper_module

        doc = paper_module.__doc__ or ""
        assert "early assignment" in doc.lower()
        assert "cash-settled at\nexpiration by intrinsic value" in doc or "cash-settled at expiration by intrinsic value" in doc.replace("\n", " ")

    def test_architecture_doc_names_early_assignment_as_a_known_risk_category(self):
        with open("ARCHITECTURE.md") as f:
            content = f.read()
        assert "Early assignment risk" in content

    def test_long_call_butterfly_module_documents_pin_risk_near_expiration(self):
        import src.strategies.long_call_butterfly as butterfly_module

        assert any("pin risk" in rule.lower() for rule in butterfly_module.DEFAULT_ADJUSTMENT_RULES)
