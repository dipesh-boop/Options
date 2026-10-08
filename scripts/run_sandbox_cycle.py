#!/usr/bin/env python3
"""PAPER_TRADING_V1.5.15: daily cycle runner for the Expanded-Universe
Sandbox -- a strictly isolated, parallel research environment that
exercises the full paper-trading lifecycle against a WIDER 12-symbol
universe than the official SPY/QQQ cohort, using the exact same Quant/
Risk/DTE/liquidity/sizing/ranking/no-trade standards as
`scripts/run_validation_cycle.py`. Universe breadth is the ONLY
experimental variable this sandbox tests.

**This is NOT the official 90-day validation cycle.** It never reads,
writes, or mutates `data/options_agent.db`, the official cohort
`paper-trading-v1.4.3-validation-2026-09-22`, or `config/universe.yaml`
-- see `src.portfolio.sandbox_guard.assert_path_is_not_official_database`,
called BEFORE any config is loaded, any store is constructed, or any
provider call is made, and `src.portfolio.sandbox_identity` for the
sandbox's own hardwired identity.

**Reuses the exact same engine functions the official cycle reuses --
never a second, divergent trading-engine implementation.** Candidate
generation/ranking (`src.portfolio.orchestrator.run_outer_cycle` /
`OpportunityScanConfig`, which internally calls
`src.portfolio.opportunity_scan.scan_and_rank_opportunities`), the Risk
Engine (`src.risk.engine.evaluate_trade_proposal`), existing-position
retrieval and TTL/lifecycle helpers
(`src.portfolio.cycle_helpers.fetch_existing_position_chain` /
`expire_stale_candidates` / `run_lifecycle_only_safety_check`), and
`PaperBroker` are the IDENTICAL functions/classes the official cycle
calls -- only the identity (cohort/account/database paths), the
universe (`config/universe_sandbox.yaml`), and the cycle-id namespace
(`sandbox-validation-...`, never `validation-...`) differ.

**Never calls `PaperBroker`'s own order-placement method for a new
position.** Exactly like the official cycle, this script identifies at
most one Risk-Engine-approved new-position candidate per day and
persists it as an immutable, `AWAITING_HUMAN` review record in the
SANDBOX's own candidate-review store. A separate, explicit operator
command, `scripts/confirm_sandbox_candidate.py <candidate-id>`, is the
only place that may authorize a simulated fill for a sandbox candidate
-- and it reuses `src.review.confirmation.confirm_candidate` completely
unmodified, pointed at sandbox stores. No real or simulated LLM review
occurs anywhere in this path.

**Never starts a new cohort.** This script only ever *reads* the
sandbox cohort/identity named in `src.portfolio.sandbox_identity` and
refuses to proceed if that cohort's `CohortRecord` has not already been
created by `scripts/init_expanded_universe_sandbox.py`
(`src.validation.cohort.start_new_cohort`/`has_cohort_started` are not
used here: unlike the official cohort -- started out-of-band on the
operator's own machine, with a Day-1 snapshot already recorded before
any automated cycle ever ran -- the sandbox's *initializer* is the
designated, in-repo mechanism that creates its `CohortRecord`, and this
script's very first cycle legitimately runs before any sandbox snapshot
exists yet. Checking `get_cohort(...) is not None` is therefore the
correct readiness test here, not "has a snapshot/trade/violation ever
been recorded").

**Requires Tradier production market data, exactly like the official
cycle.** `verify_official_provider_is_tradier_production` (a
configuration-only check of the single, global, process-wide
`OPTIONS_AGENT_DATA_PROVIDER`/Tradier-token configuration -- it is not
cohort-specific, so reusing it verbatim here enforces the sandbox's own
"same market-data standards" mission honestly, never a second,
separately-maintained check that could silently drift).

Usage:
    python scripts/run_sandbox_cycle.py              # run one sandbox cycle
    python scripts/run_sandbox_cycle.py --preflight   # read-only readiness check

Exit codes: 0 = cycle ran (even if degraded, or no candidate found), was
already done this hour/day, or `--preflight` reported ready. 1 = could
not start or complete the cycle/preflight at all (safety-gate failure,
config error, sandbox not initialized, provider not Tradier production,
database unreachable). 2 = bad CLI usage.
"""
from __future__ import annotations

