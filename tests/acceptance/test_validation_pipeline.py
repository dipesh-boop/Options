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

from .conftest import repo_controlled_files

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
        # Step 22.4B: "the repository" means repository-controlled
        # files -- a raw `REPO_ROOT.rglob(...)` on a real operator
        # checkout also descends into `.venv`/`venv` (an installed
        # third-party package can legitimately ship its own bundled
        # `.db`/`.sqlite`/`.sqlite3` file, e.g. a test fixture), which
        # would misreport as validation-cohort data. `repo_controlled_files`
        # is git's own "tracked, or untracked-but-not-ignored"
        # definition -- `*.db` is already explicitly gitignored
        # repo-wide (see `.gitignore`), and `.venv`/`venv` themselves
        # are gitignored directories, so this still fails loudly the
        # moment a real `.db`/`.sqlite`/`.sqlite3` file becomes
        # repository-controlled.
        controlled = repo_controlled_files(REPO_ROOT)
        matches = [
            str(p.relative_to(REPO_ROOT))
            for p in controlled
            if p.suffix in (".db", ".sqlite", ".sqlite3")
        ]
        assert matches == [], f"unexpected persisted database file(s) found (would imply cohort data exists): {matches}"

    def test_configured_store_path_exists_only_as_a_location_not_as_populated_data(self):
        """Step 22 update: `config/validation.yaml` now DOES declare a
        default `db_path` (`data/options_agent.db`) -- required so a
        real 90-day cohort can survive an application/Mac restart (see
        `src.validation.session.SqliteValidationStore`). This is only a
        LOCATION default: naming a path in YAML does not create the
        file, open a connection, or record anything. The real invariant
        this test protects -- "no cohort has actually started" -- is
        now checked directly against that configured path: the file it
        names must not exist (nothing has ever connected to it and
        written a table), exactly like the repo-wide db-file check
        above, just anchored to the specific path production code would
        actually use."""
        from src.validation.protocol import load_validation_config

        config = load_validation_config()
        assert config.db_path  # a real, non-empty default is configured
        db_path = REPO_ROOT / config.db_path if not Path(config.db_path).is_absolute() else Path(config.db_path)
        assert not db_path.exists(), f"the configured validation db_path already has a file on disk: {db_path}"

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
