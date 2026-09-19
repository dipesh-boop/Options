"""Tests for reconcile(): detecting orphaned local orders, unknown
broker orders, and status mismatches."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.brokers.ibkr import IBKRBroker, IBKRConfig
from tests.unit.brokers.fakes import FakeIBClient
from tests.unit.brokers.test_ibkr_orders import _csp_request


async def _connected_broker(**fake_kwargs) -> tuple[IBKRBroker, FakeIBClient]:
    fake = FakeIBClient(**fake_kwargs)
    broker = IBKRBroker(config=IBKRConfig(port=7497), ib_client=fake)
    await broker.connect()
    return broker, fake


class TestReconcileClean:
    @pytest.mark.asyncio
    async def test_no_orders_is_clean(self):
        broker, _ = await _connected_broker()
        report = await broker.reconcile()
        assert report.is_clean is True
        assert report.discrepancies == []

    @pytest.mark.asyncio
    async def test_matching_local_and_broker_order_is_clean(self):
        broker, _ = await _connected_broker()
        await broker.place_order(_csp_request())
        report = await broker.reconcile()
        assert report.is_clean is True

    @pytest.mark.asyncio
    async def test_cancelled_order_excluded_and_still_clean(self):
        broker, _ = await _connected_broker()
        await broker.place_order(_csp_request())
        await broker.cancel_order("csp-1")
        report = await broker.reconcile()
        assert report.is_clean is True


class TestReconcileDiscrepancies:
    @pytest.mark.asyncio
    async def test_unknown_broker_order_detected(self):
        """An order open at the broker that our local store has never
        heard of — e.g. placed by another process/session."""
        broker, fake = await _connected_broker()
        raw_order = SimpleNamespace(orderId=42, orderRef="external-order", action="SELL", totalQuantity=1, lmtPrice=2.5)
        fake.orders.append(
            SimpleNamespace(order=raw_order, orderStatus=SimpleNamespace(status="Submitted", filled=0), contract=SimpleNamespace(symbol="AAPL"))
        )
        report = await broker.reconcile()
        assert report.is_clean is False
        assert any(d.kind == "unknown_broker" and d.client_order_id == "external-order" for d in report.discrepancies)

    @pytest.mark.asyncio
    async def test_orphaned_local_order_detected(self):
        """A non-terminal order we have locally that the broker no
        longer reports as open (e.g. cancelled out-of-band)."""
        broker, fake = await _connected_broker()
        await broker.place_order(_csp_request())
        # Simulate the broker no longer showing this order as open,
        # without us having recorded that locally (out-of-band change).
        fake.orders.clear()
        report = await broker.reconcile()
        assert report.is_clean is False
        assert any(d.kind == "orphaned_local" and d.client_order_id == "csp-1" for d in report.discrepancies)

    @pytest.mark.asyncio
    async def test_status_mismatch_detected(self):
        broker, fake = await _connected_broker()
        await broker.place_order(_csp_request())
        # Broker-side status silently advances to PartiallyFilled
        # without us finding out via the normal order-update path.
        fake.orders[0].orderStatus.status = "PartiallyFilled"
        fake.orders[0].orderStatus.filled = 1
        report = await broker.reconcile()
        assert report.is_clean is False
        assert any(d.kind == "status_mismatch" and d.client_order_id == "csp-1" for d in report.discrepancies)

    @pytest.mark.asyncio
    async def test_discrepancy_detail_is_non_empty(self):
        broker, fake = await _connected_broker()
        raw_order = SimpleNamespace(orderId=1, orderRef="mystery", action="SELL", totalQuantity=1, lmtPrice=1.0)
        fake.orders.append(
            SimpleNamespace(order=raw_order, orderStatus=SimpleNamespace(status="Submitted", filled=0), contract=SimpleNamespace(symbol="AAPL"))
        )
        report = await broker.reconcile()
        assert all(d.detail for d in report.discrepancies)

    @pytest.mark.asyncio
    async def test_reconcile_never_mutates_state_on_its_own(self):
        """reconcile() only reports - it must never silently 'fix'
        anything (e.g. auto-cancelling an unknown broker order)."""
        broker, fake = await _connected_broker()
        raw_order = SimpleNamespace(orderId=1, orderRef="mystery", action="SELL", totalQuantity=1, lmtPrice=1.0)
        fake.orders.append(
            SimpleNamespace(order=raw_order, orderStatus=SimpleNamespace(status="Submitted", filled=0), contract=SimpleNamespace(symbol="AAPL"))
        )
        await broker.reconcile()
        assert fake.cancel_order_call_count == 0
        assert fake.place_order_call_count == 0
