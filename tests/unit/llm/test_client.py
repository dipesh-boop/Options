"""Tests for src.llm.client using an injected fake Anthropic client —
no network access, no real API key required."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from src.llm.client import LLMClient, LLMOutputError, validate_tool_response
from src.llm.router import ModelRouter, TaskType
from src.llm.schemas import MarketRegimeAssessment


def _tool_use_block(name: str, input_: Any) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", name=name, input=input_)


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _fake_response(content: list[Any], response_id: str = "msg_test123") -> SimpleNamespace:
    return SimpleNamespace(id=response_id, content=content)


VALID_REGIME_INPUT = {
    "regime": "normal",
    "commentary": "Vol is unremarkable; no notable dislocations.",
    "notable_events": [],
}


class TestValidateToolResponseHappyPath:
    def test_valid_tool_use_validates(self):
        response = _fake_response([_tool_use_block("submit_structured_output", VALID_REGIME_INPUT)])
        result = validate_tool_response(response, MarketRegimeAssessment)
        assert isinstance(result, MarketRegimeAssessment)
        assert result.regime == "normal"


class TestValidateToolResponseRejectsMalformed:
    def test_no_content_blocks(self):
        response = _fake_response([])
        with pytest.raises(LLMOutputError):
            validate_tool_response(response, MarketRegimeAssessment)

    def test_model_returned_free_text_instead_of_tool_call(self):
        response = _fake_response([_text_block("Here's my analysis in prose instead of calling the tool...")])
        with pytest.raises(LLMOutputError):
            validate_tool_response(response, MarketRegimeAssessment)

    def test_wrong_tool_name(self):
        response = _fake_response([_tool_use_block("some_other_tool", VALID_REGIME_INPUT)])
        with pytest.raises(LLMOutputError):
            validate_tool_response(response, MarketRegimeAssessment)

    def test_multiple_tool_use_blocks_rejected(self):
        response = _fake_response(
            [
                _tool_use_block("submit_structured_output", VALID_REGIME_INPUT),
                _tool_use_block("submit_structured_output", VALID_REGIME_INPUT),
            ]
        )
        with pytest.raises(LLMOutputError):
            validate_tool_response(response, MarketRegimeAssessment)

    def test_tool_input_not_a_dict(self):
        response = _fake_response([_tool_use_block("submit_structured_output", "just a string")])
        with pytest.raises(LLMOutputError):
            validate_tool_response(response, MarketRegimeAssessment)

    def test_tool_input_missing_required_field(self):
        malformed = {"commentary": "missing the regime field"}
        response = _fake_response([_tool_use_block("submit_structured_output", malformed)])
        with pytest.raises(LLMOutputError):
            validate_tool_response(response, MarketRegimeAssessment)

    def test_tool_input_with_extra_execution_shaped_field(self):
        malformed = {**VALID_REGIME_INPUT, "execute_trade": True}
        response = _fake_response([_tool_use_block("submit_structured_output", malformed)])
        with pytest.raises(LLMOutputError):
            validate_tool_response(response, MarketRegimeAssessment)

    def test_tool_input_invalid_enum_value(self):
        malformed = {**VALID_REGIME_INPUT, "regime": "extremely_bullish"}
        response = _fake_response([_tool_use_block("submit_structured_output", malformed)])
        with pytest.raises(LLMOutputError):
            validate_tool_response(response, MarketRegimeAssessment)


@dataclass
class _RecordingMessagesAPI:
    captured_kwargs: dict[str, Any] = field(default_factory=dict)
    response_to_return: Any = None

    def create(self, **kwargs: Any) -> Any:
        self.captured_kwargs = kwargs
        return self.response_to_return


@dataclass
class _FakeAnthropicClient:
    messages: _RecordingMessagesAPI


class TestLLMClientCompleteStructured:
    def _make_client(self, config_path) -> tuple[LLMClient, _RecordingMessagesAPI]:
        messages_api = _RecordingMessagesAPI(
            response_to_return=_fake_response([_tool_use_block("submit_structured_output", VALID_REGIME_INPUT)])
        )
        fake_client = _FakeAnthropicClient(messages=messages_api)
        router = ModelRouter(config_path=config_path)
        return LLMClient(router=router, api_client=fake_client), messages_api

    def test_routes_to_correct_model_and_returns_validated_output(self, tmp_path):
        config_path = tmp_path / "llm.yaml"
        config_path.write_text(
            "tiers:\n"
            "  routine:\n"
            "    model: test-routine-model\n"
            "    max_tokens: 111\n"
            "    temperature: 0.05\n"
            "task_routing:\n"
            "  basic_classification: routine\n"
        )
        client, messages_api = self._make_client(config_path)

        result = client.complete_structured(
            agent_role="market_regime",
            task_type=TaskType.BASIC_CLASSIFICATION,
            system_prompt="You are the Market Regime agent.",
            user_content="{}",
            output_schema=MarketRegimeAssessment,
        )

        assert messages_api.captured_kwargs["model"] == "test-routine-model"
        assert messages_api.captured_kwargs["max_tokens"] == 111
        assert messages_api.captured_kwargs["tool_choice"] == {
            "type": "tool",
            "name": "submit_structured_output",
        }
        assert result.agent_role == "market_regime"
        assert result.model == "test-routine-model"
        assert isinstance(result.validated_output, MarketRegimeAssessment)

    def test_malformed_response_raises_and_never_returns_a_result(self, tmp_path):
        config_path = tmp_path / "llm.yaml"
        config_path.write_text(
            "tiers:\n"
            "  routine:\n"
            "    model: test-routine-model\n"
            "    max_tokens: 111\n"
            "    temperature: 0.05\n"
            "task_routing:\n"
            "  basic_classification: routine\n"
        )
        messages_api = _RecordingMessagesAPI(
            response_to_return=_fake_response(
                [_tool_use_block("submit_structured_output", {"regime": "extremely_bullish"})]
            )
        )
        fake_client = _FakeAnthropicClient(messages=messages_api)
        client = LLMClient(router=ModelRouter(config_path=config_path), api_client=fake_client)

        with pytest.raises(LLMOutputError):
            client.complete_structured(
                agent_role="market_regime",
                task_type=TaskType.BASIC_CLASSIFICATION,
                system_prompt="You are the Market Regime agent.",
                user_content="{}",
                output_schema=MarketRegimeAssessment,
            )
