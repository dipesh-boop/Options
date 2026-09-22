#!/usr/bin/env python3
"""Operator-run daily validation-cycle runner (Step 22.5, PAPER_TRADING_V1.4.4).

**Never opens a new PaperBroker position.** This script identifies at most
one Risk-Engine-approved new-position candidate per day and persists it as
an immutable, `AWAITING_HUMAN` review record -- it never calls
`PaperBroker.place_order`. A separate, explicit operator command,
`scripts/confirm_candidate.py <candidate-id>`, is the only place in this
codebase that may do that (see `src.review.confirmation`'s module
docstring for the full revalidate-then-fill sequence and why no LLM
review, real or faked, occurs anywhere in this path).

**Never starts a new validation cohort.** This script only ever *reads* the
cohort id named in `config/operations.yaml` and refuses to proceed if that
cohort has not already been started elsewhere
(`src.validation.cohort.has_cohort_started`) -- `src.validation.cohort
.start_new_cohort` is not imported anywhere in this file.

**Never resets the paper account.** `Portfolio`/`PaperAccountState` are
loaded from their durable stores (`src.portfolio.account_state`) if they
already exist; they are bootstrapped from `config/validation.yaml`'s
`starting_capital.default_nav` ONLY the very first time this script runs
against a given account (no prior saved state at all).

Existing positions are still evaluated every cycle through the unmodified
`src.portfolio.orchestrator.run_outer_cycle` (Lifecycle Engine + Risk kill-
switch), exactly as designed -- only new-position execution is gated behind
human confirmation.

Cycle-level idempotent: if today's cycle already completed
(`SqliteControlLoopStore.get_cycle_record`), this script logs and exits 0
without re-running anything.

Usage:
    python scripts/run_validation_cycle.py

Exit codes: 0 = cycle ran (even if degraded, or no candidate found) or was
already done today. 1 = could not start or complete the cycle at all
(config error, cohort not started, database unreachable).
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data.factory import get_configured_market_data_provider  # noqa: E402
from src.data.market_calendar import is_market_open, is_trading_day  # noqa: E402
from src.data.option_chain import OptionChain  # noqa: E402
from src.data.universe import load_universe, load_universe_strategies  # noqa: E402
from src.lifecycle.persistence import SqliteLifecycleStore  # noqa: E402
from src.lifecycle.policies_library import policies_for_strategy  # noqa: E402
from src.llm.schemas import StrategyType  # noqa: E402
from src.brokers.base import SqliteIdempotencyStore  # noqa: E402
from src.brokers.paper import PaperBroker  # noqa: E402
from src.portfolio.account_state import SqlitePaperAccountStateStore, SqlitePortfolioStore  # noqa: E402
from src.portfolio.operations_config import OperationsConfigError, load_operations_config  # noqa: E402
from src.portfolio.orchestrator import OpportunityScanConfig, OuterCycleInputs, run_outer_cycle  # noqa: E402
from src.portfolio.persistence import SqliteControlLoopStore  # noqa: E402
from src.review.candidates import CandidateStatus, ReviewedCandidate, SqliteCandidateReviewStore  # noqa: E402
from src.risk.broker_constraints import load_broker_capabilities  # noqa: E402
from src.risk.engine import evaluate_trade_proposal  # noqa: E402
from src.risk.limits import get_default_limits  # noqa: E402
from src.risk.portfolio_risk import Portfolio, sector_exposure_pct, underlying_exposure_pct  # noqa: E402
from src.strategies.base import StrategyKind  # noqa: E402
from src.validation.cohort import has_cohort_started  # noqa: E402
from src.validation.protocol import ValidationConfigError, load_validation_config  # noqa: E402
from src.validation.records import OpportunityRecord  # noqa: E402
from src.validation.session import DailySnapshot, SqliteValidationStore  # noqa: E402
from src.workflows.candidate_generation import QuantFilterConfig, candidate_eligible_strategies  # noqa: E402


def _line(label: str, value: object) -> None:
    print(f"  {label}: {value}")


def _expire_stale_candidates(review_store, cohort_id: str, validation_store, now: datetime) -> int:
    expired_count = 0
    for candidate in review_store.candidates_awaiting_human(cohort_id=cohort_id):
        if not candidate.is_expired(now):
            continue
        expired = replace(
            candidate, status=CandidateStatus.EXPIRED, resolved_at=now,
            resolution_reason="expired unconfirmed before the next daily cycle swept it",
        )
        review_store.save_candidate(expired)
        validation_store.record_opportunity(
            OpportunityRecord(
                opportunity_id=candidate.candidate_id, cohort_id=candidate.cohort_id,
                ticker=candidate.proposal.ticker, created_at=candidate.created_at, cash_no_trade=False,
                alternatives=(), proposal_id=candidate.proposal.proposal_id,
                quantitative_analysis=candidate.quantitative_analysis, risk_decision=candidate.risk_decision,
                pipeline_status=CandidateStatus.EXPIRED.value,
            )
        )
        expired_count += 1
        _line("expired stale candidate", candidate.candidate_id)
    return expired_count


async def run_validation_cycle() -> bool:
    print("Validation-cycle runner -- Review-Only new-position execution (PAPER_TRADING_V1.4.4)")
    print("This path never calls PaperBroker.place_order for a new position.\n")

    try:
        ops = load_operations_config()
        val_config = load_validation_config()
        universe = load_universe()
        configured_strategy_names = load_universe_strategies()
    except (OperationsConfigError, ValidationConfigError) as exc:
        print(f"FAIL: configuration error -- {exc}")
        return False

    try:
        # config/universe.yaml lists strategy NAMES (e.g. "CASH_SECURED_PUT"),
        # resolved here -- not inside src.data.universe, which must never
        # import src.llm (see src.workflows.candidate_generation
        # .CANDIDATE_GENERATION_ELIGIBLE_STRATEGIES's own docstring).
        configured_strategies = tuple(StrategyType[name] for name in configured_strategy_names)
    except KeyError as exc:
        print(f"FAIL: configuration error -- config/universe.yaml lists an unknown strategy: {exc}")
        return False

    strategies = candidate_eligible_strategies(configured_strategies)
    skipped = [s.value for s in configured_strategies if s not in strategies]
    if skipped:
        _line("strategies configured but not yet candidate-generation-eligible (skipped)", skipped)

    limits = get_default_limits()
    validation_store = SqliteValidationStore(val_config.db_path)

    if not has_cohort_started(validation_store) or validation_store.get_cohort(ops.cohort_id) is None:
        print(
            f"FAIL: cohort {ops.cohort_id!r} has not been started in {val_config.db_path!r} -- refusing to "
            "proceed. This runner NEVER starts a new cohort."
        )
        return False

    control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
    lifecycle_store = SqliteLifecycleStore(ops.lifecycle_db_path)
    review_store = SqliteCandidateReviewStore(ops.candidate_review_db_path)
    account_state_store = SqlitePaperAccountStateStore(ops.account_state_db_path)
    portfolio_store = SqlitePortfolioStore(ops.account_state_db_path)

    now = datetime.now(timezone.utc)
    cycle_id = f"validation-{now.date().isoformat()}"
    _line("cycle_id", cycle_id)

    if control_loop_store.get_cycle_record(cycle_id) is not None:
        print(f"Cycle {cycle_id!r} already ran today -- nothing to do.")
        return True

    _expire_stale_candidates(review_store, ops.cohort_id, validation_store, now)

    portfolio = portfolio_store.get(ops.account_id)
    if portfolio is None:
        _line(
            "bootstrapping Portfolio for a never-before-seen account",
            f"{ops.account_id!r} at starting NAV ${val_config.default_starting_nav:,.2f}",
        )
        portfolio = Portfolio(
            as_of=now, nav=val_config.default_starting_nav, cash=val_config.default_starting_nav,
            peak_equity=val_config.default_starting_nav,
        )
        portfolio_store.save(ops.account_id, portfolio)

    idempotency_store = SqliteIdempotencyStore(ops.account_state_db_path)
    broker = PaperBroker(initial_cash=val_config.default_starting_nav, account_id=ops.account_id, idempotency_store=idempotency_store, now=now)
    account_state = account_state_store.get(ops.account_id)
    if account_state is not None:
        broker.restore_state(account_state)
    else:
        account_state_store.save(broker.export_state())

    broker_capabilities = load_broker_capabilities("internal_paper")

    provider = get_configured_market_data_provider()
    tickers = sorted({p.ticker for p in portfolio.positions} | {e.ticker for e in universe})
    fetch_results: dict[str, OptionChain | Exception] = {}
    try:
        for ticker in tickers:
            try:
                fetch_results[ticker] = await provider.get_option_chain(ticker)
            except Exception as exc:  # noqa: BLE001 - one bad symbol never aborts the cycle
                fetch_results[ticker] = exc
    finally:
        close = getattr(provider, "close", None)
        if close is not None:
            await close()

    chains_by_ticker = {t: c for t, c in fetch_results.items() if isinstance(c, OptionChain)}
    failed = [t for t, c in fetch_results.items() if isinstance(c, Exception)]
    if failed:
        _line("symbols failed this cycle (isolated, cycle continues)", failed)

    provider_name = next((c.source for c in chains_by_ticker.values()), "unknown")
    provider_health_status = "healthy" if not failed else "degraded"

    policy_name_for_position = {
        p.position_id: policies_for_strategy(StrategyKind(p.strategy.value))[0].name for p in portfolio.positions
    }

    inputs = OuterCycleInputs(
        cycle_id=cycle_id, as_of=now, portfolio=portfolio, limits=limits,
        provider=provider_name, provider_health_status=provider_health_status,
        is_trading_day=is_trading_day(now.date()), is_market_open=is_market_open(now),
        fetch_results=fetch_results, lifecycle_store=lifecycle_store, control_loop_store=control_loop_store,
        policy_name_for_position=policy_name_for_position,
        opportunity_scan=OpportunityScanConfig(
            universe=universe, chains_by_ticker=chains_by_ticker, strategies=list(strategies),
            quant_filter=QuantFilterConfig(), market_regime=ops.default_market_regime,
            broker_capabilities=broker_capabilities, proposal_id_prefix=f"validation-scan-{now.date().isoformat()}",
        ),
    )
    result = run_outer_cycle(inputs)

    _line("existing positions evaluated", result.control_result.cycle_record.positions_evaluated)
    _line("lifecycle triggers", result.control_result.cycle_record.lifecycle_triggers)
    _line("degraded_mode", result.control_result.cycle_record.degraded_mode)

    scan = result.opportunity_scan_result
    if scan is None:
        _line("opportunity scan", result.opportunity_scan_skipped_reason)
    elif scan.best is None:
        _line("opportunity scan", f"no new-position candidate -- {scan.no_trade_reason}")
    else:
        best = scan.best
        candidate_id = best.candidate.proposal.proposal_id
        if review_store.get_candidate(candidate_id) is not None:
            _line("candidate already on file, not overwriting", candidate_id)
        else:
            chain = chains_by_ticker[best.candidate.proposal.ticker]
            # scan_and_rank_opportunities only keeps the categorical
            # RiskDecision on ScannedCandidate, not the full RiskDecisionResult
            # (with its ApprovedOrder) -- re-run the exact same, unmodified
            # evaluate_trade_proposal call it already made internally, on the
            # exact same inputs, to recover the full result for the durable
            # review snapshot. Pure re-computation, not a second Risk Engine.
            full_risk = evaluate_trade_proposal(
                best.candidate.proposal, portfolio, best.quantitative_analysis, chain,
                broker_capabilities, limits=limits, now=now,
            )
            policy_name = policies_for_strategy(StrategyKind(best.candidate.proposal.strategy.value))[0].name
            candidate = ReviewedCandidate(
                candidate_id=candidate_id, cohort_id=ops.cohort_id, cycle_id=cycle_id, created_at=now,
                ttl_seconds=ops.confirmation_ttl_seconds, proposal=best.candidate.proposal,
                quantitative_analysis=best.quantitative_analysis, risk_decision=full_risk,
                entry_delta=best.candidate.entry_delta, entry_iv=best.candidate.entry_iv,
                quote_timestamp=chain.timestamp, market_data_source=chain.source,
                portfolio_exposure_before_pct=underlying_exposure_pct(portfolio, best.candidate.proposal.ticker),
                portfolio_exposure_after_pct=best.post_trade_underlying_exposure_pct,
                sector_exposure_before_pct=sector_exposure_pct(portfolio, best.candidate.sector),
                sector_exposure_after_pct=best.post_trade_sector_exposure_pct,
                management_policy_name=policy_name,
            )
            review_store.save_candidate(candidate)
            print(f"\nNEW CANDIDATE AWAITING HUMAN REVIEW: {candidate_id}")
            print(f"  To authorize (PaperBroker simulation only, never live): python scripts/confirm_candidate.py {candidate_id}")

    valuation = result.control_result.valuation
    per_strategy_nav: dict[str, float] = {}
    for p in portfolio.positions:
        per_strategy_nav[p.strategy.value] = per_strategy_nav.get(p.strategy.value, 0.0) + p.capital_at_risk
    snapshot = DailySnapshot(
        snapshot_date=now.date(), nav=valuation.nav, cash=valuation.cash,
        capital_deployed_pct=valuation.capital_deployed_pct, open_position_count=len(portfolio.positions),
        drawdown_pct=valuation.current_drawdown_pct, per_strategy_nav=per_strategy_nav, recorded_at=now,
    )
    validation_store.record_snapshot(snapshot, cohort_id=ops.cohort_id)
    _line("daily snapshot recorded -- nav/cash", f"${valuation.nav:,.2f} / ${valuation.cash:,.2f}")

    portfolio_store.save(ops.account_id, portfolio)
    account_state_store.save(broker.export_state())

    if result.new_alerts:
        _line("new alerts this cycle", len(result.new_alerts))

    print("\nPASS: cycle complete. No new PaperBroker position was opened from this path.")
    return True


def main() -> int:
    try:
        ok = asyncio.run(run_validation_cycle())
    except Exception as exc:  # noqa: BLE001 - a genuinely systemic failure, reported plainly
        print(f"FAIL: validation cycle could not complete -- {exc!r}")
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
