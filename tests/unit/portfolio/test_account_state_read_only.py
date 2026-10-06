"""PAPER_TRADING_V1.5.13 ACCEPTANCE CORRECTION: `src.portfolio.account_state
.load_portfolio_read_only` -- a loader that is read-only by construction,
not caller discipline, replacing the diagnostic's previous use of
`SqlitePortfolioStore` (whose `__init__` was found, by this release's own
acceptance audit, to be capable of writing a missing database/table into
existence).

Covers items A-F of the acceptance-correction test spec; item G (diagnostic
integration) and item I (official-cycle equivalence) live in
`tests/acceptance/test_diagnostic_scan_v1513.py`, alongside item H (the full
pre-existing V1.5.13 diagnostic suite, unmodified in behavior and still
green)."""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.portfolio.account_state import PortfolioLoadError, SqlitePortfolioStore, load_portfolio_read_only
from src.risk.portfolio_risk import Portfolio

NOW = datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sibling_names(path: Path) -> list[str]:
    return sorted(p.name for p in path.parent.glob(path.name + "*"))


class TestExistingInitializedDb:
    """Item A: a DB with a real row for the account loads correctly and
    leaves the file (and any sibling journal/WAL files) byte-identical."""

    def test_load_returns_the_correct_portfolio_and_leaves_the_db_unchanged(self, tmp_path):
        db_path = tmp_path / "ops.db"
        portfolio = Portfolio(as_of=NOW, nav=123_456.78, cash=100_000.0, peak_equity=123_456.78)
        store = SqlitePortfolioStore(db_path)
        store.save("acct-1", portfolio)
        del store

        before_hash = _sha256(db_path)
        before_siblings = _sibling_names(db_path)

        result = load_portfolio_read_only(db_path, "acct-1")

        after_hash = _sha256(db_path)
        after_siblings = _sibling_names(db_path)

        assert result is not None
        assert result.nav == pytest.approx(123_456.78)
        assert result.cash == pytest.approx(100_000.0)
        assert after_hash == before_hash, "the db file must be byte-identical after a read-only load"
        assert after_siblings == before_siblings, "no sibling journal/WAL file may be left behind"


class TestNonexistentDb:
    """Item B: a database path that does not exist yet -- never created
    by the read-only loader, including its parent directory."""

    def test_missing_db_returns_none_and_creates_nothing(self, tmp_path):
        db_path = tmp_path / "does_not_exist.db"
        assert not db_path.exists()

        result = load_portfolio_read_only(db_path, "acct-1")

        assert result is None
        assert not db_path.exists(), "load_portfolio_read_only must never create the database file"

    def test_missing_parent_directory_returns_none_and_creates_nothing(self, tmp_path):
        db_path = tmp_path / "subdir_that_does_not_exist" / "ops.db"
        assert not db_path.parent.exists()

        result = load_portfolio_read_only(db_path, "acct-1")

        assert result is None
        assert not db_path.parent.exists(), "load_portfolio_read_only must never mkdir a missing parent directory"
        assert not db_path.exists()


class TestMissingTable:
    """Item C: an existing, valid SQLite file that simply has never had
    the account_portfolio table created in it."""

    def test_db_without_the_table_returns_none_and_is_left_byte_identical(self, tmp_path):
        db_path = tmp_path / "ops.db"
        sqlite3.connect(db_path).close()  # a bare, schema-less, valid sqlite file
        before_hash = _sha256(db_path)

        result = load_portfolio_read_only(db_path, "acct-1")

        after_hash = _sha256(db_path)
        with sqlite3.connect(db_path) as conn:
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()

        assert result is None
        assert after_hash == before_hash, "a schema-less db must remain schema-less and byte-identical"
        assert tables == [], "load_portfolio_read_only must never initialize the account_portfolio schema"


class TestMissingAccountRow:
    """Item D: the table exists and has rows, but not for the requested
    account_id -- a clean None, no mutation."""

    def test_table_exists_but_account_has_no_row(self, tmp_path):
        db_path = tmp_path / "ops.db"
        store = SqlitePortfolioStore(db_path)
        store.save("some-other-account", Portfolio(as_of=NOW, nav=50_000.0, cash=50_000.0, peak_equity=50_000.0))
        del store
        before_hash = _sha256(db_path)

        result = load_portfolio_read_only(db_path, "acct-not-present")

        assert result is None
        assert _sha256(db_path) == before_hash


class TestSqliteEnforcedWriteRejection:
    """Item E: a write attempted through the SAME mode=ro mechanism this
    loader uses is rejected by SQLite itself (OperationalError), not
    merely absent from today's source text."""

    def test_an_attempted_write_through_a_mode_ro_connection_is_rejected_by_sqlite(self, tmp_path):
        db_path = tmp_path / "ops.db"
        store = SqlitePortfolioStore(db_path)
        store.save("acct-1", Portfolio(as_of=NOW, nav=100_000.0, cash=100_000.0, peak_equity=100_000.0))
        del store
        before_hash = _sha256(db_path)

        uri = f"{db_path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                conn.execute("INSERT INTO account_portfolio (account_id, portfolio_json) VALUES ('x', '{}')")
                conn.commit()
        finally:
            conn.close()

        assert _sha256(db_path) == before_hash, "a rejected write must leave the file untouched"

    def test_query_only_pragma_is_set_and_does_not_mutate_the_file(self, tmp_path):
        db_path = tmp_path / "ops.db"
        store = SqlitePortfolioStore(db_path)
        store.save("acct-1", Portfolio(as_of=NOW, nav=100_000.0, cash=100_000.0, peak_equity=100_000.0))
        del store
        before_hash = _sha256(db_path)

        uri = f"{db_path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            conn.execute("PRAGMA query_only = ON")
            assert conn.execute("PRAGMA query_only").fetchone() == (1,)
        finally:
            conn.close()

        assert _sha256(db_path) == before_hash, "setting PRAGMA query_only must not mutate the file"


class TestCorruptPortfolioJsonFailsClosed:
    """Item F: a row exists but its JSON is corrupt/invalid -- the loader
    must raise PortfolioLoadError, never silently return None (which
    would be indistinguishable from "no portfolio," understating risk
    instead of refusing to guess)."""

    def test_corrupt_json_raises_portfolio_load_error_not_none(self, tmp_path):
        db_path = tmp_path / "ops.db"
        SqlitePortfolioStore(db_path)  # create the schema
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO account_portfolio (account_id, portfolio_json) VALUES (?, ?)",
                ("acct-1", "{not valid json at all"),
            )

        with pytest.raises(PortfolioLoadError):
            load_portfolio_read_only(db_path, "acct-1")

    def test_json_valid_but_schema_invalid_raises_portfolio_load_error(self, tmp_path):
        db_path = tmp_path / "ops.db"
        SqlitePortfolioStore(db_path)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO account_portfolio (account_id, portfolio_json) VALUES (?, ?)",
                ("acct-1", '{"this": "is valid json but not a Portfolio"}'),
            )

        with pytest.raises(PortfolioLoadError):
            load_portfolio_read_only(db_path, "acct-1")
