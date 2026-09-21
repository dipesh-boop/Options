"""Tests for src.validation.cohort -- the direct answer to Step 19A's
own questions #11/#12 (has the existing validation cohort started; is a
new cohort required)."""
from __future__ import annotations

from datetime import date, datetime, timezone

from src.validation.cohort import (
    MULTI_STRATEGY_COHORT_LABEL,
    PRE_EXPANSION_COHORT_LABEL,
    decide_cohort_transition,
    has_cohort_started,
    start_new_cohort,
)
from src.validation.session import InMemoryValidationStore

from .conftest import _trade

NOW = datetime(2026, 9, 22, tzinfo=timezone.utc)


class TestHasCohortStarted:
    def test_no_store_has_not_started(self):
        assert has_cohort_started(None) is False

    def test_empty_store_has_not_started(self):
        assert has_cohort_started(InMemoryValidationStore()) is False

    def test_store_with_a_trade_has_started(self):
        store = InMemoryValidationStore()
        store.record_trade(_trade())
        assert has_cohort_started(store) is True

    def test_store_with_only_a_snapshot_has_started(self):
        from src.validation.session import DailySnapshot

        store = InMemoryValidationStore()
        store.record_snapshot(DailySnapshot(
            snapshot_date=date(2026, 1, 5), nav=100_000.0, cash=80_000.0, capital_deployed_pct=0.2,
            open_position_count=1, drawdown_pct=0.0, per_strategy_nav={}, recorded_at=NOW,
        ))
        assert has_cohort_started(store) is True


class TestDecideCohortTransition:
    def test_no_store_at_all_starts_fresh_cohort(self):
        """The actual, current state of this codebase: no validation
        session has ever run. Per Step 19A's own rule, this means
        finish the expansion, freeze a new manifest, and start Day 1 --
        never close a PRE_EXPANSION_VALIDATION cohort that never existed."""
        plan = decide_cohort_transition(None)
        assert plan.action == "START_FRESH_COHORT"
        assert plan.previous_cohort_label is None
        assert plan.new_cohort_label == MULTI_STRATEGY_COHORT_LABEL

    def test_empty_store_with_a_manifest_still_starts_fresh(self):
        """A manifest can be built and frozen before Day 1 without a
        single day of the run actually having happened -- "started" is
        about recorded events, not manifest existence."""
        store = InMemoryValidationStore()
        plan = decide_cohort_transition(store, existing_manifest=None)
        assert plan.action == "START_FRESH_COHORT"

    def test_store_with_real_trades_closes_and_starts_new_cohort(self):
        store = InMemoryValidationStore()
        store.record_trade(_trade())
        plan = decide_cohort_transition(store)
        assert plan.action == "CLOSE_AND_START_NEW_COHORT"
        assert plan.new_cohort_label == MULTI_STRATEGY_COHORT_LABEL

    def test_started_cohort_with_no_manifest_supplied_is_honestly_unknown(self):
        """Without a manifest, this module genuinely cannot know what
        the prior cohort was called -- "unknown" is the honest answer,
        never a guessed label."""
        store = InMemoryValidationStore()
        store.record_trade(_trade())
        plan = decide_cohort_transition(store, existing_manifest=None)
        assert plan.previous_cohort_label == "unknown"

    def test_started_cohort_with_default_labeled_manifest_falls_back_to_pre_expansion_label(self):
        """A manifest frozen via the pre-cohort-labeling code path
        (StrategyVersionManifest's own "default" fallback) is treated as
        the pre-expansion cohort, not literally re-labeled "default"."""
        from datetime import datetime, timezone

        from src.validation.protocol import ValidationPeriod, build_validation_manifest

        store = InMemoryValidationStore()
        store.record_trade(_trade())
        period = ValidationPeriod(start_date=date(2026, 1, 1), end_date=date(2026, 4, 1), duration_days=90)
        manifest = build_validation_manifest(
            manifest_id="run-0", period=period, frozen_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            starting_nav=100_000.0, strategy_versions={}, config_paths=(),
        )  # cohort_label left at its "default" default
        plan = decide_cohort_transition(store, existing_manifest=manifest)
        assert plan.previous_cohort_label == PRE_EXPANSION_COHORT_LABEL

    def test_started_cohort_with_explicit_prior_label_is_preserved(self):
        from src.validation.protocol import ValidationPeriod, build_validation_manifest

        store = InMemoryValidationStore()
        store.record_trade(_trade())
        period = ValidationPeriod(start_date=date(2026, 1, 1), end_date=date(2026, 4, 1), duration_days=90)
        manifest = build_validation_manifest(
            manifest_id="run-1", period=period, frozen_at=NOW, starting_nav=100_000.0,
            strategy_versions={}, config_paths=(), cohort_label="ORIGINAL_3_STRATEGY_COHORT",
        )
        plan = decide_cohort_transition(store, existing_manifest=manifest)
        assert plan.action == "CLOSE_AND_START_NEW_COHORT"
        assert plan.previous_cohort_label == "ORIGINAL_3_STRATEGY_COHORT"

    def test_never_mixes_results_by_reusing_the_same_label(self):
        store = InMemoryValidationStore()
        store.record_trade(_trade())
        plan = decide_cohort_transition(store)
        assert plan.previous_cohort_label != plan.new_cohort_label


class TestStartNewCohort:
    def test_produces_a_frozen_manifest_and_store(self):
        store = InMemoryValidationStore()
        cohort = start_new_cohort(
            manifest_id="mstrat-v1", start_date=date(2026, 9, 22), duration_days=90, frozen_at=NOW,
            starting_nav=100_000.0, strategy_versions={"bull_call_spread": "v1"}, cohort_label=MULTI_STRATEGY_COHORT_LABEL,
            store=store,
        )
        assert cohort.manifest.cohort_label == MULTI_STRATEGY_COHORT_LABEL
        assert cohort.manifest.period.start_date == date(2026, 9, 22)
        assert cohort.store is store
        assert has_cohort_started(cohort.store) is False  # freezing the manifest is not the same as Day 1 having happened
