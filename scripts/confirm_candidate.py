#!/usr/bin/env python3
"""Operator-run confirmation command for one specific Review-Only new-
position candidate (Step 22.5, PAPER_TRADING_V1.4.4).

**This is the ONLY command in this codebase that may call
`PaperBroker.place_order` for a new position during this cohort's
validation.** It takes exactly one argument -- a candidate id, as printed
by `scripts/run_validation_cycle.py` -- and nothing else: there is no flag
that can change strategy, strikes, expiration, quantity, or sizing.
Confirmation is authorization, not trade selection.

Before ever touching `PaperBroker`, this fully revalidates the candidate
against fresh Tradier market data: reloads the exact stored candidate,
refuses an already-resolved or TTL-expired one, refetches and reprices the
exact same trade, reruns the unmodified deterministic Risk Engine against
the CURRENT paper account, checks price/capital drift tolerance, and only
then places the paper order. Any material change blocks the fill and
records why (`src.review.confirmation.confirm_candidate`'s own module
docstring has the full sequence). Running this command twice on the same
candidate id never creates two positions.

**Never Tradier execution. Never Fidelity execution. PaperBroker
simulation only.** No LLM call, real or simulated, occurs anywhere in this
path.

Usage:
    python scripts/confirm_candidate.py <candidate-id>

Exit codes:
    0 = CONFIRMED (a real, simulated PaperBroker fill occurred)
    2 = a safety-driven non-fill outcome (EXPIRED, DATA_INSUFFICIENT,
        REPRICE_REQUIRED, REJECTED_BY_RISK, REJECTED_BY_BROKER, NO_FILL) --
        this is the system working correctly, not a script failure
    3 = ALREADY_RESOLVED or NOT_FOUND -- nothing to do
    1 = could not run at all (config error, database unreachable)
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.brokers.base import SqliteIdempotencyStore  # noqa: E402
from src.brokers.paper import PaperBroker  # noqa: E402
from src.data.factory import get_configured_market_data_provider  # noqa: E402
from src.portfolio.account_state import SqlitePaperAccountStateStore, SqlitePortfolioStore  # noqa: E402
from src.portfolio.operations_config import OperationsConfigError, load_operations_config  # noqa: E402
from src.review.candidates import SqliteCandidateReviewStore  # noqa: E402
from src.review.confirmation import ConfirmationOutcome, ConfirmCandidateInputs, confirm_candidate  # noqa: E402
from src.risk.broker_constraints import load_broker_capabilities  # noqa: E402
from src.risk.limits import get_default_limits  # noqa: E402
from src.validation.protocol import ValidationConfigError, load_validation_config  # noqa: E402
from src.validation.session import SqliteValidationStore  # noqa: E402

_EXIT_CODE = {
    ConfirmationOutcome.CONFIRMED: 0,
    ConfirmationOutcome.ALREADY_RESOLVED: 3,
    ConfirmationOutcome.NOT_FOUND: 3,
}


async def _run(candidate_id: str) -> int:
    print(f"Confirming candidate: {candidate_id}")
    print("This is the only command that may open a real (simulated) PaperBroker position.\n")

    try:
        ops = load_operations_config()
        val_config = load_validation_config()
    except (OperationsConfigError, ValidationConfigError) as exc:
        print(f"FAIL: configuration error -- {exc}")
        return 1

    limits = get_default_limits()
    validation_store = SqliteValidationStore(val_config.db_path)
    review_store = SqliteCandidateReviewStore(ops.candidate_review_db_path)
    account_state_store = SqlitePaperAccountStateStore(ops.account_state_db_path)
    portfolio_store = SqlitePortfolioStore(ops.account_state_db_path)
    idempotency_store = SqliteIdempotencyStore(ops.account_state_db_path)

    now = datetime.now(timezone.utc)
    broker = PaperBroker(initial_cash=val_config.default_starting_nav, account_id=ops.account_id, idempotency_store=idempotency_store, now=now)
    account_state = account_state_store.get(ops.account_id)
    if account_state is None:
        print(f"FAIL: no PaperBroker account state found for {ops.account_id!r} -- run scripts/run_validation_cycle.py at least once first.")
        return 1
    broker.restore_state(account_state)

    provider = get_configured_market_data_provider()
    try:
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

    candidate = review_store.get_candidate(candidate_id)
    print(f"OUTCOME: {outcome.value}")
    if candidate is not None:
        print(f"  status: {candidate.status.value}")
        print(f"  resolution: {candidate.resolution_reason}")
        if candidate.paper_order_id:
            print(f"  paper_order_id: {candidate.paper_order_id}")

    if outcome == ConfirmationOutcome.CONFIRMED:
        print("\nPASS: PaperBroker simulated fill recorded. Never a real brokerage order.")
    elif outcome in _EXIT_CODE:
        print("\nNo action taken.")
    else:
        print("\nNo fill occurred -- this is a safety-driven outcome, not a script error.")

    return _EXIT_CODE.get(outcome, 2)


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python scripts/confirm_candidate.py <candidate-id>")
        return 1
    try:
        return asyncio.run(_run(sys.argv[1]))
    except Exception as exc:  # noqa: BLE001 - a genuinely systemic failure, reported plainly
        print(f"FAIL: could not run confirmation -- {exc!r}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
