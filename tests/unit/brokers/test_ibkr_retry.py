"""Tests specifically isolating retry behavior: read-only calls retry
(including reconnecting between attempts), order-mutating calls never
do — this is the core safety property from the task."""
from __future__ import annotations

import pytest

from src.brokers.base import BrokerConnectionError
from src.brokers.ibkr import IBKRBroker, IBKRConfig
from tests.unit.brokers.fakes import FakeIBClient


class TestReadRetry:
    @pytest.mark.asyncio
    async def test_get_account_retries_through_a_transient_failure(self):
        fake = FakeIBClient(fail_read_times=1)
        broker = IBKRBroker(config=IBKRConfig(port=7497), ib_client=fake)
        await broker.connect()
        account = await broker.get_account()
        assert account.account_id == "DU1234567"

    @pytest.mark.asyncio
    async def test_get_account_reconnects_between_retry_attempts(self):
        fake = FakeIBClient(fail_read_times=1)
        broker = IBKRBroker(config=IBKRConfig(port=7497), ib_client=fake)
        await broker.connect()
        initial_connect_count = fake.connect_call_count
        await broker.get_account()
        # The simulated failure also drops the connection - a bare retry
        # without reconnecting would fail identically every time.
        assert fake.connect_call_count > initial_connect_count

    @pytest.mark.asyncio
    async def test_get_positions_retries(self):
        fake = FakeIBClient(fail_read_times=2, positions=[])
        broker = IBKRBroker(config=IBKRConfig(port=7497), ib_client=fake)
        await broker.connect()
        assert await broker.get_positions() == []

    @pytest.mark.asyncio
    async def test_get_underlying_quote_retries(self):
        fake = FakeIBClient(fail_read_times=1)
        broker = IBKRBroker(config=IBKRConfig(port=7497), ib_client=fake)
        await broker.connect()
        quote = await broker.get_underlying_quote("AAPL")
        assert quote.symbol == "AAPL"

    @pytest.mark.asyncio
    async def test_persistent_read_failure_eventually_raises(self):
        fake = FakeIBClient(fail_read_times=99)
        broker = IBKRBroker(config=IBKRConfig(port=7497), ib_client=fake)
        await broker.connect()
        with pytest.raises(BrokerConnectionError):
            await broker.get_account()

    @pytest.mark.asyncio
    async def test_read_retry_is_bounded_not_infinite(self):
        fake = FakeIBClient(fail_read_times=99)
        broker = IBKRBroker(config=IBKRConfig(port=7497), ib_client=fake)
        await broker.connect()
        with pytest.raises(BrokerConnectionError):
            await broker.get_account()
        # A bounded number of underlying read attempts, not an
        # unbounded retry loop.
        assert fake._read_attempts <= 5


class TestOrderMutationNeverRetried:
    """Restates, from the retry-mechanism's own point of view (rather
    than place_order's/cancel_order's), that neither is ever wrapped by
    the same retry-and-reconnect helper read methods use."""

    @pytest.mark.asyncio
    async def test_place_order_does_not_use_the_read_retry_helper(self):
        import inspect

        from src.brokers.ibkr import IBKRBroker as Cls

        source = inspect.getsource(Cls.place_order)
        assert "_with_read_retry" not in source

    @pytest.mark.asyncio
    async def test_cancel_order_does_not_use_the_read_retry_helper(self):
        import inspect

        from src.brokers.ibkr import IBKRBroker as Cls

        source = inspect.getsource(Cls.cancel_order)
        assert "_with_read_retry" not in source
