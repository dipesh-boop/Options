"""Step 22 Part 6: backup/restore acceptance. Runs the REAL shell
scripts (not a reimplementation of their logic) against a temporary,
isolated `OPTIONS_AGENT_VALIDATION_DB_PATH`/`cwd`, so a bug in the
actual script a human would run is what this test would catch -- never
a bug in a parallel Python-only backup routine nobody actually uses.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(*args: str, cwd: Path, input_text: str | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, input=input_text, capture_output=True, text=True, env=env)


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """A throwaway copy of just the scripts this test needs, run
    against a throwaway working directory -- never touches the real
    repo's data/ or backups/ directories."""
    sandbox_dir = tmp_path / "sandbox"
    (sandbox_dir / "scripts").mkdir(parents=True)
    shutil.copy(REPO_ROOT / "scripts" / "backup.sh", sandbox_dir / "scripts" / "backup.sh")
    shutil.copy(REPO_ROOT / "scripts" / "restore.sh", sandbox_dir / "scripts" / "restore.sh")
    os.chmod(sandbox_dir / "scripts" / "backup.sh", 0o755)
    os.chmod(sandbox_dir / "scripts" / "restore.sh", 0o755)
    return sandbox_dir


class TestBackupScript:
    def test_backup_with_no_database_yet_exits_cleanly_without_creating_anything(self, sandbox: Path):
        result = _run("./scripts/backup.sh", cwd=sandbox)
        assert result.returncode == 0
        assert "nothing to back up yet" in result.stdout
        assert not (sandbox / "backups").exists()

    def test_backup_creates_a_timestamped_file_containing_the_real_database_bytes(self, sandbox: Path):
        (sandbox / "data").mkdir()
        db_path = sandbox / "data" / "options_agent.db"
        db_path.write_bytes(b"SQLite format 3\x00" + b"fake-but-nonempty-db-content")

        result = _run("./scripts/backup.sh", cwd=sandbox)
        assert result.returncode == 0, result.stderr

        backups = list((sandbox / "backups").glob("options_agent_*.db"))
        assert len(backups) == 1
        assert backups[0].read_bytes() == db_path.read_bytes()
        assert "Backup written" in result.stdout

    def test_two_backups_a_second_apart_get_distinct_timestamped_filenames(self, sandbox: Path):
        import time

        (sandbox / "data").mkdir()
        (sandbox / "data" / "options_agent.db").write_bytes(b"v1")
        _run("./scripts/backup.sh", cwd=sandbox)
        time.sleep(1.1)
        (sandbox / "data" / "options_agent.db").write_bytes(b"v2")
        _run("./scripts/backup.sh", cwd=sandbox)

        backups = sorted((sandbox / "backups").glob("options_agent_*.db"))
        assert len(backups) == 2
        assert backups[0].name != backups[1].name


class TestRestoreScript:
    def test_restore_requires_exactly_one_argument(self, sandbox: Path):
        result = _run("./scripts/restore.sh", cwd=sandbox)
        assert result.returncode != 0
        assert "Usage" in result.stdout

    def test_restore_refuses_a_nonexistent_backup_file(self, sandbox: Path):
        result = _run("./scripts/restore.sh", "backups/does_not_exist.db", cwd=sandbox)
        assert result.returncode != 0
        assert "not found" in result.stdout

    def test_restore_without_typing_yes_changes_nothing(self, sandbox: Path):
        (sandbox / "data").mkdir()
        (sandbox / "data" / "options_agent.db").write_bytes(b"live-data")
        (sandbox / "backups").mkdir()
        (sandbox / "backups" / "old_backup.db").write_bytes(b"backup-data")

        result = _run("./scripts/restore.sh", "backups/old_backup.db", cwd=sandbox, input_text="no\n")
        assert result.returncode != 0
        assert "cancelled" in result.stdout.lower()
        # Never auto-restores -- the live file is untouched.
        assert (sandbox / "data" / "options_agent.db").read_bytes() == b"live-data"

    def test_restore_with_explicit_yes_replaces_the_live_database_and_safety_backs_up_the_old_one(self, sandbox: Path):
        (sandbox / "data").mkdir()
        (sandbox / "data" / "options_agent.db").write_bytes(b"live-data-before-restore")
        (sandbox / "backups").mkdir()
        (sandbox / "backups" / "old_backup.db").write_bytes(b"backup-data-to-restore")

        result = _run("./scripts/restore.sh", "backups/old_backup.db", cwd=sandbox, input_text="YES\n")
        assert result.returncode == 0, result.stderr
        assert (sandbox / "data" / "options_agent.db").read_bytes() == b"backup-data-to-restore"

        # The pre-restore live data was never silently discarded -- a
        # safety backup of it exists.
        safety_backups = list((sandbox / "backups").glob("pre_restore_safety_*.db"))
        assert len(safety_backups) == 1
        assert safety_backups[0].read_bytes() == b"live-data-before-restore"
