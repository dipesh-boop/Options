"""Orchestration tests for src.llm.portfolio_manager (Step 10)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.llm.client import LLMClient
from src.llm.portfolio_manager import (
    MissingInputError,
    PortfolioManagerCrossCheckError,
    PortfolioManagerInputs,
    build_portfolio_manager_context,
    evaluate_proposal,
    validate_inputs_complete,
)
from tests.unit.llm.conftest import (
    DATA_TS,
    NOW,
    make_devil_advocate_review,
    make_market_regime,
    make_portfolio_state,
    make_proposal,
    make_quant_analysis,
    make_risk_engine_result,
    make_risk_reviewer_note,
    valid_portfolio_decision_input,
)


def _full_inputs(**overrides) -> PortfolioManagerInputs:
    base = dict(
        proposal=make_proposal(),
        market_regime=make_market_regime(),
        quant_analysis=make_quant_analysis(),
        devil_advocate_review=make_devil_advocate_review(),
        risk_reviewer_note=make_risk_reviewer_note(),
        portfolio_state=make_portfolio_state(),
        risk_engine_result=make_risk_engine_result(),
    )
    base.update(overrides)
    return PortfolioManagerInputs(**base)


def _fake_client(response_input: dict) -> LLMClient:
    def _create(**kwargs):
        block = SimpleNamespace(type="tool_use", name="submit_structured_output", input=response_input)
        return SimpleNamespace(id="msg_1", content=[block])

    api_client = SimpleNamespace(messages=SimpleNamespace(create=_create))
    return LLMClient(api_client=api_client)


class TestMissingData:
    @pytest.mark.parametrize(
        "field_name",
        ["proposal", "market_regime", "quant_analysis", "devil_advocate_review", "risk_reviewer_note", "portfolio_state"],
    )
    def test_each_required_input_missing_raises(self, field_name: str):
        inputs = _full_inputs(**{field_name: None})
        with pytest.raises(MissingInputError):
            validate_inputs_complete(inputs)

    def test_build_context_also_refuses_missing_input(self):
        inputs = _full_inputs(market_regime=None)
        with pytest.raises(MissingInputError):
            build_portfolio_manager_context(inputs)

    def test_evaluate_proposal_never_calls_the_model_when_input_is_missing(self):
        inputs = _full_inputs(market_regime=None)
        calls = []

        def _create(**kwargs):
            calls.append(kwargs)
            raise AssertionError("should never be called")

        client = LLMClient(api_client=SimpleNamespace(messages=SimpleNamespace(create=_create)))
        with pytest.raises(MissingInputError):
            evaluate_proposal(inputs, client=client, system_prompt="You are the Portfolio Manager.")
        assert calls == []

    def test_risk_engine_result_is_legitimately_optional_since_step_12(self):
        # Step 12's required pipeline order runs the Portfolio Manager
        # BEFORE Python Risk Engine, so this input can't be hard-required
        # any more — a None here must not raise, unlike every other
        # field above.
        inputs = _full_inputs(risk_engine_result=None)
        validate_inputs_complete(inputs)  # does not raise
        context = build_portfolio_manager_context(inputs)
        assert '"risk_engine_result": null' in context


class TestMalformedTradeProposal:
    def test_a_plain_dict_in_place_of_a_trade_proposal_is_rejected(self):
        inputs = _full_inputs(proposal={"ticker": "SPY", "strategy": "put_credit_spread"})
        with pytest.raises(TypeError):
            validate_inputs_complete(inputs)

    def test_a_subclass_of_trade_proposal_is_rejected(self):
        from src.llm.schemas import TradeProposal

        class SneakyTradeProposal(TradeProposal):
            pass

        sneaky = SneakyTradeProposal(**make_proposal().model_dump())
        inputs = _full_inputs(proposal=sneaky)
        with pytest.raises(TypeError):
            validate_inputs_complete(inputs)


class TestStaleData:
    def test_a_proposal_built_from_stale_market_data_cannot_even_be_constructed(self):
        # TradeProposal's own freshness validator (src.llm.schemas,
        # unmodified by Step 10) already rejects this — proof that stale
        # data can never reach the Portfolio Manager in the first place.
        from src.llm.schemas import TradeProposal

        with pytest.raises(ValidationError):
            make_proposal(data_timestamp=NOW - timedelta(hours=2))


class TestSuccessfulEvaluation:
    def test_propose_advance_decision_round_trips(self):
        inputs = _full_inputs()
        client = _fake_client(valid_portfolio_decision_input())
        evaluation = evaluate_proposal(inputs, client=client, system_prompt="You are the Portfolio Manager.")
        assert evaluation.decision.decision == "propose_advance"
        assert evaluation.decision.proposal_id == inputs.proposal.proposal_id
        assert evaluation.call_result.agent_role == "portfolio_manager"

    def test_reject_decision_round_trips(self):
        inputs = _full_inputs()
        payload = valid_portfolio_decision_input(decision="reject", cash_preferred=False)
        client = _fake_client(payload)
        evaluation = evaluate_proposal(inputs, client=client, system_prompt="You are the Portfolio Manager.")
        assert evaluation.decision.decision == "reject"

    def test_hold_cash_decision_round_trips(self):
        inputs = _full_inputs()
        payload = valid_portfolio_decision_input(decision="hold_cash", cash_preferred=True)
        client = _fake_client(payload)
        evaluation = evaluate_proposal(inputs, client=client, system_prompt="You are the Portfolio Manager.")
        assert evaluation.decision.decision == "hold_cash"
        assert evaluation.decision.cash_preferred is True


class TestCrossCheckAgainstTheActualProposal:
    def test_decision_for_a_different_proposal_id_is_rejected(self):
        inputs = _full_inputs()
        payload = valid_portfolio_decision_input(proposal_id="some-other-proposal")
        client = _fake_client(payload)
        with pytest.raises(PortfolioManagerCrossCheckError):
            evaluate_proposal(inputs, client=client, system_prompt="You are the Portfolio Manager.")

    def test_decision_with_a_different_market_regime_than_supplied_is_rejected(self):
        inputs = _full_inputs(market_regime=make_market_regime(regime="crisis"))
        payload = valid_portfolio_decision_input(market_regime="normal")  # doesn't match "crisis"
        client = _fake_client(payload)
        with pytest.raises(PortfolioManagerCrossCheckError):
            evaluate_proposal(inputs, client=client, system_prompt="You are the Portfolio Manager.")


class TestAttemptedRiskOverride:
    """The Portfolio Manager has no field through which it could ever
    override Python Risk Engine or change an approved position size —
    these tests prove that holds even when the model's raw response
    tries to smuggle one in."""

    def test_extra_field_claiming_an_approved_contract_count_is_rejected(self):
        from src.llm.client import LLMOutputError

        inputs = _full_inputs()
        payload = {**valid_portfolio_decision_input(), "approved_contracts": 999}
        client = _fake_client(payload)
        with pytest.raises(LLMOutputError):
            evaluate_proposal(inputs, client=client, system_prompt="You are the Portfolio Manager.")

    def test_extra_field_claiming_a_risk_override_is_rejected(self):
        from src.llm.client import LLMOutputError

        inputs = _full_inputs()
        payload = {**valid_portfolio_decision_input(), "override_risk_engine": True, "bypass_reason": "urgent"}
        client = _fake_client(payload)
        with pytest.raises(LLMOutputError):
            evaluate_proposal(inputs, client=client, system_prompt="You are the Portfolio Manager.")

    def test_approve_as_a_decision_value_is_rejected_not_silently_coerced(self):
        from src.llm.client import LLMOutputError

        inputs = _full_inputs()
        payload = valid_portfolio_decision_input(decision="approve")
        client = _fake_client(payload)
        with pytest.raises(LLMOutputError):
            evaluate_proposal(inputs, client=client, system_prompt="You are the Portfolio Manager.")

    def test_risk_engine_result_in_the_prompt_is_never_mutated_by_the_call(self):
        # The RiskEngineContext object passed in is read-only reference
        # data; nothing about calling the model can alter it.
        inputs = _full_inputs()
        original_decision = inputs.risk_engine_result.decision
        client = _fake_client(valid_portfolio_decision_input())
        evaluate_proposal(inputs, client=client, system_prompt="You are the Portfolio Manager.")
        assert inputs.risk_engine_result.decision == original_decision == "resize"


class TestHallucinatedNumericalFields:
    def test_a_numeric_looking_field_injected_anywhere_is_rejected(self):
        from src.llm.client import LLMOutputError

        inputs = _full_inputs()
        for bad_field in ({"max_loss": 100.0}, {"implied_volatility": 0.3}, {"account_balance": 1_000_000.0}, {"delta": 0.4}):
            payload = {**valid_portfolio_decision_input(), **bad_field}
            client = _fake_client(payload)
            with pytest.raises(LLMOutputError):
                evaluate_proposal(inputs, client=client, system_prompt="You are the Portfolio Manager.")


class TestMalformedLLMOutput:
    def test_missing_required_field_rejected(self):
        from src.llm.client import LLMOutputError

        inputs = _full_inputs()
        payload = {k: v for k, v in valid_portfolio_decision_input().items() if k != "bear_case"}
        client = _fake_client(payload)
        with pytest.raises(LLMOutputError):
            evaluate_proposal(inputs, client=client, system_prompt="You are the Portfolio Manager.")

    def test_free_text_instead_of_tool_call_rejected(self):
        from src.llm.client import LLMOutputError

        inputs = _full_inputs()
        text_response = SimpleNamespace(
            id="msg_text", content=[SimpleNamespace(type="text", text="I recommend approving this trade.")]
        )
        client = LLMClient(
            api_client=SimpleNamespace(messages=SimpleNamespace(create=lambda **kwargs: text_response))
        )
        with pytest.raises(LLMOutputError):
            evaluate_proposal(inputs, client=client, system_prompt="You are the Portfolio Manager.")

    def test_wrong_type_for_a_field_rejected(self):
        from src.llm.client import LLMOutputError

        inputs = _full_inputs()
        payload = valid_portfolio_decision_input(confidence=95)  # should be a Conviction string
        client = _fake_client(payload)
        with pytest.raises(LLMOutputError):
            evaluate_proposal(inputs, client=client, system_prompt="You are the Portfolio Manager.")
