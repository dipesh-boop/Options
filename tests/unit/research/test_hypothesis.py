"""Tests for the Hypothesis registry: status transitions and the
counting mechanism `src.research.overfitting_guards` relies on."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.research.hypothesis import (
    DuplicateHypothesisError,
    Hypothesis,
    HypothesisRegistry,
    InvalidStatusTransitionError,
    UnknownHypothesisError,
)

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def _hyp(hypothesis_id: str = "h1", family: str = "pcs:delta") -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hypothesis_id,
        statement="15-20 delta PCS outperform 25-30 delta in high IV",
        dimensions=("delta", "iv_percentile"),
        parameter_family_key=family,
        created_at=NOW,
    )


class TestRegisterAndGet:
    def test_register_then_get_round_trips(self):
        registry = HypothesisRegistry()
        registry.register(_hyp())
        record = registry.get("h1")
        assert record.status == "proposed"
        assert record.hypothesis.hypothesis_id == "h1"

    def test_duplicate_registration_rejected(self):
        registry = HypothesisRegistry()
        registry.register(_hyp())
        with pytest.raises(DuplicateHypothesisError):
            registry.register(_hyp())

    def test_get_unknown_hypothesis_rejected(self):
        registry = HypothesisRegistry()
        with pytest.raises(UnknownHypothesisError):
            registry.get("does-not-exist")


class TestStatusTransitions:
    def test_full_happy_path(self):
        registry = HypothesisRegistry()
        registry.register(_hyp())
        registry.update_status("h1", "backtested", trade_count=40)
        registry.update_status("h1", "validated")
        registry.update_status("h1", "out_of_sample_tested")
        record = registry.update_status("h1", "survived_out_of_sample")
        assert record.status == "survived_out_of_sample"
        assert record.trade_count == 40

    def test_can_reject_from_any_non_terminal_status(self):
        registry = HypothesisRegistry()
        registry.register(_hyp())
        registry.update_status("h1", "backtested")
        record = registry.update_status("h1", "rejected", note="win rate collapsed out of sample")
        assert record.status == "rejected"
        assert record.notes == ["win rate collapsed out of sample"]

    def test_cannot_skip_a_stage(self):
        registry = HypothesisRegistry()
        registry.register(_hyp())
        with pytest.raises(InvalidStatusTransitionError):
            registry.update_status("h1", "validated")  # skipping "backtested"

    def test_cannot_move_backward(self):
        registry = HypothesisRegistry()
        registry.register(_hyp())
        registry.update_status("h1", "backtested")
        registry.update_status("h1", "validated")
        with pytest.raises(InvalidStatusTransitionError):
            registry.update_status("h1", "proposed")

    def test_terminal_statuses_accept_no_further_transitions(self):
        registry = HypothesisRegistry()
        registry.register(_hyp())
        registry.update_status("h1", "rejected")
        with pytest.raises(InvalidStatusTransitionError):
            registry.update_status("h1", "backtested")

    def test_update_status_on_unknown_hypothesis_rejected(self):
        registry = HypothesisRegistry()
        with pytest.raises(UnknownHypothesisError):
            registry.update_status("ghost", "backtested")


class TestCounting:
    def test_tested_count_excludes_still_proposed(self):
        registry = HypothesisRegistry()
        registry.register(_hyp("h1"))
        registry.register(_hyp("h2"))
        registry.update_status("h1", "backtested")
        assert registry.tested_count() == 1  # h2 is still "proposed"

    def test_count_by_status(self):
        registry = HypothesisRegistry()
        registry.register(_hyp("h1"))
        registry.register(_hyp("h2"))
        registry.update_status("h1", "backtested")
        registry.update_status("h1", "rejected")
        registry.update_status("h2", "backtested")
        assert registry.count_by_status("rejected") == 1
        assert registry.count_by_status("backtested") == 1
        assert registry.count_by_status("proposed") == 0

    def test_family_count_groups_by_parameter_family_key(self):
        registry = HypothesisRegistry()
        registry.register(_hyp("h1", family="pcs:delta"))
        registry.register(_hyp("h2", family="pcs:delta"))
        registry.register(_hyp("h3", family="csp:dte"))
        assert registry.family_count("pcs:delta") == 2
        assert registry.family_count("csp:dte") == 1
        assert registry.family_count("unknown:family") == 0

    def test_all_records_returns_every_registered_hypothesis(self):
        registry = HypothesisRegistry()
        registry.register(_hyp("h1"))
        registry.register(_hyp("h2"))
        assert {r.hypothesis.hypothesis_id for r in registry.all_records()} == {"h1", "h2"}
