"""Schema-level tests for PortfolioDecision (Step 10)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.llm.schemas import PortfolioDecision, ensure_portfolio_decision
from tests.unit.llm.conftest import NOW, valid_portfolio_decision_input


class TestValidConstruction:
    def test_well_formed_decision_constructs(self):
        decision = PortfolioDecision(**valid_portfolio_decision_input())
        assert decision.decision == "propose_advance"
        assert decision.cash_preferred is False

    def test_hold_cash_with_cash_preferred_true_constructs(self):
        decision = PortfolioDecision(**valid_portfolio_decision_input(decision="hold_cash", cash_preferred=True))
        assert decision.decision == "hold_cash"

    def test_reject_constructs(self):
        decision = PortfolioDecision(**valid_portfolio_decision_input(decision="reject", cash_preferred=False))
        assert decision.decision == "reject"


class TestNoNumericFieldsExistAtAll:
    """The structural guarantee the schema's docstring claims: there is
    no field of numeric type anywhere on PortfolioDecision."""

    def test_every_field_is_str_bool_list_or_datetime(self):
        for name, field in PortfolioDecision.model_fields.items():
            annotation = field.annotation
            assert annotation not in (float, int, "float", "int"), f"field {name!r} has a numeric type"


class TestCashPreferredConsistency:
    def test_hold_cash_with_cash_preferred_false_rejected(self):
        with pytest.raises(ValidationError):
            PortfolioDecision(**valid_portfolio_decision_input(decision="hold_cash", cash_preferred=False))

    def test_advance_with_cash_preferred_true_rejected(self):
        with pytest.raises(ValidationError):
            PortfolioDecision(**valid_portfolio_decision_input(decision="propose_advance", cash_preferred=True))

    def test_reject_with_cash_preferred_true_rejected(self):
        with pytest.raises(ValidationError):
            PortfolioDecision(**valid_portfolio_decision_input(decision="reject", cash_preferred=True))


class TestForbiddenExtraFields:
    @pytest.mark.parametrize(
        "smuggled",
        [
            {"approved_contracts": 500},
            {"max_loss": 1000.0},
            {"override_risk_engine": True},
            {"final_approved_contracts": 100},
            {"execute": True},
            {"broker_order_id": "IBKR-1"},
            {"authoritative_max_loss": 250.0},
            {"account_balance": 500_000.0},
            {"implied_volatility": 0.22},
        ],
    )
    def test_smuggled_field_rejected(self, smuggled: dict):
        payload = {**valid_portfolio_decision_input(), **smuggled}
        with pytest.raises(ValidationError):
            PortfolioDecision(**payload)


class TestInvalidDecisionValues:
    def test_approve_is_not_a_valid_decision_value(self):
        # "approve" is deliberately not in the literal — this agent has
        # no approval authority, only Python Risk Engine does.
        with pytest.raises(ValidationError):
            PortfolioDecision(**valid_portfolio_decision_input(decision="approve"))

    def test_arbitrary_string_decision_rejected(self):
        with pytest.raises(ValidationError):
            PortfolioDecision(**valid_portfolio_decision_input(decision="execute_immediately"))


class TestRequiredFieldsAndBlankEntries:
    def test_missing_thesis_summary_rejected(self):
        payload = {k: v for k, v in valid_portfolio_decision_input().items() if k != "thesis_summary"}
        with pytest.raises(ValidationError):
            PortfolioDecision(**payload)

    def test_empty_invalidation_conditions_rejected(self):
        with pytest.raises(ValidationError):
            PortfolioDecision(**valid_portfolio_decision_input(invalidation_conditions=[]))

    def test_blank_invalidation_condition_entry_rejected(self):
        with pytest.raises(ValidationError):
            PortfolioDecision(**valid_portfolio_decision_input(invalidation_conditions=["   "]))

    def test_naive_timestamp_rejected(self):
        with pytest.raises(ValidationError):
            PortfolioDecision(**valid_portfolio_decision_input(timestamp=datetime(2026, 9, 20, 14, 0).isoformat()))


class TestEnsurePortfolioDecisionBoundaryGuard:
    def test_guard_accepts_a_real_instance(self):
        decision = PortfolioDecision(**valid_portfolio_decision_input())
        assert ensure_portfolio_decision(decision) is decision

    def test_guard_rejects_a_plain_dict(self):
        with pytest.raises(TypeError):
            ensure_portfolio_decision(valid_portfolio_decision_input())

    def test_guard_rejects_a_subclass(self):
        class SneakyPortfolioDecision(PortfolioDecision):
            pass

        sneaky = SneakyPortfolioDecision(**valid_portfolio_decision_input())
        with pytest.raises(TypeError):
            ensure_portfolio_decision(sneaky)

    def test_guard_rejects_a_forged_object_with_matching_attributes(self):
        class Forged:
            decision_id = "dec-1"
            proposal_id = "prop-1"
            decision = "propose_advance"

        with pytest.raises(TypeError):
            ensure_portfolio_decision(Forged())
