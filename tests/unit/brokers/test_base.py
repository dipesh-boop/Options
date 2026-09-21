"""Tests for the canonical trading-domain schemas and IdempotencyStore."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from src.brokers.base import (
    Account,
    Broker,
    BrokerEnvironment,
    Fill,
    InMemoryIdempotencyStore,
    Order,
    OrderAction,
    OrderLeg,
    OrderStatus,
    OrderType,
    PlaceOrderRequest,
    Position,
    ReconciliationDiscrepancy,
    ReconciliationReport,
    SqliteIdempotencyStore,
)
from src.data.option_chain import OptionRight

NOW = datetime(2026, 1, 15, 14, 30, tzinfo=timezone.utc)


def _order_kwargs(**overrides) -> dict:
    base = dict(
        client_order_id="co-1",
        broker_order_id="12345",
        legs=[OrderLeg(symbol="AAPL", right=OptionRight.PUT, strike=210.0, expiration=date(2026, 3, 20), action=OrderAction.SELL, quantity=1)],
        order_type=OrderType.LIMIT,
        limit_price=2.5,
        status=OrderStatus.SUBMITTED,
        timestamp=NOW,
        source="ibkr",
    )
    base.update(overrides)
    return base


class TestOrderLeg:
    def test_option_leg_valid(self):
        leg = OrderLeg(symbol="AAPL", right=OptionRight.PUT, strike=210.0, expiration=date(2026, 3, 20), action=OrderAction.SELL, quantity=1)
        assert leg.strike == 210.0

    def test_equity_leg_valid_without_option_fields(self):
        leg = OrderLeg(symbol="AAPL", action=OrderAction.BUY, quantity=100)
        assert leg.right is None

    def test_non_positive_quantity_rejected(self):
        with pytest.raises(ValidationError):
            OrderLeg(symbol="AAPL", action=OrderAction.BUY, quantity=0)

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            OrderLeg.model_validate({"symbol": "AAPL", "action": "buy", "quantity": 1, "execute": True})


class TestPlaceOrderRequest:
    def test_valid_request(self):
        req = PlaceOrderRequest(
            client_order_id="co-1",
            legs=[OrderLeg(symbol="AAPL", right=OptionRight.PUT, strike=210.0, action=OrderAction.SELL, quantity=1)],
            limit_price=2.5,
        )
        assert req.order_type == OrderType.LIMIT
        assert req.time_in_force == "DAY"

    def test_no_broker_order_id_field_anywhere(self):
        # A caller cannot pre-assign a broker order id - the request is
        # purely an intent to submit, identified only by client_order_id.
        assert "broker_order_id" not in PlaceOrderRequest.model_fields

    def test_no_execution_authorization_field(self):
        assert "execute" not in PlaceOrderRequest.model_fields
        assert "authorized" not in PlaceOrderRequest.model_fields

    def test_empty_legs_rejected(self):
        with pytest.raises(ValidationError):
            PlaceOrderRequest(client_order_id="co-1", legs=[], limit_price=1.0)

    def test_too_many_legs_rejected(self):
        legs = [OrderLeg(symbol="AAPL", action=OrderAction.SELL, quantity=1) for _ in range(5)]
        with pytest.raises(ValidationError):
            PlaceOrderRequest(client_order_id="co-1", legs=legs, limit_price=1.0)

    def test_non_positive_limit_price_rejected(self):
        with pytest.raises(ValidationError):
            PlaceOrderRequest(
                client_order_id="co-1",
                legs=[OrderLeg(symbol="AAPL", action=OrderAction.SELL, quantity=1)],
                limit_price=0,
            )


class TestPlaceOrderRequestLegRatioValidatorRegressionOP003:
    """SECURITY_AUDIT.md OP-003: every leg's quantity must be an exact
    integer multiple of the smallest leg's quantity (`base_combo_quantity`
    in `PaperBroker`), and the resulting ratio must match a shape this
    platform's strategy library actually defines -- uniform 1:1:...:1 on
    every leg, or (long_call_butterfly only) 1:2:1 on exactly 3 legs.
    A future/malformed caller supplying an arbitrary mismatch (e.g.
    [2, 5]) must fail closed here rather than reach `PaperBroker`, which
    would otherwise silently treat it as "2 combo units, with the qty=5
    leg over-filled relative to what was intended."""

    def _leg(self, quantity: int, action: OrderAction = OrderAction.SELL) -> OrderLeg:
        return OrderLeg(symbol="AAPL", action=action, quantity=quantity)

    def test_single_leg_always_valid(self):
        req = PlaceOrderRequest(client_order_id="co-1", legs=[self._leg(3)], limit_price=1.0)
        assert req.legs[0].quantity == 3

    def test_uniform_two_leg_ratio_valid(self):
        req = PlaceOrderRequest(client_order_id="co-1", legs=[self._leg(4), self._leg(4)], limit_price=1.0)
        assert [leg.quantity for leg in req.legs] == [4, 4]

    def test_uniform_four_leg_ratio_valid(self):
        # Shape of a short iron condor / short iron butterfly order.
        req = PlaceOrderRequest(client_order_id="co-1", legs=[self._leg(2)] * 4, limit_price=1.0)
        assert len(req.legs) == 4

    def test_butterfly_one_two_one_ratio_valid(self):
        # Shape of a long_call_butterfly order: middle (short) leg is 2x.
        req = PlaceOrderRequest(
            client_order_id="co-1",
            legs=[self._leg(1, OrderAction.BUY), self._leg(2, OrderAction.SELL), self._leg(1, OrderAction.BUY)],
            limit_price=1.0,
        )
        assert [leg.quantity for leg in req.legs] == [1, 2, 1]

    def test_non_integer_multiple_rejected(self):
        # 5 is not an integer multiple of the smaller leg's quantity (2).
        with pytest.raises(ValidationError):
            PlaceOrderRequest(client_order_id="co-1", legs=[self._leg(2), self._leg(5)], limit_price=1.0)

    def test_uneven_two_leg_ratio_rejected(self):
        # An exact integer multiple (2x) but not a ratio this platform
        # defines for a 2-leg order -- every real 2-leg strategy is 1:1.
        with pytest.raises(ValidationError):
            PlaceOrderRequest(client_order_id="co-1", legs=[self._leg(1), self._leg(2)], limit_price=1.0)

    def test_three_leg_ratio_with_two_twos_rejected(self):
        # Not the platform's actual 1:2:1 butterfly shape (two legs at 2x).
        with pytest.raises(ValidationError):
            PlaceOrderRequest(
                client_order_id="co-1", legs=[self._leg(1), self._leg(2), self._leg(2)], limit_price=1.0,
            )

    def test_four_leg_non_uniform_ratio_rejected(self):
        with pytest.raises(ValidationError):
            PlaceOrderRequest(
                client_order_id="co-1", legs=[self._leg(1), self._leg(1), self._leg(1), self._leg(2)], limit_price=1.0,
            )


class TestOrder:
    def test_is_terminal_true_for_filled_cancelled_rejected(self):
        for status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
            order = Order(**_order_kwargs(status=status))
            assert order.is_terminal is True

    def test_is_terminal_false_for_pending_submitted_partial(self):
        for status in (OrderStatus.PENDING_SUBMIT, OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED):
            order = Order(**_order_kwargs(status=status))
            assert order.is_terminal is False

    def test_is_frozen(self):
        order = Order(**_order_kwargs())
        with pytest.raises(ValidationError):
            order.status = OrderStatus.CANCELLED  # type: ignore[misc]

    def test_naive_timestamp_rejected(self):
        with pytest.raises(ValidationError):
            Order(**_order_kwargs(timestamp=datetime(2026, 1, 15, 14, 30)))


class TestFillPositionAccount:
    def test_fill_valid(self):
        fill = Fill(broker_order_id="123", symbol="AAPL", action=OrderAction.SELL, quantity=1, price=2.5, commission=0.65, timestamp=NOW, source="ibkr")
        assert fill.commission == 0.65

    def test_fill_negative_commission_rejected(self):
        with pytest.raises(ValidationError):
            Fill(broker_order_id="123", symbol="AAPL", action=OrderAction.SELL, quantity=1, price=2.5, commission=-1, timestamp=NOW, source="ibkr")

    def test_position_can_be_short(self):
        pos = Position(symbol="AAPL", quantity=-1, avg_cost=2.5, market_price=2.1, market_value=-210.0, unrealized_pnl=40.0, timestamp=NOW, source="ibkr")
        assert pos.quantity == -1

    def test_account_valid(self):
        acct = Account(account_id="DU1234567", currency="USD", net_liquidation=100_000, buying_power=200_000, cash_balance=50_000, maintenance_margin=1_500, timestamp=NOW, source="ibkr")
        assert acct.account_id == "DU1234567"


class TestReconciliationReport:
    def test_is_clean_true_with_no_discrepancies(self):
        report = ReconciliationReport(checked_at=NOW)
        assert report.is_clean is True

    def test_is_clean_false_with_discrepancies(self):
        report = ReconciliationReport(
            checked_at=NOW,
            discrepancies=[ReconciliationDiscrepancy(kind="orphaned_local", client_order_id="co-1", broker_order_id=None, detail="not found")],
        )
        assert report.is_clean is False


class TestInMemoryIdempotencyStore:
    def test_get_missing_returns_none(self):
        store = InMemoryIdempotencyStore()
        assert store.get("nope") is None

    def test_save_then_get_roundtrips(self):
        store = InMemoryIdempotencyStore()
        order = Order(**_order_kwargs())
        store.save(order)
        assert store.get("co-1") is order

    def test_all_returns_every_saved_order(self):
        store = InMemoryIdempotencyStore()
        store.save(Order(**_order_kwargs(client_order_id="co-1")))
        store.save(Order(**_order_kwargs(client_order_id="co-2")))
        assert {o.client_order_id for o in store.all()} == {"co-1", "co-2"}

    def test_save_overwrites_same_client_order_id(self):
        store = InMemoryIdempotencyStore()
        store.save(Order(**_order_kwargs(status=OrderStatus.SUBMITTED)))
        store.save(Order(**_order_kwargs(status=OrderStatus.FILLED)))
        assert len(store.all()) == 1
        assert store.get("co-1").status == OrderStatus.FILLED


class TestSqliteIdempotencyStoreRegressionSY002:
    """SY-002: `InMemoryIdempotencyStore` cannot recognize a retry after
    a process restart -- these tests prove `SqliteIdempotencyStore`
    does, by discarding the Python object entirely (simulating a crash)
    and constructing a brand-new instance against the same file."""

    def test_get_missing_returns_none(self, tmp_path):
        store = SqliteIdempotencyStore(tmp_path / "idempotency.db")
        assert store.get("nope") is None

    def test_save_then_get_roundtrips(self, tmp_path):
        store = SqliteIdempotencyStore(tmp_path / "idempotency.db")
        order = Order(**_order_kwargs())
        store.save(order)
        fetched = store.get("co-1")
        assert fetched is not None
        assert fetched.client_order_id == "co-1"
        assert fetched.status == OrderStatus.SUBMITTED

    def test_all_returns_every_saved_order(self, tmp_path):
        store = SqliteIdempotencyStore(tmp_path / "idempotency.db")
        store.save(Order(**_order_kwargs(client_order_id="co-1")))
        store.save(Order(**_order_kwargs(client_order_id="co-2")))
        assert {o.client_order_id for o in store.all()} == {"co-1", "co-2"}

    def test_save_overwrites_same_client_order_id(self, tmp_path):
        store = SqliteIdempotencyStore(tmp_path / "idempotency.db")
        store.save(Order(**_order_kwargs(status=OrderStatus.SUBMITTED)))
        store.save(Order(**_order_kwargs(status=OrderStatus.FILLED)))
        assert len(store.all()) == 1
        assert store.get("co-1").status == OrderStatus.FILLED

    def test_survives_simulated_process_restart(self, tmp_path):
        """The actual SY-002 reproduction: save an order, discard the
        store object entirely (no shared connection, no shared Python
        state of any kind survives), then construct a *brand-new*
        instance pointed at the same file path -- a genuine restart
        must still recognize the client_order_id."""
        db_path = tmp_path / "idempotency.db"
        first_process_store = SqliteIdempotencyStore(db_path)
        first_process_store.save(Order(**_order_kwargs(client_order_id="co-crash-then-retry", status=OrderStatus.FILLED)))
        del first_process_store  # simulate the process crashing

        second_process_store = SqliteIdempotencyStore(db_path)
        recovered = second_process_store.get("co-crash-then-retry")
        assert recovered is not None
        assert recovered.status == OrderStatus.FILLED

    def test_in_memory_store_by_contrast_does_not_survive_a_restart(self):
        """Documents exactly the gap SqliteIdempotencyStore closes: a
        fresh InMemoryIdempotencyStore (the "process restart") has no
        way to see an order saved by a prior instance."""
        first_process_store = InMemoryIdempotencyStore()
        first_process_store.save(Order(**_order_kwargs(client_order_id="co-1")))
        del first_process_store

        second_process_store = InMemoryIdempotencyStore()
        assert second_process_store.get("co-1") is None


def test_broker_is_abstract():
    with pytest.raises(TypeError):
        Broker()  # type: ignore[abstract]


class TestBrokerEnvironmentRegressionStep22Part19:
    """Step 22 Part 19 safety-invariant re-verification: "no real-money/
    live auto-execution path exists anywhere" depended, until now, only
    on indirect/behavioral test coverage (IBKR paper-port enforcement,
    Fidelity's no-submission tests) -- this is the one direct,
    structural assertion of the claim itself: `BrokerEnvironment` has
    and can only ever have exactly one member. Adding a `LIVE` value
    would be a real, deliberate code change to this enum (and to every
    concrete adapter's connection-validation logic, per this type's own
    docstring), never a config flip -- this test is what would actually
    break if that line were ever crossed."""

    def test_broker_environment_has_exactly_one_member_paper(self):
        assert list(BrokerEnvironment) == [BrokerEnvironment.PAPER]

    def test_broker_environment_member_count_is_exactly_one(self):
        assert len(BrokerEnvironment) == 1
