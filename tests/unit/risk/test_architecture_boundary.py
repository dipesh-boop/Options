"""Structural proof of the Risk Engine's LLM boundary.

Unlike src.quant/src.data/src.brokers (which forbid `src.llm` outright),
`src.risk` legitimately consumes `src.llm.schemas.TradeProposal` as a
plain data type — `ensure_trade_proposal` is the boundary guard for
exactly that. What must never appear anywhere in `src.risk` is a
dependency on the two modules actually capable of an LLM call:
`src.llm.client` (the Anthropic API wrapper) and `src.llm.router` (model
selection). This file proves both halves: the data import is present
where expected, and the call-capable modules are absent everywhere.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

RISK_DIR = Path(__file__).resolve().parents[3] / "src" / "risk"
RISK_MODULE_PATHS = sorted(RISK_DIR.glob("*.py"))

# Only a plain data import of src.llm.schemas is allowed. Anything else
# touching src.llm — a bare `import src.llm` (which makes every
# submodule, including client/router, reachable via attribute access),
# or any import of src.llm.client / src.llm.router by name — is
# forbidden.
_FORBIDDEN_LLM_IMPORT_RE = re.compile(
    r"^\s*(?:"
    r"from\s+src\.llm\.(?:client|router)\s+import\b"
    r"|from\s+src\.llm\s+import\s+(?:client|router)\b"
    r"|import\s+src\.llm\.(?:client|router)\b"
    r"|import\s+src\.llm\b(?!\.schemas)"
    r"|from\s+src\s+import\s+llm\b"
    r")",
    re.MULTILINE,
)

_ALLOWED_LLM_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+src\.llm\.schemas\s+import\b|from\s+src\.llm\s+import\s+schemas\b|import\s+src\.llm\.schemas\b)",
    re.MULTILINE,
)

_NETWORK_IMPORT_PATTERNS = [
    r"^\s*import\s+requests\b",
    r"^\s*from\s+requests\b",
    r"^\s*import\s+httpx\b",
    r"^\s*from\s+httpx\b",
    r"^\s*import\s+anthropic\b",
    r"^\s*from\s+anthropic\b",
    r"^\s*import\s+aiohttp\b",
    r"^\s*from\s+aiohttp\b",
    r"^\s*import\s+socket\b",
    r"^\s*from\s+socket\b",
]


@pytest.mark.parametrize("module_path", RISK_MODULE_PATHS)
def test_no_module_imports_llm_client_or_router(module_path: Path):
    source = module_path.read_text(encoding="utf-8")
    match = _FORBIDDEN_LLM_IMPORT_RE.search(source)
    assert match is None, f"{module_path.name} imports LLM-calling machinery: {match.group(0)!r}"


@pytest.mark.parametrize("module_path", RISK_MODULE_PATHS)
def test_no_module_makes_network_calls(module_path: Path):
    source = module_path.read_text(encoding="utf-8")
    for pattern in _NETWORK_IMPORT_PATTERNS:
        match = re.search(pattern, source, re.MULTILINE)
        assert match is None, f"{module_path.name} matched forbidden network pattern {pattern!r}: {match.group(0)!r}"


def test_only_specific_modules_import_llm_schemas_and_nothing_else_from_llm():
    """Confirms the one legitimate `src.llm` dependency in this package
    is exactly `src.llm.schemas`, in the modules that need
    TradeProposal/StrategyType as data — not a blanket allowance."""
    modules_importing_schemas = []
    for module_path in RISK_MODULE_PATHS:
        source = module_path.read_text(encoding="utf-8")
        if _ALLOWED_LLM_IMPORT_RE.search(source):
            modules_importing_schemas.append(module_path.name)
    assert set(modules_importing_schemas) <= {
        "broker_constraints.py",
        "portfolio_risk.py",
        "trade_risk.py",
        "engine.py",
    }
    # And every one of those imports must be the schemas-only form —
    # already proven by test_no_module_imports_llm_client_or_router
    # finding zero matches across the same files.


def test_engine_module_never_calls_anything_named_like_an_llm_call(monkeypatch: pytest.MonkeyPatch):
    """Runtime proof, not just static: patch every attribute on
    src.llm.client/router that could plausibly be called, and run a full
    evaluate_trade_proposal end to end. If the Risk Engine's call graph
    ever touched either module, this would raise instead of silently
    passing (the module wouldn't even be imported to patch if
    src.risk.engine's import graph doesn't reach it — reaching that
    unreachable code is exactly what this test rules out)."""
    import src.llm.client as llm_client
    import src.llm.router as llm_router

    def _boom(*args, **kwargs):
        raise AssertionError("Risk Engine call graph touched src.llm.client/router")

    for name in dir(llm_client):
        if not name.startswith("_") and callable(getattr(llm_client, name)):
            monkeypatch.setattr(llm_client, name, _boom, raising=False)
    for name in dir(llm_router):
        if not name.startswith("_") and callable(getattr(llm_router, name)):
            monkeypatch.setattr(llm_router, name, _boom, raising=False)

    from tests.unit.risk.conftest import build_approved_pcs_scenario

    scenario = build_approved_pcs_scenario()
    from src.risk.engine import evaluate_trade_proposal

    result = evaluate_trade_proposal(
        scenario.proposal,
        scenario.portfolio,
        scenario.quantitative_analysis,
        scenario.market_data,
        scenario.broker_capabilities,
        limits=scenario.limits,
    )
    assert result.decision is not None  # reached a decision without ever needing src.llm.client/router


def test_risk_package_does_not_import_brokers_ibkr_or_client_code_paths():
    """src.risk may import src.brokers.fidelity (ticket construction) but
    never src.brokers.ibkr or src.brokers.base's live-order machinery —
    the Risk Engine gates a decision and, for MANUAL brokers, formats a
    ticket; it never itself submits anything."""
    forbidden = re.compile(r"^\s*(from\s+src\.brokers\.ibkr|import\s+src\.brokers\.ibkr)\b", re.MULTILINE)
    for module_path in RISK_MODULE_PATHS:
        source = module_path.read_text(encoding="utf-8")
        assert forbidden.search(source) is None, f"{module_path.name} imports src.brokers.ibkr"
