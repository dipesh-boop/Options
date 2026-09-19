"""Proof that the Risk Engine never hard-codes one of the policy values
that belongs in config/risk_limits.yaml — mirrors the pattern
tests/unit/llm/test_router.py::TestNoHardcodedModelNames established for
model names. Every one of the ten named portfolio limits from Step 9's
spec is checked for as a literal float in every src.risk module; none
should appear (each module must read it from a `RiskLimitsConfig`
instance instead)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

RISK_DIR = Path(__file__).resolve().parents[3] / "src" / "risk"
RISK_MODULE_PATHS = sorted(p for p in RISK_DIR.glob("*.py") if p.name not in {"limits.py", "__init__.py"})

# The exact policy values named in config/risk_limits.yaml. limits.py
# itself is exempt (it defines the Field bounds, not policy defaults —
# see test_limits.py::test_no_hardcoded_policy_numbers_in_limits_module
# for that module's own, separate proof).
_POLICY_LITERALS = [
    "0.01",  # target_risk_per_trade_pct
    "0.02",  # absolute_max_risk_per_trade_pct
    "0.10",  # max_underlying_exposure_pct / drawdown_risk_reduction_pct
    "0.25",  # max_sector_exposure_pct
    "0.20",  # min_cash_reserve_pct
    "0.60",  # normal_max_capital_deployed_pct
    "0.70",  # absolute_max_capital_deployed_pct
    "0.08",  # drawdown_warning_pct
    "0.15",  # drawdown_halt_pct
]

# Documented false positives: a literal that textually matches a policy
# value but is used for something unrelated to config/risk_limits.yaml.
# Anything added here must justify itself — this is not a general
# escape hatch, just the two known coincidental collisions.
_ALLOWED_COLLISIONS = {
    # engine.py's fallback floor for a displayed dollar price
    # (never let a ticket show $0.00) — a penny, not a risk fraction;
    # coincidentally the same digits as target_risk_per_trade_pct.
    ("engine.py", "0.01"),
    # stress.py's STRESS_VOL_SHOCKS = (0.10, 0.25, 0.50): the exact
    # volatility shock magnitudes named in the platform spec
    # ("+10%/+25%/+50%"), a fixed scenario definition rather than a
    # configurable portfolio limit — see that module's own docstring.
    ("stress.py", "0.10"),
    ("stress.py", "0.25"),
    # STRESS_SPOT_SHOCKS = (-0.20, -0.10, ..., 0.20): the exact
    # underlying shock magnitudes named in the spec ("-20%/.../+20%"),
    # same rationale as the vol-shock collisions above.
    ("stress.py", "0.20"),
}


@pytest.mark.parametrize("module_path", RISK_MODULE_PATHS)
@pytest.mark.parametrize("literal", _POLICY_LITERALS)
def test_no_module_hardcodes_a_policy_limit(module_path: Path, literal: str):
    if (module_path.name, literal) in _ALLOWED_COLLISIONS:
        pytest.skip("documented false positive — see _ALLOWED_COLLISIONS")
    source = module_path.read_text(encoding="utf-8")
    # Word-boundary match: a bare "0.10" as its own token, not part of a
    # longer number like "10.10" or a different literal like "0.105".
    pattern = re.compile(rf"(?<![\d.]){re.escape(literal)}(?![\d])")
    match = pattern.search(source)
    assert match is None, f"{module_path.name} appears to hard-code policy value {literal!r}"
