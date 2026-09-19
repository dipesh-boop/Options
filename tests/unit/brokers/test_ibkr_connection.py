"""Tests for IBKRBroker connection lifecycle: paper-account
verification (the second, independent "no live trading" check beyond
the port check), connect retry, health checks."""
from __future__ import annotations

import pytest

from src.brokers.base import BrokerConnectionError, BrokerEnvironment, LiveTradingBlockedError
from src.brokers.ibkr import IBKRBroker, IBKRConfig
from tests.unit.brokers.fakes import FakeIBClient


class TestConnect:
    @pytest.mark.asyncio
    async def test_connects_to_a_known_paper_account(self):
        fake = FakeIBClient(managed_accounts=("DU1234567",))
        broker = IBKRBroker(config=IBKRConfig(port=7497), ib_client=fake)
        await broker.connect()
        assert broker.is_connected() is True

    @pytest.mark.asyncio
    async def test_non_paper_account_blocked(self):
        fake = FakeIBClient(managed_accounts=("U7654321",))  # live-style prefix
        broker = IBKRBroker(config=IBKRConfig(port=7497), ib_client=fake)
        with pytest.raises(LiveTradingBlockedError):
            await broker.connect()
        assert fake.isConnected() is False  # disconnected again after the rejection

    @pytest.mark.asyncio
    async def test_mixed_accounts_with_any_non_paper_blocked(self):
        fake = FakeIBClient(managed_accounts=("DU1234567", "U7654321"))
        broker = IBKRBroker(config=IBKRConfig(port=7497), ib_client=fake)
        with pytest.raises(LiveTradingBlockedError):
            await broker.connect()

    @pytest.mark.asyncio
    async def test_no_managed_accounts_blocked(self):
        fake = FakeIBClient(managed_accounts=())
        broker = IBKRBroker(config=IBKRConfig(port=7497), ib_client=fake)
        with pytest.raises(LiveTradingBlockedError):
            await broker.connect()

    @pytest.mark.asyncio
    async def test_configured_account_id_not_reported_blocked(self):
        fake = FakeIBClient(managed_accounts=("DU1234567",))
        broker = IBKRBroker(config=IBKRConfig(port=7497, account_id="DU9999999"), ib_client=fake)
        with pytest.raises(LiveTradingBlockedError, match="DU9999999"):
            await broker.connect()

    @pytest.mark.asyncio
    async def test_configured_account_id_matching_succeeds(self):
        fake = FakeIBClient(managed_accounts=("DU1234567", "DU7654321"))
        broker = IBKRBroker(config=IBKRConfig(port=7497, account_id="DU7654321"), ib_client=fake)
        await broker.connect()
        assert broker.is_connected() is True

    @pytest.mark.asyncio
    async def test_already_connected_is_a_noop(self):
        fake = FakeIBClient()
        broker = IBKRBroker(config=IBKRConfig(port=7497), ib_client=fake)
        await broker.connect()
        await broker.connect()
        assert fake.connect_call_count == 1

    def test_constructing_broker_with_live_port_config_raises_immediately(self):
        fake = FakeIBClient()
        with pytest.raises(LiveTradingBlockedError):
            IBKRBroker(config=IBKRConfig(port=7496), ib_client=fake)  # __init__ itself rejects


class TestConnectRetry:
    @pytest.mark.asyncio
    async def test_transient_connect_failure_recovers(self):
        fake = FakeIBClient(fail_connect_times=2)
        broker = IBKRBroker(config=IBKRConfig(port=7497), ib_client=fake)
        await broker.connect()
        assert broker.is_connected() is True
        assert fake.connect_call_count == 3

    @pytest.mark.asyncio
    async def test_persistent_connect_failure_raises_broker_connection_error(self):
        fake = FakeIBClient(fail_connect_times=99)
        broker = IBKRBroker(config=IBKRConfig(port=7497), ib_client=fake)
        with pytest.raises(BrokerConnectionError):
            await broker.connect()

    @pytest.mark.asyncio
    async def test_live_trading_rejection_is_never_retried(self):
        fake = FakeIBClient(managed_accounts=("U1234567",))
        broker = IBKRBroker(config=IBKRConfig(port=7497), ib_client=fake)
        with pytest.raises(LiveTradingBlockedError):
            await broker.connect()
        # Exactly one connect attempt - a safety rejection must never be
        # retried into eventually succeeding.
        assert fake.connect_call_count == 1


class TestDisconnectAndHealth:
    @pytest.mark.asyncio
    async def test_disconnect_updates_is_connected(self):
        fake = FakeIBClient()
        broker = IBKRBroker(config=IBKRConfig(port=7497), ib_client=fake)
        await broker.connect()
        await broker.disconnect()
        assert broker.is_connected() is False

    @pytest.mark.asyncio
    async def test_health_check_when_connected(self):
        fake = FakeIBClient()
        broker = IBKRBroker(config=IBKRConfig(port=7497), ib_client=fake)
        await broker.connect()
        health = await broker.health_check()
        assert health.connected is True
        assert health.environment == BrokerEnvironment.PAPER
        assert health.account_id == "DU1234567"
        assert health.latency_ms is not None

    @pytest.mark.asyncio
    async def test_health_check_when_not_connected(self):
        fake = FakeIBClient()
        broker = IBKRBroker(config=IBKRConfig(port=7497), ib_client=fake)
        health = await broker.health_check()
        assert health.connected is False
        assert health.latency_ms is None
