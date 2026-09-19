"""Tests for the deterministic overfitting-protection warnings: multiple-
testing bias, parameter mining, small samples, regime dependence,
survivorship bias."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.research.hypothesis import Hypothesis, HypothesisRegistry
from src.research.overfitting_guards import (
    MAX_FAMILY_VARIATIONS_BEFORE_WARNING,
    MIN_TESTED_FOR_MULTIPLE_TESTING_CHECK,
    MIN_TRADES_FOR_SIGNIFICANCE,
    check_multiple_testing_bias,
    check_parameter_mining,
    check_regime_dependence,
    check_small_sample,
    run_overfitting_guards,
    survivorship_bias_note,
)
from src.research.performance_breakdown import BucketStats

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def _hyp(hypothesis_id: str, family: str) -> Hypothesis:
    return Hypothesis(hypothesis_id=hypothesis_id, statement="s", dimensions=("delta",), parameter_family_key=family, created_at=NOW)


def _bucket(bucket: str, total_pnl: float) -> BucketStats:
    return BucketStats(bucket=bucket, trade_count=10, win_rate=0.6, total_pnl=total_pnl, average_pnl=total_pnl / 10, best_pnl=max(total_pnl, 0), worst_pnl=min(total_pnl, 0))


class TestCheckSmallSample:
    def test_below_threshold_warns(self):
        warning = check_small_sample(10)
        assert warning is not None
        assert "small sample" in warning

    def test_at_or_above_threshold_no_warning(self):
        assert check_small_sample(MIN_TRADES_FOR_SIGNIFICANCE) is None
        assert check_small_sample(MIN_TRADES_FOR_SIGNIFICANCE + 50) is None


class TestCheckRegimeDependence:
    def test_concentrated_profit_warns(self):
        buckets = (_bucket("high_iv", 1000.0), _bucket("low_iv", 50.0))
        warning = check_regime_dependence(buckets)
        assert warning is not None
        assert "high_iv" in warning

    def test_evenly_spread_profit_no_warning(self):
        buckets = (_bucket("high_iv", 500.0), _bucket("low_iv", 500.0))
        assert check_regime_dependence(buckets) is None

    def test_no_profit_at_all_no_warning(self):
        buckets = (_bucket("high_iv", -100.0), _bucket("low_iv", -50.0))
        assert check_regime_dependence(buckets) is None

    def test_empty_buckets_no_warning(self):
        assert check_regime_dependence(()) is None


class TestCheckParameterMining:
    def test_below_threshold_no_warning(self):
        registry = HypothesisRegistry()
        for i in range(MAX_FAMILY_VARIATIONS_BEFORE_WARNING):
            registry.register(_hyp(f"h{i}", "pcs:delta"))
        assert check_parameter_mining(registry, "pcs:delta") is None

    def test_above_threshold_warns(self):
        registry = HypothesisRegistry()
        for i in range(MAX_FAMILY_VARIATIONS_BEFORE_WARNING + 1):
            registry.register(_hyp(f"h{i}", "pcs:delta"))
        warning = check_parameter_mining(registry, "pcs:delta")
        assert warning is not None
        assert "parameter mining" in warning

    def test_different_family_unaffected(self):
        registry = HypothesisRegistry()
        for i in range(MAX_FAMILY_VARIATIONS_BEFORE_WARNING + 1):
            registry.register(_hyp(f"h{i}", "pcs:delta"))
        assert check_parameter_mining(registry, "csp:dte") is None


class TestCheckMultipleTestingBias:
    def test_below_min_tested_no_warning(self):
        registry = HypothesisRegistry()
        for i in range(MIN_TESTED_FOR_MULTIPLE_TESTING_CHECK - 1):
            registry.register(_hyp(f"h{i}", f"fam{i}"))
            registry.update_status(f"h{i}", "backtested")
        assert check_multiple_testing_bias(registry) is None

    def test_many_tested_few_survivors_warns(self):
        registry = HypothesisRegistry()
        for i in range(MIN_TESTED_FOR_MULTIPLE_TESTING_CHECK):
            registry.register(_hyp(f"h{i}", f"fam{i}"))
            registry.update_status(f"h{i}", "backtested")
            registry.update_status(f"h{i}", "rejected")
        warning = check_multiple_testing_bias(registry)
        assert warning is not None
        assert "multiple-testing bias" in warning

    def test_high_survivor_ratio_no_warning(self):
        registry = HypothesisRegistry()
        for i in range(MIN_TESTED_FOR_MULTIPLE_TESTING_CHECK):
            registry.register(_hyp(f"h{i}", f"fam{i}"))
            registry.update_status(f"h{i}", "backtested")
            registry.update_status(f"h{i}", "validated")
            registry.update_status(f"h{i}", "out_of_sample_tested")
            registry.update_status(f"h{i}", "survived_out_of_sample")
        assert check_multiple_testing_bias(registry) is None


class TestSurvivorshipBiasNote:
    def test_always_returns_a_standing_caution(self):
        note = survivorship_bias_note()
        assert "survivorship bias" in note
        assert "ARCHITECTURE.md" in note


class TestRunOverfittingGuards:
    def test_assembles_counts_and_warnings(self):
        registry = HypothesisRegistry()
        registry.register(_hyp("h1", "pcs:delta"))
        registry.update_status("h1", "backtested")
        registry.update_status("h1", "validated")
        registry.update_status("h1", "out_of_sample_tested")
        registry.update_status("h1", "survived_out_of_sample")

        result = run_overfitting_guards(
            registry, hypothesis_id="h1", trade_count=40, regime_buckets=(_bucket("high_iv", 500.0), _bucket("low_iv", 500.0)),
            parameter_family_key="pcs:delta",
        )
        assert result.hypotheses_tested == 1
        assert result.hypotheses_rejected == 0
        assert result.surviving_validation == 1
        assert result.surviving_out_of_sample == 1
        # survivorship-bias note is always present even when no other warning fires
        assert any("survivorship bias" in w for w in result.warnings)
        assert len(result.warnings) == 1  # no small-sample/regime/mining/multiple-testing warning triggered here

    def test_unknown_hypothesis_rejected(self):
        registry = HypothesisRegistry()
        with pytest.raises(Exception):
            run_overfitting_guards(registry, hypothesis_id="ghost", trade_count=40, regime_buckets=(), parameter_family_key="x")

    def test_small_sample_warning_included(self):
        registry = HypothesisRegistry()
        registry.register(_hyp("h1", "pcs:delta"))
        result = run_overfitting_guards(registry, hypothesis_id="h1", trade_count=5, regime_buckets=(), parameter_family_key="pcs:delta")
        assert any("small sample" in w for w in result.warnings)
