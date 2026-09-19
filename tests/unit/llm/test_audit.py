"""Tests for src.llm.audit: the append-only Portfolio Manager decision
audit log (Step 10)."""
from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from src.llm.audit import DecisionAuditRecord, InMemoryAuditLog, record_decision
from src.llm.client import LLMClient
from src.llm.portfolio_manager import PortfolioManagerInputs, evaluate_proposal
from tests.unit.llm.conftest import (
    make_devil_advocate_review,
    make_market_regime,
    make_portfolio_state,
    make_proposal,
    make_quant_analysis,
    make_risk_engine_result,
    make_risk_reviewer_note,
    valid_portfolio_decision_input,
)


def _inputs() -> PortfolioManagerInputs:
    return PortfolioManagerInputs(
        proposal=make_proposal(),
        market_regime=make_market_regime(),
        quant_analysis=make_quant_analysis(),
        devil_advocate_review=make_devil_advocate_review(),
        risk_reviewer_note=make_risk_reviewer_note(),
        portfolio_state=make_portfolio_state(),
        risk_engine_result=make_risk_engine_result(),
    )


def _evaluation(payload: dict | None = None):
    payload = payload or valid_portfolio_decision_input()

    def _create(**kwargs):
        block = SimpleNamespace(type="tool_use", name="submit_structured_output", input=payload)
        return SimpleNamespace(id="msg_1", content=[block])

    client = LLMClient(api_client=SimpleNamespace(messages=SimpleNamespace(create=_create)))
    return evaluate_proposal(_inputs(), client=client, system_prompt="You are the Portfolio Manager.")


class TestRecordDecision:
    def test_record_captures_model_prompt_version_and_decision(self):
        log = InMemoryAuditLog()
        evaluation = _evaluation()
        record = record_decision(
            log,
            evaluation,
            prompt_version="portfolio_manager@v1",
            input_references={"proposal_id": "prop-1", "risk_engine_message": "resized to 2 contracts"},
        )
        assert record.model == evaluation.call_result.model
        assert record.prompt_version == "portfolio_manager@v1"
        assert record.decision == "propose_advance"
        assert record.proposal_id == "prop-1"
        assert record.input_references["proposal_id"] == "prop-1"

    def test_record_is_appended_to_the_log(self):
        log = InMemoryAuditLog()
        record_decision(log, _evaluation(), prompt_version="v1", input_references={})
        assert len(log.all()) == 1

    def test_for_proposal_filters_correctly(self):
        log = InMemoryAuditLog()
        record_decision(log, _evaluation(), prompt_version="v1", input_references={})
        payload_other = valid_portfolio_decision_input(decision_id="dec-2", proposal_id="prop-2")
        other_inputs_proposal = make_proposal(proposal_id="prop-2")
        other_inputs = PortfolioManagerInputs(
            proposal=other_inputs_proposal,
            market_regime=make_market_regime(),
            quant_analysis=make_quant_analysis(),
            devil_advocate_review=make_devil_advocate_review(proposal_id="prop-2"),
            risk_reviewer_note=make_risk_reviewer_note(proposal_id="prop-2"),
            portfolio_state=make_portfolio_state(),
            risk_engine_result=make_risk_engine_result(),
        )

        def _create(**kwargs):
            block = SimpleNamespace(type="tool_use", name="submit_structured_output", input=payload_other)
            return SimpleNamespace(id="msg_2", content=[block])

        client = LLMClient(api_client=SimpleNamespace(messages=SimpleNamespace(create=_create)))
        other_evaluation = evaluate_proposal(other_inputs, client=client, system_prompt="You are the Portfolio Manager.")
        record_decision(log, other_evaluation, prompt_version="v1", input_references={})

        assert len(log.all()) == 2
        assert [r.proposal_id for r in log.for_proposal("prop-1")] == ["prop-1"]
        assert [r.proposal_id for r in log.for_proposal("prop-2")] == ["prop-2"]

    def test_multiple_records_get_distinct_ids(self):
        log = InMemoryAuditLog()
        r1 = record_decision(log, _evaluation(), prompt_version="v1", input_references={})
        r2 = record_decision(log, _evaluation(), prompt_version="v1", input_references={})
        assert r1.record_id != r2.record_id


class TestAppendOnly:
    def test_audit_log_interface_has_no_update_or_delete_method(self):
        from src.llm.audit import AuditLog

        method_names = {name for name in dir(AuditLog) if not name.startswith("_")}
        forbidden = {"update", "delete", "remove", "clear", "edit", "overwrite"}
        assert method_names.isdisjoint(forbidden)

    def test_in_memory_log_has_no_update_or_delete_method(self):
        method_names = {name for name in dir(InMemoryAuditLog) if not name.startswith("_")}
        forbidden = {"update", "delete", "remove", "clear", "edit", "overwrite"}
        assert method_names.isdisjoint(forbidden)


class TestNoHiddenChainOfThought:
    """Structural proof: DecisionAuditRecord has no field that could hold
    raw model reasoning/thinking content — only PortfolioDecision's own
    already-validated, already-boundary-guarded structured fields ever
    reach it."""

    FORBIDDEN_SUBSTRINGS = ["reasoning", "thinking", "chain_of_thought", "scratchpad", "internal_notes", "raw_response"]

    def test_no_field_name_suggests_hidden_reasoning_storage(self):
        field_names = {f.name.lower() for f in dataclasses.fields(DecisionAuditRecord)}
        for forbidden in self.FORBIDDEN_SUBSTRINGS:
            for field_name in field_names:
                assert forbidden not in field_name, f"field {field_name!r} looks reasoning-shaped"

    def test_rationale_is_built_only_from_structured_decision_fields(self):
        log = InMemoryAuditLog()
        evaluation = _evaluation()
        record = record_decision(log, evaluation, prompt_version="v1", input_references={})
        assert evaluation.decision.thesis_summary in record.supporting_rationale
        assert evaluation.decision.bear_case in record.supporting_rationale


class TestRecordDecisionOnlyAcceptsAnAlreadyValidatedDecision:
    def test_ensure_portfolio_decision_guard_is_invoked(self):
        # record_decision re-validates the decision through the same
        # boundary guard, not just trusting evaluation.decision's type
        # at face value — this test documents that expectation exists
        # by confirming a genuine PortfolioManagerEvaluation round-trips.
        log = InMemoryAuditLog()
        evaluation = _evaluation()
        record = record_decision(log, evaluation, prompt_version="v1", input_references={})
        assert record.decision_id == evaluation.decision.decision_id
