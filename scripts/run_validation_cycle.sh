#!/usr/bin/env bash
# Runs one daily validation cycle (Step 22.5, PAPER_TRADING_V1.4.4). Safe
# to run manually now and to schedule externally (cron/launchd) later --
# see scripts/run_validation_cycle.py's own module docstring for the full
# safety guarantees (never opens a new PaperBroker position, never starts
# a new cohort, never resets the account, cycle-level idempotent).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

exec python3 scripts/run_validation_cycle.py
