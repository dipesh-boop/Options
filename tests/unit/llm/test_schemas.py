"""Schema-level proof that malformed or execution-shaped LLM output
cannot be constructed as a trusted object in the first place."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.llm.schemas import (
    AdversarialReview,
    Conviction,
    RiskFlag,
    StrategyType,
    StructureIntent,
    TradeAction,
    TradeProposal,
    ensure_trade_proposal,
)


def _valid_structure_kwargs() -> dict:
    return dict(
        symbol="AAPL",
        strategy_type=StrategyType.CASH_SECURED_PUT,
        action=TradeAction.OPEN,
        target_dte_min=21,
        target_dte_max=35,
        approx_target_delta=0.3,
        notes="30-delta CSP, mid-expiry",
    )


def _valid_proposal_kwargs(**overrides) -> dict:
    base = dict(
        proposal_id="prop-1",
        source_agent="strategy_analyst",
        structure=StructureIntent(**_valid_structure_kwargs()),
        rationale="High IV percentile, liquid chain, no earnings before expiry.",
        conviction=Conviction.MEDIUM,
        risk_flags=[RiskFlag(code="iv_elevated", severity="caution", note="IV rank > 80")],
        rank=1,
    )
    base.update(overrides)
    return base


class TestTradeProposalPositiveControl:
    def test_valid_proposal_parses(self):
        proposal = TradeProposal(**_valid_proposal_kwargs())
        assert proposal.proposal_id == "prop-1"
        assert proposal.structure.symbol == "AAPL"

    def test_ensure_trade_proposal_accepts_real_instance(self):
        proposal = TradeProposal(**_valid_proposal_kwargs())
        assert ensure_trade_proposal(proposal) is proposal


class TestTradeProposalRejectsExecutionShapedFields:
    """`extra='forbid'` should reject any field that looks like it's
    trying to become an executable broker instruction, even if every
    other field is otherwise perfectly valid."""

    @pytest.mark.parametrize(
        "injected_field",
        [
            {"order_id": "IBKR-12345"},
            {"execute": True},
            {"submit": True},
            {"broker": "ibkr"},
            {"quantity": 100},
            {"limit_price": 1.23},
            {"account_id": "U1234567"},
            {"bypass_risk_gate": True},
        ],
    )
    def test_extra_field_rejected(self, injected_field: dict):
        payload = _valid_proposal_kwargs()
        payload_dict = {
            **{k: v for k, v in payload.items() if k != "structure"},
            "structure": payload["structure"].model_dump(),
            **injected_field,
        }
        with pytest.raises(ValidationError):
            TradeProposal.model_validate(payload_dict)

    def test_execution_field_on_structure_rejected(self):
        structure_dict = _valid_structure_kwargs()
        structure_dict["execute_immediately"] = True
        with pytest.raises(ValidationError):
            StructureIntent.model_validate(structure_dict)


class TestTradeProposalRejectsMalformedData:
    def test_missing_required_field(self):
        payload = _valid_proposal_kwargs()
        payload_dict = {k: v for k, v in payload.items() if k != "rationale"}
        payload_dict["structure"] = payload["structure"].model_dump()
        with pytest.raises(ValidationError):
            TradeProposal.model_validate(payload_dict)

    def test_invalid_strategy_type_enum(self):
        structure_dict = _valid_structure_kwargs()
        structure_dict["strategy_type"] = "naked_call"  # explicitly excluded strategy
        with pytest.raises(ValidationError):
            StructureIntent.model_validate(structure_dict)

    def test_invalid_action_enum(self):
        structure_dict = _valid_structure_kwargs()
        structure_dict["action"] = "execute_live"
        with pytest.raises(ValidationError):
            StructureIntent.model_validate(structure_dict)

    def test_dte_max_below_dte_min_rejected(self):
        structure_dict = _valid_structure_kwargs()
        structure_dict["target_dte_min"] = 30
        structure_dict["target_dte_max"] = 10
        with pytest.raises(ValidationError):
            StructureIntent.model_validate(structure_dict)

    def test_delta_out_of_range_rejected(self):
        structure_dict = _valid_structure_kwargs()
        structure_dict["approx_target_delta"] = 1.5
        with pytest.raises(ValidationError):
            StructureIntent.model_validate(structure_dict)

    def test_rank_must_be_positive(self):
        payload = _valid_proposal_kwargs()
        payload_dict = {**payload, "structure": payload["structure"].model_dump(), "rank": 0}
        with pytest.raises(ValidationError):
            TradeProposal.model_validate(payload_dict)

    def test_non_dict_payload_rejected(self):
        with pytest.raises(ValidationError):
            TradeProposal.model_validate("not a trade proposal")


class TestEnsureTradeProposalBoundaryGuard:
    """This is the guard every future execution-adjacent entry point must
    call. It must reject anything that isn't a genuine, already-validated
    TradeProposal instance."""

    def test_rejects_plain_dict_even_with_correct_shape(self):
        payload = _valid_proposal_kwargs()
        payload_dict = {**payload, "structure": payload["structure"].model_dump()}
        with pytest.raises(TypeError):
            ensure_trade_proposal(payload_dict)

    def test_rejects_other_schema_instance(self):
        review = AdversarialReview(proposal_id="prop-1", critique="Too correlated with existing book.")
        with pytest.raises(TypeError):
            ensure_trade_proposal(review)

    def test_rejects_subclass_instance(self):
        class SneakyTradeProposal(TradeProposal):
            pass

        proposal = SneakyTradeProposal(**_valid_proposal_kwargs())
        with pytest.raises(TypeError):
            ensure_trade_proposal(proposal)

    def test_rejects_none_and_primitives(self):
        for bad in (None, "trade_proposal", 42, [1, 2, 3]):
            with pytest.raises(TypeError):
                ensure_trade_proposal(bad)


class TestModelsAreFrozen:
    def test_trade_proposal_is_immutable(self):
        proposal = TradeProposal(**_valid_proposal_kwargs())
        with pytest.raises(ValidationError):
            proposal.rank = 99  # type: ignore[misc]
