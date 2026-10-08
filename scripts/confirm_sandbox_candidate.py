#!/usr/bin/env python3
"""PAPER_TRADING_V1.5.15: sandbox confirmation command for one specific
Expanded-Universe Sandbox candidate.

**Thin wrapper -- reuses `src.review.confirmation.confirm_candidate`
completely unmodified.** This file never reimplements, duplicates, or
weakens the revalidate-then-fill sequence (exact-expiration refresh,
Quant rerun, Risk rerun, price/capital-drift tolerance checks,
PaperBroker fill) -- see that module's own docstring for the full
sequence. The only thing this wrapper adds is sandbox IDENTITY: which
database, which cohort, and an extra, sandbox-specific pre-check layer
(below) before `confirm_candidate` is ever called.

**Hard-selects the sandbox database -- never an operator-supplied path.**
There is no `--db`/`--account`/`--cohort` flag anywhere in this file.
Every store this script constructs is pointed at
`src.portfolio.sandbox_identity.load_sandbox_operations_config()`'s own
paths, and `src.portfolio.sandbox_guard.assert_path_is_not_official_database`
is checked, on every one of them, before any store is constructed --
the very first thing this module does, before any other project import
even runs.

**Sandbox-specific pre-checks, on top of (never instead of)
`confirm_candidate`'s own internal checks.** Before this script calls
`confirm_candidate` at all, it independently verifies the loaded
candidate: exists in the sandbox candidate-review store, belongs to
the sandbox cohort (`ReviewedCandidate.cohort_id ==
SANDBOX_COHORT_ID`), belongs to the sandbox cycle-id namespace
(`ReviewedCandidate.cycle_id` starts with `SANDBOX_CYCLE_ID_PREFIX`),
and is in the canonical `AWAITING_HUMAN` state. Each check prints a
clear, sandbox-specific explanation and exits before `confirm_candidate`
is reached -- a candidate failing any of these can never reach a
PaperBroker call from this script. This is defense in depth, not a
substitute for `confirm_candidate`'s own, unmodified
NOT_FOUND/ALREADY_RESOLVED/EXPIRED handling, which still runs exactly
as it does for the official cohort. Because this script only ever
constructs its `CandidateReviewStore`/`ValidationStore` against the
sandbox database, an official-cohort candidate_id can never even be
loaded here in the first place -- `get_candidate` on the sandbox store
simply returns `None` for an id that only exists in the official
database, so the official-cohort-only check is really the same
database-isolation guarantee the rest of this release depends on,
restated explicitly for this one entry point.

**Explicit candidate id required -- no zero-argument form.** Exactly
one positional argument, exactly like `scripts/confirm_candidate.py`.
There is no "confirm the latest candidate" shortcut: a bare `python
scripts/confirm_sandbox_candidate.py` with no argument prints usage and
exits 1 without loading any configuration, constructing any store, or
touching any sandbox state.

**This is the ONLY command that may call the simulated broker's own
order-placement method for a sandbox candidate.**
`scripts/run_sandbox_cycle.py` never calls it, directly or indirectly --
confirmation always requires a separate, explicit operator invocation
naming the exact candidate id. PaperBroker simulation only: there is no
Tradier order-execution path, no Fidelity ticket path, and no IBKR path
anywhere in this file or in `confirm_candidate` itself.

Usage:
    python scripts/confirm_sandbox_candidate.py <candidate-id>

Exit codes:
    0 = CONFIRMED (a real, simulated PaperBroker fill occurred, in the
        sandbox account only)
    2 = a safety-driven non-fill outcome (EXPIRED, DATA_INSUFFICIENT,
        REPRICE_REQUIRED, REJECTED_BY_RISK, REJECTED_BY_BROKER, NO_FILL)
        -- this is the system working correctly, not a script failure
    3 = ALREADY_RESOLVED, NOT_FOUND, or a sandbox-specific pre-check
        rejection (wrong cohort, wrong cycle namespace, not awaiting
        human) -- nothing to do
    1 = could not run at all (config error, database unreachable, bad
        usage)
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Section 1/27A: the official-database rejection gate -- imported and
# invoked BEFORE any other project import below has a chance to load a
# config file, construct a store, or touch a provider.
from src.portfolio.sandbox_guard import SandboxGuardError, assert_path_is_not_official_database  # noqa: E402
from src.portfolio.sandbox_identity import SANDBOX_DATABASE_PATH  # noqa: E402

try:
    assert_path_is_not_official_database(SANDBOX_DATABASE_PATH)
except SandboxGuardError as exc:
    print(f"FAIL: {exc}")
    raise SystemExit(1)

from src.brokers.base import SqliteIdempotencyStore  # noqa: E402
from src.brokers.paper import PaperBroker  # noqa: E402
from src.data.factory import get_configured_market_data_provider  # noqa: E402
from src.data.historical import HistoricalDataProvider  # noqa: E402
from src.data.universe import load_universe  # noqa: E402
from src.portfolio.account_state import SqlitePaperAccountStateStore, SqlitePortfolioStore  # noqa: E402
from src.portfolio.risk_data import apply_risk_data_wiring  # noqa: E402
from src.portfolio.sandbox_identity import (  # noqa: E402
    SANDBOX_ACCOUNT_ID,
    SANDBOX_COHORT_ID,
    SANDBOX_CYCLE_ID_PREFIX,
    SANDBOX_STARTING_NAV,
    SANDBOX_UNIVERSE_CONFIG_PATH,
    load_sandbox_operations_config,
)
from src.review.candidates import CandidateStatus, SqliteCandidateReviewStore  # noqa: E402
from src.review.confirmation import ConfirmCandidateInputs, ConfirmationOutcome, confirm_candidate  # noqa: E402
from src.risk.broker_constraints import load_broker_capabilities  # noqa: E402
from src.risk.limits import get_default_limits  # noqa: E402
from src.validation.session import SqliteValidationStore  # noqa: E402

_EXIT_CODE = {
    ConfirmationOutcome.CONFIRMED: 0,
    ConfirmationOutcome.ALREADY_RESOLVED: 3,
    ConfirmationOutcome.NOT_FOUND: 3,
}

_BANNER = (
    "=================================================================\n"
    "EXPANDED-UNIVERSE SANDBOX -- CANDIDATE CONFIRMATION\n"
    "NOT OFFICIAL VALIDATION. PAPERBROKER SIMULATION ONLY.\n"
    "================================================================="
)


def _sandbox_precheck_failure(candidate, candidate_id: str) -> str | None:
    """Returns a human-readable rejection reason if this candidate fails
    any sandbox-specific pre-check, or `None` if it passes all of them
    and `confirm_candidate` should be called. Never mutates anything --
    purely a read-only classification of state already loaded from the
    sandbox's own `CandidateReviewStore`."""
    if candidate is None:
        return f"no candidate {candidate_id!r} found in the sandbox candidate-review store"
    if candidate.cohort_id != SANDBOX_COHORT_ID:
        return (
            f"candidate {candidate_id!r} belongs to cohort {candidate.cohort_id!r}, not the sandbox cohort "
            f"{SANDBOX_COHORT_ID!r} -- refusing to confirm an official-cohort (or otherwise foreign) candidate "
            "through the sandbox confirmation path"
        )
    if not candidate.cycle_id.startswith(f"{SANDBOX_CYCLE_ID_PREFIX}-"):
        return (
            f"candidate {candidate_id!r} was created under cycle_id {candidate.cycle_id!r}, which is not in the "
            f"sandbox cycle-id namespace ({SANDBOX_CYCLE_ID_PREFIX!r}-...) -- refusing to confirm"
        )
    if candidate.status != CandidateStatus.AWAITING_HUMAN:
        return (
            f"candidate {candidate_id!r} is already {candidate.status.value!r}, not AWAITING_HUMAN -- nothing to "
            "confirm (it has already been accepted, rejected, or expired)"
        )
    return None


