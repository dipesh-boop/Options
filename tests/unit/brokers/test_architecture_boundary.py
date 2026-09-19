"""Structural proof that src.brokers never imports from src.llm. The
Broker Abstraction Layer sits strictly beneath the Multi-Agent Layer
(ARCHITECTURE.md §5, §8) — same pattern as src.quant and src.data."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

BROKERS_DIR = Path(__file__).resolve().parents[3] / "src" / "brokers"

_IMPORT_RE = re.compile(r"^\s*(from\s+src\.llm|from\s+src\s+import\s+llm|import\s+src\.llm)\b", re.MULTILINE)


@pytest.mark.parametrize("module_path", sorted(BROKERS_DIR.glob("*.py")))
def test_brokers_module_does_not_import_llm_layer(module_path: Path):
    source = module_path.read_text(encoding="utf-8")
    match = _IMPORT_RE.search(source)
    assert match is None, f"{module_path.name} imports from src.llm: {match.group(0) if match else ''!r}"
