#!/usr/bin/env python3
"""Operator-run daily validation-cycle runner (Step 22.6; operator
usability/startup fixes in Step 22.8; dashboard UI completion in
Step 22.9; market-hours safety gate in Step 2, PAPER_TRADING_V1.5.1;
lifecycle/market-hours separation in PAPER_TRADING_V1.5.9;
existing-position retrieval safety fix in PAPER_TRADING_V1.5.10;
manual intraday lifecycle recheck safety release in
PAPER_TRADING_V1.5.11).

**PAPER_TRADING_V1.5.11: a manually-initiated existing-position
lifecycle recheck may now run more than once per trading day.** The
V1.5.9/V1.5.10 lifecycle-only safety check was still blocked, in full,
by this function's OWN top-level `validation-{date}` early-return the
moment the main cycle had run once that day -- a second manual
invocation later the same day, even with open positions and even with
a materially-changed market, did nothing at all (not even reaching
`_run_lifecycle_only_safety_check`). Fixed by no longer treating
"the main cycle already ran today" as a reason to do NOTHING for the
rest of this invocation -- it is now a reason only to skip the
opportunity-scan continuation specifically (new-position scanning
remains at most once per trading day, exactly as before: the main
`cycle_id` and its own idempotency check are both completely
unmodified). Existing positions found at that point still get routed
to `_run_lifecycle_only_safety_check`, exactly as the gate-closed
branch already did. That function's own cycle id is now bucketed by
the market-local (America/New_York, DST-correct via `zoneinfo`) hour
rather than the whole day, so a genuinely new manual recheck in a new
hour runs fresh, while a second call within the SAME hour is still a
documented no-op (a duplicate/retry guard, never a scheduler -- no
automatic trigger of any kind was added; every invocation remains as
manually-initiated, via the CLI or the one existing dashboard POST
route, as it already was). See `_run_lifecycle_only_safety_check`'s own
docstring and `STEP_23_11_FREEZE_REPORT.md` for the complete trace.

**PAPER_TRADING_V1.5.10: existing-position market-data retrieval must
cover each position's ACTUAL held expiration, never merely the
candidate-entry DTE window a position happened to be OPENED under.**
`TradierMarketDataProvider.get_option_chain` fetches only its nearest
`max_expirations` (default 6) calendar expirations, regardless of DTE
-- for a position opened at 20-45 DTE on a dense-expiration underlying
(SPY/QQQ), that default is highly likely to omit the position's own
expiration entirely, and (unlike the analogous V1.5.8 confirmation-
retrieval defect) this has nothing to do with a position "aging": the
risk is highest right after a position is OPENED (far from the front
of the provider's nearest-N list) and falls as the position's DTE
naturally shrinks toward the front of that list over time -- but a
position can also legitimately still be open below `QuantFilterConfig
.min_dte` (20), which the global candidate window could never cover
either way. `_fetch_existing_position_chain` (new) fixes this by
requesting each position's own real expiration EXACTLY (`min_dte=
max_dte=` that position's actual DTE, via the same
`DteWindowOptionChainProvider` capability V1.5.8 already uses for
confirmation) rather than inferring coverage from either the provider's
nearest-N default or the global scan window. Used by BOTH
`_run_lifecycle_only_safety_check` (which had NO DTE-aware retrieval at
all before this fix) and the normal scan-eligible cycle's existing-
position fetch below (which previously only got DTE-aware coverage
when its ticker happened to ALSO be a universe ticker) -- one canonical
existing-position retrieval mechanism for both paths, so they cannot
diverge again. Contract-identity matching itself
(`src.portfolio.revaluation.build_contract_index`/`revalue_position`,
exact `(expiration, strike, right)`, fail-closed to
`DATA_INSUFFICIENT` on any unmatched/stale leg) is UNCHANGED -- this
fix only ever widens what can be successfully retrieved, never what
counts as a valid match, and never touches candidate-generation's own
`QuantFilterConfig`/[20, 45] DTE window, `max_expirations`, or the
opportunity-scan fetch path. See `_fetch_existing_position_chain`'s own
docstring and `STEP_23_10_FREEZE_REPORT.md` for the complete trace.

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
import re
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
from src.data.market_calendar import EASTERN, is_market_open, is_trading_day  # noqa: E402
from src.data.option_chain import OptionChain, merge_option_chains  # noqa: E402
from src.data.provider import DteWindowOptionChainProvider, DteWindowSelectionDiagnostics  # noqa: E402
from src.data.universe import UniverseConfigError, load_universe, load_universe_strategies  # noqa: E402
from src.lifecycle.persistence import SqliteLifecycleStore  # noqa: E402
from src.lifecycle.policies_library import policies_for_strategy  # noqa: E402
from src.llm.schemas import StrategyType  # noqa: E402
from src.brokers.base import SqliteIdempotencyStore  # noqa: E402
from src.brokers.paper import PaperBroker  # noqa: E402
from src.portfolio.account_state import (  # noqa: E402
    PortfolioLoadError,
    SqlitePaperAccountStateStore,
    SqlitePortfolioStore,
    load_portfolio_read_only,
)
from src.portfolio.market_session import evaluate_validation_cycle_eligibility  # noqa: E402
from src.portfolio.operations_config import OperationsConfigError, load_operations_config  # noqa: E402
from src.portfolio.opportunity_scan import scan_and_rank_opportunities  # noqa: E402
from src.portfolio.orchestrator import OpportunityScanConfig, OuterCycleInputs, run_outer_cycle  # noqa: E402
from src.portfolio.risk_data import apply_risk_data_wiring, resolve_price_history_for_correlation  # noqa: E402
from src.portfolio.persistence import SqliteControlLoopStore  # noqa: E402
from src.review.candidates import CandidateStatus, ReviewedCandidate, SqliteCandidateReviewStore  # noqa: E402
from src.risk.broker_constraints import load_broker_capabilities  # noqa: E402
from src.risk.engine import evaluate_trade_proposal  # noqa: E402
from src.risk.limits import RiskLimitsConfig, get_default_limits  # noqa: E402
from src.risk.portfolio_risk import PortfolioPosition, Portfolio, sector_exposure_pct, underlying_exposure_pct  # noqa: E402
from src.strategies.base import StrategyKind  # noqa: E402
from src.validation.cohort import has_cohort_started  # noqa: E402
from src.validation.protocol import ValidationConfigError, load_validation_config  # noqa: E402
from src.validation.records import OpportunityRecord  # noqa: E402
from src.validation.session import DailySnapshot, SqliteValidationStore  # noqa: E402
from src.workflows.candidate_funnel import build_candidate_funnel  # noqa: E402
from src.workflows.candidate_generation import QuantFilterConfig, candidate_eligible_strategies  # noqa: E402
from src.workflows.funnel_diagnostics import FunnelDiagnostics  # noqa: E402
from src.workflows.universe_feasibility import (  # noqa: E402
    RateLimitSafetyConfigError,
    SkipReason,
    build_aggregate_feasibility_report,
    build_candidate_feasibility_details,
    build_correlation_feasibility_summary,
    build_symbol_feasibility_summaries,
    classify_opportunity_today,
    classify_structural_suitability,
    expected_correlation_request_count,
    expected_opportunity_scan_request_count,
    expected_tradier_request_count,
    has_sufficient_observed_headroom,
    load_rate_limit_safety_config,
    usable_request_headroom,
)
from src.quant.correlations import flag_highly_correlated_pairs  # noqa: E402
from src.portfolio.cycle_helpers import (  # noqa: E402
    expire_stale_candidates as _expire_stale_candidates,
    fetch_existing_position_chain as _fetch_existing_position_chain,
    line as _line,
    run_lifecycle_only_safety_check as _run_lifecycle_only_safety_check,
)


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

    # PAPER_TRADING_V1.5.11: captured as a plain boolean rather than an
    # immediate `return True` -- the old unconditional early-return here
    # blocked EVERYTHING this function could do for the rest of the day
    # the instant the main cycle ran once, including a later, manually-
    # initiated existing-position lifecycle recheck. The main cycle's own
    # once-per-trading-day guarantee is unchanged (see below: this boolean
    # only ever SKIPS the opportunity-scan continuation; it never lets a
    # second opportunity scan, candidate, or snapshot through) -- it is
    # just no longer the one check that decides whether the ENTIRE
    # function does anything at all this invocation.
    main_cycle_already_ran = control_loop_store.get_cycle_record(cycle_id) is not None

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
            provider=get_configured_market_data_provider(),
        )

    _line("market-hours gate", f"{eligibility.market_session_state.value} -- new-position scan window open")

    # PAPER_TRADING_V1.5.11: the new-position scan window is open, but
    # today's one-and-only opportunity scan already happened under
    # `cycle_id` -- new-position scanning stays at most once per trading
    # day (this never re-enters the opportunity-scan continuation below).
    # Existing positions, if any, may still receive a manually-initiated
    # lifecycle recheck -- `_run_lifecycle_only_safety_check` uses its OWN,
    # hourly-bucketed cycle id (never `cycle_id` itself), so this can never
    # consume, duplicate, or otherwise interact with the main cycle's own
    # idempotency slot.
    if main_cycle_already_ran:
        block_reason = f"cycle {cycle_id!r} already completed today's new-position scan"
        print(f"Cycle {cycle_id!r} already ran today -- new-position scanning stays at most once per trading day.")
        if not existing_positions:
            print("No existing positions -- nothing further to do this invocation.")
            return True
        return await _run_lifecycle_only_safety_check(
            now=now, block_reason=block_reason, limits=limits, portfolio=existing_portfolio,
            lifecycle_store=lifecycle_store, control_loop_store=control_loop_store,
            provider=get_configured_market_data_provider(),
        )

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
        positions_by_ticker: dict[str, list[PortfolioPosition]] = {}
        for p in portfolio.positions:
            positions_by_ticker.setdefault(p.ticker, []).append(p)
        position_tickers = set(positions_by_ticker)
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
                    # this step fixes). Existing-position tickers get
                    # `_fetch_existing_position_chain` (PAPER_TRADING_V1.5.10)
                    # instead of a bare `get_option_chain` call -- a
                    # position can be well under 20 DTE, or simply never
                    # within the provider's nearest-N default to begin
                    # with (see that function's own docstring) -- merged
                    # with `scan_chain`, never at the expense of it, for a
                    # ticker that is BOTH an existing position AND in the
                    # universe.
                    diag = DteWindowSelectionDiagnostics()
                    dte_selection_diagnostics[ticker] = diag
                    scan_chain = await provider.get_option_chain_for_dte_window(
                        ticker, min_dte=quant_filter.min_dte, max_dte=quant_filter.max_dte, as_of=now.date(),
                        diagnostics=diag,
                    )
                    if ticker in position_tickers:
                        lifecycle_chain = await _fetch_existing_position_chain(
                            provider, positions_by_ticker[ticker], now=now,
                        )
                        fetch_results[ticker] = merge_option_chains(lifecycle_chain, scan_chain)
                    else:
                        fetch_results[ticker] = scan_chain
                elif ticker in position_tickers:
                    # PAPER_TRADING_V1.5.10: a position-only ticker (not in
                    # the opportunity-scan universe), or a universe ticker
                    # whose provider doesn't implement
                    # DteWindowOptionChainProvider at all -- either way,
                    # this existing position's own held expiration(s) are
                    # the authoritative retrieval requirement, same as the
                    # branch above.
                    fetch_results[ticker] = await _fetch_existing_position_chain(
                        provider, positions_by_ticker[ticker], now=now,
                    )
                else:
                    # A universe-only ticker whose provider doesn't
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
            # PAPER_TRADING_V1.5.12: do NOT re-add the scan date here --
            # `generate_candidates`'s own `_next_id()` already encodes
            # `now.date().isoformat()` into every proposal_id it builds
            # (see that function's SY-001 comment). Supplying a prefix
            # that ALSO carries the date double-counted it, pushing a
            # realistic multi-leg PUT_CREDIT_SPREAD id (ticker + two
            # strikes) past TradeProposal.proposal_id's max_length=64
            # and failing every such candidate with a Pydantic
            # ValidationError before it ever reached Quant/Risk -- see
            # STEP_23_12_FREEZE_REPORT.md for the full root-cause trace.
            broker_capabilities=broker_capabilities, proposal_id_prefix="validation-scan",
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


_DIAGNOSTIC_BANNER = (
    "DIAGNOSTIC SCAN ONLY -- NOT AN OFFICIAL VALIDATION CYCLE\n"
    "NO VALIDATION STATE WILL BE MUTATED\n"
    "RESULTS DO NOT COUNT TOWARD THE 90-DAY VALIDATION"
)


def _print_diagnostic_banner() -> None:
    print(_DIAGNOSTIC_BANNER)


def _sanitize_diagnostic_exception_message(exc: Exception) -> str:
    """PAPER_TRADING_V1.5.13, `--diagnostic-scan` only: a short, bounded,
    best-effort-redacted rendering of a generation exception's own
    message, printed directly to the operator's terminal so the
    diagnostic can show WHY a candidate failed construction beyond the
    bare exception CLASS name `FunnelDiagnostics
    .record_generation_exception` is deliberately limited to (see that
    method's own docstring) -- never fed back into
    `FunnelDiagnostics`/`CandidateFunnel`, never changing what an
    official cycle persists or decides.

    The only generation exception reachable through this codebase's own
    strategy-construction code today is a Pydantic `ValidationError` on
    `TradeProposal` field values (e.g. PAPER_TRADING_V1.5.12's
    proposal_id-length defect) -- never a provider/auth payload -- but
    this function applies defensive redaction regardless, so it never
    assumes that stays true forever."""
    message = str(exc).replace("\n", " ").replace("\r", " ")
    message = re.sub(r"(?i)(bearer\s+)\S+", r"\1[REDACTED]", message)
    message = re.sub(r"(?i)(authorization\s*[:=]\s*)\S+", r"\1[REDACTED]", message)
    message = re.sub(
        r"(?i)\b([a-z0-9_-]*(?:token|secret|api[_-]?key)[a-z0-9_-]*\s*[:=]\s*)\S+", r"\1[REDACTED]", message,
    )
    message = " ".join(message.split())
    if len(message) > 400:
        message = message[:400] + "...[truncated]"
    return message


async def run_diagnostic_scan(*, now: datetime | None = None) -> bool:
    """PAPER_TRADING_V1.5.13: an explicit, operator-facing, READ-ONLY
    diagnostic (`--diagnostic-scan`) that runs the exact same
    candidate -> Quant -> Risk pipeline the official cycle uses
    (`scan_and_rank_opportunities`, called DIRECTLY here) against LIVE
    Tradier production market data, so an operator can verify a
    software fix (e.g. PAPER_TRADING_V1.5.12's proposal_id fix) without
    running -- or counting as -- an official validation day.

    **Never calls `run_outer_cycle`/`run_control_cycle`.** Both
    unconditionally persist records (a `ControlCycleRecord`, lifecycle
    positions, decision snapshots, alerts) even for an existing-
    position-only evaluation -- there is no way to use either and stay
    read-only. This function calls `scan_and_rank_opportunities`
    directly instead: the exact same, unmodified pure function
    `_run_opportunity_scan_stage` (`src.portfolio.orchestrator`) itself
    calls, so this diagnostic's candidate/Quant/Risk decisions are
    byte-identical to what the official cycle's own opportunity-scan
    stage would produce against the same inputs -- never a second,
    reimplemented screening algorithm.

    **Zero-persistence by construction, not by convention.** This
    function never imports or constructs `PaperBroker`,
    `SqliteControlLoopStore`, `SqliteLifecycleStore`,
    `SqliteCandidateReviewStore`, `SqliteIdempotencyStore`,
    `SqliteValidationStore`, or `SqlitePortfolioStore` -- none of those
    names appear anywhere in this function's body (confirmed by its own
    acceptance test's source scan). PAPER_TRADING_V1.5.13's acceptance
    audit found that `SqlitePortfolioStore.__init__` is NOT actually
    read-only in general -- it unconditionally runs `CREATE TABLE IF
    NOT EXISTS`, which genuinely writes a new database file/table into
    existence when either is missing (verified empirically: a
    nonexistent path or table goes from 0 to 12,288 bytes on
    construction alone). "Read-only because the real account_state.db
    already has this table" was a fact about today's file, not a
    structural guarantee -- so this function now loads the portfolio
    via `src.portfolio.account_state.load_portfolio_read_only` instead,
    which opens the database through SQLite's own `mode=ro` URI
    connection option (enforced by SQLite at the OS file-descriptor
    level, not by caller discipline) and never creates a directory,
    file, or table under any condition. There is therefore no code
    path inside this function capable of writing a cycle record, a
    lifecycle record, a daily snapshot, an alert, a candidate, an
    order, a fill, or a portfolio database/table/row, regardless of
    what the scan finds or what state the database starts in.

    **Same Tradier-production preflight, same market-hours gate, same
    order.** `verify_official_provider_is_tradier_production` first
    (config-only, no network, no provider instance) -- diagnostic mode
    refuses mock/sandbox/missing-token exactly like the official cycle.
    Then `evaluate_validation_cycle_eligibility`, BEFORE the market-data
    provider is ever constructed -- a closed gate means zero Tradier
    calls, exactly like the official cycle's own gate-closed branch,
    except this function never falls through to a lifecycle-only check:
    diagnostic scope is new-position opportunity-scan diagnostics ONLY,
    never position-lifecycle monitoring (that stays the official
    cycle's job).

    **Portfolio loaded READ-ONLY, by construction.**
    `load_portfolio_read_only(ops.account_state_db_path, ops.account_id)`
    -- for the currently active cohort a portfolio already exists and
    is returned as-is. `None` covers every "nothing on record yet"
    case (missing database file, missing table, or missing account
    row) -- in any of those cases this function builds an in-memory
    `Portfolio` from `config/validation.yaml`'s own
    `starting_capital.default_nav` (the exact value the official
    cycle's own bootstrap would use), never persisted. A
    `PortfolioLoadError` (a genuinely abnormal condition -- corrupt
    `portfolio_json`, or a sqlite error that isn't "doesn't exist yet")
    is NOT treated as "no portfolio" -- it fails the whole diagnostic
    closed (`FAIL`, banner, `return False`) rather than silently
    substituting a fresh empty-NAV portfolio for real account data this
    function could not read.

    **Same DTE-aware retrieval, same post-fetch evaluation timestamp,
    same proposal_id_prefix.** Universe tickers are fetched via
    `provider.get_option_chain_for_dte_window(min_dte=quant_filter
    .min_dte, max_dte=quant_filter.max_dte, ...)` whenever the
    configured provider implements `DteWindowOptionChainProvider`
    (PAPER_TRADING_V1.5.6's own fix), never the provider's nearest-N
    default. `evaluation_as_of` is captured only once every chain fetch
    has completed (PAPER_TRADING_V1.5.7's own fix), so every
    `TradeProposal` this scan builds satisfies its own `data_timestamp
    <= timestamp` integrity check by construction. `proposal_id_prefix
    ="validation-scan"` -- the exact, corrected, bare prefix
    PAPER_TRADING_V1.5.12 fixed the official cycle to use, never the
    redundantly-dated pre-V1.5.12 form.

    **A surviving candidate is diagnostic-only.** If `scan_result.best`
    is not `None`, it is printed for the operator's own inspection --
    never saved as a `ReviewedCandidate`, never made confirmable, and no
    `confirm_candidate.py` command is ever printed for it (unlike the
    official cycle's own candidate output). There is no code path from
    here to `PaperBroker.place_order`/`confirm_fill`/`confirm_candidate`.

    Existing positions are read ONLY to feed Risk's own post-trade
    exposure calculations (`underlying_exposure_pct`/`sector_exposure_pct`)
    -- this function never fetches a position-only ticker's own chain
    and never evaluates lifecycle for it; that stays the official
    cycle's job.

    `now` is `None` in every real invocation -- see
    `run_validation_cycle`'s own docstring for why this parameter
    exists (deterministic market-hours-gate testing only)."""
    _print_diagnostic_banner()
    print()

    try:
        ops = load_operations_config()
        val_config = load_validation_config()
        universe = load_universe()
        configured_strategy_names = load_universe_strategies()
    except (OperationsConfigError, ValidationConfigError) as exc:
        print(f"FAIL: configuration error -- {exc}")
        print()
        _print_diagnostic_banner()
        return False

    try:
        configured_strategies = tuple(StrategyType[name] for name in configured_strategy_names)
    except KeyError as exc:
        print(f"FAIL: configuration error -- config/universe.yaml lists an unknown strategy: {exc}")
        print()
        _print_diagnostic_banner()
        return False

    strategies = candidate_eligible_strategies(configured_strategies)
    skipped = [s.value for s in configured_strategies if s not in strategies]
    if skipped:
        _line("strategies configured but not yet candidate-generation-eligible (skipped)", skipped)

    limits = get_default_limits()

    # Same official provider CONFIGURATION preflight -- read-only, no
    # network call, no provider instance constructed.
    try:
        verify_official_provider_is_tradier_production()
    except OfficialProviderPreflightError as exc:
        print(f"FAIL: provider preflight -- {exc}")
        print()
        _print_diagnostic_banner()
        return False
    _line("provider preflight", "Tradier production market data configured")

    now = now or datetime.now(timezone.utc)

    # Same new-position market-hours gate the official cycle uses --
    # evaluated BEFORE the market-data provider is constructed, so a
    # closed gate makes zero Tradier calls. Diagnostic mode is new-
    # position opportunity-scan diagnostics ONLY -- unlike the official
    # cycle, it never falls through to a lifecycle-only check.
    eligibility = evaluate_validation_cycle_eligibility(
        now, scan_open_buffer_minutes=ops.scan_open_buffer_minutes, scan_close_buffer_minutes=ops.scan_close_buffer_minutes,
    )
    if not eligibility.validation_cycle_allowed:
        _line("market-hours gate", f"new-position scan window closed -- {eligibility.block_reason}")
        print(
            "FAIL: new-position scan window closed -- diagnostic scan makes no market-data calls "
            "while this window is closed."
        )
        print()
        _print_diagnostic_banner()
        return False
    _line("market-hours gate", f"{eligibility.market_session_state.value} -- new-position scan window open")

    # READ-ONLY portfolio load, enforced by SQLite's own mode=ro URI
    # connection -- SqlitePortfolioStore is never constructed here (its
    # own CREATE TABLE IF NOT EXISTS can genuinely write a missing
    # database/table into existence; see this function's own docstring
    # and PAPER_TRADING_V1.5.13's acceptance audit).
    try:
        portfolio = load_portfolio_read_only(ops.account_state_db_path, ops.account_id)
    except PortfolioLoadError as exc:
        print(f"FAIL: could not read portfolio state read-only -- {exc}")
        print()
        _print_diagnostic_banner()
        return False
    if portfolio is None:
        _line(
            "no persisted portfolio for this account -- using an in-memory diagnostic portfolio (never saved)",
            f"starting NAV ${val_config.default_starting_nav:,.2f}",
        )
        portfolio = Portfolio(
            as_of=now, nav=val_config.default_starting_nav, cash=val_config.default_starting_nav,
            peak_equity=val_config.default_starting_nav,
        )
    else:
        _line("existing portfolio loaded read-only", f"{len(portfolio.positions)} open position(s), NAV ${portfolio.nav:,.2f}")

    broker_capabilities = load_broker_capabilities("internal_paper")
    quant_filter = QuantFilterConfig()

    universe_tickers = tuple(e.ticker for e in universe)
    fetch_results: dict[str, OptionChain | Exception] = {}
    provider = get_configured_market_data_provider()
    try:
        for ticker in universe_tickers:
            try:
                if isinstance(provider, DteWindowOptionChainProvider):
                    fetch_results[ticker] = await provider.get_option_chain_for_dte_window(
                        ticker, min_dte=quant_filter.min_dte, max_dte=quant_filter.max_dte, as_of=now.date(),
                    )
                else:
                    fetch_results[ticker] = await provider.get_option_chain(ticker)
            except Exception as exc:  # noqa: BLE001 - one bad symbol never aborts the diagnostic scan
                fetch_results[ticker] = exc
    finally:
        close = getattr(provider, "close", None)
        if close is not None:
            await close()

    # Same PAPER_TRADING_V1.5.7 fix: captured only once every chain fetch
    # above has completed, so every TradeProposal built from this scan
    # satisfies its own data_timestamp <= timestamp integrity check by
    # construction, never by chance.
    evaluation_as_of = datetime.now(timezone.utc)
    _line("evaluation timestamp (captured after market-data fetch)", evaluation_as_of.isoformat())

    chains_by_ticker = {t: c for t, c in fetch_results.items() if isinstance(c, OptionChain)}
    failed = [t for t, c in fetch_results.items() if isinstance(c, Exception)]
    if failed:
        _line("symbols failed this diagnostic scan (isolated, scan continues)", failed)

    diagnostics_by_ticker = {t: FunnelDiagnostics(ticker=t) for t in universe_tickers}
    sanitized_exceptions: list[tuple[str, str, str]] = []  # (ticker, strategy, sanitized message)

    def _on_generation_exception(ticker: str, strategy: str, exc: Exception) -> None:
        sanitized_exceptions.append((ticker, strategy, _sanitize_diagnostic_exception_message(exc)))

    # The exact same pure function the official cycle's own
    # _run_opportunity_scan_stage calls -- never run_outer_cycle/
    # run_control_cycle (both of which unconditionally persist).
    scan_result = scan_and_rank_opportunities(
        list(universe), chains_by_ticker, list(strategies), quant_filter, limits, portfolio,
        ops.default_market_regime, broker_capabilities, now=evaluation_as_of,
        proposal_id_prefix="validation-scan", diagnostics_by_ticker=diagnostics_by_ticker,
        on_generation_exception=_on_generation_exception,
    )

    # The exact same pure aggregator the official cycle's own
    # collect_candidate_funnel=True path uses -- candidates_persisted is
    # always 0 here, since this path never persists anything.
    funnel = build_candidate_funnel(
        cycle_id="diagnostic-scan-only", generated_at=evaluation_as_of,
        universe_tickers=universe_tickers, chain_received_tickers=frozenset(chains_by_ticker.keys()),
        diagnostics_by_ticker=diagnostics_by_ticker, scan_result=scan_result, candidates_persisted=0,
    )

    print()
    _line("candidate funnel -- symbols scanned", funnel.symbols_requested)
    _line("candidate funnel -- chains usable/failed", f"{funnel.option_chains_quality_passed}/{funnel.symbols_market_data_failed + funnel.option_chains_quality_failed}")
    _line("candidate funnel -- contracts examined", funnel.contracts_seen)
    _line("candidate funnel -- expirations seen/eligible/rejected", f"{funnel.expirations_seen}/{funnel.expirations_eligible}/{funnel.expirations_rejected}")
    _line("candidate funnel -- strategy attempts/ineligible", f"{funnel.strategy_attempts}/{funnel.strategy_ineligible}")
    _line("candidate funnel -- construction attempts/successes/rejections", f"{funnel.construction_attempts}/{funnel.construction_successes}/{funnel.construction_rejections}")
    _line("candidate funnel -- generation exceptions", funnel.generation_exceptions)
    for ticker, strategy, message in sanitized_exceptions:
        _line(f"  generation exception detail -- {ticker}/{strategy}", message)
    _line("candidate funnel -- quant evaluations/pass/reject", f"{funnel.quant_evaluations}/{funnel.quant_passed}/{funnel.quant_rejected}")
    _line("candidate funnel -- risk evaluations/pass/reject", f"{funnel.risk_evaluations}/{funnel.risk_passed}/{funnel.risk_rejected}")
    _line("candidate funnel -- candidates generated/ranked", f"{funnel.candidates_generated}/{funnel.candidates_ranked}")
    if funnel.rejection_reasons:
        _line("candidate funnel -- rejection reasons", ", ".join(f"{r.stage}:{r.reason}x{r.count}" for r in funnel.rejection_reasons))
    for s in funnel.by_symbol:
        _line(
            f"  by-symbol -- {s.ticker}",
            f"market_data_ok={s.market_data_successful}, chain_usable={s.chain_usable}, contracts={s.contracts_seen}, "
            f"expirations_seen/eligible={s.expirations_seen}/{s.expirations_eligible}, "
            f"strategy_attempts={s.strategy_attempts}, construction_successes={s.construction_successes}",
        )
    for s in funnel.by_strategy:
        _line(
            f"  by-strategy -- {s.strategy}",
            f"attempts={s.attempts}, ineligible={s.ineligible}, construction={s.construction_successes}/{s.construction_rejections}, "
            f"generation_exceptions={s.generation_exceptions}, quant={s.quant_passed}/{s.quant_rejected}, "
            f"risk={s.risk_passed}/{s.risk_rejected}",
        )

    if scan_result.best is None:
        _line("best candidate", f"none -- {scan_result.no_trade_reason}")
    else:
        best = scan_result.best
        print()
        print(f"  DIAGNOSTIC CANDIDATE SURVIVED QUANT/RISK (NOT PERSISTED, NOT CONFIRMABLE): {best.candidate.proposal.proposal_id}")
        _line("  ticker/strategy", f"{best.candidate.proposal.ticker} / {best.candidate.proposal.strategy.value}")
        _line("  proposal_id length", len(best.candidate.proposal.proposal_id))
        _line("  risk decision", f"{best.risk_decision.value} -- {best.risk_reason}")
        print(
            "  This is a DIAGNOSTIC-ONLY observation -- no ReviewedCandidate was created, no confirmation\n"
            "  command is available for it, and no PaperBroker order/fill can result from this path."
        )

    print()
    _print_diagnostic_banner()
    return True


# ---------------------------------------------------------------------------
# PAPER_TRADING_V1.5.14: --universe-feasibility
# ---------------------------------------------------------------------------

_FEASIBILITY_BANNER = (
    "UNIVERSE FEASIBILITY STUDY ONLY\n"
    "NOT AN OFFICIAL VALIDATION CYCLE\n"
    "NO VALIDATION STATE WILL BE MUTATED\n"
    "NO CANDIDATE WILL BE PERSISTED\n"
    "NO ORDER OR FILL CAN OCCUR"
)

_FEASIBILITY_UNIVERSE_CONFIG_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "universe_feasibility.yaml"
)

_DEFAULT_TRADIER_MAX_EXPIRATIONS = 6  # TradierConfig.max_expirations's own default -- see src.data.tradier_provider


def _print_feasibility_banner() -> None:
    print(_FEASIBILITY_BANNER)


async def run_universe_feasibility_study(*, now: datetime | None = None) -> bool:
    """PAPER_TRADING_V1.5.14: a READ-ONLY, operator-facing research study
    (`--universe-feasibility`) that runs the exact same candidate ->
    Quant -> Risk pipeline the official cycle and the V1.5.13 diagnostic
    both use, against a WIDER research universe
    (`config/universe_feasibility.yaml`, 12 tickers) than the official
    active universe (`config/universe.yaml`, SPY/QQQ) -- so an operator
    can decide, from real data, whether and how to expand the official
    universe, WITHOUT running or counting as an official validation day
    and WITHOUT activating anything.

    **Zero-persistence by construction -- same architecture as
    `run_diagnostic_scan`, extended to more symbols plus a read-only
    correlation summary, never anything that writes.** This function
    never imports or constructs `PaperBroker`, `SqlitePortfolioStore`,
    `SqliteControlLoopStore`, `SqliteLifecycleStore`,
    `SqliteCandidateReviewStore`, `SqliteIdempotencyStore`,
    `SqliteValidationStore`, or `ReviewedCandidate` -- none of those
    names appear anywhere in this function's body. The portfolio is
    loaded exclusively via `load_portfolio_read_only` (PAPER_TRADING_
    V1.5.13's own true-read-only SQLite `mode=ro` loader) -- a
    `PortfolioLoadError` fails this study closed exactly like it does
    for `run_diagnostic_scan`, never silently substituting a fresh
    portfolio for unreadable real account data. `config/universe.yaml`
    (the OFFICIAL universe) and `config/operations.yaml`'s
    `risk_data_wiring.enabled` flag are never read, written, or
    toggled by this function -- the correlation summary below is built
    entirely from a side-channel, read-only call to
    `resolve_price_history_for_correlation` (never `apply_risk_data_
    wiring`/`apply_correlation_wiring`, which would attach the result
    to the `Portfolio` object Quant/Risk actually evaluate candidates
    against) -- the official active cohort's real Risk/Quant behavior
    for these candidates is therefore byte-for-byte what it already is
    today, correlation-wiring-disabled, exactly matching the official
    cycle's own current behavior.

    **Same Tradier-production preflight, same market-hours gate, same
    order, as `run_diagnostic_scan`/`run_validation_cycle`.** A closed
    gate makes zero Tradier calls (the provider is never constructed)
    and zero database writes.

    **Same production pipeline, same strategy restriction.**
    `load_universe`/`load_universe_strategies` are called with
    `config/universe_feasibility.yaml`'s own path -- never falling back
    to, merging with, or writing `config/universe.yaml`. The resulting
    strategy list is narrowed through the exact same
    `candidate_eligible_strategies` filter the official cycle and the
    diagnostic both already use, which only ever allows
    `CASH_SECURED_PUT`/`COVERED_CALL`/`PUT_CREDIT_SPREAD` today --
    structurally, not merely by convention, preventing this study from
    ever testing strategy breadth alongside universe breadth. Candidate
    generation, Quant, Risk, ranking, and the no-trade hurdle are the
    exact same, unmodified `scan_and_rank_opportunities` call
    `run_diagnostic_scan` itself uses, with `proposal_id_prefix=
    "validation-scan"` and `evaluation_as_of` captured strictly after
    the whole market-data fetch loop completes (the same V1.5.7
    discipline) -- never a reimplemented or loosened screen.

    **A surviving candidate is observational only.** Every candidate
    that reaches Risk APPROVE/RESIZE is printed with sanitized
    economics (`src.workflows.universe_feasibility
    .build_candidate_feasibility_details`) -- never saved as a
    `ReviewedCandidate`, never made confirmable, and no
    `confirm_candidate.py` command is ever printed for any of them,
    exactly like `run_diagnostic_scan`'s own single best candidate.

    **V1.5.14 acceptance correction -- conservative, sequential,
    budget-aware fetching across TWO separately-budgeted phases.**
    Neither this change, nor anything else in this function, touches
    `TradierMarketDataProvider`'s own production semantics, retry
    policy, DTE retrieval, or canonical timestamps -- those remain
    exactly as frozen for the official cycle; only how MANY symbols
    this STUDY asks for, and in what order, changed.

    Phase 1 (option-chain opportunity scan) fetches `universe_tickers`
    in small, strictly sequential batches of
    `config/universe_feasibility.yaml`'s own `rate_limit_safety.
    batch_size` (default 3) -- never aggressive concurrency. The FIRST
    batch always proceeds (the provider's `rate_limit_state` starts
    `None`; there is nothing observed yet to check against -- this is
    the exact, documented bootstrap: allow only enough initial work to
    obtain real response headers, then decide every subsequent batch
    from what was actually observed, never from an assumed plan
    allowance). Before every batch AFTER the first,
    `has_sufficient_observed_headroom` checks the provider's own last-
    observed `rate_limit_state` against that batch's worst-case logical
    request count, reserving `rate_limit_safety.reserved_headroom_pct`
    of the TOTAL allowance as a margin for other concurrent consumers
    of the same real, Tradier-account-wide budget (the official cycle,
    a dashboard-triggered run, a second `--diagnostic-scan`) that this
    process cannot see. If headroom is ever insufficient, the phase
    stops BEFORE that batch starts -- every ticker in it, and every
    ticker not yet reached, is marked SKIPPED (never attempted, never
    counted against this run's own successful/failed-chain tally) and
    reported with `StructuralSuitability.NOT_EVALUATED` /
    `SkipReason.RATE_LIMIT_HEADROOM` -- never silently reclassified as
    a market-data failure or an UNSUITABLE ticker. A `TradierRateLimit
    Error` mid-symbol (the provider's own existing retries already
    exhausted) is isolated to that one symbol exactly like any other
    fetch failure -- this orchestration layer never adds a SECOND,
    outer retry on top of the provider's own.

    Phase 2 (correlation/history) begins only AFTER phase 1 completes
    or stops, and only for tickers phase 1 actually ATTEMPTED (a
    skipped ticker never gets a correlation attempt either -- it is
    NOT_EVALUATED overall). Its own pre-flight check
    (`rate_limit_safety.correlation_reserved_headroom_pct`, a larger
    reserve -- correlation is this study's lowest-priority phase)
    decides how many of those attempted tickers current headroom can
    still support; it may run for all of them, a subset (`usable_
    request_headroom`-based, never an outer retry here either), or
    none. A ticker phase 2 deliberately never asked about is
    `CorrelationSymbolStatus.SKIPPED_BUDGET` -- reported separately
    from, and never conflated with, `INSUFFICIENT_HISTORY` (a ticker
    phase 2 DID ask about, via the unmodified `resolve_price_history_
    for_correlation`, whose own per-ticker fetch did not yield enough
    aligned observations).

    `now` is `None` in every real invocation -- see
    `run_diagnostic_scan`'s own docstring for why this parameter exists
    (deterministic market-hours-gate testing only)."""
    _print_feasibility_banner()
    print()

    try:
        ops = load_operations_config()
        val_config = load_validation_config()
        feasibility_universe = load_universe(_FEASIBILITY_UNIVERSE_CONFIG_PATH)
        configured_strategy_names = load_universe_strategies(_FEASIBILITY_UNIVERSE_CONFIG_PATH)
        safety_config = load_rate_limit_safety_config(_FEASIBILITY_UNIVERSE_CONFIG_PATH)
    except (OperationsConfigError, ValidationConfigError, UniverseConfigError, RateLimitSafetyConfigError) as exc:
        print(f"FAIL: configuration error -- {exc}")
        print()
        _print_feasibility_banner()
        return False

    try:
        configured_strategies = tuple(StrategyType[name] for name in configured_strategy_names)
    except KeyError as exc:
        print(f"FAIL: configuration error -- config/universe_feasibility.yaml lists an unknown strategy: {exc}")
        print()
        _print_feasibility_banner()
        return False

    strategies = candidate_eligible_strategies(configured_strategies)
    skipped = [s.value for s in configured_strategies if s not in strategies]
    if skipped:
        _line("strategies configured but not yet candidate-generation-eligible (skipped)", skipped)

    limits = get_default_limits()

    try:
        verify_official_provider_is_tradier_production()
    except OfficialProviderPreflightError as exc:
        print(f"FAIL: provider preflight -- {exc}")
        print()
        _print_feasibility_banner()
        return False
    _line("provider preflight", "Tradier production market data configured")

    now = now or datetime.now(timezone.utc)

    eligibility = evaluate_validation_cycle_eligibility(
        now, scan_open_buffer_minutes=ops.scan_open_buffer_minutes, scan_close_buffer_minutes=ops.scan_close_buffer_minutes,
    )
    if not eligibility.validation_cycle_allowed:
        _line("market-hours gate", f"new-position scan window closed -- {eligibility.block_reason}")
        print(
            "FAIL: new-position scan window closed -- the feasibility study makes no market-data calls "
            "while this window is closed."
        )
        print()
        _print_feasibility_banner()
        return False
    _line("market-hours gate", f"{eligibility.market_session_state.value} -- new-position scan window open")

    try:
        portfolio = load_portfolio_read_only(ops.account_state_db_path, ops.account_id)
    except PortfolioLoadError as exc:
        print(f"FAIL: could not read portfolio state read-only -- {exc}")
        print()
        _print_feasibility_banner()
        return False
    if portfolio is None:
        _line(
            "no persisted portfolio for this account -- using an in-memory diagnostic portfolio (never saved)",
            f"starting NAV ${val_config.default_starting_nav:,.2f}",
        )
        portfolio = Portfolio(
            as_of=now, nav=val_config.default_starting_nav, cash=val_config.default_starting_nav,
            peak_equity=val_config.default_starting_nav,
        )
    else:
        _line("existing portfolio loaded read-only", f"{len(portfolio.positions)} open position(s), NAV ${portfolio.nav:,.2f}")

    broker_capabilities = load_broker_capabilities("internal_paper")
    quant_filter = QuantFilterConfig()

    universe_tickers = tuple(e.ticker for e in feasibility_universe)
    max_expirations = _DEFAULT_TRADIER_MAX_EXPIRATIONS
    _line(
        "expected Tradier request budget (LOGICAL requests before retries, worst case, this run)",
        expected_tradier_request_count(num_symbols=len(universe_tickers), max_expirations=max_expirations),
    )
    _line(
        "  -- option-scan phase (logical, before retries)",
        expected_opportunity_scan_request_count(num_symbols=len(universe_tickers), max_expirations=max_expirations),
    )
    _line(
        "  -- correlation phase (logical, before retries)",
        expected_correlation_request_count(num_symbols=len(universe_tickers)),
    )
    _line(
        "  -- actual outbound HTTP attempts can exceed this",
        "provider-level retries (up to 3 attempts per logical request on a transient failure/429) are not counted above",
    )
    _line(
        "batching policy (batch size / opportunity-scan reserve / correlation reserve)",
        f"{safety_config.batch_size} / {safety_config.reserved_headroom_pct:.0%} / {safety_config.correlation_reserved_headroom_pct:.0%}",
    )

    fetch_results: dict[str, OptionChain | Exception] = {}
    evaluated_tickers: list[str] = []
    skipped_tickers: list[str] = []
    skip_reason: SkipReason | None = None
    provider = get_configured_market_data_provider()
    price_history: dict[str, list[float]] = {}
    correlation_attempted: tuple[str, ...] = ()
    correlation_skipped: tuple[str, ...] = ()
    try:
        max_expirations = getattr(getattr(provider, "_config", None), "max_expirations", max_expirations)

        # Phase 1: option-chain opportunity scan, in small, strictly
        # sequential batches, with a stop-BEFORE-starting-the-next-
        # batch check driven ONLY by the provider's own observed
        # rate_limit_state -- see this function's own docstring for the
        # full bootstrap/stop-before-exhaustion reasoning.
        remaining = list(universe_tickers)
        while remaining:
            batch = remaining[: safety_config.batch_size]
            observed_state = getattr(provider, "rate_limit_state", None)
            projected = expected_opportunity_scan_request_count(num_symbols=len(batch), max_expirations=max_expirations)
            if not has_sufficient_observed_headroom(
                observed_state, projected_requests=projected, reserved_headroom_pct=safety_config.reserved_headroom_pct,
            ):
                skipped_tickers.extend(remaining)
                skip_reason = SkipReason.RATE_LIMIT_HEADROOM
                break
            remaining = remaining[safety_config.batch_size :]

            for ticker in batch:
                evaluated_tickers.append(ticker)
                try:
                    if isinstance(provider, DteWindowOptionChainProvider):
                        fetch_results[ticker] = await provider.get_option_chain_for_dte_window(
                            ticker, min_dte=quant_filter.min_dte, max_dte=quant_filter.max_dte, as_of=now.date(),
                        )
                    else:
                        fetch_results[ticker] = await provider.get_option_chain(ticker)
                except Exception as exc:  # noqa: BLE001 - one bad symbol never aborts the feasibility study; never retried a second time at this layer
                    fetch_results[ticker] = exc

        if skipped_tickers:
            _line(
                "opportunity-scan phase stopped early -- observed rate-limit headroom insufficient for the next batch",
                f"{len(skipped_tickers)} symbol(s) skipped (NOT_EVALUATED, never UNSUITABLE): {skipped_tickers}",
            )

        # Phase 2: correlation/history, a SEPARATE budget check, AFTER
        # phase 1 completes or stops, and ONLY for tickers phase 1
        # actually attempted. Deliberately a side channel --
        # resolve_price_history_for_correlation never mutates
        # `portfolio`, never sets `portfolio.risk_data_required`, and
        # is called regardless of `risk_data_wiring.enabled` (this
        # study reports on correlation; it never activates risk-data
        # wiring for the real Quant/Risk evaluation below).
        if isinstance(provider, HistoricalDataProvider) and evaluated_tickers:
            observed_state = getattr(provider, "rate_limit_state", None)
            headroom = usable_request_headroom(observed_state, reserved_headroom_pct=safety_config.correlation_reserved_headroom_pct)
            if headroom is None:
                affordable = len(evaluated_tickers)
            else:
                affordable = min(len(evaluated_tickers), headroom // max(1, expected_correlation_request_count(num_symbols=1)))
            correlation_attempted = tuple(evaluated_tickers[:affordable])
            correlation_skipped = tuple(evaluated_tickers[affordable:])
            if correlation_attempted:
                try:
                    price_history = await resolve_price_history_for_correlation(
                        frozenset(correlation_attempted), historical_provider=provider, now=now,
                        lookback_days=ops.correlation_lookback_days, min_observations=ops.min_correlation_observations,
                    )
                except Exception:  # noqa: BLE001 - correlation is observational; never aborts the study
                    price_history = {}
            if correlation_skipped:
                _line("correlation phase: skipped due to rate-limit headroom", list(correlation_skipped))
    finally:
        close = getattr(provider, "close", None)
        if close is not None:
            await close()

    evaluation_as_of = datetime.now(timezone.utc)
    _line("evaluation timestamp (captured after market-data fetch)", evaluation_as_of.isoformat())

    chains_by_ticker = {t: c for t, c in fetch_results.items() if isinstance(c, OptionChain)}
    failed = [t for t, c in fetch_results.items() if isinstance(c, Exception)]
    if failed:
        _line("symbols failed this feasibility study (isolated, study continues)", failed)

    diagnostics_by_ticker = {t: FunnelDiagnostics(ticker=t) for t in evaluated_tickers}
    sanitized_exceptions: list[tuple[str, str, str]] = []

    def _on_generation_exception(ticker: str, strategy: str, exc: Exception) -> None:
        sanitized_exceptions.append((ticker, strategy, _sanitize_diagnostic_exception_message(exc)))

    scan_result = scan_and_rank_opportunities(
        list(feasibility_universe), chains_by_ticker, list(strategies), quant_filter, limits, portfolio,
        ops.default_market_regime, broker_capabilities, now=evaluation_as_of,
        proposal_id_prefix="validation-scan", diagnostics_by_ticker=diagnostics_by_ticker,
        on_generation_exception=_on_generation_exception,
    )

    symbol_summaries = build_symbol_feasibility_summaries(
        evaluated_tickers=tuple(evaluated_tickers), chain_received_tickers=frozenset(chains_by_ticker.keys()),
        diagnostics_by_ticker=diagnostics_by_ticker, scan_result=scan_result,
    )
    candidate_details = build_candidate_feasibility_details(scan_result)
    aggregate = build_aggregate_feasibility_report(
        symbols_requested=len(universe_tickers), symbol_summaries=symbol_summaries,
        chain_received_tickers=frozenset(chains_by_ticker.keys()), scan_result=scan_result,
        skipped_tickers=tuple(skipped_tickers), skip_reason=skip_reason,
    )
    correlation_summary = build_correlation_feasibility_summary(
        correlation_attempted_tickers=correlation_attempted, correlation_skipped_tickers=correlation_skipped,
        price_history=price_history,
        high_correlation_pairs=tuple(
            (p.symbol_a, p.symbol_b, p.correlation)
            for p in (flag_highly_correlated_pairs(price_history, threshold=limits.high_correlation_threshold) if len(price_history) >= 2 else [])
        ),
    )

    print()
    print("  PER-SYMBOL DIAGNOSTICS")
    for s in symbol_summaries:
        structural = classify_structural_suitability(s)
        opportunity = classify_opportunity_today(s)
        print(f"  -- {s.ticker} -- structural_suitability: {structural.value} -- opportunity_today: {opportunity.value}")
        _line("    market-data success/chain usable", f"{s.market_data_successful}/{s.chain_usable}")
        _line("    contracts examined", s.contracts_seen)
        _line("    expirations seen/eligible/rejected", f"{s.expirations_seen}/{s.expirations_eligible}/{s.expirations_rejected}")
        _line("    strategy attempts", s.strategy_attempts)
        _line("    construction attempts/successes/rejections", f"{s.construction_attempts}/{s.construction_successes}/{s.construction_rejections}")
        _line("    generation exceptions", s.generation_exceptions)
        _line("    quant evaluations/passes/rejects", f"{s.quant_evaluations}/{s.quant_passed}/{s.quant_rejected}")
        _line("    risk evaluations/passes/rejects", f"{s.risk_evaluations}/{s.risk_passed}/{s.risk_rejected}")
        _line("    ranked candidates / clearing no-trade hurdle", f"{s.ranked_candidates}/{s.ranked_candidates_clearing_hurdle}")
        _line("    selected candidate (observational only)", s.selected_candidate_proposal_id or "none")
        if s.dominant_rejection_reasons:
            _line("    dominant rejection reasons", ", ".join(s.dominant_rejection_reasons))
    for ticker in skipped_tickers:
        print(f"  -- {ticker} -- structural_suitability: NOT_EVALUATED -- skip_reason: {skip_reason.value if skip_reason else 'unknown'}")
    for ticker, strategy, message in sanitized_exceptions:
        _line(f"  generation exception detail -- {ticker}/{strategy}", message)

    print()
    print("  RANKED-CANDIDATE ECONOMICS (OBSERVATIONAL ONLY -- NOT PERSISTED, NOT CONFIRMABLE)")
    for d in candidate_details:
        marker = " <== WOULD HAVE BEEN SELECTED" if d.would_have_been_selected else ""
        print(f"  -- {d.ticker} / {d.strategy} / exp {d.expiration} (DTE {d.dte}){marker}")
        _line("    legs (right/strike/side)", list(zip(d.leg_rights, d.leg_strikes, d.leg_sides)))
        _line("    target entry", d.target_entry)
        _line("    max profit/max loss/capital required", f"{d.max_profit}/{d.max_loss}/{d.capital_required}")
        _line("    contracts requested", d.contracts_requested)
        _line("    risk decision", d.risk_decision)
        _line("    risk-adjusted return vs. no-trade hurdle", f"{d.risk_adjusted_return}/{d.no_trade_hurdle}")

    print()
    print("  AGGREGATE FEASIBILITY REPORT")
    _line("  symbols requested", aggregate.symbols_requested)
    _line("  symbols evaluated/skipped", f"{aggregate.symbols_evaluated}/{aggregate.symbols_skipped}")
    if aggregate.skipped_tickers:
        _line("  skipped tickers (NOT_EVALUATED, never UNSUITABLE)", aggregate.skipped_tickers)
        _line("  skip reason", aggregate.skip_reason.value if aggregate.skip_reason else "unknown")
    _line("  successful/failed chains (among evaluated symbols only)", f"{aggregate.successful_chains}/{aggregate.failed_chains}")
    _line("  total contracts examined", aggregate.total_contracts_examined)
    _line("  total eligible expirations", aggregate.total_eligible_expirations)
    _line("  total strategy attempts", aggregate.total_strategy_attempts)
    _line("  total construction attempts/successes", f"{aggregate.total_construction_attempts}/{aggregate.total_construction_successes}")
    _line("  total quant passed", aggregate.total_quant_passed)
    _line("  total risk passed", aggregate.total_risk_passed)
    _line("  total ranked candidates", aggregate.total_ranked_candidates)
    _line("  total candidates that would clear the no-trade hurdle (observational -- never preferred for the permanent universe on this basis alone)", aggregate.total_would_clear_no_trade_hurdle)
    _line("  by-strategy ranked counts", aggregate.by_strategy)
    _line("  by-ticker ranked counts", aggregate.by_ticker_ranked)
    _line("  structural suitability counts (STRONG/ACCEPTABLE/WEAK/UNSUITABLE/NOT_EVALUATED)", aggregate.structural_suitability_counts)
    _line("  opportunity-today progression counts (observational only)", aggregate.opportunity_today_counts)
    if aggregate.top_construction_bottlenecks:
        _line("  top construction bottlenecks", aggregate.top_construction_bottlenecks)
    if aggregate.top_quant_rejection_reasons:
        _line("  top quant rejection reasons", aggregate.top_quant_rejection_reasons)
    if aggregate.top_risk_rejection_reasons:
        _line("  top risk rejection reasons", aggregate.top_risk_rejection_reasons)
    print(
        "  PERMANENT-UNIVERSE GUIDANCE: driven primarily by structural suitability and "
        "diversification/correlation (below) -- never by whether one day's candidate happened to "
        "rank or clear the no-trade hurdle."
    )

    print()
    print("  CORRELATION / DIVERSIFICATION ANALYSIS (READ-ONLY RESEARCH OUTPUT -- risk_data_wiring unchanged)")
    _line("  symbols with sufficient history", correlation_summary.symbols_with_sufficient_history)
    _line("  symbols with insufficient history", correlation_summary.symbols_with_insufficient_history)
    if correlation_summary.symbols_skipped_budget:
        _line(
            "  symbols skipped due to rate-limit headroom (never misclassified as insufficient history)",
            correlation_summary.symbols_skipped_budget,
        )
    if correlation_summary.high_correlation_pairs:
        for symbol_a, symbol_b, correlation in correlation_summary.high_correlation_pairs:
            _line("  high-correlation pair", f"{symbol_a}/{symbol_b} = {correlation:.2f}")
    elif len(correlation_summary.symbols_with_sufficient_history) >= 2:
        _line("  high-correlation pairs", "none at or above the configured threshold")
    else:
        _line("  high-correlation pairs", "not computable -- fewer than 2 symbols had sufficient aligned history")

    print()
    _print_feasibility_banner()
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
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--preflight",
        action="store_true",
        help=(
            "Run read-only readiness checks only (configuration, cohort existence, Tradier "
            "production provider configuration) and exit. Mutates NO validation state."
        ),
    )
    group.add_argument(
        "--diagnostic-scan",
        action="store_true",
        help=(
            "PAPER_TRADING_V1.5.13: run a READ-ONLY diagnostic opportunity scan against live "
            "Tradier production market data, using the exact same candidate -> Quant -> Risk "
            "pipeline as an official cycle, WITHOUT counting as a validation day and WITHOUT "
            "mutating any validation/candidate/trade/account/lifecycle state. For verifying a "
            "software fix (e.g. the PAPER_TRADING_V1.5.12 proposal_id fix) against live data "
            "after today's official cycle has already run. Mutually exclusive with --preflight."
        ),
    )
    group.add_argument(
        "--universe-feasibility",
        action="store_true",
        help=(
            "PAPER_TRADING_V1.5.14: run a READ-ONLY feasibility study of a WIDER research "
            "universe (config/universe_feasibility.yaml, 12 tickers) against live Tradier "
            "production market data, using the exact same candidate -> Quant -> Risk pipeline "
            "as an official cycle, WITHOUT counting as a validation day, WITHOUT persisting any "
            "candidate, and WITHOUT activating the expanded universe for the official cycle. "
            "Mutually exclusive with --preflight/--diagnostic-scan."
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

    if args.diagnostic_scan:
        try:
            ok = asyncio.run(run_diagnostic_scan())
        except Exception as exc:  # noqa: BLE001 - a genuinely systemic failure, reported plainly
            print(f"FAIL: diagnostic scan could not complete -- {exc!r}")
            return 1
        return 0 if ok else 1

    if args.universe_feasibility:
        try:
            ok = asyncio.run(run_universe_feasibility_study())
        except Exception as exc:  # noqa: BLE001 - a genuinely systemic failure, reported plainly
            print(f"FAIL: universe feasibility study could not complete -- {exc!r}")
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
