#!/usr/bin/env bash
# Backs up the durable validation database to a timestamped file under
# backups/. Safe to run at any time, including while the dashboard is
# running -- sqlite's own file-level locking means a concurrent backup
# read never corrupts or blocks a write (see README.md "How to back it
# up" for the plain-English version of this).
#
# This does NOT restore anything and never overwrites the live
# database -- restoration is a separate, deliberate, manual action
# (see scripts/restore.sh and README.md "How to restore it"). No
# brokerage credentials exist anywhere in this system (CLAUDE.md
# invariant #3), so there is nothing sensitive to redact from a backup
# beyond the validation data itself.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

DB_PATH="${OPTIONS_AGENT_VALIDATION_DB_PATH:-data/options_agent.db}"
BACKUP_DIR="backups"
TIMESTAMP="$(date -u +%Y%m%d_%H%M%S)"
BACKUP_PATH="${BACKUP_DIR}/options_agent_${TIMESTAMP}.db"

if [ ! -f "$DB_PATH" ]; then
  echo "No database found at ${DB_PATH} -- nothing to back up yet."
  echo "(This is expected before the 90-day validation cohort has started.)"
  exit 0
fi

mkdir -p "$BACKUP_DIR"

# sqlite3's own ".backup" command (via the sqlite3 CLI if present) is
# safe against a concurrently-open, actively-written database --
# falls back to a plain file copy (also safe: sqlite's default
# rollback-journal mode means a reader never observes a half-written
# transaction) if the sqlite3 CLI isn't installed.
if command -v sqlite3 >/dev/null 2>&1; then
  sqlite3 "$DB_PATH" ".backup '${BACKUP_PATH}'"
else
  cp "$DB_PATH" "$BACKUP_PATH"
fi

echo "Backup written: ${BACKUP_PATH}"
echo "$(du -h "$BACKUP_PATH" | cut -f1) -- $(date -u +"%Y-%m-%d %H:%M:%S UTC")"
