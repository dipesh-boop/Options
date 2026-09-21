#!/usr/bin/env bash
# Step 22 Part 24: verifies the PAPER_TRADING_V1.0 freeze is intact --
# VALIDATION_MANIFEST.json exists, every config/prompt/code hash it
# recorded still matches the current working tree (no material drift
# since freeze), the database schema version matches, Fidelity is
# still MANUAL_EXECUTION, live trading is still disabled, and the
# manifest itself still honestly says the 90-day validation has not
# started.
#
# This is read-only: it never edits VALIDATION_MANIFEST.json, never
# re-freezes, and never starts anything. A failed check here means
# "investigate and decide," not "run this again until it passes."
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

python -m src.validation.freeze verify
