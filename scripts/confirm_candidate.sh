#!/usr/bin/env bash
# Confirms exactly one Review-Only new-position candidate (Step 22.5,
# PAPER_TRADING_V1.4.4). See scripts/confirm_candidate.py's own module
# docstring for the full safety guarantees -- this is the ONLY path that
# may open a real (simulated) PaperBroker position.
#
# Usage: scripts/confirm_candidate.sh <candidate-id>
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

exec python3 scripts/confirm_candidate.py "$@"
