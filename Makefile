.PHONY: run install test backup restore verify-freeze freeze-manifest validate-cycle confirm-candidate

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

# Runs one daily validation cycle (Step 22.5). Never opens a new
# PaperBroker position -- see scripts/run_validation_cycle.py's own
# module docstring.
validate-cycle:
	./scripts/run_validation_cycle.sh

# Confirms exactly one Review-Only new-position candidate. The ONLY
# command that may open a real (simulated) PaperBroker position.
# Usage: make confirm-candidate ID=validation-scan-2026-09-22-SPY-...
confirm-candidate:
	./scripts/confirm_candidate.sh "$(ID)"
