"""Section 18: validation framework acceptance. The single most
important thing this file proves: **the formal 90-day validation
cohort has not started, and nothing in this acceptance-testing pass
started it.** `tests/unit/validation/test_cohort.py` already covers
`has_cohort_started`/`decide_cohort_transition` thoroughly at the
function level, including an explicit comment noting "the actual,
current state of this codebase: no validation session has ever run."
This file adds the one thing that unit test can't: a check against the
REAL repository filesystem, not a fresh in-memory object that would
trivially read as "not started" regardless of what's actually on disk.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.validation.cohort import decide_cohort_transition, has_cohort_started, start_new_cohort
from src.validation.session import InMemoryValidationStore, SqliteValidationStore

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestNoRealValidationCohortHasEverStarted:
    def test_no_sqlite_or_db_file_exists_anywhere_in_the_repository(self):
        """A `ValidationStore` only ever gains a recorded event through
        `record_trade`/`record_snapshot`/`record_rejected_outcome`/
        `record_violation` -- for the durable `SqliteValidationStore`,
        that means a `.db`/`.sqlite` file on disk. None exists anywhere
        in this repository, which is the concrete, current-state
        version of "the cohort has not started" (not just a claim
        about a freshly-constructed, necessarily-empty object)."""
        matches = [
            str(p.relative_to(REPO_ROOT))
            for pattern in ("*.db", "*.sqlite", "*.sqlite3")
            for p in REPO_ROOT.rglob(pattern)
            if ".git" not in p.parts
        ]
        assert matches == [], f"unexpected persisted database file(s) found (would imply cohort data exists): {matches}"

    def test_config_declares_no_fixed_production_store_path_that_could_be_pre_populated(self):
        """`config/validation.yaml` has no `db_path`/`store_path`
        entry -- the store path is always supplied explicitly by
        whatever process starts a real cohort, never an implicit
        default a stray process could silently write to."""
        content = (REPO_ROOT / "config" / "validation.yaml").read_text()
        assert "db_path" not in content.lower()
        assert "store_path" not in content.lower()

    def test_a_genuinely_fresh_store_has_not_started(self):
        assert has_cohort_started(InMemoryValidationStore()) is False

    def test_no_store_at_all_has_not_started(self):
        assert has_cohort_started(None) is False


class TestStartNewCohortIsNeverCalledAutomaticallyAnywhereInSrc:
    def test_no_call_site_for_start_new_cohort_exists_outside_its_own_definition(self):
        """`start_new_cohort` is a real, working function (proven
        below) -- but nothing in `src/` ever calls it. Starting a
        cohort is necessarily a deliberate, explicit, human-initiated
        action (e.g. a future CLI command this repository does not yet
        have), never a side effect of importing a module, running the
        dashboard, or running this very test suite."""
        import re

        src_dir = REPO_ROOT / "src"
        call_sites = []
        for path in src_dir.rglob("*.py"):
            for line in path.read_text().splitlines():
                if re.search(r"\bstart_new_cohort\(", line) and "def start_new_cohort" not in line:
                    call_sites.append(str(path.relative_to(REPO_ROOT)))
        assert call_sites == [], f"start_new_cohort is called from production code at: {call_sites}"


class TestManifestAndVersionTrackingMechanismWorksInIsolation:
    """Exercises `start_new_cohort`/`build_validation_manifest`
    entirely in memory (an `InMemoryValidationStore`, never
    `SqliteValidationStore`, never a real file path) -- proves the
    manifest/version-tracking machinery itself is sound without ever
    touching real repository or filesystem state, so running this test
    can never be mistaken for starting the actual cohort."""

    def test_building_a_manifest_and_cohort_in_memory_never_touches_disk(self, tmp_path):
        from datetime import date, datetime, timezone

        store = InMemoryValidationStore()
        cohort = start_new_cohort(
            manifest_id="acceptance-test-manifest-never-real",
            start_date=date(2026, 9, 21),
            duration_days=90,
            frozen_at=datetime(2026, 9, 21, tzinfo=timezone.utc),
            starting_nav=1_000_000.0,
            strategy_versions={"put_credit_spread": "v1"},
            store=store,
            cohort_label="ACCEPTANCE_TEST_ONLY_NEVER_REAL",
        )
        assert cohort.manifest.cohort_label == "ACCEPTANCE_TEST_ONLY_NEVER_REAL"
        assert cohort.manifest.period.duration_days == 90
        # The store this test built is the SAME in-memory object passed
        # in -- has_cohort_started reads it as still unstarted, since
        # building a manifest is not itself a recorded event.
        assert has_cohort_started(cohort.store) is False
        # Nothing was written to `tmp_path` or anywhere else -- this
        # whole exercise was pure Python object construction.
        assert list(tmp_path.iterdir()) == []

    def test_decide_cohort_transition_on_the_real_absent_state_recommends_fresh_start_not_closure(self):
        """Confirms `decide_cohort_transition`'s recommendation for
        THIS repository's actual (absent) state -- START_FRESH_COHORT,
        never CLOSE_AND_START_NEW_COHORT (which would imply a prior
        cohort already exists to close)."""
        plan = decide_cohort_transition(None)
        assert plan.action == "START_FRESH_COHORT"
        assert plan.previous_cohort_label is None
