.PHONY: run install test backup restore verify-freeze freeze-manifest validate-cycle validate-preflight diagnostic-scan universe-feasibility confirm-candidate

install:
	pip install -r requirements.txt -r requirements-dev.txt

run:
	./scripts/start.sh

test:
	python -m pytest -q

backup:
	./scripts/backup.sh

# Usage: make restore FILE=backups/options_agent_20260921_140000.db
restore:
	./scripts/restore.sh "$(FILE)"

verify-freeze:
	./scripts/verify_freeze.sh

# Regenerates VALIDATION_MANIFEST.json from the current working tree.
# Only run this deliberately, as part of freezing/re-freezing a
# version -- never as a routine step, and never to "fix" a failed
# `make verify-freeze` (that means investigate the drift first).
freeze-manifest:
	python -m src.validation.freeze build

# Runs one daily validation cycle (Step 22.6). Never opens a new
# PaperBroker position; refuses to run at all unless the configured
# market-data provider is Tradier production -- see
# scripts/run_validation_cycle.py's own module docstring.
validate-cycle:
	./scripts/run_validation_cycle.sh

# Read-only readiness check (Step 22.6) -- verifies configuration, cohort
# existence, and Tradier production provider configuration, and exits.
# Mutates NO validation state.
validate-preflight:
	./scripts/run_validation_cycle.sh --preflight

# PAPER_TRADING_V1.5.13: READ-ONLY diagnostic opportunity scan against
# live Tradier production market data, using the exact same candidate
# -> Quant -> Risk pipeline as an official cycle. Does NOT count as a
# validation day and mutates NO validation/candidate/trade/account/
# lifecycle state (zero-persistence by construction -- see
# scripts/run_validation_cycle.py's own run_diagnostic_scan docstring).
diagnostic-scan:
	./scripts/run_validation_cycle.sh --diagnostic-scan

# PAPER_TRADING_V1.5.14: READ-ONLY universe-breadth feasibility study
# against live Tradier production market data, over a wider 12-symbol
# research universe (config/universe_feasibility.yaml) than the
# official active universe (SPY, QQQ). Does NOT count as a validation
# day, does NOT persist any candidate, and does NOT activate the
# expanded universe for the official cycle -- zero-persistence by
# construction, same architecture as `diagnostic-scan` -- see
# scripts/run_validation_cycle.py's own run_universe_feasibility_study
# docstring.
universe-feasibility:
	./scripts/run_validation_cycle.sh --universe-feasibility

# Confirms exactly one Review-Only new-position candidate. The ONLY
# command that may open a real (simulated) PaperBroker position.
# Usage: make confirm-candidate ID=validation-scan-SPY-2026-09-22-csp-2026-10-15-600-1
confirm-candidate:
	./scripts/confirm_candidate.sh "$(ID)"
