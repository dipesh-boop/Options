"""Structural proof of the one-way dependency the task requires: 'The
LLM must consume these calculations. The LLM must never replace these
calculations with its own arithmetic.' That only holds if src.quant
never imports from src.llm — otherwise the deterministic engine could
end up depending on (and implicitly trusting) agent-layer types."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

QUANT_DIR = Path(__file__).resolve().parents[3] / "src" / "quant"

# Matches an actual import statement, not prose mentioning "src.llm" in a
# docstring (several modules document this exact boundary in comments).
_IMPORT_RE = re.compile(r"^\s*(from\s+src\.llm|from\s+src\s+import\s+llm|import\s+src\.llm)\b", re.MULTILINE)


@pytest.mark.parametrize("module_path", sorted(QUANT_DIR.glob("*.py")))
def test_quant_module_does_not_import_llm_layer(module_path: Path):
    source = module_path.read_text(encoding="utf-8")
    match = _IMPORT_RE.search(source)
    assert match is None, f"{module_path.name} imports from src.llm: {match.group(0) if match else ''!r}"
