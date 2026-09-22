"""Step 22 Part 6: backup/restore acceptance. Runs the REAL shell
scripts (not a reimplementation of their logic) against a temporary,
isolated `OPTIONS_AGENT_VALIDATION_DB_PATH`/`cwd`, so a bug in the
actual script a human would run is what this test would catch -- never
a bug in a parallel Python-only backup routine nobody actually uses.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run(*args: str, cwd: Path, input_text: str | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, input=input_text, capture_output=True, text=True, env=env)


def _write_minimal_sqlite_db(path: Path, content: str) -> None:
    """Creates a genuinely valid, minimal SQLite database at `path` via
    the stdlib `sqlite3` module -- Step 22.4B: `backup.sh` correctly
    uses the real `sqlite3` CLI's own `.backup` command when it's
    installed (the common case on a real operator machine, e.g. macOS),
    which opens and validates the *actual* SQLite file structure, not
    just a "SQLite format 3" magic-string prefix. A fixture that writes
    that magic string followed by arbitrary bytes is correctly rejected
    by `.backup` as "file is not a database" -- that was always a test
    fixture defect, never something `backup.sh`'s own integrity
    validation should be weakened to accept."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE validation_marker (id INTEGER PRIMARY KEY, content TEXT NOT NULL)")
        conn.execute("INSERT INTO validation_marker (content) VALUES (?)", (content,))
        conn.commit()
    finally:
        conn.close()


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
        _write_minimal_sqlite_db(db_path, "real-database-content")

        result = _run("./scripts/backup.sh", cwd=sandbox)
        assert result.returncode == 0, result.stderr

        backups = list((sandbox / "backups").glob("options_agent_*.db"))
        assert len(backups) == 1
        assert backups[0].read_bytes() == db_path.read_bytes()
        assert "Backup written" in result.stdout

        # The backed-up database is itself a valid, openable SQLite
        # database, and the intended content survived the backup.
        conn = sqlite3.connect(str(backups[0]))
        try:
            row = conn.execute("SELECT content FROM validation_marker").fetchone()
        finally:
            conn.close()
        assert row == ("real-database-content",)

    def test_two_backups_a_second_apart_get_distinct_timestamped_filenames(self, sandbox: Path):
        import time

        (sandbox / "data").mkdir()
        db_path = sandbox / "data" / "options_agent.db"
        _write_minimal_sqlite_db(db_path, "v1")
        result1 = _run("./scripts/backup.sh", cwd=sandbox)
        assert result1.returncode == 0, result1.stderr
        time.sleep(1.1)
        db_path.unlink()
        _write_minimal_sqlite_db(db_path, "v2")
        result2 = _run("./scripts/backup.sh", cwd=sandbox)
        assert result2.returncode == 0, result2.stderr

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
