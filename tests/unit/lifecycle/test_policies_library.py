"""Part 15/16: every named research management policy in the library."""
from __future__ import annotations

import pytest

from src.lifecycle.policies_library import (
    PCS_25PCT_14DTE,
    PCS_DELTA_DEFENSE,
    PCS_HOLD_TO_EXPIRY,
    POLICIES_LIBRARY,
    PUT_CREDIT_SPREAD_STANDARD,
    WHEEL_STANDARD,
    get_policy,
    policies_for_strategy,
)
from src.strategies.base import StrategyKind


class TestLibraryCoverage:
    def test_every_strategy_kind_has_at_least_one_named_policy(self):
        covered = {p.strategy_kind for p in POLICIES_LIBRARY.values()}
        assert covered == set(StrategyKind)

    def test_every_policy_name_starts_with_research_default(self):
        for policy in POLICIES_LIBRARY.values():
            assert policy.description.startswith("RESEARCH DEFAULT")

    def test_at_least_19_named_policies(self):
        assert len(POLICIES_LIBRARY) >= 19


class TestPart1NeverAssumeOnePolicySuperior:
    def test_put_credit_spread_has_multiple_independently_named_policies(self):
        pcs_policies = policies_for_strategy(StrategyKind.PUT_CREDIT_SPREAD)
        names = {p.name for p in pcs_policies}
        assert {"PUT_CREDIT_SPREAD_STANDARD", "PCS_25PCT_14DTE", "PCS_DELTA_DEFENSE", "PCS_HOLD_TO_EXPIRY"} <= names

    def test_the_four_pcs_policies_have_genuinely_different_configurations(self):
        assert PUT_CREDIT_SPREAD_STANDARD.profit_target_pct != PCS_25PCT_14DTE.profit_target_pct
        assert PCS_DELTA_DEFENSE.profit_target_pct is None  # delta-driven, not profit-driven
        assert PCS_HOLD_TO_EXPIRY.management_dte is None  # no mandatory review point at all


class TestWheelIntegration:
    def test_wheel_standard_targets_the_wheel_strategy_kind(self):
        assert WHEEL_STANDARD.strategy_kind == StrategyKind.WHEEL

    def test_wheel_standard_documents_delegation_to_wheel_state_machine(self):
        assert "src.wheel" in WHEEL_STANDARD.description


class TestGetPolicy:
    def test_exact_lookup(self):
        assert get_policy("WHEEL_STANDARD") is WHEEL_STANDARD

    def test_unknown_name_raises_keyerror(self):
        with pytest.raises(KeyError):
            get_policy("NOT_A_REAL_POLICY")


class TestEveryPolicyIsStructurallyValid:
    """Redundant with the module's own import-time `_validate_library()`
    call, but explicit here so a future regression is caught by the
    test suite even if that call is ever accidentally removed."""

    @pytest.mark.parametrize("name", sorted(POLICIES_LIBRARY))
    def test_policy_passes_structural_validation(self, name):
        from src.lifecycle.policy import validate_policy_for_strategy

        validate_policy_for_strategy(POLICIES_LIBRARY[name])
