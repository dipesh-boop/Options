"""Tests for order placement, idempotency/duplicate prevention,
cancellation, open orders, and fills. Also proves place_order and
cancel_order are never automatically retried."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from src.brokers.base import (
    DuplicateOrderError,
    Order,
    OrderAction,
    OrderLeg,
    OrderStatus,
    PlaceOrderRequest,
)
from src.brokers.ibkr import IBKRBroker, IBKRConfig
from src.data.option_chain import OptionRight
from tests.unit.brokers.fakes import FakeIBClient


def _csp_request(client_order_id: str = "csp-1") -> PlaceOrderRequest:
    return PlaceOrderRequest(
        client_order_id=client_order_id,
        legs=[OrderLeg(symbol="AAPL", right=OptionRight.PUT, strike=210.0, expiration=date(2026, 3, 20), action=OrderAction.SELL, quantity=1)],
        limit_price=2.50,
    )


async def _connected_broker(**fake_kwargs) -> tuple[IBKRBroker, FakeIBClient]:
    fake = FakeIBClient(**fake_kwargs)
    broker = IBKRBroker(config=IBKRConfig(port=7497), ib_client=fake)
    await broker.connect()
    return broker, fake


class TestPlaceOrderHappyPath:
    @pytest.mark.asyncio
    async def test_places_order_and_returns_canonical_order(self):
        broker, fake = await _connected_broker()
        order = await broker.place_order(_csp_request())
        assert isinstance(order, Order)
        assert order.client_order_id == "csp-1"
        assert order.broker_order_id is not None
        assert order.status == OrderStatus.SUBMITTED
        assert fake.place_order_call_count == 1

    @pytest.mark.asyncio
    async def test_order_tagged_with_order_ref_for_traceability(self):
        broker, fake = await _connected_broker()
        await broker.place_order(_csp_request())
        assert fake.orders[0].order.orderRef == "csp-1"

    @pytest.mark.asyncio
    async def test_combo_order_for_two_legs(self):
        broker, fake = await _connected_broker()
        request = PlaceOrderRequest(
            client_order_id="pcs-1",
            legs=[
                OrderLeg(symbol="AAPL", right=OptionRight.PUT, strike=220.0, expiration=date(2026, 3, 20), action=OrderAction.SELL, quantity=1),
                OrderLeg(symbol="AAPL", right=OptionRight.PUT, strike=210.0, expiration=date(2026, 3, 20), action=OrderAction.BUY, quantity=1),
            ],
            limit_price=1.20,
        )
        order = await broker.place_order(request)
        assert order.client_order_id == "pcs-1"
        assert len(order.legs) == 2
        assert fake.orders[0].contract.secType == "BAG"


class TestIdempotencyPreventsDuplicateOrders:
    @pytest.mark.asyncio
    async def test_resubmitting_same_client_order_id_does_not_place_a_second_order(self):
        broker, fake = await _connected_broker()
        request = _csp_request()
        order1 = await broker.place_order(request)
        order2 = await broker.place_order(request)
        assert order1.broker_order_id == order2.broker_order_id
        assert fake.place_order_call_count == 1
        assert len(fake.orders) == 1

    @pytest.mark.asyncio
    async def test_many_retries_still_only_place_one_order(self):
        broker, fake = await _connected_broker()
        request = _csp_request()
        for _ in range(10):
            await broker.place_order(request)
        assert fake.place_order_call_count == 1

    @pytest.mark.asyncio
    async def test_different_client_order_ids_place_separate_orders(self):
        broker, fake = await _connected_broker()
        await broker.place_order(_csp_request("csp-1"))
        await broker.place_order(_csp_request("csp-2"))
        assert fake.place_order_call_count == 2

    @pytest.mark.asyncio
    async def test_broker_side_order_found_by_orderref_adopted_without_resubmitting(self):
        """Simulates the crash-after-submit-before-recording-locally
        case: an order with this client_order_id already exists at the
        broker (found via orderRef), but our local idempotency store has
        no record of it (e.g. the process restarted). place_order must
        adopt the existing broker order, never submit a second one."""
        broker, fake = await _connected_broker()
        # Simulate a prior, already-submitted broker-side order that our
        # local store doesn't know about.
        pre_existing_order = SimpleNamespace(orderId=555, orderRef="csp-1", action="SELL", totalQuantity=1, lmtPrice=2.5)
        fake.orders.append(SimpleNamespace(order=pre_existing_order, orderStatus=SimpleNamespace(status="Submitted", filled=0), contract=SimpleNamespace(symbol="AAPL")))

        order = await broker.place_order(_csp_request("csp-1"))

        assert order.broker_order_id == "555"
        assert fake.place_order_call_count == 0  # never actually submitted again

    @pytest.mark.asyncio
    async def test_two_broker_side_orders_with_same_orderref_raises_duplicate_order_error(self):
        """A genuine data-integrity invariant violation: the same
        client_order_id should never resolve to more than one open
        broker-side order. If it somehow does, place_order must fail
        loudly rather than silently pick one."""
        broker, fake = await _connected_broker()
        for order_id in (777, 888):
            raw_order = SimpleNamespace(orderId=order_id, orderRef="csp-1", action="SELL", totalQuantity=1, lmtPrice=2.5)
            fake.orders.append(
                SimpleNamespace(
                    order=raw_order,
                    orderStatus=SimpleNamespace(status="Submitted", filled=0),
                    contract=SimpleNamespace(symbol="AAPL"),
                )
            )
        with pytest.raises(DuplicateOrderError):
            await broker.place_order(_csp_request("csp-1"))


class TestPlaceOrderNeverAutoRetried:
    @pytest.mark.asyncio
    async def test_transport_failure_propagates_immediately_no_retry(self):
        class FailingPlaceOrderClient(FakeIBClient):
            def placeOrder(self, contract, order):
                self.place_order_call_count += 1
                raise ConnectionError("simulated broker timeout - outcome unknown")

        fake = FailingPlaceOrderClient()
        broker = IBKRBroker(config=IBKRConfig(port=7497), ib_client=fake)
        await broker.connect()

        with pytest.raises(ConnectionError):
            await broker.place_order(_csp_request())

        # Exactly one attempt - place_order must never retry blindly,
        # since a second attempt after an uncertain failure could create
        # a duplicate order.
        assert fake.place_order_call_count == 1

    @pytest.mark.asyncio
    async def test_caller_retry_with_same_client_order_id_is_safe(self):
        """The correct pattern: the CALLER decides to retry after a
        failure, using the same client_order_id. That retry is safe
        because of idempotency, not because of automatic retry."""

        class FlakyThenFineClient(FakeIBClient):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self._attempts = 0

            def placeOrder(self, contract, order):
                self._attempts += 1
                if self._attempts == 1:
                    raise ConnectionError("simulated failure on first attempt")
                return super().placeOrder(contract, order)

        fake = FlakyThenFineClient()
        broker = IBKRBroker(config=IBKRConfig(port=7497), ib_client=fake)
        await broker.connect()

        request = _csp_request()
        with pytest.raises(ConnectionError):
            await broker.place_order(request)

        # Caller-initiated retry, same client_order_id:
        order = await broker.place_order(request)
        assert order.status == OrderStatus.SUBMITTED
        assert len(fake.orders) == 1  # only the successful attempt created an order


class TestCancelOrder:
    @pytest.mark.asyncio
    async def test_cancels_a_known_open_order(self):
        broker, fake = await _connected_broker()
        await broker.place_order(_csp_request())
        cancelled = await broker.cancel_order("csp-1")
        assert cancelled.status == OrderStatus.CANCELLED
        assert fake.cancel_order_call_count == 1

    @pytest.mark.asyncio
    async def test_cancelling_unknown_client_order_id_raises(self):
        broker, _ = await _connected_broker()
        with pytest.raises(ValueError):
            await broker.cancel_order("does-not-exist")

    @pytest.mark.asyncio
    async def test_cancelling_already_terminal_order_is_a_noop_not_an_error(self):
        broker, fake = await _connected_broker()
        await broker.place_order(_csp_request())
        await broker.cancel_order("csp-1")
        # Second cancel of an already-cancelled order should not error
        # or issue a second broker-side cancel call.
        cancelled_again = await broker.cancel_order("csp-1")
        assert cancelled_again.status == OrderStatus.CANCELLED
        assert fake.cancel_order_call_count == 1

    @pytest.mark.asyncio
    async def test_cancel_order_never_auto_retried_on_transport_failure(self):
        broker, fake = await _connected_broker()
        await broker.place_order(_csp_request())

        original_cancel = fake.cancelOrder
        call_count = {"n": 0}

        def failing_cancel(order):
            call_count["n"] += 1
            raise ConnectionError("simulated cancel timeout - outcome unknown")

        fake.cancelOrder = failing_cancel  # type: ignore[method-assign]

        with pytest.raises(ConnectionError):
            await broker.cancel_order("csp-1")

        assert call_count["n"] == 1


class TestGetOpenOrdersAndFills:
    @pytest.mark.asyncio
    async def test_open_orders_reflects_submitted_orders(self):
        broker, _ = await _connected_broker()
        await broker.place_order(_csp_request())
        open_orders = await broker.get_open_orders()
        assert len(open_orders) == 1
        assert open_orders[0].client_order_id == "csp-1"

    @pytest.mark.asyncio
    async def test_cancelled_orders_excluded_from_open_orders(self):
        broker, _ = await _connected_broker()
        await broker.place_order(_csp_request())
        await broker.cancel_order("csp-1")
        assert await broker.get_open_orders() == []

    @pytest.mark.asyncio
    async def test_fills_returns_canonical_fills(self):
        broker, fake = await _connected_broker()
        fake.add_fill(order_id=42, symbol="AAPL", side="SELL", shares=1, price=2.45)
        fills = await broker.get_fills()
        assert len(fills) == 1
        assert fills[0].symbol == "AAPL"
        assert fills[0].price == 2.45
        assert fills[0].action.value == "sell"

    @pytest.mark.asyncio
    async def test_fills_filtered_by_since(self):
        import datetime as dt

        broker, fake = await _connected_broker()
        fake.add_fill(order_id=1, symbol="AAPL", side="SELL", shares=1, price=2.45)
        far_future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
        fills = await broker.get_fills(since=far_future)
        assert fills == []
