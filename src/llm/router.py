"""Configurable model router — task_type -> tier -> model.

Application code must never hard-code a model name (per the platform's
LLM orchestration spec). Every call site asks the router "what model do I
use for this task_type" and the router answers from config/llm.yaml
(with an optional per-tier environment variable override), never from a
literal string in this module. This file intentionally contains no
Claude model identifiers anywhere in its source.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "llm.yaml"


class TaskType(str, Enum):
    """Task categories the Multi-Agent Layer routes on. Callers pass one
    of these (not an agent name) — the same agent role can use different
    task types, and therefore different tiers, for different calls."""

    # High-reasoning tier by default (config/llm.yaml decides, not this enum).
    PORTFOLIO_MANAGER = "portfolio_manager"
    STRATEGY_ANALYSIS = "strategy_analysis"
    ADVERSARIAL_TRADE_REVIEW = "adversarial_trade_review"
    WEEKLY_PORTFOLIO_REVIEW = "weekly_portfolio_review"
    STRATEGY_RESEARCH = "strategy_research"

    # Routine tier by default.
    SUMMARIZATION = "summarization"
    CANDIDATE_FORMATTING = "candidate_formatting"
    BASIC_CLASSIFICATION = "basic_classification"
    JOURNAL_ANALYSIS = "journal_analysis"


class ModelTier(str, Enum):
    HIGH_REASONING = "high_reasoning"
    ROUTINE = "routine"


@dataclass(frozen=True)
class ModelSpec:
    """A fully resolved model configuration for one call."""

    tier: ModelTier
    model: str
    max_tokens: int
    temperature: float


class RouterConfigError(RuntimeError):
    """Raised when config/llm.yaml is missing, malformed, or incomplete
    for the requested task_type/tier."""


class ModelRouter:
    def __init__(self, config_path: Path | str | None = None) -> None:
        self._config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self._config = self._load_config(self._config_path)

    @staticmethod
    def _load_config(path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise RouterConfigError(f"LLM router config not found: {path}")
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if "tiers" not in data or "task_routing" not in data:
            raise RouterConfigError(f"{path} must define both 'tiers' and 'task_routing'")
        return data

    def tier_for(self, task_type: TaskType | str) -> ModelTier:
        key = task_type.value if isinstance(task_type, TaskType) else task_type
        routing = self._config["task_routing"]
        if key not in routing:
            raise RouterConfigError(
                f"No task_routing entry for task_type={key!r} in {self._config_path}"
            )
        try:
            return ModelTier(routing[key])
        except ValueError as exc:
            raise RouterConfigError(
                f"task_routing[{key!r}] = {routing[key]!r} is not a valid tier"
            ) from exc

    def resolve(self, task_type: TaskType | str) -> ModelSpec:
        """Resolve a task_type to a fully-specified ModelSpec, honoring
        the tier's environment variable override if set."""
        tier = self.tier_for(task_type)
        tier_cfg = self._config["tiers"].get(tier.value)
        if tier_cfg is None:
            raise RouterConfigError(
                f"No tiers entry for tier={tier.value!r} in {self._config_path}"
            )
        for required in ("model", "max_tokens", "temperature"):
            if required not in tier_cfg:
                raise RouterConfigError(
                    f"tiers.{tier.value} in {self._config_path} is missing {required!r}"
                )

        override_env = tier_cfg.get("model_env_override")
        model = (os.environ.get(override_env) if override_env else None) or tier_cfg["model"]

        return ModelSpec(
            tier=tier,
            model=model,
            max_tokens=int(tier_cfg["max_tokens"]),
            temperature=float(tier_cfg["temperature"]),
        )


_default_router: ModelRouter | None = None


def get_default_router() -> ModelRouter:
    """Process-wide default router, lazily constructed so importing this
    module never touches the filesystem by itself."""
    global _default_router
    if _default_router is None:
        _default_router = ModelRouter()
    return _default_router
