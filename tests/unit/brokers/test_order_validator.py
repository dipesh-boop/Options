"""Tests for src.brokers.order_validator (Step 12)."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.brokers.base import OrderAction, OrderType
from src.brokers.fidelity import ApprovedOrder, FidelityLegAction, FidelityOrderLeg
from src.brokers.order_validator import OrderValidationError, build_occ_symbol, validate_and_build_order_request
from src.data.option_chain import OptionRight
from src.risk.broker_constraints import load_broker_capabilities
from src.risk.reason_codes import RiskDecision

NOW = datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc)
MD_TS = datetime(2026, 9, 20, 13, 55, tzinfo=timezone.utc)
EXPIRATION = date(2026, 10, 16)


def make_approved_order(**overrides) -> ApprovedOrder:
    base = dict(
        risk_approval_id="risk-approval-123",
        account_alias="INTERNAL_PAPER",
        ticker="SPY",
        strategy="PUT CREDIT SPREAD",
        underlying_price=628.5,
        expiration=EXPIRATION,
        legs=[
            FidelityOrderLeg(action=FidelityLegAction.SELL_TO_OPEN, put_call=OptionRight.PUT, strike=620.0, expiration=EXPIRATION, contracts=2),
            FidelityOrderLeg(action=FidelityLegAction.BUY_TO_OPEN, put_call=OptionRight.PUT, strike=615.0, expiration=EXPIRATION, contracts=2),
        ],
        quantity=2,
        limit_price=0.75,
        minimum_acceptable_price=0.68,
        estimated_credit_debit=0.75,
        net_bid=0.70,
        net_ask=0.80,
        max_profit=150.0,
        max_loss=850.0,
        breakeven=619.25,
        capital_at_risk=850.0,
        return_on_capital=150 / 850,
        profit_target=0.38,
        loss_management_rule="close at 2x credit",
        DTE_management_rule="review at 21 DTE",
        management_dte=21,
        timestamp=NOW,
        market_data_timestamp=MD_TS,
    )
    base.update(overrides)
    return ApprovedOrder(**base)


def internal_paper_caps():
    return load_broker_capabilities("internal_paper")


def fidelity_caps():
    return load_broker_capabilities("fidelity")


class TestOccSymbol:
    def test_matches_the_platforms_existing_test_fixture_format(self):
        assert build_occ_symbol("SPY", EXPIRATION, OptionRight.PUT, 620.0) == "SPY261016P00620000"

    def test_call_right(self):
        assert build_occ_symbol("SPY", EXPIRATION, OptionRight.CALL, 640.0) == "SPY261016C00640000"

    def test_fractional_strike(self):
        assert build_occ_symbol("XYZ", EXPIRATION, OptionRight.PUT, 9.5) == "XYZ261016P00009500"


class TestHappyPath:
    def test_builds_a_valid_request_for_an_automated_broker(self):
        approved = make_approved_order()
        req = validate_and_build_order_request(
            approved, risk_decision=RiskDecision.APPROVE, approved_contracts=2, broker_capabilities=internal_paper_caps()
        )
        assert req.limit_price == pytest.approx(0.75)
        assert len(req.legs) == 2
        assert req.legs[0].symbol == "SPY261016P00620000"
        assert req.legs[0].action == OrderAction.SELL
        assert req.legs[1].action == OrderAction.BUY
        assert req.order_type == OrderType.LIMIT

    def test_resize_decision_also_accepted(self):
        approved = make_approved_order(quantity=1, legs=[
            FidelityOrderLeg(action=FidelityLegAction.SELL_TO_OPEN, put_call=OptionRight.PUT, strike=620.0, expiration=EXPIRATION, contracts=1),
            FidelityOrderLeg(action=FidelityLegAction.BUY_TO_OPEN, put_call=OptionRight.PUT, strike=615.0, expiration=EXPIRATION, contracts=1),
        ])
        req = validate_and_build_order_request(
            approved, risk_decision=RiskDecision.RESIZE, approved_contracts=1, broker_capabilities=internal_paper_caps()
        )
        assert req.legs[0].quantity == 1

    def test_explicit_client_order_id_used_when_given(self):
        approved = make_approved_order()
        req = validate_and_build_order_request(
            approved, risk_decision=RiskDecision.APPROVE, approved_contracts=2,
            broker_capabilities=internal_paper_caps(), client_order_id="my-explicit-id",
        )
        assert req.client_order_id == "my-explicit-id"

    def test_defaults_client_order_id_to_the_risk_approval_id(self):
        approved = make_approved_order()
        req = validate_and_build_order_request(
            approved, risk_decision=RiskDecision.APPROVE, approved_contracts=2, broker_capabilities=internal_paper_caps()
        )
        assert req.client_order_id == approved.risk_approval_id


class TestRejectedDecisions:
    @pytest.mark.parametrize("decision", [RiskDecision.REJECT, RiskDecision.HALT])
    def test_non_approving_decision_rejected(self, decision: RiskDecision):
        approved = make_approved_order()
        with pytest.raises(OrderValidationError):
            validate_and_build_order_request(
                approved, risk_decision=decision, approved_contracts=2, broker_capabilities=internal_paper_caps()
            )


class TestMissingOrInvalidApprovedContracts:
    def test_none_approved_contracts_rejected(self):
        approved = make_approved_order()
        with pytest.raises(OrderValidationError):
            validate_and_build_order_request(
                approved, risk_decision=RiskDecision.APPROVE, approved_contracts=None, broker_capabilities=internal_paper_caps()
            )

    def test_zero_approved_contracts_rejected(self):
        approved = make_approved_order()
        with pytest.raises(OrderValidationError):
            validate_and_build_order_request(
                approved, risk_decision=RiskDecision.APPROVE, approved_contracts=0, broker_capabilities=internal_paper_caps()
            )

    def test_mismatched_quantity_rejected(self):
        approved = make_approved_order()  # quantity=2
        with pytest.raises(OrderValidationError):
            validate_and_build_order_request(
                approved, risk_decision=RiskDecision.APPROVE, approved_contracts=5, broker_capabilities=internal_paper_caps()
            )


class TestBrokerCapabilityChecks:
    def test_missing_broker_capabilities_rejected(self):
        approved = make_approved_order()
        with pytest.raises(OrderValidationError):
            validate_and_build_order_request(
                approved, risk_decision=RiskDecision.APPROVE, approved_contracts=2, broker_capabilities=None
            )

    def test_manual_execution_broker_rejected_paper_broker_only_takes_automated(self):
        approved = make_approved_order()
        with pytest.raises(OrderValidationError):
            validate_and_build_order_request(
                approved, risk_decision=RiskDecision.APPROVE, approved_contracts=2, broker_capabilities=fidelity_caps()
            )


class TestDuplicateOrders:
    def test_client_order_id_already_submitted_is_rejected(self):
        approved = make_approved_order()
        with pytest.raises(OrderValidationError):
            validate_and_build_order_request(
                approved,
                risk_decision=RiskDecision.APPROVE,
                approved_contracts=2,
                broker_capabilities=internal_paper_caps(),
                existing_client_order_ids=frozenset({approved.risk_approval_id}),
            )
