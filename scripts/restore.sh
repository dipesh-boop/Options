#!/usr/bin/env bash
# Restores the validation database from a backup file created by
# scripts/backup.sh. This is a DELIBERATE, MANUAL action only -- it is
# never run automatically by any other script, by the application on
# startup, or by any scheduled process. You must name the exact backup
# file to restore from, and confirm the overwrite.
#
# Usage:
#   ./scripts/restore.sh backups/options_agent_20260921_140000.db
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

DB_PATH="${OPTIONS_AGENT_VALIDATION_DB_PATH:-data/options_agent.db}"

if [ $# -ne 1 ]; then
  echo "Usage: $0 <path-to-backup-file>"
  echo "Available backups:"
  ls -1t backups/*.db 2>/dev/null || echo "  (none found in backups/)"
  exit 1
fi

BACKUP_FILE="$1"

if [ ! -f "$BACKUP_FILE" ]; then
  echo "Error: backup file not found: ${BACKUP_FILE}"
  exit 1
fi

echo "This will REPLACE the live database at:"
echo "    ${DB_PATH}"
echo "with the contents of:"
echo "    ${BACKUP_FILE}"
echo
if [ -f "$DB_PATH" ]; then
  echo "The CURRENT database will first be safety-backed-up (never discarded silently)."
fi
read -r -p "Type YES (all caps) to continue: " CONFIRM
if [ "$CONFIRM" != "YES" ]; then
  echo "Restore cancelled -- nothing was changed."
  exit 1
fi

mkdir -p data backups
if [ -f "$DB_PATH" ]; then
  SAFETY_BACKUP="backups/pre_restore_safety_$(date -u +%Y%m%d_%H%M%S).db"
  cp "$DB_PATH" "$SAFETY_BACKUP"
  echo "Current database safety-backed-up to: ${SAFETY_BACKUP}"
fi

cp "$BACKUP_FILE" "$DB_PATH"
echo "Restored ${DB_PATH} from ${BACKUP_FILE}."
echo "Restart the application for it to pick up the restored data."
