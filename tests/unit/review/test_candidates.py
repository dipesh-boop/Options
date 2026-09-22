"""Tests for src.review.candidates: the durable review-candidate store,
its append-only confirmation-attempt audit trail, and the human-
confirmation funnel-counts aggregation."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from src.review.candidates import (
    CandidateStatus,
    ConfirmationAttemptRecord,
    InMemoryCandidateReviewStore,
    SqliteCandidateReviewStore,
    funnel_counts,
)
from tests.unit.review.conftest import NOW, make_candidate


class TestCandidateStore:
    @pytest.fixture(params=["memory", "sqlite"])
    def store(self, request, tmp_path):
        if request.param == "memory":
            return InMemoryCandidateReviewStore()
        return SqliteCandidateReviewStore(tmp_path / "review.db")

    def test_save_and_get_round_trip(self, store):
        candidate = make_candidate()
        store.save_candidate(candidate)
        loaded = store.get_candidate("cand-1")
        assert loaded is not None
        assert loaded.proposal.ticker == "SPY"
        assert loaded.risk_decision.decision == candidate.risk_decision.decision
        assert loaded.status == CandidateStatus.AWAITING_HUMAN

    def test_get_returns_none_for_unknown_candidate(self, store):
        assert store.get_candidate("does-not-exist") is None

    def test_save_is_replace_on_save_never_two_rows(self, store):
        candidate = make_candidate()
        store.save_candidate(candidate)
        confirmed = replace(candidate, status=CandidateStatus.CONFIRMED, resolved_at=NOW, resolution_reason="filled")
        store.save_candidate(confirmed)
        assert store.get_candidate("cand-1").status == CandidateStatus.CONFIRMED
        assert len(store.all_candidates()) == 1

    def test_candidates_awaiting_human_excludes_resolved(self, store):
        awaiting = make_candidate(candidate_id="cand-awaiting")
        resolved = replace(make_candidate(candidate_id="cand-resolved"), status=CandidateStatus.REJECTED)
        store.save_candidate(awaiting)
        store.save_candidate(resolved)
        ids = {c.candidate_id for c in store.candidates_awaiting_human()}
        assert ids == {"cand-awaiting"}

    def test_candidates_awaiting_human_filters_by_cohort(self, store):
        store.save_candidate(make_candidate(candidate_id="c1", cohort_id="cohort-a"))
        store.save_candidate(make_candidate(candidate_id="c2", cohort_id="cohort-b"))
        ids = {c.candidate_id for c in store.candidates_awaiting_human(cohort_id="cohort-a")}
        assert ids == {"c1"}

    def test_candidates_for_cycle(self, store):
        store.save_candidate(make_candidate(candidate_id="c1", cycle_id="cycle-a"))
        store.save_candidate(make_candidate(candidate_id="c2", cycle_id="cycle-b"))
        ids = {c.candidate_id for c in store.candidates_for_cycle("cycle-a")}
        assert ids == {"c1"}

    def test_all_candidates_filters_by_cohort(self, store):
        store.save_candidate(make_candidate(candidate_id="c1", cohort_id="cohort-a"))
        store.save_candidate(make_candidate(candidate_id="c2", cohort_id="cohort-b"))
        assert len(store.all_candidates(cohort_id="cohort-a")) == 1
        assert len(store.all_candidates()) == 2

    def test_confirmation_attempts_are_append_only_and_ordered(self, store):
        store.save_candidate(make_candidate())
        for i in range(3):
            store.record_confirmation_attempt(
                ConfirmationAttemptRecord(
                    attempt_id=f"a{i}", candidate_id="cand-1", attempted_at=NOW + timedelta(seconds=i),
                    outcome="already_resolved", detail=f"attempt {i}", original_quoted_economics={},
                )
            )
        attempts = store.confirmation_attempts_for_candidate("cand-1")
        assert [a.attempt_id for a in attempts] == ["a0", "a1", "a2"]

    def test_sqlite_store_survives_a_fresh_instance_against_the_same_file(self, tmp_path):
        db_path = tmp_path / "review.db"
        SqliteCandidateReviewStore(db_path).save_candidate(make_candidate())
        reloaded = SqliteCandidateReviewStore(db_path).get_candidate("cand-1")
        assert reloaded is not None
        assert reloaded.proposal.ticker == "SPY"


class TestReviewedCandidateHelpers:
    def test_is_expired_true_after_ttl_elapses(self):
        candidate = make_candidate(ttl_seconds=60)
        assert not candidate.is_expired(NOW)
        assert candidate.is_expired(NOW + timedelta(seconds=61))

    def test_is_terminal_only_for_resolved_statuses(self):
        awaiting = make_candidate()
        assert not awaiting.is_terminal
        for status in (CandidateStatus.CONFIRMED, CandidateStatus.REJECTED, CandidateStatus.EXPIRED, CandidateStatus.REPRICE_REQUIRED, CandidateStatus.DATA_INSUFFICIENT, CandidateStatus.NO_FILL):
            assert replace(awaiting, status=status).is_terminal


class TestFunnelCounts:
    def test_counts_by_status_and_resolution_timing(self):
        store = InMemoryCandidateReviewStore()
        confirmed = replace(make_candidate(candidate_id="c1"), status=CandidateStatus.CONFIRMED, resolved_at=NOW + timedelta(seconds=30))
        rejected = replace(make_candidate(candidate_id="c2"), status=CandidateStatus.REJECTED, resolved_at=NOW + timedelta(seconds=90))
        awaiting = make_candidate(candidate_id="c3")
        for c in (confirmed, rejected, awaiting):
            store.save_candidate(c)

        counts = funnel_counts(store, cohort_id="cohort-1")
        assert counts.presented == 3
        assert counts.confirmed == 1
        assert counts.rejected == 1
        assert counts.expired == 0
        assert counts.median_seconds_to_resolution == 60.0
        assert counts.mean_seconds_to_resolution == 60.0

    def test_no_resolved_candidates_gives_none_timings(self):
        store = InMemoryCandidateReviewStore()
        store.save_candidate(make_candidate())
        counts = funnel_counts(store, cohort_id="cohort-1")
        assert counts.median_seconds_to_resolution is None
        assert counts.mean_seconds_to_resolution is None
