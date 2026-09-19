"""Structural proof of Step 14's two hard boundaries:

1. "It may NOT modify production rules." `src.llm.strategy_research` has
   no filesystem write of any kind and no import of anything that writes
   config or risk-limit files.
2. "The Research Agent cannot promote a strategy to production."
   `src.llm.strategy_research` never imports or calls
   `src.research.promotion.promote_strategy`, and
   `StrategyResearchReview` (its entire output schema) has no
   `human_approved` field or anything resembling one.

Same source-inspection technique
`test_devils_advocate_independence.py` already established for a
different boundary: gated by there being no such capability to use, not
by convention.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from src.llm import strategy_research
from src.llm.schemas import StrategyResearchReview
from src.research import promotion

STRATEGY_RESEARCH_SOURCE = Path(strategy_research.__file__).read_text(encoding="utf-8")


class TestCannotPromoteAStrategy:
    def test_module_does_not_import_promotion(self):
        pattern = re.compile(r"^\s*(from\s+src\.research\.promotion|import\s+src\.research\.promotion)\b", re.MULTILINE)
        assert pattern.search(STRATEGY_RESEARCH_SOURCE) is None

    def test_module_does_not_call_promote_strategy(self):
        assert "promote_strategy(" not in STRATEGY_RESEARCH_SOURCE

    def test_promotion_module_not_a_live_dependency(self):
        for name, value in vars(strategy_research).items():
            assert "promotion" not in repr(getattr(value, "__module__", ""))

    def test_review_schema_has_no_human_approved_field(self):
        forbidden_field_names = {"human_approved", "approved", "promote", "promoted", "approval"}
        actual_fields = set(StrategyResearchReview.model_fields.keys())
        assert not (forbidden_field_names & actual_fields)

    def test_promote_strategy_signature_has_no_llm_shaped_parameter(self):
        """Confirms the other direction too: `promote_strategy` itself
        takes a `PromotionRequest`, not raw fields an LLM call's output
        could be splatted into directly."""
        sig = inspect.signature(promotion.promote_strategy)
        assert list(sig.parameters) == ["request"]


class TestMayNotModifyProductionRules:
    def test_module_performs_no_filesystem_write(self):
        # no open() call in write/append mode, no Path(...).write_text/write_bytes
        forbidden_patterns = [
            r'open\([^)]*["\']w', r'open\([^)]*["\']a',
            r"\.write_text\(", r"\.write_bytes\(",
            r"yaml\.dump\(", r"yaml\.safe_dump\(",
        ]
        for pattern in forbidden_patterns:
            assert re.search(pattern, STRATEGY_RESEARCH_SOURCE) is None, f"matched forbidden pattern {pattern!r}"

    def test_module_does_not_import_config_writing_utilities(self):
        assert "import yaml" not in STRATEGY_RESEARCH_SOURCE
        assert "risk_limits" not in STRATEGY_RESEARCH_SOURCE

    def test_module_does_not_import_src_risk_limits_or_config_loaders(self):
        pattern = re.compile(r"^\s*(from\s+src\.risk\.limits|import\s+src\.risk\.limits)\b", re.MULTILINE)
        assert pattern.search(STRATEGY_RESEARCH_SOURCE) is None


class TestRecommendationCannotSpellApprovalOrPromotion:
    def test_no_recommendation_value_spells_promote_or_approve(self):
        from src.llm.schemas import ResearchRecommendation

        for value in ResearchRecommendation.__args__:  # type: ignore[attr-defined]
            upper = value.upper()
            assert "PROMOTE" not in upper
            assert "APPROVE" not in upper
            assert "PRODUCTION" not in upper
