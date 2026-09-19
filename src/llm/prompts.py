"""Prompt assembly for the Multi-Agent Layer.

Loads each agent's persona/instructions from `.claude/agents/<role>.md`
(YAML frontmatter + Markdown body) and combines it with per-call context
(src/llm/context.py) into the system prompt sent via src/llm/client.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

AGENTS_DIR = Path(__file__).resolve().parents[2] / ".claude" / "agents"

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)

_REQUIRED_FRONTMATTER_KEYS = ("name", "description", "default_task_type", "tools")


class AgentDefinitionError(RuntimeError):
    """Raised when an agent's .md definition file is missing or malformed."""


@dataclass(frozen=True)
class AgentDefinition:
    role: str
    name: str
    description: str
    default_task_type: str
    tools: list[str]
    instructions: str


def load_agent_definition(role: str, *, agents_dir: Path = AGENTS_DIR) -> AgentDefinition:
    """Parse `.claude/agents/<role>.md` into an AgentDefinition. Raises
    AgentDefinitionError for a missing file, missing/invalid frontmatter,
    or a missing required frontmatter key — never returns a partially
    populated definition."""
    path = agents_dir / f"{role}.md"
    if not path.is_file():
        raise AgentDefinitionError(f"No agent definition found for role={role!r} at {path}")

    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise AgentDefinitionError(f"{path} is missing YAML frontmatter (--- ... ---)")

    frontmatter_raw, body = match.groups()
    try:
        frontmatter: dict[str, Any] = yaml.safe_load(frontmatter_raw) or {}
    except yaml.YAMLError as exc:
        raise AgentDefinitionError(f"{path} has invalid YAML frontmatter: {exc}") from exc

    missing = [k for k in _REQUIRED_FRONTMATTER_KEYS if k not in frontmatter]
    if missing:
        raise AgentDefinitionError(f"{path} frontmatter is missing required key(s): {missing}")

    body_stripped = body.strip()
    if not body_stripped:
        raise AgentDefinitionError(f"{path} has empty instructions body")

    return AgentDefinition(
        role=role,
        name=str(frontmatter["name"]),
        description=str(frontmatter["description"]),
        default_task_type=str(frontmatter["default_task_type"]),
        tools=list(frontmatter["tools"]),
        instructions=body_stripped,
    )


_SCHEMA_INSTRUCTION_SUFFIX = (
    "\n\n---\n"
    "You must respond only by calling the provided tool, with output that "
    "matches its schema exactly. Do not include any other text, "
    "explanation, or additional fields. If you cannot produce a confident "
    "response, call the tool anyway with your best qualitative assessment "
    "and appropriate risk_flags — never invent numeric data you were not "
    "given, and never suggest that any output of yours is itself an order "
    "or an instruction to a broker."
)


def build_system_prompt(agent_def: AgentDefinition) -> str:
    """Combine an agent's persona instructions with the fixed schema
    discipline reminder every role gets, regardless of which schema it
    targets on a given call."""
    return f"{agent_def.instructions}{_SCHEMA_INSTRUCTION_SUFFIX}"
