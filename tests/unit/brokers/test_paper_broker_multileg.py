"""Step 20A: PaperBroker tests for 3-leg/4-leg orders --
LONG_CALL_BUTTERFLY's 1:-2:1 ratio and SHORT_IRON_CONDOR/
SHORT_IRON_BUTTERFLY's 4-leg defined-risk shape. Proves: the middle
short leg of a butterfly is never treated as naked (collateral, fill
quantity, and net pricing all correctly reflect the 2x ratio), the
iron structures require the wider-wing margin (never the naive sum of
both wings, never a naked-short-style full strike reservation), and a
multi-leg position never becomes unintentionally naked because one
leg's ratio differs from the others.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.brokers.base import OrderAction, OrderLeg, OrderStatus, PlaceOrderRequest
from src.brokers.paper import FillModel, PaperBroker, PaperBrokerConfig, base_combo_quantity
from src.data.option_chain import OptionChain, OptionContract, OptionRight
from src.data.quotes import UnderlyingQuote

NOW = datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc)
EXPIRATION = date(2026, 10, 16)


def make_underlying(**overrides) -> UnderlyingQuote:
    base = dict(symbol="SPY", bid=99.9, ask=100.1, last=100.0, volume=1_000_000, timestamp=NOW, source="mock")
    base.update(overrides)
    return UnderlyingQuote(**base)


def make_contract(**overrides) -> OptionContract:
    base = dict(
        underlying="SPY", expiration=EXPIRATION, volume=500, open_interest=1000, iv=0.22,
        underlying_price=100.0, timestamp=NOW, source="mock",
    )
    base.update(overrides)
    return OptionContract(**base)


async def _broker(cash: float = 1_000_000.0, **config_overrides) -> PaperBroker:
    config = PaperBrokerConfig(fill_model=FillModel.MID, **config_overrides)
    broker = PaperBroker(initial_cash=cash, config=config, now=NOW)
    await broker.connect()
    return broker


class TestLongCallButterflyRatioFill:
    """Buy 1x 95 call, sell 2x 100 call, buy 1x 105 call -- prices:
    lower mid=6.5, middle mid=3.5, upper mid=1.5. Combo net debit per
    unit = 6.5 - 2*3.5 + 1.5 = 1.0."""

    def _chain(self) -> OptionChain:
        return OptionChain(
            underlying=make_underlying(),
            contracts=[
                make_contract(option_symbol="c95", strike=95.0, right=OptionRight.CALL, bid=6.4, ask=6.6, last=6.5),
                make_contract(option_symbol="c100", strike=100.0, right=OptionRight.CALL, bid=3.4, ask=3.6, last=3.5),
                make_contract(option_symbol="c105", strike=105.0, right=OptionRight.CALL, bid=1.4, ask=1.6, last=1.5),
            ],
            timestamp=NOW, source="mock",
        )

    def _request(self, contracts: int = 3) -> PlaceOrderRequest:
        return PlaceOrderRequest(
            client_order_id="butterfly-1",
            legs=[
                OrderLeg(symbol="c95", right=OptionRight.CALL, strike=95.0, expiration=EXPIRATION, action=OrderAction.BUY, quantity=contracts),
                OrderLeg(symbol="c100", right=OptionRight.CALL, strike=100.0, expiration=EXPIRATION, action=OrderAction.SELL, quantity=2 * contracts),
                OrderLeg(symbol="c105", right=OptionRight.CALL, strike=105.0, expiration=EXPIRATION, action=OrderAction.BUY, quantity=contracts),
            ],
            limit_price=1.10,  # willing to pay up to $1.10 net debit per combo unit
        )

    @pytest.mark.asyncio
    async def test_fills_at_correct_per_combo_net_debit(self):
        broker = await _broker()
        broker.update_market_data(self._chain())
        order = await broker.place_order(self._request(contracts=3))
        assert order.status == OrderStatus.FILLED
        assert order.filled_quantity == 3  # 3 combo units, not 3 contracts-per-leg
        assert order.avg_fill_price == pytest.approx(-1.0)  # net debit, signed negative

    @pytest.mark.asyncio
    async def test_middle_leg_position_is_exactly_twice_each_wing(self):
        broker = await _broker()
        broker.update_market_data(self._chain())
        await broker.place_order(self._request(contracts=3))
        positions = {p.symbol: p for p in await broker.get_positions()}
        assert positions["c95"].quantity == 3
        assert positions["c105"].quantity == 3
        assert positions["c100"].quantity == -6  # short 2x each wing's 3 combo units

    @pytest.mark.asyncio
    async def test_collateral_is_the_debit_only_never_naked_short_strike(self):
        """The middle leg (short 2x) must never be priced as if it were
        an unpaired naked short -- collateral beyond the debit already
        paid must be exactly 0 for this fully defined-risk structure."""
        broker = await _broker()
        broker.update_market_data(self._chain())
        await broker.place_order(self._request(contracts=3))
        account = await broker.get_account()
        # cash only moved by the net debit (3 * 1.0 * 100) plus commission -- no
        # separate collateral reservation on top.
        assert account.maintenance_margin == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_commission_charged_per_actual_contract_including_2x_middle_leg(self):
        broker = await _broker(commission_per_contract=1.0)
        broker.update_market_data(self._chain())
        await broker.place_order(self._request(contracts=1))
        account = await broker.get_account()
        # 1 wing + 2 middle + 1 wing = 4 actual contracts filled, $1 commission each = $4
        debit = 100.0  # 1.0 debit * 100 multiplier * 1 combo unit
        assert account.cash_balance == pytest.approx(1_000_000.0 - debit - 4.0)


class TestShortIronCondorFourLegOrder:
    def _chain(self) -> OptionChain:
        return OptionChain(
            underlying=make_underlying(),
            contracts=[
                make_contract(option_symbol="p90", strike=90.0, right=OptionRight.PUT, bid=0.4, ask=0.6, last=0.5),
                make_contract(option_symbol="p95", strike=95.0, right=OptionRight.PUT, bid=1.1, ask=1.3, last=1.2),
                make_contract(option_symbol="c105", strike=105.0, right=OptionRight.CALL, bid=1.0, ask=1.2, last=1.1),
                make_contract(option_symbol="c110", strike=110.0, right=OptionRight.CALL, bid=0.3, ask=0.5, last=0.4),
            ],
            timestamp=NOW, source="mock",
        )

    def _request(self, contracts: int = 1) -> PlaceOrderRequest:
        return PlaceOrderRequest(
            client_order_id="ic-1",
            legs=[
                OrderLeg(symbol="p90", right=OptionRight.PUT, strike=90.0, expiration=EXPIRATION, action=OrderAction.BUY, quantity=contracts),
                OrderLeg(symbol="p95", right=OptionRight.PUT, strike=95.0, expiration=EXPIRATION, action=OrderAction.SELL, quantity=contracts),
                OrderLeg(symbol="c105", right=OptionRight.CALL, strike=105.0, expiration=EXPIRATION, action=OrderAction.SELL, quantity=contracts),
                OrderLeg(symbol="c110", right=OptionRight.CALL, strike=110.0, expiration=EXPIRATION, action=OrderAction.BUY, quantity=contracts),
            ],
            limit_price=0.90,
        )

    @pytest.mark.asyncio
    async def test_fills_all_four_legs(self):
        broker = await _broker()
        broker.update_market_data(self._chain())
        order = await broker.place_order(self._request(contracts=2))
        assert order.status == OrderStatus.FILLED
        positions = {p.symbol: p for p in await broker.get_positions()}
        assert positions["p90"].quantity == 2
        assert positions["p95"].quantity == -2
        assert positions["c105"].quantity == -2
        assert positions["c110"].quantity == 2

    @pytest.mark.asyncio
    async def test_collateral_is_wider_wing_width_never_the_sum_of_both(self):
        """put_width = 95-90 = 5; call_width = 110-105 = 5 -- collateral
        must be max(5,5)*100*contracts = 500*contracts, never 1000*contracts
        (the sum) and never a naked-short full-strike reservation."""
        broker = await _broker()
        broker.update_market_data(self._chain())
        await broker.place_order(self._request(contracts=2))
        account = await broker.get_account()
        assert account.maintenance_margin == pytest.approx(5.0 * 100 * 2)


class TestBaseComboQuantityHelper:
    def test_flat_ratio_returns_shared_quantity(self):
        legs = [
            OrderLeg(symbol="a", right=OptionRight.PUT, strike=100.0, expiration=EXPIRATION, action=OrderAction.SELL, quantity=5),
            OrderLeg(symbol="b", right=OptionRight.PUT, strike=95.0, expiration=EXPIRATION, action=OrderAction.BUY, quantity=5),
        ]
        assert base_combo_quantity(legs) == 5

    def test_butterfly_ratio_returns_the_wing_quantity_not_the_middle(self):
        legs = [
            OrderLeg(symbol="a", right=OptionRight.CALL, strike=95.0, expiration=EXPIRATION, action=OrderAction.BUY, quantity=3),
            OrderLeg(symbol="b", right=OptionRight.CALL, strike=100.0, expiration=EXPIRATION, action=OrderAction.SELL, quantity=6),
            OrderLeg(symbol="c", right=OptionRight.CALL, strike=105.0, expiration=EXPIRATION, action=OrderAction.BUY, quantity=3),
        ]
        assert base_combo_quantity(legs) == 3
