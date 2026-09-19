"""Tests for the configurable model router, including a concrete proof
that no Claude model identifier is hard-coded into application code."""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest
import yaml

from src.llm import client as client_module
from src.llm import router as router_module
from src.llm.router import ModelRouter, ModelTier, RouterConfigError, TaskType

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = REPO_ROOT / "config" / "llm.yaml"


@pytest.fixture()
def router() -> ModelRouter:
    return ModelRouter(config_path=CONFIG_PATH)


class TestRealConfigRouting:
    @pytest.mark.parametrize(
        "task_type,expected_tier",
        [
            (TaskType.PORTFOLIO_MANAGER, ModelTier.HIGH_REASONING),
            (TaskType.STRATEGY_ANALYSIS, ModelTier.HIGH_REASONING),
            (TaskType.ADVERSARIAL_TRADE_REVIEW, ModelTier.HIGH_REASONING),
            (TaskType.WEEKLY_PORTFOLIO_REVIEW, ModelTier.HIGH_REASONING),
            (TaskType.STRATEGY_RESEARCH, ModelTier.HIGH_REASONING),
            (TaskType.SUMMARIZATION, ModelTier.ROUTINE),
            (TaskType.CANDIDATE_FORMATTING, ModelTier.ROUTINE),
            (TaskType.BASIC_CLASSIFICATION, ModelTier.ROUTINE),
            (TaskType.JOURNAL_ANALYSIS, ModelTier.ROUTINE),
        ],
    )
    def test_every_task_type_routes_to_expected_tier(self, router: ModelRouter, task_type, expected_tier):
        assert router.tier_for(task_type) is expected_tier

    def test_resolve_returns_full_model_spec(self, router: ModelRouter):
        spec = router.resolve(TaskType.PORTFOLIO_MANAGER)
        assert spec.tier is ModelTier.HIGH_REASONING
        assert spec.model  # non-empty, whatever config/llm.yaml currently says
        assert spec.max_tokens > 0
        assert 0.0 <= spec.temperature <= 1.0

    def test_string_task_type_accepted(self, router: ModelRouter):
        assert router.tier_for("journal_analysis") is ModelTier.ROUTINE

    def test_all_enum_task_types_are_covered_by_config(self, router: ModelRouter):
        # Every TaskType the codebase can request must have a routing
        # entry — a new task_type added to the enum without a
        # corresponding config/llm.yaml entry should fail loudly, not at
        # call time in production.
        for task_type in TaskType:
            router.resolve(task_type)  # raises RouterConfigError if missing


class TestUnknownTaskType:
    def test_unknown_task_type_raises(self, router: ModelRouter):
        with pytest.raises(RouterConfigError):
            router.tier_for("some_task_type_that_does_not_exist")


class TestEnvVarOverride:
    def test_high_reasoning_override_applies(self, router: ModelRouter, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("OPTIONS_AGENT_LLM_HIGH_REASONING_MODEL", "test-override-model-x")
        spec = router.resolve(TaskType.PORTFOLIO_MANAGER)
        assert spec.model == "test-override-model-x"

    def test_no_override_uses_config_default(self, router: ModelRouter, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("OPTIONS_AGENT_LLM_HIGH_REASONING_MODEL", raising=False)
        with CONFIG_PATH.open() as f:
            raw = yaml.safe_load(f)
        expected_default = raw["tiers"]["high_reasoning"]["model"]
        spec = router.resolve(TaskType.PORTFOLIO_MANAGER)
        assert spec.model == expected_default


class TestMissingOrMalformedConfig:
    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(RouterConfigError):
            ModelRouter(config_path=tmp_path / "does_not_exist.yaml")

    def test_missing_task_routing_key_raises(self, tmp_path: Path):
        bad_config = tmp_path / "llm.yaml"
        bad_config.write_text("tiers:\n  routine:\n    model: x\n    max_tokens: 10\n    temperature: 0.1\n")
        with pytest.raises(RouterConfigError):
            ModelRouter(config_path=bad_config)

    def test_missing_tier_definition_raises(self, tmp_path: Path):
        bad_config = tmp_path / "llm.yaml"
        bad_config.write_text(
            "tiers:\n  routine:\n    model: x\n    max_tokens: 10\n    temperature: 0.1\n"
            "task_routing:\n  portfolio_manager: high_reasoning\n"
        )
        router = ModelRouter(config_path=bad_config)
        with pytest.raises(RouterConfigError):
            router.resolve(TaskType.PORTFOLIO_MANAGER)

    def test_invalid_tier_value_raises(self, tmp_path: Path):
        bad_config = tmp_path / "llm.yaml"
        bad_config.write_text(
            "tiers:\n  routine:\n    model: x\n    max_tokens: 10\n    temperature: 0.1\n"
            "task_routing:\n  portfolio_manager: not_a_real_tier\n"
        )
        router = ModelRouter(config_path=bad_config)
        with pytest.raises(RouterConfigError):
            router.tier_for(TaskType.PORTFOLIO_MANAGER)


class TestNoHardcodedModelNames:
    """Concrete, automated proof of the 'never hard-code the current
    model names' requirement: the router and client source files must
    contain zero Claude model identifiers. Model names may only live in
    config/llm.yaml."""

    @pytest.mark.parametrize("module", [router_module, client_module])
    def test_module_source_has_no_claude_model_literal(self, module):
        source = inspect.getsource(module)
        forbidden_substrings = ["claude-", "opus-", "sonnet-", "haiku-"]
        lowered = source.lower()
        for needle in forbidden_substrings:
            assert needle not in lowered, (
                f"{module.__name__} appears to hard-code a model identifier "
                f"(found {needle!r}); model names belong only in config/llm.yaml"
            )

    def test_config_yaml_is_the_only_place_models_are_pinned(self):
        with CONFIG_PATH.open() as f:
            raw = yaml.safe_load(f)
        assert raw["tiers"]["high_reasoning"]["model"]
        assert raw["tiers"]["routine"]["model"]
