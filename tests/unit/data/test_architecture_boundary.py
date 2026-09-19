"""Structural proof that src.data never imports from src.llm. The
Market Data Layer sits strictly beneath the Multi-Agent Layer
(ARCHITECTURE.md §3, §9) — this only holds if the dependency points one
way, the same property tests/unit/quant/test_architecture_boundary.py
verifies for src.quant."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

DATA_DIR = Path(__file__).resolve().parents[3] / "src" / "data"

_IMPORT_RE = re.compile(r"^\s*(from\s+src\.llm|from\s+src\s+import\s+llm|import\s+src\.llm)\b", re.MULTILINE)


@pytest.mark.parametrize("module_path", sorted(DATA_DIR.glob("*.py")))
def test_data_module_does_not_import_llm_layer(module_path: Path):
    source = module_path.read_text(encoding="utf-8")
    match = _IMPORT_RE.search(source)
    assert match is None, f"{module_path.name} imports from src.llm: {match.group(0) if match else ''!r}"
