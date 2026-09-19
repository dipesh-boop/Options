"""Tests for src.brokers.paper.PaperBroker (Step 12)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from src.brokers.base import OrderAction, OrderLeg, OrderStatus, PlaceOrderRequest
from src.brokers.paper import FillModel, PaperBroker, PaperBrokerConfig
from src.data.option_chain import OptionChain, OptionContract, OptionRight
from src.data.quotes import UnderlyingQuote

NOW = datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc)
EXPIRATION = date(2026, 10, 16)
SHORT_SYMBOL = "SPY261016P00620000"
LONG_SYMBOL = "SPY261016P00615000"
CALL_SYMBOL = "SPY261016C00640000"


def make_underlying(**overrides) -> UnderlyingQuote:
    base = dict(symbol="SPY", bid=628.4, ask=628.6, last=628.5, volume=1_000_000, timestamp=NOW, source="mock")
    base.update(overrides)
    return UnderlyingQuote(**base)


def make_contract(**overrides) -> OptionContract:
    base = dict(
        underlying="SPY",
        option_symbol=SHORT_SYMBOL,
        expiration=EXPIRATION,
        strike=620.0,
        right=OptionRight.PUT,
        bid=1.32,
        ask=1.38,
        last=1.35,
        volume=500,
        open_interest=1000,
        iv=0.18,
        underlying_price=628.5,
        timestamp=NOW,
        source="mock",
    )
    base.update(overrides)
    return OptionContract(**base)


def make_pcs_chain(**underlying_overrides) -> OptionChain:
    underlying = make_underlying(**underlying_overrides)
    short_leg = make_contract()
    long_leg = make_contract(option_symbol=LONG_SYMBOL, strike=615.0, bid=0.58, ask=0.62, last=0.60, volume=400, open_interest=800, iv=0.19)
    return OptionChain(underlying=underlying, contracts=[short_leg, long_leg], timestamp=NOW, source="mock")


def make_single_leg_chain(**contract_overrides) -> OptionChain:
    underlying = make_underlying()
    contract = make_contract(**contract_overrides)
    return OptionChain(underlying=underlying, contracts=[contract], timestamp=contract_overrides.get("timestamp", NOW), source="mock")


def pcs_request(**overrides) -> PlaceOrderRequest:
    base = dict(
        client_order_id="order-1",
        legs=[
            OrderLeg(symbol=SHORT_SYMBOL, right=OptionRight.PUT, strike=620.0, expiration=EXPIRATION, action=OrderAction.SELL, quantity=2),
            OrderLeg(symbol=LONG_SYMBOL, right=OptionRight.PUT, strike=615.0, expiration=EXPIRATION, action=OrderAction.BUY, quantity=2),
        ],
        limit_price=0.70,
    )
    base.update(overrides)
    return PlaceOrderRequest(**base)


def csp_request(**overrides) -> PlaceOrderRequest:
    base = dict(
        client_order_id="csp-1",
        legs=[OrderLeg(symbol=SHORT_SYMBOL, right=OptionRight.PUT, strike=620.0, expiration=EXPIRATION, action=OrderAction.SELL, quantity=1)],
        limit_price=1.30,
    )
    base.update(overrides)
    return PlaceOrderRequest(**base)


async def _broker(cash: float = 1_000_000.0, **config_overrides) -> PaperBroker:
    config = PaperBrokerConfig(**config_overrides) if config_overrides else PaperBrokerConfig()
    broker = PaperBroker(initial_cash=cash, config=config, now=NOW)
    await broker.connect()
    return broker


class TestBasicFillAndAccounting:
    @pytest.mark.asyncio
    async def test_put_credit_spread_fills_at_mid(self):
        broker = await _broker(fill_model=FillModel.MID)
        broker.update_market_data(make_pcs_chain())
        order = await broker.place_order(pcs_request())
        assert order.status == OrderStatus.FILLED
        assert order.filled_quantity == 2
        assert order.avg_fill_price == pytest.approx(0.75)  # 1.35 mid - 0.60 mid

    @pytest.mark.asyncio
    async def test_cash_and_collateral_after_fill(self):
        broker = await _broker(fill_model=FillModel.MID)
        broker.update_market_data(make_pcs_chain())
        await broker.place_order(pcs_request())
        account = await broker.get_account()
        # credit received: 2 * 0.75 * 100 = 150; commission: 0.65 * 2 legs * 2 contracts = 2.6
        assert account.cash_balance == pytest.approx(1_000_000.0 + 150.0 - 2.6)
        # collateral: spread width 5 * 100 * 2 contracts = 1000
        assert account.maintenance_margin == pytest.approx(1000.0)
        assert account.buying_power == pytest.approx(account.cash_balance - 1000.0)

    @pytest.mark.asyncio
    async def test_positions_created_for_both_legs(self):
        broker = await _broker(fill_model=FillModel.MID)
        broker.update_market_data(make_pcs_chain())
        await broker.place_order(pcs_request())
        positions = {p.symbol: p for p in await broker.get_positions()}
        assert positions[SHORT_SYMBOL].quantity == -2
        assert positions[LONG_SYMBOL].quantity == 2


class TestFillModels:
    @pytest.mark.asyncio
    async def test_bid_model_gives_worse_credit_than_mid(self):
        bid_broker = await _broker(fill_model=FillModel.BID)
        mid_broker = await _broker(fill_model=FillModel.MID)
        bid_broker.update_market_data(make_pcs_chain())
        mid_broker.update_market_data(make_pcs_chain())
        bid_order = await bid_broker.place_order(pcs_request(limit_price=0.01))
        mid_order = await mid_broker.place_order(pcs_request(client_order_id="order-2", limit_price=0.01))
        assert bid_order.avg_fill_price < mid_order.avg_fill_price

    @pytest.mark.asyncio
    async def test_ask_model_gives_better_credit_than_mid(self):
        ask_broker = await _broker(fill_model=FillModel.ASK)
        mid_broker = await _broker(fill_model=FillModel.MID)
        ask_broker.update_market_data(make_pcs_chain())
        mid_broker.update_market_data(make_pcs_chain())
        ask_order = await ask_broker.place_order(pcs_request(limit_price=0.01))
        mid_order = await mid_broker.place_order(pcs_request(client_order_id="order-2", limit_price=0.01))
        assert ask_order.avg_fill_price > mid_order.avg_fill_price

    @pytest.mark.asyncio
    async def test_mid_with_slippage_is_worse_than_mid(self):
        slip_broker = await _broker(fill_model=FillModel.MID_WITH_SLIPPAGE)
        mid_broker = await _broker(fill_model=FillModel.MID)
        slip_broker.update_market_data(make_pcs_chain())
        mid_broker.update_market_data(make_pcs_chain())
        slip_order = await slip_broker.place_order(pcs_request(limit_price=0.01))
        mid_order = await mid_broker.place_order(pcs_request(client_order_id="order-2", limit_price=0.01))
        assert slip_order.avg_fill_price < mid_order.avg_fill_price

    @pytest.mark.asyncio
    async def test_liquidity_adjusted_is_worse_still_for_thin_markets(self):
        liq_broker = await _broker(fill_model=FillModel.LIQUIDITY_ADJUSTED)
        slip_broker = await _broker(fill_model=FillModel.MID_WITH_SLIPPAGE)
        thin_chain = make_pcs_chain()
        liq_broker.update_market_data(thin_chain)
        slip_broker.update_market_data(thin_chain)
        # Force thin liquidity by using a low open-interest threshold config trick:
        # rebuild brokers with a threshold high enough that this chain counts as thin.
        liq_broker = await _broker(fill_model=FillModel.LIQUIDITY_ADJUSTED, thin_open_interest_threshold=5000)
        slip_broker = await _broker(fill_model=FillModel.MID_WITH_SLIPPAGE, thin_open_interest_threshold=5000)
        liq_broker.update_market_data(thin_chain)
        slip_broker.update_market_data(thin_chain)
        liq_order = await liq_broker.place_order(pcs_request(limit_price=0.01))
        slip_order = await slip_broker.place_order(pcs_request(client_order_id="order-2", limit_price=0.01))
        assert liq_order.avg_fill_price < slip_order.avg_fill_price

    @pytest.mark.asyncio
    async def test_default_fill_model_is_liquidity_adjusted(self):
        config = PaperBrokerConfig()
        assert config.fill_model == FillModel.LIQUIDITY_ADJUSTED


class TestNoFill:
    @pytest.mark.asyncio
    async def test_limit_above_market_credit_does_not_fill(self):
        broker = await _broker(fill_model=FillModel.MID)
        broker.update_market_data(make_pcs_chain())
        order = await broker.place_order(pcs_request(limit_price=2.00))  # mid is 0.75
        assert order.status == OrderStatus.SUBMITTED
        assert order.filled_quantity == 0

    @pytest.mark.asyncio
    async def test_resting_order_fills_later_after_market_moves(self):
        broker = await _broker(fill_model=FillModel.MID)
        broker.update_market_data(make_pcs_chain())
        order = await broker.place_order(pcs_request(limit_price=2.00))
        assert order.status == OrderStatus.SUBMITTED

        # Market moves: short leg's premium jumps.
        moved = make_pcs_chain()
        richer_short = moved.contracts[0].model_copy(update={"bid": 3.20, "ask": 3.30, "last": 3.25})
        moved = moved.model_copy(update={"contracts": [richer_short, moved.contracts[1]]})
        broker.update_market_data(moved)
        updated = await broker.attempt_fill("order-1")
        assert updated.status == OrderStatus.FILLED


class TestPartialFill:
    @pytest.mark.asyncio
    async def test_thin_volume_caps_fill_quantity(self):
        broker = await _broker(cash=100_000_000.0, fill_model=FillModel.MID, max_fill_fraction_of_volume=0.1)
        broker.update_market_data(make_single_leg_chain(volume=50, open_interest=1000))
        order = await broker.place_order(csp_request(legs=[
            OrderLeg(symbol=SHORT_SYMBOL, right=OptionRight.PUT, strike=620.0, expiration=EXPIRATION, action=OrderAction.SELL, quantity=100)
        ]))
        assert order.status == OrderStatus.PARTIALLY_FILLED
        assert 0 < order.filled_quantity < 100

    @pytest.mark.asyncio
    async def test_partial_fill_can_complete_on_a_later_attempt(self):
        broker = await _broker(cash=100_000_000.0, fill_model=FillModel.MID, max_fill_fraction_of_volume=0.5, min_guaranteed_fill_contracts=1)
        broker.update_market_data(make_single_leg_chain(volume=10, open_interest=1000))
        order = await broker.place_order(csp_request(legs=[
            OrderLeg(symbol=SHORT_SYMBOL, right=OptionRight.PUT, strike=620.0, expiration=EXPIRATION, action=OrderAction.SELL, quantity=10)
        ]))
        assert order.status == OrderStatus.PARTIALLY_FILLED
        first_filled = order.filled_quantity

        broker.update_market_data(make_single_leg_chain(volume=10_000, open_interest=1000))
        updated = await broker.attempt_fill("csp-1")
        assert updated.filled_quantity >= first_filled
        assert updated.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)


class TestStaleData:
    @pytest.mark.asyncio
    async def test_stale_quote_rejects_the_order(self):
        broker = await _broker()
        broker.update_market_data(make_single_leg_chain(timestamp=NOW - timedelta(hours=2)))
        order = await broker.place_order(csp_request())
        assert order.status == OrderStatus.REJECTED
        assert "stale" in (broker.get_rejection_reason("csp-1") or "").lower() or "old" in (broker.get_rejection_reason("csp-1") or "").lower()

    @pytest.mark.asyncio
    async def test_missing_quote_rejects_the_order(self):
        broker = await _broker()
        # No market data loaded at all.
        order = await broker.place_order(csp_request())
        assert order.status == OrderStatus.REJECTED


class TestWideSpreads:
    @pytest.mark.asyncio
    async def test_wide_spread_still_fills_but_worse_than_a_tight_market(self):
        tight_broker = await _broker(fill_model=FillModel.LIQUIDITY_ADJUSTED)
        wide_broker = await _broker(fill_model=FillModel.LIQUIDITY_ADJUSTED)
        tight_broker.update_market_data(make_single_leg_chain(bid=1.32, ask=1.38, volume=1000, open_interest=5000))
        wide_broker.update_market_data(make_single_leg_chain(bid=0.90, ask=1.80, volume=1000, open_interest=5000))
        tight_order = await tight_broker.place_order(csp_request(limit_price=0.5))
        wide_order = await wide_broker.place_order(csp_request(limit_price=0.5))
        assert wide_order.avg_fill_price < tight_order.avg_fill_price


class TestDuplicateOrders:
    @pytest.mark.asyncio
    async def test_same_client_order_id_returns_the_same_order_not_a_new_one(self):
        broker = await _broker(fill_model=FillModel.MID)
        broker.update_market_data(make_pcs_chain())
        first = await broker.place_order(pcs_request())
        second = await broker.place_order(pcs_request())
        assert first.broker_order_id == second.broker_order_id
        fills = await broker.get_fills()
        # Only the first submission actually generated fills (2 legs).
        assert len(fills) == 2


class TestCancelOrder:
    @pytest.mark.asyncio
    async def test_resting_order_can_be_cancelled(self):
        broker = await _broker(fill_model=FillModel.MID)
        broker.update_market_data(make_pcs_chain())
        order = await broker.place_order(pcs_request(limit_price=2.00))
        assert order.status == OrderStatus.SUBMITTED
        cancelled = await broker.cancel_order("order-1")
        assert cancelled.status == OrderStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_filled_order_cannot_be_cancelled(self):
        broker = await _broker(fill_model=FillModel.MID)
        broker.update_market_data(make_pcs_chain())
        order = await broker.place_order(pcs_request())
        assert order.status == OrderStatus.FILLED
        result = await broker.cancel_order("order-1")
        assert result.status == OrderStatus.FILLED  # unchanged, filled is terminal


class TestInsufficientCash:
    @pytest.mark.asyncio
    async def test_naked_put_beyond_buying_power_is_rejected(self):
        broker = await _broker(cash=1_000.0)
        broker.update_market_data(make_single_leg_chain())
        order = await broker.place_order(csp_request())
        assert order.status == OrderStatus.REJECTED
        assert "collateral" in (broker.get_rejection_reason("csp-1") or "")

    @pytest.mark.asyncio
    async def test_put_credit_spread_needs_only_the_defined_risk_width_not_full_naked_collateral(self):
        # Width x 100 x 2 = 1000, well within a $50k account, even though
        # the short leg alone would need $124,000 cash-secured naked.
        broker = await _broker(cash=50_000.0, fill_model=FillModel.MID)
        broker.update_market_data(make_pcs_chain())
        order = await broker.place_order(pcs_request())
        assert order.status == OrderStatus.FILLED


class TestPositionClosing:
    @pytest.mark.asyncio
    async def test_closing_a_put_credit_spread_releases_collateral_and_positions(self):
        broker = await _broker(fill_model=FillModel.MID)
        broker.update_market_data(make_pcs_chain())
        await broker.place_order(pcs_request())
        account_open = await broker.get_account()
        assert account_open.maintenance_margin == pytest.approx(1000.0)

        close_request = PlaceOrderRequest(
            client_order_id="close-1",
            legs=[
                OrderLeg(symbol=SHORT_SYMBOL, right=OptionRight.PUT, strike=620.0, expiration=EXPIRATION, action=OrderAction.BUY, quantity=2),
                OrderLeg(symbol=LONG_SYMBOL, right=OptionRight.PUT, strike=615.0, expiration=EXPIRATION, action=OrderAction.SELL, quantity=2),
            ],
            limit_price=0.01,  # willing to pay up to $0.01 net... but this is a debit close, needs slack
        )
        # A closing trade is a net debit (buying back the spread); use a
        # generous limit so it can fill under the MID model in this test.
        close_request = close_request.model_copy(update={"limit_price": 5.0})
        order = await broker.place_order(close_request)
        assert order.status == OrderStatus.FILLED
        positions = await broker.get_positions()
        assert positions == []
        account_closed = await broker.get_account()
        assert account_closed.maintenance_margin == pytest.approx(0.0)


class TestExpirationAssignmentExercise:
    @pytest.mark.asyncio
    async def test_otm_expiration_removes_position_with_no_extra_cash_impact(self):
        broker = await _broker(fill_model=FillModel.MID)
        broker.update_market_data(make_single_leg_chain())
        await broker.place_order(csp_request())
        cash_before = (await broker.get_account()).cash_balance
        settlements = broker.settle_expiration("SPY", EXPIRATION, settlement_price=628.5)  # well above 620 strike -> OTM put
        assert len(settlements) == 1
        assert settlements[0].was_itm is False
        assert settlements[0].assigned_or_exercised is False
        assert (await broker.get_account()).cash_balance == pytest.approx(cash_before)
        assert await broker.get_positions() == []

    @pytest.mark.asyncio
    async def test_itm_short_put_is_assigned_buys_shares_at_strike(self):
        broker = await _broker(fill_model=FillModel.MID)
        broker.update_market_data(make_single_leg_chain())
        await broker.place_order(csp_request())
        settlements = broker.settle_expiration("SPY", EXPIRATION, settlement_price=600.0)  # below 620 -> ITM put
        assert settlements[0].was_itm is True
        assert settlements[0].assigned_or_exercised is True
        assert settlements[0].share_impact == 100
        assert settlements[0].cash_impact == pytest.approx(-62_000.0)
        positions = {p.symbol: p for p in await broker.get_positions()}
        assert positions["SPY"].quantity == 100

    @pytest.mark.asyncio
    async def test_covered_call_assignment_sells_shares_at_strike(self):
        broker = await _broker(cash=200_000.0, fill_model=FillModel.MID)
        # First, get long 100 shares via a direct equity fill: simplest
        # path is to simulate assignment on a short put to acquire them.
        broker.update_market_data(make_single_leg_chain())
        await broker.place_order(csp_request())
        broker.settle_expiration("SPY", EXPIRATION, settlement_price=600.0)
        shares = {p.symbol: p for p in await broker.get_positions()}["SPY"]
        assert shares.quantity == 100

        # Now sell a covered call against those shares.
        call_expiration = date(2026, 11, 20)
        call_chain = OptionChain(
            underlying=make_underlying(),
            contracts=[
                make_contract(
                    option_symbol=CALL_SYMBOL, expiration=call_expiration, strike=640.0, right=OptionRight.CALL,
                    bid=1.18, ask=1.24, last=1.20, underlying_price=628.5,
                )
            ],
            timestamp=NOW,
            source="mock",
        )
        broker.update_market_data(call_chain)
        call_request = PlaceOrderRequest(
            client_order_id="cc-1",
            legs=[OrderLeg(symbol=CALL_SYMBOL, right=OptionRight.CALL, strike=640.0, expiration=call_expiration, action=OrderAction.SELL, quantity=1)],
            limit_price=1.0,
        )
        call_order = await broker.place_order(call_request)
        assert call_order.status == OrderStatus.FILLED

        account_before_assignment = await broker.get_account()
        assert account_before_assignment.maintenance_margin == pytest.approx(0.0)  # covered, no cash collateral needed

        settlements = broker.settle_expiration("SPY", call_expiration, settlement_price=650.0)  # ITM call
        assert settlements[0].was_itm is True
        assert settlements[0].share_impact == -100
        assert settlements[0].cash_impact == pytest.approx(64_000.0)
        positions_after = await broker.get_positions()
        assert not any(p.symbol == "SPY" and p.quantity != 0 for p in positions_after)


class TestBrokerLifecycle:
    @pytest.mark.asyncio
    async def test_connect_and_health_check(self):
        broker = PaperBroker(initial_cash=100_000.0, now=NOW)
        assert broker.is_connected() is False
        await broker.connect()
        assert broker.is_connected() is True
        health = await broker.health_check()
        assert health.connected is True

    @pytest.mark.asyncio
    async def test_reconcile_is_always_clean(self):
        broker = await _broker()
        report = await broker.reconcile()
        assert report.is_clean is True

    @pytest.mark.asyncio
    async def test_get_open_orders_excludes_terminal_orders(self):
        broker = await _broker(fill_model=FillModel.MID)
        broker.update_market_data(make_pcs_chain())
        await broker.place_order(pcs_request())  # fills -> terminal
        resting = await broker.place_order(pcs_request(client_order_id="resting-1", limit_price=5.0))
        assert resting.status == OrderStatus.SUBMITTED
        open_orders = await broker.get_open_orders()
        assert [o.client_order_id for o in open_orders] == ["resting-1"]
