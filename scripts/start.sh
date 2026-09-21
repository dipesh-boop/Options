#!/usr/bin/env bash
# Starts the dashboard (the one thing a non-developer needs running day
# to day). See README.md "How to start it" for the full walkthrough.
#
# Loads .env if present (never commit that file -- see .env.example),
# then starts the FastAPI dashboard with uvicorn. Ctrl-C stops it; see
# README.md "How to stop it."
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

HOST="${OPTIONS_AGENT_DASHBOARD_HOST:-127.0.0.1}"
PORT="${OPTIONS_AGENT_DASHBOARD_PORT:-8000}"

echo "Starting dashboard at http://${HOST}:${PORT} ..."
exec python3 -m uvicorn src.dashboard.app:app --host "${HOST}" --port "${PORT}"
