"""Ensures the repo root is importable as `src.*` regardless of how
pytest is invoked (bare `pytest`, `python -m pytest`, from a subdir)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