import argparse
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

from src.data.factory import (  # noqa: E402
    OfficialProviderPreflightError,
    get_configured_market_data_provider,
    verify_official_provider_is_tradier_production,
)
from src.data.historical import HistoricalDataProvider  # noqa: E402
from src.data.market_calendar import is_market_open, is_trading_day  # noqa: E402
from src.data.option_chain import OptionChain, merge_option_chains  # noqa: E402
from src.data.provider import DteWindowOptionChainProvider, DteWindowSelectionDiagnostics  # noqa: E402
from src.data.universe import UniverseConfigError, load_universe, load_universe_strategies  # noqa: E402
from src.lifecycle.persistence import SqliteLifecycleStore  # noqa: E402
from src.lifecycle.policies_library import policies_for_strategy  # noqa: E402
from src.llm.schemas import StrategyType  # noqa: E402
from src.brokers.base import SqliteIdempotencyStore  # noqa: E402
from src.brokers.paper import PaperBroker  # noqa: E402
from src.portfolio.account_state import SqlitePaperAccountStateStore, SqlitePortfolioStore  # noqa: E402
from src.portfolio.cycle_helpers import (  # noqa: E402
    expire_stale_candidates as _expire_stale_candidates,
    fetch_existing_position_chain as _fetch_existing_position_chain,
    line as _line,
    run_lifecycle_only_safety_check as _run_lifecycle_only_safety_check,
)
from src.portfolio.market_session import evaluate_validation_cycle_eligibility  # noqa: E402
from src.portfolio.orchestrator import OpportunityScanConfig, OuterCycleInputs, run_outer_cycle  # noqa: E402
from src.portfolio.persistence import SqliteControlLoopStore  # noqa: E402
from src.portfolio.risk_data import apply_risk_data_wiring  # noqa: E402
from src.portfolio.sandbox_identity import (  # noqa: E402
    SANDBOX_ACCOUNT_ID,
    SANDBOX_COHORT_ID,
    SANDBOX_CYCLE_ID_PREFIX,
    SANDBOX_STARTING_NAV,
    SANDBOX_UNIVERSE_CONFIG_PATH,
    load_sandbox_operations_config,
)
from src.review.candidates import ReviewedCandidate, SqliteCandidateReviewStore  # noqa: E402
from src.risk.broker_constraints import load_broker_capabilities  # noqa: E402
from src.risk.engine import evaluate_trade_proposal  # noqa: E402
from src.risk.limits import get_default_limits  # noqa: E402
from src.risk.portfolio_risk import Portfolio, PortfolioPosition, sector_exposure_pct, underlying_exposure_pct  # noqa: E402
from src.strategies.base import StrategyKind  # noqa: E402
from src.validation.session import DailySnapshot, SqliteValidationStore  # noqa: E402
from src.workflows.candidate_generation import QuantFilterConfig, candidate_eligible_strategies  # noqa: E402

_BANNER = (
    "=================================================================\n"
    "EXPANDED-UNIVERSE SANDBOX -- DAILY CYCLE RUNNER\n"
    "NOT OFFICIAL VALIDATION -- RESULTS NEVER COUNT TOWARD THE 90-DAY\n"
    "OFFICIAL VALIDATION COHORT.\n"
    "PAPERBROKER SIMULATION ONLY. NEVER CALLS THE BROKER'S OWN\n"
    "ORDER-PLACEMENT METHOD FOR A NEW POSITION -- HUMAN CONFIRMATION\n"
    "VIA scripts/confirm_sandbox_candidate.py IS ALWAYS REQUIRED FIRST.\n"
    "================================================================="
)


