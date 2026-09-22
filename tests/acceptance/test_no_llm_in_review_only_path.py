"""Step 22.5 (PAPER_TRADING_V1.4.4) structural negative-capability test:
a direct, executable (AST-based, not just a docstring claim) proof that
the Review-Only new-position execution path -- `scripts
/run_validation_cycle.py`, `scripts/confirm_candidate.py`, and every
module under `src/review/` -- never imports anything from `src.llm`
(real or a faked deterministic stand-in would both be forbidden the same
way, since this checks the import surface, not just "was it a real API
call") and never calls `src.orchestration.pipeline.run_order_pipeline`
(which hard-requires the two LLM stages `src.review.confirmation`'s own
module docstring explains this path deliberately does not fake).

Matches the style of `src.validation.freeze`'s own `_verify_*` functions:
a plain AST/regex scan over the actual files, independent of (and a
second, structural proof alongside) `tests/acceptance
/test_review_only_daily_cycle.py`'s behavioral end-to-end proof that no
LLM stage ever runs on this path.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

TARGET_FILES: tuple[Path, ...] = (
    REPO_ROOT / "scripts" / "run_validation_cycle.py",
    REPO_ROOT / "scripts" / "confirm_candidate.py",
    *sorted((REPO_ROOT / "src" / "review").glob("*.py")),
)

# `src.llm.schemas` is the platform-wide home of `TradeProposal`/`StrategyType`/
# etc -- plain, immutable Pydantic data shapes with no LLM call anywhere in
# them (CLAUDE.md invariant #2 is exactly why they're categorical/read-only
# in the first place: "so a model can't fabricate a plausible-looking
# number"). Every deterministic layer in this codebase already imports from
# it, unconditionally -- `src.risk.engine`, `src.risk.trade_risk`,
# `src.risk.portfolio_risk`, `src.risk.broker_constraints`,
# `src.portfolio.opportunity_scan`, `src.portfolio.orchestrator`,
# `src.workflows.candidate_generation` among them -- so importing it here is
# not a sign of LLM involvement and is explicitly exempted, matching
# `src.validation.freeze._verify_tradier_is_market_data_only`'s own
# per-module exemption idiom. Every OTHER `src.llm.*` module
# (`client`/`router`/`devils_advocate`/`portfolio_manager`/`strategy_research`
# /`prompts`/`audit`/`context`) either calls the real Anthropic API,
# orchestrates an agent role, or builds LLM-specific prompt/context
# objects -- any of those appearing on this path would mean a real or a
# faked LLM review is happening, which this test exists to rule out.
_ALLOWED_LLM_MODULES = {"src.llm.schemas"}


def _imported_module_names(tree: ast.AST) -> set[str]:
    """Every module name this file imports from, via either `import X`
    or `from X import Y` -- including submodule paths like `src.llm.schemas`,
    not just the top-level package."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


def _calls_run_order_pipeline(tree: ast.AST) -> bool:
    """A direct call to `run_order_pipeline(...)` or `<something>
    .run_order_pipeline(...)` anywhere in the file -- not just an
    import of it (which `_imported_module_names`'s own `src.llm`
    check would never catch anyway, since `run_order_pipeline` itself
    lives in `src.orchestration.pipeline`, not `src.llm`)."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "run_order_pipeline":
            return True
        if isinstance(func, ast.Attribute) and func.attr == "run_order_pipeline":
            return True
    return False


class TestReviewOnlyPathNeverTouchesLLM:
    def test_target_files_exist(self):
        """A guard against this test silently checking nothing -- if
        any of these files ever moves, this test must fail loudly, not
        pass vacuously over an empty file list."""
        assert len(TARGET_FILES) >= 3
        for path in TARGET_FILES:
            assert path.is_file(), f"expected file missing: {path}"

    def test_no_target_file_imports_an_llm_orchestration_module(self):
        violations: list[str] = []
        for path in TARGET_FILES:
            tree = ast.parse(path.read_text(), filename=str(path))
            for module_name in _imported_module_names(tree):
                is_llm_module = module_name == "src.llm" or module_name.startswith("src.llm.")
                if is_llm_module and module_name not in _ALLOWED_LLM_MODULES:
                    violations.append(f"{path.relative_to(REPO_ROOT)} imports {module_name!r}")
        assert violations == [], (
            "Review-Only new-position execution must never import an LLM-orchestration module "
            f"(no real LLM call, and no faked deterministic stand-in either): {violations}"
        )

    def test_no_target_file_calls_run_order_pipeline(self):
        violations: list[str] = []
        for path in TARGET_FILES:
            tree = ast.parse(path.read_text(), filename=str(path))
            if _calls_run_order_pipeline(tree):
                violations.append(str(path.relative_to(REPO_ROOT)))
        assert violations == [], (
            "run_order_pipeline hard-requires devils_advocate_stage/portfolio_manager_stage -- "
            f"the Review-Only path must call the unmodified underlying stages directly instead: {violations}"
        )

    def test_review_confirmation_module_docstring_states_the_boundary(self):
        """`src.review.confirmation`'s own module docstring is the
        canonical explanation of *why* -- this test only asserts that
        explanation hasn't silently disappeared, not that the words
        alone prove anything (the two tests above are the actual proof)."""
        from src.review import confirmation

        assert confirmation.__doc__ is not None
        assert "run_order_pipeline" in confirmation.__doc__
        assert "No LLM review of any kind occurs here" in confirmation.__doc__