async def _run(candidate_id: str) -> int:
    print(_BANNER)
    print(f"\nConfirming sandbox candidate: {candidate_id}")
    print("This is the only command that may open a real (simulated) PaperBroker position for this sandbox.\n")

    try:
        ops = load_sandbox_operations_config()
    except Exception as exc:  # noqa: BLE001 - a genuinely systemic failure, reported plainly
        print(f"FAIL: could not load sandbox configuration -- {exc!r}")
        return 1

    limits = get_default_limits()
    validation_store = SqliteValidationStore(ops.account_state_db_path)
    review_store = SqliteCandidateReviewStore(ops.candidate_review_db_path)
    account_state_store = SqlitePaperAccountStateStore(ops.account_state_db_path)
    portfolio_store = SqlitePortfolioStore(ops.account_state_db_path)
    idempotency_store = SqliteIdempotencyStore(ops.account_state_db_path)

    # Sandbox-specific pre-check layer -- see this module's own docstring.
    # Loaded from the SANDBOX store only, so an official-cohort candidate
    # id can never even be found here in the first place.
    candidate = review_store.get_candidate(candidate_id)
    rejection = _sandbox_precheck_failure(candidate, candidate_id)
    if rejection is not None:
        print(f"FAIL (sandbox pre-check): {rejection}")
        return 3

    now = datetime.now(timezone.utc)
    broker = PaperBroker(
        initial_cash=SANDBOX_STARTING_NAV, account_id=SANDBOX_ACCOUNT_ID,
        idempotency_store=idempotency_store, now=now,
    )
    account_state = account_state_store.get(SANDBOX_ACCOUNT_ID)
    if account_state is None:
        print(
            f"FAIL: no PaperBroker account state found for {SANDBOX_ACCOUNT_ID!r} -- run "
            "scripts/run_sandbox_cycle.py at least once first."
        )
        return 1
    broker.restore_state(account_state)

    provider = get_configured_market_data_provider()
    historical_provider: HistoricalDataProvider | None = (
        provider if isinstance(provider, HistoricalDataProvider) else None
    )
    try:
        existing_portfolio = portfolio_store.get(SANDBOX_ACCOUNT_ID)
        if existing_portfolio is not None:
            universe = load_universe(SANDBOX_UNIVERSE_CONFIG_PATH)
            wired_portfolio = await apply_risk_data_wiring(
                existing_portfolio, universe=universe, enabled=ops.risk_data_wiring_enabled,
                historical_provider=historical_provider, now=now,
                lookback_days=ops.correlation_lookback_days, min_observations=ops.min_correlation_observations,
            )
            if wired_portfolio is not existing_portfolio:
                portfolio_store.save(SANDBOX_ACCOUNT_ID, wired_portfolio)

        inputs = ConfirmCandidateInputs(
            candidate_id=candidate_id, now=now, market_data_provider=provider, review_store=review_store,
            validation_store=validation_store, portfolio_store=portfolio_store, account_state_store=account_state_store,
            paper_broker=broker, limits=limits, automated_broker_capabilities=load_broker_capabilities("internal_paper"),
            max_price_drift_pct=ops.max_price_drift_pct, max_capital_required_drift_pct=ops.max_capital_required_drift_pct,
        )
        outcome = await confirm_candidate(inputs)
    finally:
        close = getattr(provider, "close", None)
        if close is not None:
            await close()

    resolved = review_store.get_candidate(candidate_id)
    print(f"OUTCOME: {outcome.value}")
    if resolved is not None:
        print(f"  status: {resolved.status.value}")
        print(f"  resolution: {resolved.resolution_reason}")
        if resolved.paper_order_id:
            print(f"  paper_order_id: {resolved.paper_order_id}")

    if outcome == ConfirmationOutcome.CONFIRMED:
        print("\nPASS: PaperBroker simulated fill recorded in the SANDBOX account. Never a real brokerage order.")
    elif outcome in _EXIT_CODE:
        print("\nNo action taken.")
    else:
        print("\nNo fill occurred -- this is a safety-driven outcome, not a script error.")

    return _EXIT_CODE.get(outcome, 2)


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python scripts/confirm_sandbox_candidate.py <candidate-id>")
        print("There is no zero-argument or \"latest candidate\" form -- a candidate id is always required.")
        return 1
    try:
        return asyncio.run(_run(sys.argv[1]))
    except Exception as exc:  # noqa: BLE001 - a genuinely systemic failure, reported plainly
        print(f"FAIL: could not run sandbox confirmation -- {exc!r}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