def _print_banner() -> None:
    print(_BANNER)


def _print_ranked_candidate_audit(scan) -> None:
    """PAPER_TRADING_V1.5.15, Section H observability: durably prints
    (captured by whatever the operator redirects this script's own
    stdout to, the exact same convention this script's candidate-
    funnel block below already uses) every candidate the opportunity
    scan actually ranked this cycle -- not merely the single `best`
    one -- so an operator can answer "why did this candidate not
    become a trade" without re-running live market data.

    **Purely additive, read-only observation of data `scan_and_rank_
    opportunities` (`src.portfolio.opportunity_scan`, completely
    unmodified) already computes and returns on `OpportunityScanResult
    .scanned` every cycle, for both this sandbox and the official
    cycle alike -- neither path has ever persisted or printed this
    per-candidate detail before now. This function changes NONE of
    that computation: no scoring formula, no hurdle, no Risk Engine
    rule. Deliberately does NOT re-run `evaluate_trade_proposal` per
    candidate to also recover `approved_order.estimated_credit_debit`
    for display (unlike the single `best` candidate above, which this
    script already reprices once to build its durable
    `ReviewedCandidate`) -- doing that for every scanned candidate on
    every cycle would mean extra Risk Engine calls purely for display.
    `max_profit`/`max_loss`/`capital_required`/`risk_adjusted_return`/
    `risk_reason` already answer "why did this candidate not become a
    trade" without it.

    **Sandbox-only, print-based, not a new persistence layer.** This
    codebase's own validation/candidate-review stores are keyed
    one-row-per-candidate-id (`ReviewedCandidate`) or one-row-per-
    cohort-day (`DailySnapshot`) -- neither shape fits "every ranked
    candidate from every cycle," and inventing a new durable table for
    it would be the kind of shared-architecture change this release's
    own task specification explicitly says to avoid rather than risk
    contaminating the official path with. Print output, captured in
    whatever log file the operator already redirects this script's
    stdout to (the same convention the candidate-funnel diagnostics
    below already rely on), is this release's deliberate, narrower
    choice -- documented explicitly, never silently assumed."""
    if scan is None:
        return
    if not scan.scanned:
        _line("ranked candidate audit", "no candidate reached ranking this cycle")
        return

    _line("ranked candidate audit -- candidates ranked this cycle", len(scan.scanned))
    ranked = sorted(
        scan.scanned, key=lambda c: (c.risk_adjusted_return is None, -(c.risk_adjusted_return or 0.0)),
    )
    for rank, sc in enumerate(ranked, start=1):
        proposal = sc.candidate.proposal
        legs_summary = ", ".join(f"{leg.side.value} {leg.right.value} {leg.strike}" for leg in proposal.legs)
        qa = sc.quantitative_analysis
        is_best = scan.best is not None and sc.candidate.proposal.proposal_id == scan.best.candidate.proposal.proposal_id
        print(
            f"  #{rank}{' (SELECTED)' if is_best else ''}: {proposal.ticker} {proposal.strategy.value} "
            f"exp={proposal.expiration.isoformat()} legs=[{legs_summary}]"
        )
        if qa is not None:
            print(
                f"      max_profit=${qa.max_profit:,.2f} max_loss=${qa.max_loss:,.2f} "
                f"capital_required=${qa.capital_required:,.2f} "
                f"risk_adjusted_return={sc.risk_adjusted_return if sc.risk_adjusted_return is not None else 'n/a'}"
            )
        else:
            print("      quant pricing did not succeed for this candidate -- no economics available")
        print(f"      risk_decision={sc.risk_decision.value} reason={sc.risk_reason}")
    if scan.no_trade_reason is not None:
        print(f"  NO-TRADE HURDLE: {scan.no_trade_reason}")


