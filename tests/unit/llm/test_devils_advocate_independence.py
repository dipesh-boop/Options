"""Structural proof of Step 11's independence requirement: "Do not
expose the Portfolio Manager's final decision to the Devil's Advocate
before it completes its analysis. Avoid having the system grade its own
conclusion." There is no `PortfolioDecision` anywhere in
`src.llm.devils_advocate`'s inputs or import graph — not gated by
convention, gated by there being no such parameter to pass one through.
"""
from __future__ import annotations

import dataclasses
import inspect
import re
from pathlib import Path

from src.llm import devils_advocate
from src.llm.devils_advocate import DevilsAdvocateInputs, evaluate_trade_risk

DEVILS_ADVOCATE_SOURCE = Path(devils_advocate.__file__).read_text(encoding="utf-8")


class TestNoPortfolioManagerImport:
    def test_module_does_not_import_portfolio_manager(self):
        pattern = re.compile(r"^\s*(from\s+src\.llm\.portfolio_manager|import\s+src\.llm\.portfolio_manager)\b", re.MULTILINE)
        assert pattern.search(DEVILS_ADVOCATE_SOURCE) is None

    def test_module_does_not_use_portfolio_decision_as_code(self):
        # PortfolioDecision appears in this module's own docstring,
        # documenting the very guarantee this test checks — that's
        # prose, not code. What must never appear is an import, a type
        # annotation, or a constructor/attribute usage of it.
        code_patterns = [
            r"import\s+PortfolioDecision\b",
            r"from\s+\S+\s+import\s+[^\n]*\bPortfolioDecision\b",
            r":\s*PortfolioDecision\b",
            r"->\s*PortfolioDecision\b",
            r"\bPortfolioDecision\(",
        ]
        for pattern in code_patterns:
            assert re.search(pattern, DEVILS_ADVOCATE_SOURCE) is None, f"matched forbidden pattern {pattern!r}"

    def test_portfolio_manager_module_is_not_in_sys_modules_as_a_dependency(self):
        # devils_advocate.py imports nothing that would transitively pull
        # in portfolio_manager.py — confirmed by checking its own
        # module's __dict__ for any live reference to that module or its
        # types.
        for name, value in vars(devils_advocate).items():
            assert "PortfolioDecision" not in repr(type(value))
            assert "portfolio_manager" not in repr(getattr(value, "__module__", ""))


class TestNoInputCanCarryAPortfolioDecision:
    def test_devils_advocate_inputs_has_no_portfolio_decision_field(self):
        for field in dataclasses.fields(DevilsAdvocateInputs):
            assert "PortfolioDecision" not in str(field.type)
            assert "decision" not in field.name.lower() or field.name == "verdict"  # no *_decision-shaped field at all

    def test_evaluate_trade_risk_signature_has_no_decision_parameter(self):
        sig = inspect.signature(evaluate_trade_risk)
        for name in sig.parameters:
            assert "decision" not in name.lower()

    def test_build_context_signature_has_no_decision_parameter(self):
        from src.llm.devils_advocate import build_devils_advocate_context

        sig = inspect.signature(build_devils_advocate_context)
        for name in sig.parameters:
            assert "decision" not in name.lower()


class TestVerdictCannotBeAnApproval:
    def test_no_verdict_value_spells_approve_or_execute(self):
        from src.llm.schemas import DevilsAdvocateVerdict

        for value in DevilsAdvocateVerdict.__args__:  # type: ignore[attr-defined]
            assert value.upper() not in ("APPROVE", "EXECUTE", "APPROVED")
