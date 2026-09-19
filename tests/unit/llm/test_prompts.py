"""Tests for .claude/agents/*.md parsing and system prompt assembly."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.llm.prompts import (
    AGENTS_DIR,
    AgentDefinitionError,
    build_system_prompt,
    load_agent_definition,
)

EXPECTED_ROLES = [
    "portfolio_manager",
    "market_regime",
    "opportunity_scanner",
    "strategy_analyst",
    "devil_advocate",
    "risk_reviewer",
    "trade_manager",
    "performance_auditor",
]


class TestRealAgentDefinitions:
    @pytest.mark.parametrize("role", EXPECTED_ROLES)
    def test_each_shipped_agent_file_parses(self, role: str):
        agent_def = load_agent_definition(role)
        assert agent_def.role == role
        assert agent_def.name
        assert agent_def.description
        assert agent_def.default_task_type
        assert isinstance(agent_def.tools, list) and agent_def.tools
        assert len(agent_def.instructions) > 50

    def test_all_expected_files_exist_on_disk(self):
        found = {p.stem for p in AGENTS_DIR.glob("*.md")}
        assert set(EXPECTED_ROLES) <= found

    @pytest.mark.parametrize("role", EXPECTED_ROLES)
    def test_default_task_type_is_a_known_task_type(self, role: str):
        from src.llm.router import TaskType

        agent_def = load_agent_definition(role)
        # Raises ValueError if not a real TaskType value.
        TaskType(agent_def.default_task_type)


class TestBuildSystemPrompt:
    def test_appends_schema_instruction_suffix(self):
        agent_def = load_agent_definition("portfolio_manager")
        prompt = build_system_prompt(agent_def)
        assert prompt.startswith(agent_def.instructions)
        assert "only by calling the provided tool" in prompt


class TestMissingOrMalformedDefinitions:
    def test_missing_role_raises(self):
        with pytest.raises(AgentDefinitionError):
            load_agent_definition("does_not_exist")

    def test_missing_frontmatter_raises(self, tmp_path: Path):
        (tmp_path / "broken.md").write_text("# No frontmatter here\n\nJust a body.")
        with pytest.raises(AgentDefinitionError):
            load_agent_definition("broken", agents_dir=tmp_path)

    def test_missing_required_key_raises(self, tmp_path: Path):
        (tmp_path / "broken.md").write_text(
            "---\nname: Broken\ndescription: missing keys\n---\n\nBody text.\n"
        )
        with pytest.raises(AgentDefinitionError):
            load_agent_definition("broken", agents_dir=tmp_path)

    def test_invalid_yaml_frontmatter_raises(self, tmp_path: Path):
        (tmp_path / "broken.md").write_text("---\nname: [unclosed\n---\n\nBody.\n")
        with pytest.raises(AgentDefinitionError):
            load_agent_definition("broken", agents_dir=tmp_path)

    def test_empty_body_raises(self, tmp_path: Path):
        (tmp_path / "broken.md").write_text(
            "---\nname: Broken\ndescription: d\ndefault_task_type: summarization\ntools: [a]\n---\n"
        )
        with pytest.raises(AgentDefinitionError):
            load_agent_definition("broken", agents_dir=tmp_path)
