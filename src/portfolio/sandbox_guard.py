"""PAPER_TRADING_V1.5.15: the single, shared safety gate every
Expanded-Universe Sandbox entry point (initializer, cycle runner,
confirmation wrapper, status command) calls BEFORE constructing any
store, touching any database file, initializing any schema, making any
provider call, or performing any other mutation -- proves a sandbox-
resolved database path can never alias the official 90-day validation
database.

**Resolution, never string comparison.** A sandbox misconfiguration
that names the official path via a different relative spelling
(`./data/options_agent.db` vs `data/options_agent.db`), a different
working directory, or a symlink must still be caught.
`Path.resolve()` (`strict=False`, the default) canonicalizes as far as
the filesystem allows even when the target file does not yet exist --
exactly the case for a sandbox database that genuinely has never been
created yet, or an official database path being checked before the
official database itself has ever been created in a fresh checkout.

**Fails closed, structurally first.** Every sandbox script in this
release calls `assert_path_is_not_official_database` (or the multi-path
convenience wrapper below) as its very first action after parsing
arguments -- before `load_operations_config`-equivalent sandbox
identity resolution even finishes, and unconditionally before any
`Sqlite*Store` is constructed. This module itself never opens a
database connection, never constructs a store, and never imports
anything from the account-state persistence module, the validation-
store persistence module, or any other persistence module -- it is pure
path arithmetic, so it can
never itself be the thing that accidentally touches the official
database."""
from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

OFFICIAL_DATABASE_PATH = _REPO_ROOT / "data" / "options_agent.db"


class SandboxGuardError(RuntimeError):
    """Raised when a sandbox operation's own database (or other
    protected) path resolves to the official validation database --
    fails closed BEFORE any store is constructed, any schema is
    initialized, any provider call is made, or any mutation of any
    kind occurs. Never caught and silently ignored anywhere in this
    codebase -- every sandbox script that can raise this lets it
    propagate to a plain, visible `FAIL:` message and a non-zero exit
    code."""


def assert_path_is_not_official_database(
    path: Path | str, *, official_database_path: Path | str = OFFICIAL_DATABASE_PATH,
) -> None:
    """Resolves both `path` and `official_database_path` canonically
    (`Path.resolve()`, which works correctly whether or not either
    file currently exists) and raises `SandboxGuardError` if they name
    the same file -- catching not just the literal official relative
    path, but any alias of it (a different spelling, a different
    starting working directory, or a symlink that ultimately points at
    it)."""
    resolved = Path(path).resolve()
    forbidden = Path(official_database_path).resolve()
    if resolved == forbidden:
        raise SandboxGuardError(
            f"refusing to use {str(path)!r} -- it resolves to the OFFICIAL validation database "
            f"({forbidden}). Sandbox operations must never read, write, initialize schema in, or "
            "otherwise touch the official database, under any alias."
        )


def assert_sandbox_paths_are_safe(*paths: Path | str) -> None:
    """Convenience wrapper for a sandbox entry point with more than one
    candidate path to check (e.g. a database path and a separate
    config path) -- checks every one, raising on the first that
    aliases the official database. An empty `paths` is a no-op, never
    an error -- a caller that genuinely has nothing yet to check (e.g.
    before any path has been resolved) is not itself a safety
    violation."""
    for p in paths:
        assert_path_is_not_official_database(p)
