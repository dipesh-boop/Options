"""Anthropic API client wrapper for the LLM orchestration layer.

Every completion this client returns is a validated Pydantic instance
(src/llm/schemas.py) or an exception — never a raw string/dict a caller
might be tempted to trust as-is. Structured output is enforced by giving
the model exactly one tool matching the target schema and forcing
tool_choice to it; the tool call's `input` is the only thing ever
validated and returned.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from src.llm.router import ModelRouter, TaskType, get_default_router

logger = logging.getLogger(__name__)

SchemaT = TypeVar("SchemaT", bound=BaseModel)

_SUBMIT_TOOL_NAME = "submit_structured_output"


class LLMOutputError(RuntimeError):
    """Raised when the model's response cannot be validated into the
    requested schema. Callers must treat this as 'no usable output' —
    this client has no partial-trust fallback path anywhere."""


class _MessagesAPI(Protocol):
    def create(self, **kwargs: Any) -> Any: ...


class _AnthropicClientLike(Protocol):
    messages: _MessagesAPI


@dataclass(frozen=True)
class AgentCallResult:
    """What every agent call returns, alongside the validated schema
    instance, for the append-only audit log (not implemented yet — see
    IMPLEMENTATION_PLAN.md Phase 5)."""

    agent_role: str
    task_type: str
    model: str
    raw_response_id: str
    validated_output: BaseModel


def validate_tool_response(response: Any, output_schema: type[SchemaT]) -> SchemaT:
    """Extract the forced tool_use block from an Anthropic Messages API
    response and validate its `input` against `output_schema`.

    Raises LLMOutputError for anything that isn't a single, correctly
    named tool_use block whose input passes Pydantic validation — there
    is no silent coercion, no "best effort" partial parse, and no path
    from a malformed response to a usable schema instance.

    `response` is accessed only via `.content` (a list of blocks with
    `.type`, `.name`, `.input`), so callers can pass either a real
    `anthropic.types.Message` or a lightweight test double.
    """
    content = getattr(response, "content", None)
    if not content:
        raise LLMOutputError("Model response has no content blocks; nothing to validate.")

    tool_use_blocks = [b for b in content if getattr(b, "type", None) == "tool_use"]
    if not tool_use_blocks:
        raise LLMOutputError(
            "Model response contained no tool_use block (it likely returned free text "
            "instead of calling the structured-output tool); nothing to validate."
        )
    if len(tool_use_blocks) > 1:
        raise LLMOutputError(
            f"Model response contained {len(tool_use_blocks)} tool_use blocks; expected exactly one."
        )

    block = tool_use_blocks[0]
    if block.name != _SUBMIT_TOOL_NAME:
        raise LLMOutputError(f"Unexpected tool name in response: {block.name!r}")

    raw_input = block.input
    if not isinstance(raw_input, dict):
        raise LLMOutputError(f"Tool input was not a JSON object: {type(raw_input).__name__}")

    try:
        return output_schema.model_validate(raw_input)
    except ValidationError as exc:
        raise LLMOutputError(f"Model output failed {output_schema.__name__} validation: {exc}") from exc


class LLMClient:
    """Thin wrapper around the Anthropic Messages API. Accepts an
    injected API client and router so tests never need network access or
    a real API key."""

    def __init__(
        self,
        router: ModelRouter | None = None,
        api_client: _AnthropicClientLike | None = None,
    ) -> None:
        self._router = router or get_default_router()
        self._client = api_client or self._build_default_client()

    @staticmethod
    def _build_default_client() -> _AnthropicClientLike:
        # Imported lazily so this module has no hard dependency on a
        # configured API key unless a real client is actually needed.
        import anthropic

        return anthropic.Anthropic()

    def complete_structured(
        self,
        *,
        agent_role: str,
        task_type: TaskType | str,
        system_prompt: str,
        user_content: str,
        output_schema: type[SchemaT],
    ) -> AgentCallResult:
        model_spec = self._router.resolve(task_type)

        tool = {
            "name": _SUBMIT_TOOL_NAME,
            "description": (
                f"Submit the {output_schema.__name__} result. This is the only "
                "way to respond — do not respond with plain text."
            ),
            "input_schema": output_schema.model_json_schema(),
        }

        response = self._client.messages.create(
            model=model_spec.model,
            max_tokens=model_spec.max_tokens,
            temperature=model_spec.temperature,
            system=system_prompt,
            tools=[tool],
            tool_choice={"type": "tool", "name": _SUBMIT_TOOL_NAME},
            messages=[{"role": "user", "content": user_content}],
        )

        validated = validate_tool_response(response, output_schema)

        return AgentCallResult(
            agent_role=agent_role,
            task_type=task_type.value if isinstance(task_type, TaskType) else task_type,
            model=model_spec.model,
            raw_response_id=getattr(response, "id", ""),
            validated_output=validated,
        )
