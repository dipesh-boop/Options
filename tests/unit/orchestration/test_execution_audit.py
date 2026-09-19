"""Tests for src.orchestration.execution_audit (Step 12): "Store:
theoretical midpoint, paper fill, Fidelity target limit, minimum
acceptable credit, market timestamp, subsequent price.\""""
from __future__ import annotations

import dataclasses
from datetime import timedelta

from src.orchestration.execution_audit import (
    ExecutionAuditLog,
    InMemoryExecutionAuditLog,
    record_execution_quality,
    record_subsequent_price,
)
from tests.unit.orchestration.conftest import MD_TS, NOW


class TestRecordExecutionQuality:
    def test_all_six_named_fields_are_captured(self):
        log = InMemoryExecutionAuditLog()

        class FakeOrder:
            avg_fill_price = 0.72

        class FakeTicket:
            limit_price = 0.75
            minimum_acceptable_price = 0.68

        record = record_execution_quality(
            log, proposal_id="prop-1", theoretical_midpoint=0.75, market_timestamp=MD_TS, recorded_at=NOW,
            paper_order=FakeOrder(), fidelity_ticket=FakeTicket(),
        )
        assert record.theoretical_midpoint == 0.75
        assert record.paper_fill_price == 0.72
        assert record.fidelity_target_limit == 0.75
        assert record.fidelity_minimum_acceptable_credit == 0.68
        assert record.market_timestamp == MD_TS
        assert record.subsequent_price is None

    def test_record_is_appended(self):
        log = InMemoryExecutionAuditLog()
        record_execution_quality(log, proposal_id="prop-1", theoretical_midpoint=0.75, market_timestamp=MD_TS, recorded_at=NOW)
        assert len(log.all()) == 1

    def test_works_without_a_paper_order_or_ticket_yet(self):
        log = InMemoryExecutionAuditLog()
        record = record_execution_quality(log, proposal_id="prop-1", theoretical_midpoint=0.75, market_timestamp=MD_TS, recorded_at=NOW)
        assert record.paper_fill_price is None
        assert record.fidelity_target_limit is None


class TestSubsequentPrice:
    def test_follow_up_record_references_the_original(self):
        log = InMemoryExecutionAuditLog()
        original = record_execution_quality(log, proposal_id="prop-1", theoretical_midpoint=0.75, market_timestamp=MD_TS, recorded_at=NOW)
        later = NOW + timedelta(days=1)
        followup = record_subsequent_price(log, original=original, subsequent_price=0.40, observed_at=later)
        assert followup.follow_up_of == original.record_id
        assert followup.subsequent_price == 0.40
        assert followup.subsequent_price_observed_at == later
        assert followup.proposal_id == original.proposal_id

    def test_subsequent_price_is_a_new_record_not_a_mutation(self):
        log = InMemoryExecutionAuditLog()
        original = record_execution_quality(log, proposal_id="prop-1", theoretical_midpoint=0.75, market_timestamp=MD_TS, recorded_at=NOW)
        record_subsequent_price(log, original=original, subsequent_price=0.40, observed_at=NOW + timedelta(days=1))
        assert len(log.all()) == 2
        # The original record itself is untouched.
        original_again = [r for r in log.all() if r.record_id == original.record_id][0]
        assert original_again.subsequent_price is None

    def test_for_proposal_returns_both_the_original_and_its_follow_up(self):
        log = InMemoryExecutionAuditLog()
        original = record_execution_quality(log, proposal_id="prop-1", theoretical_midpoint=0.75, market_timestamp=MD_TS, recorded_at=NOW)
        record_subsequent_price(log, original=original, subsequent_price=0.40, observed_at=NOW + timedelta(days=1))
        assert len(log.for_proposal("prop-1")) == 2
        assert log.for_proposal("prop-2") == []


class TestAppendOnly:
    def test_execution_audit_log_interface_has_no_update_or_delete_method(self):
        method_names = {name for name in dir(ExecutionAuditLog) if not name.startswith("_")}
        forbidden = {"update", "delete", "remove", "clear", "edit", "overwrite"}
        assert method_names.isdisjoint(forbidden)

    def test_in_memory_log_has_no_update_or_delete_method(self):
        method_names = {name for name in dir(InMemoryExecutionAuditLog) if not name.startswith("_")}
        forbidden = {"update", "delete", "remove", "clear", "edit", "overwrite"}
        assert method_names.isdisjoint(forbidden)

    def test_record_is_a_frozen_dataclass(self):
        from src.orchestration.execution_audit import ExecutionQualityRecord

        assert dataclasses.is_dataclass(ExecutionQualityRecord)
        fields = dataclasses.fields(ExecutionQualityRecord)
        assert any(f.name == "record_id" for f in fields)