async def run_sandbox_cycle(*, now: datetime | None = None) -> bool:
    """Mirrors `scripts.run_validation_cycle.run_validation_cycle`'s own
    structure and safety ordering exactly, with sandbox identity/
    universe/config substituted throughout -- see this module's own
    docstring for the complete list of what differs and what does not.
    `now` is `None` in every real invocation; the parameter exists
    solely so the market-hours gate can be exercised deterministically
    by tests, exactly like the official runner's own `now` parameter."""
    _print_banner()
    print()

    try:
        ops = load_sandbox_operations_config()
        universe = load_universe(SANDBOX_UNIVERSE_CONFIG_PATH)
        configured_strategy_names = load_universe_strategies(SANDBOX_UNIVERSE_CONFIG_PATH)
    except UniverseConfigError as exc:
        print(f"FAIL: sandbox universe config error -- {exc}")
        return False
    except Exception as exc:  # noqa: BLE001 - a genuinely systemic failure, reported plainly
        print(f"FAIL: could not load sandbox configuration -- {exc!r}")
        return False

    try:
        configured_strategies = tuple(StrategyType[name] for name in configured_strategy_names)
    except KeyError as exc:
        print(f"FAIL: configuration error -- config/universe_sandbox.yaml lists an unknown strategy: {exc}")
        return False

    strategies = candidate_eligible_strategies(configured_strategies)
    skipped = [s.value for s in configured_strategies if s not in strategies]
    if skipped:
        _line("strategies configured but not yet candidate-generation-eligible (skipped)", skipped)

    limits = get_default_limits()
    validation_store = SqliteValidationStore(ops.account_state_db_path)

    # This sandbox's own initializer (never this script) is the ONLY
    # place that creates the sandbox CohortRecord -- see this module's
    # own docstring for why `has_cohort_started` (the official runner's
    # own check) is the wrong test here.
    if validation_store.get_cohort(SANDBOX_COHORT_ID) is None:
        print(
            f"FAIL: sandbox cohort {SANDBOX_COHORT_ID!r} does not exist in {ops.account_state_db_path!r} -- "
            "run scripts/init_expanded_universe_sandbox.py first. This runner never creates a cohort itself."
        )
        return False

    # Same provider CONFIGURATION preflight as the official cycle --
    # read-only, no network call, no provider instance constructed.
    try:
        verify_official_provider_is_tradier_production()
    except OfficialProviderPreflightError as exc:
        print(f"FAIL: provider preflight -- {exc}")
        return False
    _line("provider preflight", "Tradier production market data configured")

    now = now or datetime.now(timezone.utc)
    eligibility = evaluate_validation_cycle_eligibility(
        now, scan_open_buffer_minutes=ops.scan_open_buffer_minutes, scan_close_buffer_minutes=ops.scan_close_buffer_minutes,
    )

    control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
    lifecycle_store = SqliteLifecycleStore(ops.lifecycle_db_path)
    review_store = SqliteCandidateReviewStore(ops.candidate_review_db_path)
    account_state_store = SqlitePaperAccountStateStore(ops.account_state_db_path)
    portfolio_store = SqlitePortfolioStore(ops.account_state_db_path)

    cycle_id = f"{SANDBOX_CYCLE_ID_PREFIX}-{now.date().isoformat()}"
    _line("cycle_id", cycle_id)

    main_cycle_already_ran = control_loop_store.get_cycle_record(cycle_id) is not None

    _expire_stale_candidates(review_store, SANDBOX_COHORT_ID, validation_store, now)

    existing_portfolio = portfolio_store.get(SANDBOX_ACCOUNT_ID)
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
            provider=get_configured_market_data_provider(), cycle_id_prefix=SANDBOX_CYCLE_ID_PREFIX,
        )

    _line("market-hours gate", f"{eligibility.market_session_state.value} -- new-position scan window open")

    if main_cycle_already_ran:
        block_reason = f"cycle {cycle_id!r} already completed today's new-position scan"
        print(f"Cycle {cycle_id!r} already ran today -- new-position scanning stays at most once per trading day.")
        if not existing_positions:
            print("No existing positions -- nothing further to do this invocation.")
            return True
        return await _run_lifecycle_only_safety_check(
            now=now, block_reason=block_reason, limits=limits, portfolio=existing_portfolio,
            lifecycle_store=lifecycle_store, control_loop_store=control_loop_store,
            provider=get_configured_market_data_provider(), cycle_id_prefix=SANDBOX_CYCLE_ID_PREFIX,
        )

    portfolio = existing_portfolio
    if portfolio is None:
        _line(
            "bootstrapping Portfolio for a never-before-seen sandbox account",
            f"{SANDBOX_ACCOUNT_ID!r} at starting NAV ${SANDBOX_STARTING_NAV:,.2f}",
        )
        portfolio = Portfolio(
            as_of=now, nav=SANDBOX_STARTING_NAV, cash=SANDBOX_STARTING_NAV, peak_equity=SANDBOX_STARTING_NAV,
        )
        portfolio_store.save(SANDBOX_ACCOUNT_ID, portfolio)

    idempotency_store = SqliteIdempotencyStore(ops.account_state_db_path)
    broker = PaperBroker(
        initial_cash=SANDBOX_STARTING_NAV, account_id=SANDBOX_ACCOUNT_ID,
        idempotency_store=idempotency_store, now=now,
    )
    account_state = account_state_store.get(SANDBOX_ACCOUNT_ID)
    if account_state is not None:
        broker.restore_state(account_state)
    else:
        account_state_store.save(broker.export_state())

    broker_capabilities = load_broker_capabilities("internal_paper")

    provider = get_configured_market_data_provider()
    historical_provider: HistoricalDataProvider | None = (
        provider if isinstance(provider, HistoricalDataProvider) else None
    )
    try:
        portfolio = await apply_risk_data_wiring(
            portfolio, universe=universe, enabled=ops.risk_data_wiring_enabled, historical_provider=historical_provider,
            now=now, lookback_days=ops.correlation_lookback_days, min_observations=ops.min_correlation_observations,
        )

        quant_filter = QuantFilterConfig()
        positions_by_ticker: dict[str, list[PortfolioPosition]] = {}
        for p in portfolio.positions:
            positions_by_ticker.setdefault(p.ticker, []).append(p)
        position_tickers = set(positions_by_ticker)
        universe_tickers = {e.ticker for e in universe}
        tickers = sorted(position_tickers | universe_tickers)
        fetch_results: dict[str, OptionChain | Exception] = {}
        dte_selection_diagnostics: dict[str, DteWindowSelectionDiagnostics] = {}
        for ticker in tickers:
            try:
                if ticker in universe_tickers and isinstance(provider, DteWindowOptionChainProvider):
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
                    fetch_results[ticker] = await _fetch_existing_position_chain(
                        provider, positions_by_ticker[ticker], now=now,
                    )
                else:
                    fetch_results[ticker] = await provider.get_option_chain(ticker)
            except Exception as exc:  # noqa: BLE001 - one bad symbol never aborts the cycle
                fetch_results[ticker] = exc
    finally:
        close = getattr(provider, "close", None)
        if close is not None:
            await close()

    # PAPER_TRADING_V1.5.7's post-fetch-timestamp fix applies equally
    # here -- see the official runner's own comment for why.
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
            broker_capabilities=broker_capabilities, proposal_id_prefix="sandbox-validation-scan",
            collect_candidate_funnel=True, evaluation_as_of=evaluation_as_of,
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
            full_risk = evaluate_trade_proposal(
                best.candidate.proposal, portfolio, best.quantitative_analysis, chain,
                broker_capabilities, limits=limits, now=evaluation_as_of,
            )
            policy_name = policies_for_strategy(StrategyKind(best.candidate.proposal.strategy.value))[0].name
            candidate = ReviewedCandidate(
                candidate_id=candidate_id, cohort_id=SANDBOX_COHORT_ID, cycle_id=cycle_id, created_at=now,
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
            print(f"\nNEW SANDBOX CANDIDATE AWAITING HUMAN REVIEW: {candidate_id}")
            print(f"  To authorize (PaperBroker simulation only, never live): python scripts/confirm_sandbox_candidate.py {candidate_id}")

    _print_ranked_candidate_audit(scan)

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
    validation_store.record_snapshot(snapshot, cohort_id=SANDBOX_COHORT_ID)
    _line("daily snapshot recorded -- nav/cash", f"${valuation.nav:,.2f} / ${valuation.cash:,.2f}")

    portfolio_store.save(SANDBOX_ACCOUNT_ID, portfolio)
    account_state_store.save(broker.export_state())

    if result.new_alerts:
        _line("new alerts this cycle", len(result.new_alerts))

    print("\nPASS: sandbox cycle complete. No new PaperBroker position was opened from this path.")
    return True


async def run_sandbox_preflight() -> bool:
    """Read-only sandbox readiness check -- same checks
    `run_sandbox_cycle` performs before its own first mutating step, run
    here in isolation and never followed by anything that could mutate
    sandbox state."""
    _print_banner()
    print("\nPREFLIGHT -- read-only, mutates NO sandbox state.\n")

    try:
        ops = load_sandbox_operations_config()
        load_universe(SANDBOX_UNIVERSE_CONFIG_PATH)
        configured_strategy_names = load_universe_strategies(SANDBOX_UNIVERSE_CONFIG_PATH)
    except UniverseConfigError as exc:
        print(f"FAIL: sandbox universe config error -- {exc}")
        return False
    except Exception as exc:  # noqa: BLE001 - a genuinely systemic failure, reported plainly
        print(f"FAIL: could not load sandbox configuration -- {exc!r}")
        return False

    try:
        tuple(StrategyType[name] for name in configured_strategy_names)
    except KeyError as exc:
        print(f"FAIL: configuration error -- config/universe_sandbox.yaml lists an unknown strategy: {exc}")
        return False
    _line("configuration", "loaded OK")

    validation_store = SqliteValidationStore(ops.account_state_db_path)
    if validation_store.get_cohort(SANDBOX_COHORT_ID) is None:
        print(f"FAIL: sandbox cohort {SANDBOX_COHORT_ID!r} does not exist -- run the initializer first.")
        return False
    _line("sandbox cohort", f"{SANDBOX_COHORT_ID!r} -- already initialized, as required")

    try:
        verify_official_provider_is_tradier_production()
    except OfficialProviderPreflightError as exc:
        print(f"FAIL: provider preflight -- {exc}")
        return False
    _line("provider", "tradier (production endpoint, token configured)")

    print("\nPREFLIGHT ONLY -- NO SANDBOX STATE MUTATED")
    print("PASS: ready for a sandbox cycle (python scripts/run_sandbox_cycle.py).")
    return True


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_sandbox_cycle.py",
        description=(
            "Daily cycle runner for the PAPER_TRADING_V1.5.15 Expanded-Universe Sandbox. With no "
            "arguments, runs one sandbox cycle against Tradier production market data. Never "
            "calls the simulated broker's own order-placement method for a new position -- see "
            "the module docstring for the full safety guarantees. NEVER touches official "
            "validation state."
        ),
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Run read-only readiness checks only and exit. Mutates NO sandbox state.",
    )
    return parser


def main() -> int:
    parser = _build_arg_parser()
    args = parser.parse_args()

    if args.preflight:
        try:
            ok = asyncio.run(run_sandbox_preflight())
        except Exception as exc:  # noqa: BLE001 - a genuinely systemic failure, reported plainly
            print(f"FAIL: sandbox preflight could not complete -- {exc!r}")
            return 1
        return 0 if ok else 1

    try:
        ok = asyncio.run(run_sandbox_cycle())
    except Exception as exc:  # noqa: BLE001 - a genuinely systemic failure, reported plainly
        print(f"FAIL: sandbox cycle could not complete -- {exc!r}")
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
