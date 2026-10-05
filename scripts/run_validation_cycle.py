#!/usr/bin/env python3
"""Operator-run daily validation-cycle runner (Step 22.6; operator
usability/startup fixes in Step 22.8; dashboard UI completion in
Step 22.9; market-hours safety gate in Step 2, PAPER_TRADING_V1.5.1;
lifecycle/market-hours separation in PAPER_TRADING_V1.5.9).

**PAPER_TRADING_V1.5.9: the new-position market-hours gate must never
suppress existing-position Lifecycle Engine/Risk kill-switch monitoring.**
Before this step, a closed scan window made this function `return False`
immediately -- before `run_outer_cycle` (the only caller of
`run_control_cycle`) was ever reached, for ANY reason the gate was
closed, even on a day with open positions genuinely needing evaluation.
Fixed by separating two questions this function now asks independently:
"may a new-position scan start right now" (unchanged: still
`evaluate_validation_cycle_eligibility`, still enforced before any
market-data provider call or candidate persistence) and "do existing
positions need lifecycle/risk evaluation regardless" (new: whenever the
gate is closed AND `Portfolio.positions` is non-empty, this function
runs a lifecycle-ONLY pass through the unmodified `run_outer_cycle`,
with `OpportunityScanConfig` omitted and `skip_opportunity_scan=True` so
no scan, no candidate, and no `PaperBroker.place_order` call can occur).
This lifecycle-only pass uses its OWN cycle id
(`f"validation-{date}-lifecycle"`), distinct from the scan-eligible
cycle's own `f"validation-{date}"` id -- `run_control_cycle` unconditionally
consumes a per-cycle-id idempotency slot the moment it runs, so sharing
one id between the two would let an early, gate-closed lifecycle check
silently consume the day's REAL scan-eligible slot, permanently blocking
that day's actual new-position opportunity once the window opened. The
zero-position, gate-closed case is UNCHANGED from pre-V1.5.9: still
returns `False`, still touches no provider, persists no record -- see
`_run_lifecycle_only_safety_check`'s own docstring for the full
reasoning and `STEP_23_9_FREEZE_REPORT.md` for the complete trace.

**Explicit CLI, parsed before anything else runs.** `main()`'s very first
statement is `parser.parse_args()`. `--help`/`-h` prints usage and exits 0;
an unrecognized argument exits 2 (`argparse`'s own default behavior) --
either way, `sys.exit()` fires from inside `parse_args()` itself, before
any config is loaded, any store is constructed, any provider is touched,
or any validation state is read or written. With no arguments, runs one
official, state-mutating validation cycle. `--preflight` runs only the
read-only readiness checks below and mutates nothing (see `run_preflight`).

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

**Official validation requires Tradier production market data --
verified BEFORE any mutation.** `run_validation_cycle`'s very first
non-read-only step, after config/cohort verification, is
`src.data.factory.verify_official_provider_is_tradier_production` (Step
22.6): if `OPTIONS_AGENT_DATA_PROVIDER` does not resolve to `tradier`, or
no Tradier token is configured, or the configured base URL is not
Tradier's own production host (never the sandbox host), this function
raises before `_expire_stale_candidates`, before any Portfolio/
PaperAccountState bootstrap, before the market-data provider is even
constructed, and before any cycle record or daily snapshot is written.
This is a CONFIGURATION preflight, never a network call -- a Tradier API
outage discovered during the real fetch that follows (once this check
has already passed) is legitimately recorded as a degraded cycle by the
existing architecture (Part 7's isolation doctrine); this check exists
so a provider that was never going to be Tradier in the first place
(`mock`, `alpaca`, `ibkr`, Tradier's own sandbox host) can never reach
that fetch, or touch official validation state, at all.

Existing positions are evaluated through the unmodified
`src.portfolio.orchestrator.run_outer_cycle` (Lifecycle Engine + Risk kill-
switch) -- during the normal scan-eligible cycle below, AND (PAPER_TRADING_V1.5.9)
via a dedicated lifecycle-only pass whenever the scan window is closed but
open positions exist, so this evaluation is never conditional on the scan
window being open. Only new-position execution is gated behind human
confirmation -- that gate is unconditional and unaffected by this step.

**Step 2: the new-position daily opportunity scan may only START during
the approved regular market session.** Immediately after the provider
preflight above (still before `_expire_stale_candidates`, before the
market-data provider is constructed, before any cycle/candidate/snapshot
record is persisted), `src.portfolio.market_session
.evaluate_validation_cycle_eligibility` decides whether today's session
is open, in a trading day, and past the configured post-open/pre-close
buffer (`config/operations.yaml`'s `market_hours` section). This gate
lives ONLY here, in this one script's daily-invocation entry point; it
never touches `run_control_cycle`/`evaluate_position`/`check_kill_switch`
themselves (confirmed by a dedicated structural test), so existing-position
Lifecycle/Risk monitoring remains fully intact and independently callable.
PAPER_TRADING_V1.5.9 correction: before this step, this function also
*stopped running entirely* the moment the gate closed -- meaning
`run_outer_cycle` (the only caller of `run_control_cycle`) was never
reached either, for ANY reason the gate was closed, regardless of
whether positions existed. The module docstring above now reflects the
corrected behavior; see `_run_lifecycle_only_safety_check` for exactly
how the gate closing now affects ONLY the opportunity-scan sub-stage.
Fails closed if the market calendar itself cannot be evaluated -- see
that module's own docstring for the full reasoning.

Cycle-level idempotent: if today's cycle already completed
(`SqliteControlLoopStore.get_cycle_record`), this script logs and exits 0
without re-running anything.

Usage:
    python scripts/run_validation_cycle.py              # run one official cycle
    python scripts/run_validation_cycle.py --preflight   # read-only readiness check
    python scripts/run_validation_cycle.py --help        # usage; mutates nothing

Exit codes: 0 = cycle ran (even if degraded, or no candidate found), was
already done today, or `--preflight` reported ready. 1 = could not start
or complete the cycle/preflight at all (config error, cohort not started,
provider not Tradier production, database unreachable). 2 = bad CLI usage
(argparse's own default for an unrecognized argument).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data.factory import (  # noqa: E402
    OfficialProviderPreflightError,
    get_configured_market_data_provider,
    verify_official_provider_is_tradier_production,
)
from src.data.historical import HistoricalDataProvider  # noqa: E402
from src.data.market_calendar import is_market_open, is_trading_day  # noqa: E402
from src.data.option_chain import OptionChain, merge_option_chains  # noqa: E402
from src.data.provider import DteWindowOptionChainProvider, DteWindowSelectionDiagnostics  # noqa: E402
from src.data.universe import load_universe, load_universe_strategies  # noqa: E402
from src.lifecycle.persistence import SqliteLifecycleStore  # noqa: E402
from src.lifecycle.policies_library import policies_for_strategy  # noqa: E402
from src.llm.schemas import StrategyType  # noqa: E402
from src.brokers.base import SqliteIdempotencyStore  # noqa: E402
from src.brokers.paper import PaperBroker  # noqa: E402
from src.portfolio.account_state import SqlitePaperAccountStateStore, SqlitePortfolioStore  # noqa: E402
from src.portfolio.market_session import evaluate_validation_cycle_eligibility  # noqa: E402
from src.portfolio.operations_config import OperationsConfigError, load_operations_config  # noqa: E402
from src.portfolio.orchestrator import OpportunityScanConfig, OuterCycleInputs, run_outer_cycle  # noqa: E402
from src.portfolio.risk_data import apply_risk_data_wiring  # noqa: E402
from src.portfolio.persistence import SqliteControlLoopStore  # noqa: E402
from src.review.candidates import CandidateStatus, ReviewedCandidate, SqliteCandidateReviewStore  # noqa: E402
from src.risk.broker_constraints import load_broker_capabilities  # noqa: E402
from src.risk.engine import evaluate_trade_proposal  # noqa: E402
from src.risk.limits import RiskLimitsConfig, get_default_limits  # noqa: E402
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


async def _run_lifecycle_only_safety_check(
    *, now: datetime, block_reason: str | None, limits: RiskLimitsConfig, portfolio: Portfolio,
    lifecycle_store, control_loop_store,
) -> bool:
    """PAPER_TRADING_V1.5.9: existing-position Lifecycle Engine/Risk
    kill-switch monitoring must never be suppressed merely because the
    new-position scan window is closed -- see this module's own
    docstring. Called ONLY by `run_validation_cycle`, and only when
    `evaluate_validation_cycle_eligibility` blocked the new-position scan
    AND `portfolio.positions` is non-empty (the zero-position case is a
    safe no-op the caller handles itself, without ever reaching here).

    **Structurally cannot scan, generate a candidate, or fill an order.**
    Calls the exact same, unmodified `src.portfolio.orchestrator
    .run_outer_cycle` the normal scan-eligible cycle below calls, with
    `OuterCycleInputs.opportunity_scan=None` and `skip_opportunity_scan=
    True` -- `_run_opportunity_scan_stage` (orchestrator.py) returns a
    pure no-op for both reasons independently, so there is no way for
    this call to reach `scan_and_rank_opportunities`,
    `evaluate_trade_proposal` for a NEW candidate, or
    `PaperBroker.place_order`. Nothing here constructs a `PaperBroker`
    at all -- lifecycle evaluation only ever reads `Portfolio.positions`
    (an independent, already-durable domain object), never the broker's
    own fill-simulation state, and nothing in this function can mutate
    either.

    **Own cycle id, deliberately.** Uses `f"validation-{date}-lifecycle"`,
    never the scan-eligible cycle's own `f"validation-{date}"` id --
    `run_control_cycle` unconditionally persists a `ControlCycleRecord`
    keyed by whatever cycle id it's given the moment it runs, consuming
    that id's once-per-day idempotency slot. Sharing the scan-eligible
    id here would let an early, gate-closed safety check silently
    consume the day's REAL opportunity-scan slot, permanently blocking
    that day's actual new-position scan once the window opened --
    exactly the kind of regression this fix must not introduce. The two
    ids are independently idempotent: a second gate-closed invocation
    the same day sees its own `-lifecycle` record already exists and
    no-ops; the scan-eligible cycle later that same day sees its own,
    still-untouched id and proceeds completely normally.

    **Fetches ONLY existing positions' tickers**, via the provider's
    plain `get_option_chain` (the same near-term-appropriate call the
    normal cycle already uses for position tickers) -- never the scan
    universe, never a DTE-windowed fetch (there is no candidate
    generation to serve). All existing freshness/quality-gate/kill-switch
    protections apply completely unchanged, since this flows through the
    identical, unmodified `run_control_cycle`."""
    lifecycle_cycle_id = f"validation-{now.date().isoformat()}-lifecycle"
    if control_loop_store.get_cycle_record(lifecycle_cycle_id) is not None:
        print(f"Lifecycle-only safety check {lifecycle_cycle_id!r} already ran today -- nothing to do.")
        return True

    provider = get_configured_market_data_provider()
    position_tickers = sorted({p.ticker for p in portfolio.positions})
    fetch_results: dict[str, OptionChain | Exception] = {}
    try:
        for ticker in position_tickers:
            try:
                fetch_results[ticker] = await provider.get_option_chain(ticker)
            except Exception as exc:  # noqa: BLE001 - one bad symbol never aborts the cycle
                fetch_results[ticker] = exc
    finally:
        close = getattr(provider, "close", None)
        if close is not None:
            await close()

    failed = [t for t, c in fetch_results.items() if isinstance(c, Exception)]
    if failed:
        _line("lifecycle-only safety check -- symbols failed (isolated, cycle continues)", failed)
    provider_name = next((c.source for c in fetch_results.values() if isinstance(c, OptionChain)), "unknown")
    provider_health_status = "healthy" if not failed else "degraded"

    policy_name_for_position = {
        p.position_id: policies_for_strategy(StrategyKind(p.strategy.value))[0].name for p in portfolio.positions
    }

    inputs = OuterCycleInputs(
        cycle_id=lifecycle_cycle_id, as_of=now, portfolio=portfolio, limits=limits,
        provider=provider_name, provider_health_status=provider_health_status,
        is_trading_day=is_trading_day(now.date()), is_market_open=is_market_open(now),
        fetch_results=fetch_results, lifecycle_store=lifecycle_store, control_loop_store=control_loop_store,
        policy_name_for_position=policy_name_for_position,
        opportunity_scan=None, skip_opportunity_scan=True,
    )
    result = run_outer_cycle(inputs)

    _line("lifecycle-only safety check -- existing positions evaluated", result.control_result.cycle_record.positions_evaluated)
    _line("lifecycle-only safety check -- lifecycle triggers", result.control_result.cycle_record.lifecycle_triggers)
    _line("lifecycle-only safety check -- degraded_mode", result.control_result.cycle_record.degraded_mode)
    if result.new_alerts:
        _line("lifecycle-only safety check -- new alerts", len(result.new_alerts))

    print(
        "\nPASS: lifecycle-only safety check complete -- existing positions were evaluated through the "
        "unmodified Lifecycle Engine/Risk kill-switch. New-position scanning was skipped this invocation "
        f"because the new-position scan window is closed ({block_reason})."
    )
    return True


async def run_validation_cycle(*, now: datetime | None = None) -> bool:
    """`now` is `None` in every real invocation (CLI, dashboard POST) --
    the real wall clock (`datetime.now(timezone.utc)`) is used, exactly
    as before Step 2. The parameter exists solely so
    `evaluate_validation_cycle_eligibility`'s market-hours gate (below)
    can be exercised deterministically by tests without depending on
    the actual current clock -- see `src.portfolio.market_session`'s
    module docstring for why this is the one function that needs it."""
    print("Validation-cycle runner -- Review-Only new-position execution (PAPER_TRADING_V1.5.1)")
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

    # Step 22.6: provider CONFIGURATION preflight -- read-only (no network
    # call, no provider instance constructed), and strictly before every
    # mutating step below (candidate expiry, Portfolio/PaperAccountState
    # bootstrap, the cycle record, the daily snapshot). An official cycle
    # must never run against mock/synthetic/non-Tradier-production data.
    try:
        verify_official_provider_is_tradier_production()
    except OfficialProviderPreflightError as exc:
        print(f"FAIL: provider preflight -- {exc}")
        return False
    _line("provider preflight", "Tradier production market data configured")

    # PAPER_TRADING_V1.5.1, Step 2 (behavior corrected in PAPER_TRADING_V1.5.9):
    # the new-position daily opportunity scan's market-hours safety gate --
    # evaluated BEFORE any store beyond `validation_store` (already open for
    # the cohort-started check above) is constructed, before `cycle_id` is
    # computed, before the market-data provider is touched, before
    # `_expire_stale_candidates` runs, and therefore before any new-position
    # candidate/scan-cycle/snapshot record could be persisted. Unlike
    # pre-V1.5.9, a closed gate no longer returns immediately here -- see
    # `_run_lifecycle_only_safety_check` below for why: existing-position
    # Lifecycle Engine/Risk kill-switch monitoring must still run whenever
    # `Portfolio.positions` is non-empty, regardless of this gate.
    now = now or datetime.now(timezone.utc)
    eligibility = evaluate_validation_cycle_eligibility(
        now, scan_open_buffer_minutes=ops.scan_open_buffer_minutes, scan_close_buffer_minutes=ops.scan_close_buffer_minutes,
    )

    control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
    lifecycle_store = SqliteLifecycleStore(ops.lifecycle_db_path)
    review_store = SqliteCandidateReviewStore(ops.candidate_review_db_path)
    account_state_store = SqlitePaperAccountStateStore(ops.account_state_db_path)
    portfolio_store = SqlitePortfolioStore(ops.account_state_db_path)

    cycle_id = f"validation-{now.date().isoformat()}"
    _line("cycle_id", cycle_id)

    if control_loop_store.get_cycle_record(cycle_id) is not None:
        print(f"Cycle {cycle_id!r} already ran today -- nothing to do.")
        return True

    # PAPER_TRADING_V1.5.9: candidate TTL hygiene is unrelated to whether
    # the new-position scan window happens to be open right now -- runs
    # unconditionally, exactly like the cohort/provider preflights above it.
    _expire_stale_candidates(review_store, ops.cohort_id, validation_store, now)

    # PAPER_TRADING_V1.5.9: peeked read-only here (no bootstrap yet) so the
    # gate-closed branch below can decide "lifecycle-only" vs "safe no-op"
    # from the REAL current position count, without ever constructing a
    # market-data provider or persisting anything for an account that has
    # no positions and whose scan window is closed (preserves the exact
    # pre-V1.5.9 zero-position behavior byte-for-byte).
    existing_portfolio = portfolio_store.get(ops.account_id)
    existing_positions = existing_portfolio.positions if existing_portfolio is not None else []

    if not eligibility.validation_cycle_allowed:
        _line("market-hours gate", f"new-position scan window closed -- {eligibility.block_reason}")
        if not existing_positions:
            print(
                "FAIL: new-position scan window closed and no existing positions require lifecycle "
                "monitoring -- safe no-op, nothing was persisted."
            )
            return False
        return await _run_lifecycle_only_safety_check(
            now=now, block_reason=eligibility.block_reason, limits=limits, portfolio=existing_portfolio,
            lifecycle_store=lifecycle_store, control_loop_store=control_loop_store,
        )

    _line("market-hours gate", f"{eligibility.market_session_state.value} -- new-position scan window open")

    portfolio = existing_portfolio
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

    # PAPER_TRADING_V1.5.4, Step 3B: the market-data provider is now
    # constructed here -- BEFORE the risk-data wiring call below, unlike
    # V1.5.3, where it was constructed later and wiring always ran with
    # `historical_provider=None`. This lets the SAME already-open
    # connection (already verified Tradier production by the preflight
    # above) also serve as risk-data wiring's `historical_provider`,
    # rather than opening a second, redundant one.
    # `isinstance(..., HistoricalDataProvider)` -- never a hardcoded
    # provider-name check -- decides whether this provider can supply
    # historical bars at all: `TradierMarketDataProvider` is the only
    # provider in this codebase that satisfies both `MarketDataProvider`
    # and `HistoricalDataProvider` today, but this line never assumes
    # that. A provider that only implements `MarketDataProvider` falls
    # through to `historical_provider=None` -- exactly the input
    # `apply_risk_data_wiring`/`resolve_price_history_for_correlation`
    # already fail closed on; this script never decides what "missing"
    # means for the Risk Engine, only whether a provider is available.
    provider = get_configured_market_data_provider()
    historical_provider: HistoricalDataProvider | None = (
        provider if isinstance(provider, HistoricalDataProvider) else None
    )
    try:
        # PAPER_TRADING_V1.5.3, Step 3 (historical-data sourcing wired in
        # Step 3B, PAPER_TRADING_V1.5.4): sector/correlation risk-data
        # wiring. A no-op (returns `portfolio` completely unchanged)
        # unless the operator's own config/operations.yaml sets
        # `risk_data_wiring.enabled: true` -- the active cohort's own
        # config leaves this at its default `false`, so its candidate
        # eligibility is unaffected AND `historical_provider.get_bars` is
        # never called for that cohort either (`apply_risk_data_wiring`
        # returns immediately on `enabled=False`, before touching
        # `historical_provider` at all).
        portfolio = await apply_risk_data_wiring(
            portfolio, universe=universe, enabled=ops.risk_data_wiring_enabled, historical_provider=historical_provider,
            now=now, lookback_days=ops.correlation_lookback_days, min_observations=ops.min_correlation_observations,
        )

        # PAPER_TRADING_V1.5.6: hoisted above the fetch loop (previously
        # constructed inline, below, when building OpportunityScanConfig)
        # so the SAME QuantFilterConfig instance's min_dte/max_dte drives
        # both the DTE-aware fetch below and candidate generation itself
        # -- one source of truth, never a second hard-coded threshold.
        quant_filter = QuantFilterConfig()
        position_tickers = {p.ticker for p in portfolio.positions}
        universe_tickers = {e.ticker for e in universe}
        tickers = sorted(position_tickers | universe_tickers)
        fetch_results: dict[str, OptionChain | Exception] = {}
        # Bounded (at most len(universe) entries), operator-facing only --
        # never persisted into CandidateFunnel/ControlCycleRecord this
        # step. See DteWindowSelectionDiagnostics's own docstring.
        dte_selection_diagnostics: dict[str, DteWindowSelectionDiagnostics] = {}
        for ticker in tickers:
            try:
                if ticker in universe_tickers and isinstance(provider, DteWindowOptionChainProvider):
                    # Opportunity-scan tickers get a DTE-aware, BOUNDED
                    # chain fetch -- only expirations inside the candidate
                    # engine's own [min_dte, max_dte] window are ever
                    # fetched, rather than the provider's nearest-N-by-
                    # calendar-date default (get_option_chain), which can
                    # silently exclude every expiration a strategy's DTE
                    # policy could ever use (the exact 2026-10-01 defect
                    # this step fixes). Existing-position tickers still
                    # need get_option_chain's own near-term behavior for
                    # lifecycle monitoring (a position can be well under
                    # 20 DTE) -- a ticker that is BOTH an existing
                    # position AND in the universe gets BOTH fetches,
                    # merged, never one at the expense of the other.
                    diag = DteWindowSelectionDiagnostics()
                    dte_selection_diagnostics[ticker] = diag
                    scan_chain = await provider.get_option_chain_for_dte_window(
                        ticker, min_dte=quant_filter.min_dte, max_dte=quant_filter.max_dte, as_of=now.date(),
                        diagnostics=diag,
                    )
                    if ticker in position_tickers:
                        lifecycle_chain = await provider.get_option_chain(ticker)
                        fetch_results[ticker] = merge_option_chains(lifecycle_chain, scan_chain)
                    else:
                        fetch_results[ticker] = scan_chain
                else:
                    # A position-only ticker, or a provider that doesn't
                    # implement DteWindowOptionChainProvider at all (e.g.
                    # Alpaca, whose get_option_chain already returns every
                    # expiration in one call -- candidate_generation's own
                    # DTE filtering already selects correctly from that,
                    # no retrieval-side fix needed for it).
                    fetch_results[ticker] = await provider.get_option_chain(ticker)
            except Exception as exc:  # noqa: BLE001 - one bad symbol never aborts the cycle
                fetch_results[ticker] = exc
    finally:
        close = getattr(provider, "close", None)
        if close is not None:
            await close()

    # PAPER_TRADING_V1.5.7: a SECOND, LATER timestamp, captured only now
    # that every chain fetch above has actually completed -- distinct
    # from `now` (captured before the fetch, and still the cycle's own
    # audit/lifecycle/control-loop timestamp, unchanged below). Every
    # `OptionChain.timestamp` this cycle's fetch loop produced was
    # necessarily stamped no later than this instant, so
    # `evaluation_as_of >=` every `chain.timestamp` this scan will see --
    # the exact invariant a `TradeProposal` built from one of those
    # chains needs (`timestamp >= data_timestamp`) to satisfy its own
    # (unmodified, zero-tolerance) integrity check honestly, rather than
    # by chance. This is the fix for the 2026-10-02 incident: `now`
    # alone, captured before the fetch, could be EARLIER than a chain
    # timestamp the fetch produced moments later, failing that same
    # check even though the data was perfectly fresh.
    evaluation_as_of = datetime.now(timezone.utc)
    _line("opportunity-evaluation timestamp (captured after market-data fetch)", evaluation_as_of.isoformat())

    for ticker in sorted(dte_selection_diagnostics):
        diag = dte_selection_diagnostics[ticker]
        _line(
            f"DTE-window chain selection -- {ticker}",
            f"provider expirations returned={diag.provider_expirations_returned}, requested window="
            f"{quant_filter.min_dte}-{quant_filter.max_dte} DTE, in window={diag.expirations_in_window}, "
            f"selected={diag.expirations_selected}, skipped (outside window)="
            f"{diag.expirations_skipped_outside_window}, skipped (over request bound)="
            f"{diag.expirations_skipped_due_to_bound}",
        )

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
            quant_filter=quant_filter, market_regime=ops.default_market_regime,
            broker_capabilities=broker_capabilities, proposal_id_prefix=f"validation-scan-{now.date().isoformat()}",
            # PAPER_TRADING_V1.5.5, Step 4: observability-only -- this
            # collects the candidate funnel for THIS cycle's
            # `ControlCycleRecord` but changes nothing about which
            # candidate (if any) is found, ranked, or persisted below.
            # See `src.workflows.candidate_funnel` module docstring.
            collect_candidate_funnel=True,
            # PAPER_TRADING_V1.5.7: see OpportunityScanConfig
            # .evaluation_as_of's own field comment -- this scan, and
            # every TradeProposal it builds, is evaluated against the
            # POST-FETCH timestamp, never the pre-fetch cycle `now`.
            evaluation_as_of=evaluation_as_of,
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
            # PAPER_TRADING_V1.5.7: `evaluation_as_of`, not `now` -- the
            # exact same timestamp the scan itself used to evaluate this
            # candidate (see `OpportunityScanConfig.evaluation_as_of`),
            # so this re-run reproduces the scan's own decision rather
            # than re-evaluating the same chain against an earlier clock
            # reading.
            full_risk = evaluate_trade_proposal(
                best.candidate.proposal, portfolio, best.quantitative_analysis, chain,
                broker_capabilities, limits=limits, now=evaluation_as_of,
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

    # PAPER_TRADING_V1.5.5, Step 4: purely diagnostic -- printed from the
    # funnel `collect_candidate_funnel=True` above already caused
    # `run_outer_cycle` to build (and never fed back into any decision
    # made above this point).
    funnel = result.candidate_funnel
    if funnel is not None:
        _line("candidate funnel -- symbols scanned", funnel.symbols_requested)
        _line("candidate funnel -- usable option chains", funnel.option_chains_quality_passed)
        _line("candidate funnel -- contracts seen", funnel.contracts_seen)
        _line("candidate funnel -- strategy construction attempts", funnel.construction_attempts)
        _line("candidate funnel -- construction successes", funnel.construction_successes)
        _line("candidate funnel -- quant rejected", funnel.quant_rejected)
        _line("candidate funnel -- risk rejected", funnel.risk_rejected)
        _line("candidate funnel -- candidates persisted for review", funnel.candidates_persisted_for_review)
        if funnel.top_bottlenecks:
            _line("candidate funnel -- top bottlenecks", ", ".join(funnel.top_bottlenecks))
        if funnel.zero_candidate_summary is not None:
            print(f"  ZERO-CANDIDATE SUMMARY: {funnel.zero_candidate_summary}")

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


async def run_preflight() -> bool:
    """Step 22.6 (`--preflight`): read-only readiness check. Verifies
    configuration loads, the expected cohort has already been started,
    and the configured market-data provider is Tradier production -- the
    exact same checks `run_validation_cycle` performs before its own
    first mutating step, run here in isolation and never followed by
    anything that could mutate validation state. Never constructs a
    `PaperBroker`, a `SqliteControlLoopStore`, a `SqliteCandidateReviewStore`,
    or any other store this script's mutating path uses; never expires a
    candidate, never records a cycle or a snapshot, never fetches a
    single market-data quote. Does not perform a live Tradier network
    call -- this is configuration-only, matching `run_validation_cycle`'s
    own provider preflight exactly (see that function's docstring for why
    connectivity and configuration are deliberately kept separate)."""
    print("Validation-cycle preflight -- read-only readiness check (PAPER_TRADING_V1.4.8)")
    print("Mutates NO validation state.\n")

    try:
        ops = load_operations_config()
        val_config = load_validation_config()
        load_universe()
        configured_strategy_names = load_universe_strategies()
    except (OperationsConfigError, ValidationConfigError) as exc:
        print(f"FAIL: configuration error -- {exc}")
        return False

    try:
        tuple(StrategyType[name] for name in configured_strategy_names)
    except KeyError as exc:
        print(f"FAIL: configuration error -- config/universe.yaml lists an unknown strategy: {exc}")
        return False
    _line("configuration", "loaded OK")

    validation_store = SqliteValidationStore(val_config.db_path)
    if not has_cohort_started(validation_store) or validation_store.get_cohort(ops.cohort_id) is None:
        print(f"FAIL: cohort {ops.cohort_id!r} has not been started in {val_config.db_path!r}.")
        return False
    _line("cohort", f"{ops.cohort_id!r} -- already started, as required")

    try:
        verify_official_provider_is_tradier_production()
    except OfficialProviderPreflightError as exc:
        print(f"FAIL: provider preflight -- {exc}")
        return False
    _line("provider", "tradier (production endpoint, token configured)")

    print("\nPREFLIGHT ONLY -- NO VALIDATION STATE MUTATED")
    print("PASS: ready for an official validation cycle (python scripts/run_validation_cycle.py).")
    return True


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_validation_cycle.py",
        description=(
            "Operator-run daily validation-cycle runner for the official PAPER_TRADING 90-day "
            "validation. With no arguments, runs one official, state-mutating validation cycle "
            "against Tradier production market data. Never opens a new PaperBroker position -- "
            "see the module docstring for the full safety guarantees."
        ),
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help=(
            "Run read-only readiness checks only (configuration, cohort existence, Tradier "
            "production provider configuration) and exit. Mutates NO validation state."
        ),
    )
    return parser


def main() -> int:
    # Parsed before anything else -- argparse itself calls sys.exit(0) for
    # --help/-h and sys.exit(2) for an unrecognized argument, in both
    # cases before a single line past this point ever runs: no config is
    # loaded, no store is constructed, no provider is touched, no
    # validation state is read or written.
    parser = _build_arg_parser()
    args = parser.parse_args()

    if args.preflight:
        try:
            ok = asyncio.run(run_preflight())
        except Exception as exc:  # noqa: BLE001 - a genuinely systemic failure, reported plainly
            print(f"FAIL: preflight could not complete -- {exc!r}")
            return 1
        return 0 if ok else 1

    try:
        ok = asyncio.run(run_validation_cycle())
    except Exception as exc:  # noqa: BLE001 - a genuinely systemic failure, reported plainly
        print(f"FAIL: validation cycle could not complete -- {exc!r}")
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
