#!/usr/bin/env bash
# Options Trading Dashboard -- double-click this file to start.
#
# Step 22.8 (PAPER_TRADING_V1.4.7): the novice-friendly one-click macOS
# launcher. What this does, in order:
#
#   1. Moves to THIS repository's own directory -- wherever it was
#      cloned or copied to on this Mac. Nothing here is a hardcoded,
#      machine-specific path.
#   2. Activates .venv (this repo's own Python virtual environment) if
#      one already exists here. It is never created, and nothing is
#      installed, by this launcher -- see README.md "Getting started"
#      if .venv doesn't exist yet.
#   3. Starts the dashboard via scripts/start.sh, which safely loads
#      .env (Step 22.8 fixed a bug where the shipped .env template's
#      intentionally-blank optional settings could crash startup) and
#      binds to 127.0.0.1 by default -- never 0.0.0.0, never a
#      publicly reachable address, unless you have deliberately
#      overridden OPTIONS_AGENT_DASHBOARD_HOST yourself.
#   4. Opens http://127.0.0.1:8000 (or your configured host/port) in
#      your default browser once the dashboard actually responds.
#
# What this launcher NEVER does:
#   - It never runs the daily validation cycle automatically. Starting
#     the dashboard is not the same as running a cycle -- that stays a
#     separate, explicit action (the dashboard's own "Run validation
#     cycle" button, or `make validate-cycle` in a terminal).
#   - It never confirms a Review-Only candidate. That stays a separate,
#     deliberate `scripts/confirm_candidate.py` command -- never
#     something a launcher or a dashboard click can do.
#   - It never prints your Tradier token, Anthropic API key, or any
#     other secret to this window or to the browser.
#
# To stop the dashboard: click this Terminal window and press Ctrl-C,
# or simply close the window.
set -euo pipefail
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Options Trading Dashboard"
echo "=========================="
echo

if [ -d ".venv" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
else
  echo "No .venv found in $(pwd)."
  echo "See README.md 'Getting started' to create one:"
  echo "    python3 -m venv .venv && source .venv/bin/activate && make install"
  echo "Then double-click this file again."
  read -r -p "Press Enter to close this window..." _
  exit 1
fi

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

HOST="${OPTIONS_AGENT_DASHBOARD_HOST:-127.0.0.1}"
PORT="${OPTIONS_AGENT_DASHBOARD_PORT:-8000}"

# Open the browser once the server responds -- in the background, so
# it never blocks (or races) the server actually starting.
(
  for _ in $(seq 1 60); do
    if curl --silent --output /dev/null --fail "http://${HOST}:${PORT}/" 2>/dev/null; then
      open "http://${HOST}:${PORT}/"
      break
    fi
    sleep 0.5
  done
) &

exec ./scripts/start.sh
