"""End-to-end proof that malformed or execution-shaped LLM output cannot
reach the execution boundary.

Each test simulates a raw Anthropic API response — including deliberately
adversarial ones an attacker or a misbehaving model might produce — and
walks it through the same two-stage path real code must use:

    1. src.llm.client.validate_tool_response()  (parse + schema validation)
    2. src.llm.schemas.ensure_trade_proposal()  (execution boundary guard)

For a response to "reach the execution system" in this codebase, it must
pass both stages and come out the other side as a `TradeProposal`
instance. These tests show that no malformed input does.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from src.llm.client import LLMOutputError, validate_tool_response
from src.llm.schemas import TradeProposal, ensure_trade_proposal


def _tool_use_response(input_: Any, *, tool_name: str = "submit_structured_output") -> SimpleNamespace:
    block = SimpleNamespace(type="tool_use", name=tool_name, input=input_)
    return SimpleNamespace(id="msg_adversarial", content=[block])


VALID_TRADE_PROPOSAL_INPUT: dict[str, Any] = {
    "proposal_id": "prop-42",
    "source_agent": "strategy_analyst",
    "structure": {
        "symbol": "MSFT",
        "strategy_type": "put_credit_spread",
        "action": "open",
        "target_dte_min": 25,
        "target_dte_max": 40,
        "approx_target_delta": 0.25,
        "notes": None,
    },
    "rationale": "Elevated IV rank, no earnings in window, liquid chain.",
    "conviction": "medium",
    "risk_flags": [],
    "rank": 1,
}


def _run_full_pipeline(raw_input: Any) -> TradeProposal:
    """The only path from a raw model response to something the (not yet
    implemented) execution system could ever consume."""
    response = _tool_use_response(raw_input)
    validated = validate_tool_response(response, TradeProposal)
    return ensure_trade_proposal(validated)


class TestValidTradeProposalReachesTheBoundary:
    """Positive control: proves the pipeline isn't just failing closed on
    everything — a genuinely well-formed proposal does get through."""

    def test_well_formed_proposal_survives_full_pipeline(self):
        proposal = _run_full_pipeline(VALID_TRADE_PROPOSAL_INPUT)
        assert isinstance(proposal, TradeProposal)
        assert proposal.proposal_id == "prop-42"


class TestExecutionShapedFieldsNeverSurvivePipeline:
    """An LLM (or an attacker crafting a payload) trying to smuggle
    something that looks like a broker instruction through a
    valid-looking TradeProposal must be stopped at stage 1."""

    @pytest.mark.parametrize(
        "smuggled_fields",
        [
            {"execute": True},
            {"submit_order": True},
            {"order_id": "IBKR-98765"},
            {"broker": "ibkr", "account_id": "U1234567"},
            {"quantity": 500},
            {"bypass_risk_gate": True, "reason": "urgent"},
            {"live_mode": True},
        ],
    )
    def test_smuggled_execution_field_blocked_at_validation(self, smuggled_fields: dict):
        malformed = {**VALID_TRADE_PROPOSAL_INPUT, **smuggled_fields}
        with pytest.raises(LLMOutputError):
            _run_full_pipeline(malformed)

    def test_smuggled_field_nested_inside_structure_blocked(self):
        malformed = {
            **VALID_TRADE_PROPOSAL_INPUT,
            "structure": {**VALID_TRADE_PROPOSAL_INPUT["structure"], "execute_immediately": True},
        }
        with pytest.raises(LLMOutputError):
            _run_full_pipeline(malformed)


class TestOutOfScopeStrategiesNeverSurvivePipeline:
    """The platform's excluded structures (naked calls, unfunded naked
    puts, 0DTE-style same-day expiry via action tricks, etc.) must not be
    expressible as a valid TradeProposal at all."""

    def test_naked_call_strategy_type_rejected(self):
        malformed = {
            **VALID_TRADE_PROPOSAL_INPUT,
            "structure": {**VALID_TRADE_PROPOSAL_INPUT["structure"], "strategy_type": "naked_call"},
        }
        with pytest.raises(LLMOutputError):
            _run_full_pipeline(malformed)

    def test_unrecognized_action_rejected(self):
        malformed = {
            **VALID_TRADE_PROPOSAL_INPUT,
            "structure": {**VALID_TRADE_PROPOSAL_INPUT["structure"], "action": "execute_live"},
        }
        with pytest.raises(LLMOutputError):
            _run_full_pipeline(malformed)


class TestFreeTextAndProtocolLevelTamperingBlocked:
    def test_free_text_response_instead_of_tool_call_blocked(self):
        text_response = SimpleNamespace(
            id="msg_text",
            content=[SimpleNamespace(type="text", text="I'll just place the order for you directly: BUY 10 MSFT...")],
        )
        with pytest.raises(LLMOutputError):
            validate_tool_response(text_response, TradeProposal)

    def test_wrong_tool_name_blocked(self):
        response = _tool_use_response(VALID_TRADE_PROPOSAL_INPUT, tool_name="place_order")
        with pytest.raises(LLMOutputError):
            validate_tool_response(response, TradeProposal)

    def test_duplicate_tool_use_blocks_blocked(self):
        block1 = SimpleNamespace(type="tool_use", name="submit_structured_output", input=VALID_TRADE_PROPOSAL_INPUT)
        block2 = SimpleNamespace(type="tool_use", name="submit_structured_output", input=VALID_TRADE_PROPOSAL_INPUT)
        response = SimpleNamespace(id="msg_dup", content=[block1, block2])
        with pytest.raises(LLMOutputError):
            validate_tool_response(response, TradeProposal)

    def test_missing_required_fields_blocked(self):
        malformed = {k: v for k, v in VALID_TRADE_PROPOSAL_INPUT.items() if k != "rationale"}
        with pytest.raises(LLMOutputError):
            _run_full_pipeline(malformed)


class TestBoundaryGuardIsTheLastLine:
    """Even if a caller somehow got a dict or the wrong schema instance
    past stage 1 (e.g. by calling ensure_trade_proposal directly instead
    of going through validate_tool_response), stage 2 must independently
    refuse it — no single point of failure lets malformed data through."""

    def test_guard_rejects_raw_dict_that_looks_like_a_valid_proposal(self):
        with pytest.raises(TypeError):
            ensure_trade_proposal(dict(VALID_TRADE_PROPOSAL_INPUT))

    def test_guard_rejects_a_forged_object_with_matching_attributes(self):
        class Forged:
            """Has every attribute a TradeProposal has, but isn't one."""

            proposal_id = "prop-42"
            source_agent = "strategy_analyst"
            structure = None
            rationale = "looks legit"
            conviction = "high"
            risk_flags: list = []
            rank = 1

        with pytest.raises(TypeError):
            ensure_trade_proposal(Forged())
