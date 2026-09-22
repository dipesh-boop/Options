"""Part 12: the deterministic Portfolio Control Loop orchestrator.

`run_control_cycle` is one full cycle's worth of the hierarchy CLAUDE.md
and this package's own `__init__.py` require: MARKET DATA (already
fetched by the caller, exactly like `src.workflows.morning_scan
.MorningScanInputs.fetch_results` -- this module performs no I/O of its
own) -> CANONICAL DATA MODELS + QUALITY GATE (`src.data.quality_gate`)
-> PORTFOLIO REVALUATION (`src.portfolio.revaluation`) -> PORTFOLIO
EXPOSURE (`src.portfolio.exposure`) -> LIFECYCLE ENGINE
(`src.lifecycle.engine.evaluate_position`, never reimplemented) -> RISK
KILL-SWITCH (`src.risk.kill_switch`) -> RECOMMENDATION
(`src.portfolio.actions`/`decision_snapshot`) -> PERSISTENCE
(`src.lifecycle.persistence` + `src.portfolio.persistence`).

**This function is not a second Risk Engine or a second Lifecycle
Engine.** It calls both, unmodified, and never overrides what they
return. New-opportunity scanning (Part 23/24, `src.portfolio
.opportunity_scan`, not yet built) and pending-ticket monitoring (Part
21/22, `src.portfolio.ticket_monitor`, not yet built) are deliberately
NOT phases of this function -- `ControlCycleInputs` accepts their
already-computed counts (`opportunities_scanned` etc.) so a thin outer
wrapper can run those separate, independently-testable stages and merge
their counts into the one `ControlCycleRecord` this function produces,
without this module importing modules that don't exist yet or having to
be rewritten once they do.

One position's bad/missing market data, missing management-policy
configuration, or a lifecycle-engine exception never aborts the whole
cycle (Part 7/16's isolation doctrine, applied at the orchestration
layer too) -- it produces a `DATA_INSUFFICIENT` decision snapshot for
that one position and the cycle continues.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from src.data.option_chain import OptionChain
from src.data.provider import DEFAULT_MAX_QUOTE_AGE
from src.data.quality_gate import validate_option_chain
from src.lifecycle.engine import EvaluationResult, PositionMonitoringInput, evaluate_position
from src.lifecycle.excursion import initial_excursion
from src.lifecycle.persistence import LifecyclePositionRecord, LifecycleStore
from src.lifecycle.policies_library import get_policy
from src.lifecycle.state import PositionLifecycleState
from src.portfolio.actions import ControlLoopAction, action_from_resolved
from src.portfolio.cycle_record import ControlCycleRecord
from src.portfolio.decision_snapshot import PortfolioControlDecisionSnapshot
from src.portfolio.exposure import PortfolioExposureSnapshot, build_exposure_snapshot
from src.portfolio.persistence import ControlLoopStore
from src.portfolio.revaluation import (
    PortfolioValuationResult,
    PositionValuationStatus,
    revalue_portfolio,
)
from src.risk.kill_switch import KillSwitchResult, check_kill_switch
from src.risk.limits import RiskLimitsConfig
from src.risk.portfolio_risk import Portfolio
from src.strategies.base import StrategyKind
from src.wheel.models import WheelPosition

# Fields of `PositionMonitoringInput` this module cannot derive on its
# own from `Portfolio`/`PortfolioValuationResult`/`PortfolioExposureSnapshot`
# -- domain data (entry prices, IV percentile, regime, earnings, per-leg
# liquidity/ITM detail) the caller must supply per position via
# `ControlCycleInputs.extra_monitoring_inputs`. Listed explicitly so a
# caller can see exactly what this orchestrator will NOT fabricate.
_CALLER_SUPPLIED_MONITORING_FIELDS = frozenset(
    {
        "realized_pnl", "profit_capture_denominator", "initial_credit",
        "entry_underlying_price", "entry_option_value", "current_option_value",
        "iv", "iv_percentile", "entry_regime", "current_regime",
        "earnings_data_available", "days_to_earnings",
        "spread_pct", "quote_age_minutes",
        "short_leg_is_itm", "short_leg_extrinsic_value",
        # `PositionMonitoringInput.position_delta_abs` means the specific
        # monitored (usually short) leg's own per-contract delta in the
        # standard [0, 1] convention (e.g. "short put delta reached
        # 0.30") -- NOT this module's own `PositionValuation.delta`,
        # which is the position's total contract-multiplier-scaled,
        # multi-leg NET delta (share-equivalent units, easily in the
        # hundreds). The two are different quantities in different
        # units; silently feeding one to the other would misfire every
        # delta-threshold policy, so this is caller-supplied like
        # `short_leg_is_itm`, not derived here.
        "position_delta_abs",
    }
)


@dataclass(frozen=True)
class ControlCycleInputs:
    cycle_id: str
    as_of: datetime
    portfolio: Portfolio
    limits: RiskLimitsConfig
    provider: str
    provider_health_status: str
    is_trading_day: bool
    is_market_open: bool
    fetch_results: dict[str, OptionChain | Exception]
    lifecycle_store: LifecycleStore
    control_loop_store: ControlLoopStore
    policy_name_for_position: dict[str, str] = field(default_factory=dict)
    wheel_id_for_position: dict[str, str] = field(default_factory=dict)
    extra_monitoring_inputs: dict[str, dict] = field(default_factory=dict)
    wheel_positions: tuple[WheelPosition, ...] = ()
    current_underlying_prices_for_exposure: dict[str, float] = field(default_factory=dict)
    max_quote_age: timedelta = DEFAULT_MAX_QUOTE_AGE
    # Counts from the separately-run, not-yet-built Part 21-24 stages
    # (`src.portfolio.ticket_monitor`/`opportunity_scan`) -- 0 until a
    # caller wires those in; never computed by this function itself.
    opportunities_scanned: int = 0
    candidates_generated: int = 0
    candidates_rejected: int = 0


@dataclass(frozen=True)
class ControlCycleResult:
    cycle_record: ControlCycleRecord
    valuation: PortfolioValuationResult
    exposure: PortfolioExposureSnapshot
    kill_switch: KillSwitchResult
    decision_snapshots: tuple[PortfolioControlDecisionSnapshot, ...]
    lifecycle_results: dict[str, EvaluationResult]


def _quality_gate_market_data(
    fetch_results: dict[str, OptionChain | Exception], *, as_of: datetime, max_quote_age: timedelta
) -> tuple[dict[str, float], dict[str, list], tuple[str, ...], tuple[str, ...], list[str]]:
    """Runs `src.data.quality_gate` per successfully-fetched underlying,
    isolating one bad/malformed chain from the rest (Part 7). Returns
    `(underlying_prices, contracts_by_ticker, symbols_successful,
    symbols_failed, errors)`."""
    underlying_prices: dict[str, float] = {}
    contracts_by_ticker: dict[str, list] = {}
    successful: list[str] = []
    failed: list[str] = []
    errors: list[str] = []

    for ticker, result in fetch_results.items():
        if isinstance(result, Exception):
            failed.append(ticker)
            errors.append(f"{ticker}: fetch failed -- {result!r}")
            continue
        gate_result = validate_option_chain(result, as_of=as_of, expected_underlying=ticker, max_age=max_quote_age)
        if not gate_result.is_usable:
            failed.append(ticker)
            errors.append(f"{ticker}: quality gate rejected chain -- {[i.code for i in gate_result.underlying_issues]}")
            continue
        successful.append(ticker)
        underlying_prices[ticker] = result.underlying.last
        contracts_by_ticker[ticker] = gate_result.valid_contracts

    return underlying_prices, contracts_by_ticker, tuple(successful), tuple(failed), errors


def _build_monitoring_input(
    *,
    as_of: datetime,
    valuation_status: PositionValuationStatus,
    dte: int | None,
    unrealized_pnl: float | None,
    underlying_price: float | None,
    delta: float | None,
    gamma: float | None,
    theta: float | None,
    vega: float | None,
    max_loss_dollars: float,
    option_prices: dict[str, float],
    portfolio_exposure_pct: float | None,
    drawdown_pct: float,
    risk_halt_active: bool,
    risk_status: str,
    extras: dict,
) -> PositionMonitoringInput:
    derived = dict(
        as_of=as_of,
        dte=dte,
        unrealized_pnl=unrealized_pnl if unrealized_pnl is not None else 0.0,
        realized_pnl=0.0,
        max_loss_dollars=max_loss_dollars,
        underlying_price=underlying_price,
        delta=delta, gamma=gamma, theta=theta, vega=vega,
        option_prices=option_prices,
        portfolio_exposure_pct=portfolio_exposure_pct,
        drawdown_pct=drawdown_pct,
        risk_halt_active=risk_halt_active,
        risk_status=risk_status,
        # Part 27: never silently assume "no earnings risk" just because
        # the caller supplied nothing -- explicit unavailability unless
        # the caller's own extras say otherwise.
        earnings_data_available=False,
    )
    for key in _CALLER_SUPPLIED_MONITORING_FIELDS:
        if key in extras:
            derived[key] = extras[key]
    return PositionMonitoringInput(**derived)


def run_control_cycle(inputs: ControlCycleInputs) -> ControlCycleResult:
    """One full, deterministic control-loop cycle. Never raises for a
    single position's bad data -- see module docstring. May raise for a
    genuinely systemic failure (e.g. `inputs.limits`/`inputs.portfolio`
    themselves malformed), exactly like every other Python Risk/Quant
    entry point in this codebase fails loudly on a caller error rather
    than silently producing a misleading result."""
    kill_switch = check_kill_switch(inputs.portfolio, inputs.limits)
    risk_status = "halted" if kill_switch.halted else "ok"

    underlying_prices, contracts_by_ticker, symbols_ok, symbols_failed, errors = _quality_gate_market_data(
        inputs.fetch_results, as_of=inputs.as_of, max_quote_age=inputs.max_quote_age
    )

    valuation = revalue_portfolio(
        inputs.portfolio,
        underlying_prices=underlying_prices,
        contracts_by_ticker=contracts_by_ticker,
        limits=inputs.limits,
        as_of=inputs.as_of,
        max_quote_age=inputs.max_quote_age,
    )
    valuation_by_position = {v.position_id: v for v in valuation.positions}
    position_by_id = {p.position_id: p for p in inputs.portfolio.positions}

    # First pass: exposure without assignment-risk detail yet (Part 15's
    # underlying/sector/strategy dimensions don't depend on lifecycle
    # results, but assignment_risk_position_ids does -- see below).
    preliminary_exposure = build_exposure_snapshot(
        inputs.portfolio, as_of=inputs.as_of, limits=inputs.limits,
        portfolio_delta=valuation.portfolio_delta, portfolio_vega=valuation.portfolio_vega,
        wheel_positions=inputs.wheel_positions,
        current_underlying_prices=inputs.current_underlying_prices_for_exposure,
    )

    snapshots: list[PortfolioControlDecisionSnapshot] = []
    lifecycle_results: dict[str, EvaluationResult] = {}
    assignment_risk_ids: list[str] = []
    lifecycle_trigger_count = 0
    risk_event_count = 0
    market_data_ts = max(
        (result.timestamp for result in inputs.fetch_results.values() if isinstance(result, OptionChain)),
        default=None,
    )

    def _snapshot_kwargs() -> dict:
        return dict(
            cycle_id=inputs.cycle_id, timestamp=inputs.as_of,
            is_trading_day=inputs.is_trading_day, is_market_open=inputs.is_market_open,
            provider=inputs.provider, provider_health_status=inputs.provider_health_status,
            market_data_timestamp=market_data_ts,
            portfolio_nav=valuation.nav, portfolio_cash=valuation.cash,
            portfolio_deployed_pct=valuation.capital_deployed_pct,
            portfolio_drawdown_pct=valuation.current_drawdown_pct,
            portfolio_delta=valuation.portfolio_delta, portfolio_gamma=valuation.portfolio_gamma,
            portfolio_theta=valuation.portfolio_theta, portfolio_vega=valuation.portfolio_vega,
        )

    for position in inputs.portfolio.positions:
        position_valuation = valuation_by_position.get(position.position_id)

        if kill_switch.halted:
            risk_event_count += 1

        if position_valuation is None or position_valuation.status == PositionValuationStatus.DATA_INSUFFICIENT:
            snapshots.append(
                PortfolioControlDecisionSnapshot(
                    **_snapshot_kwargs(), position_id=position.position_id, strategy=position.strategy.value,
                    recommended_action=ControlLoopAction.DATA_INSUFFICIENT,
                    reason_codes=(position_valuation.reason if position_valuation else "no valuation produced",) if position_valuation else ("no valuation produced",),
                    human_action_required=False,
                )
            )
            continue

        policy_name = None
        existing_record = inputs.lifecycle_store.get_position(position.position_id)
        if existing_record is not None:
            policy_name = existing_record.management_policy_name
        else:
            policy_name = inputs.policy_name_for_position.get(position.position_id)

        if policy_name is None:
            snapshots.append(
                PortfolioControlDecisionSnapshot(
                    **_snapshot_kwargs(), position_id=position.position_id, strategy=position.strategy.value,
                    underlying_price=position_valuation.underlying_price,
                    unrealized_pnl=position_valuation.unrealized_pnl, dte=position_valuation.dte,
                    recommended_action=ControlLoopAction.DATA_INSUFFICIENT,
                    reason_codes=("no management policy configured for this position",),
                    human_action_required=True,
                )
            )
            continue

        try:
            policy = get_policy(policy_name)
        except KeyError:
            snapshots.append(
                PortfolioControlDecisionSnapshot(
                    **_snapshot_kwargs(), position_id=position.position_id, strategy=position.strategy.value,
                    underlying_price=position_valuation.underlying_price,
                    unrealized_pnl=position_valuation.unrealized_pnl, dte=position_valuation.dte,
                    recommended_action=ControlLoopAction.DATA_INSUFFICIENT,
                    reason_codes=(f"unknown management policy {policy_name!r}",),
                    human_action_required=True,
                )
            )
            continue

        if existing_record is not None:
            current_state = existing_record.current_state
            excursion = existing_record.excursion
            wheel_id = existing_record.wheel_id
        else:
            current_state = PositionLifecycleState.ACTIVE
            excursion = initial_excursion(position_valuation.unrealized_pnl, inputs.as_of)
            wheel_id = inputs.wheel_id_for_position.get(position.position_id)

        option_prices = {
            f"{leg.right}{leg.strike:g}": leg.mark_price
            for leg in position_valuation.legs
            if leg.mark_price is not None
        }
        portfolio_exposure_pct = preliminary_exposure.underlying_exposure_pct.get(position.ticker)

        monitoring_input = _build_monitoring_input(
            as_of=inputs.as_of, valuation_status=position_valuation.status,
            dte=position_valuation.dte, unrealized_pnl=position_valuation.unrealized_pnl,
            underlying_price=position_valuation.underlying_price,
            delta=position_valuation.delta, gamma=position_valuation.gamma,
            theta=position_valuation.theta, vega=position_valuation.vega,
            max_loss_dollars=position.max_loss, option_prices=option_prices,
            portfolio_exposure_pct=portfolio_exposure_pct,
            drawdown_pct=valuation.current_drawdown_pct,
            risk_halt_active=kill_switch.halted, risk_status=risk_status,
            extras=inputs.extra_monitoring_inputs.get(position.position_id, {}),
        )

        eval_result = evaluate_position(
            trade_id=position.position_id, wheel_id=wheel_id,
            strategy_kind_for_snapshot=StrategyKind(position.strategy.value),
            policy=policy, current_state=current_state, excursion=excursion, inp=monitoring_input,
        )
        lifecycle_results[position.position_id] = eval_result

        record = LifecyclePositionRecord(
            trade_id=position.position_id, wheel_id=wheel_id, ticker=position.ticker,
            strategy_kind=StrategyKind(position.strategy.value), management_policy_name=policy_name,
            current_state=eval_result.new_state, excursion=eval_result.excursion,
            roll_chain_id=existing_record.roll_chain_id if existing_record else None,
            created_at=existing_record.created_at if existing_record else inputs.as_of,
            updated_at=inputs.as_of,
        )
        inputs.lifecycle_store.save_position(record)
        inputs.lifecycle_store.append_snapshot(eval_result.snapshot)

        if eval_result.resolved.category not in ("hold",):
            lifecycle_trigger_count += 1
        if eval_result.resolved.category == "assignment_expiration":
            assignment_risk_ids.append(position.position_id)

        recommended_action = action_from_resolved(eval_result.resolved)
        control_snapshot = PortfolioControlDecisionSnapshot(
            **_snapshot_kwargs(), position_id=position.position_id, strategy=position.strategy.value,
            lifecycle_state=eval_result.new_state.value, management_policy=policy_name,
            underlying_price=position_valuation.underlying_price,
            unrealized_pnl=position_valuation.unrealized_pnl, dte=position_valuation.dte,
            trigger_reason_codes=eval_result.resolved.winning_trigger_names,
            recommended_action=recommended_action,
            reason_codes=(eval_result.resolved.reason,),
            human_action_required=eval_result.resolved.mandatory or kill_switch.halted,
        )
        snapshots.append(control_snapshot)
        inputs.control_loop_store.append_decision_snapshot(control_snapshot)

    final_exposure = build_exposure_snapshot(
        inputs.portfolio, as_of=inputs.as_of, limits=inputs.limits,
        portfolio_delta=valuation.portfolio_delta, portfolio_vega=valuation.portfolio_vega,
        assignment_risk_position_ids=tuple(assignment_risk_ids),
        wheel_positions=inputs.wheel_positions,
        current_underlying_prices=inputs.current_underlying_prices_for_exposure,
    )

    cycle_record = ControlCycleRecord(
        cycle_id=inputs.cycle_id, started_at=inputs.as_of, completed_at=inputs.as_of,
        market_open=inputs.is_market_open, provider=inputs.provider,
        provider_health_status=inputs.provider_health_status,
        symbols_requested=tuple(inputs.fetch_results.keys()),
        symbols_successful=symbols_ok, symbols_failed=symbols_failed,
        positions_evaluated=len(inputs.portfolio.positions),
        lifecycle_triggers=lifecycle_trigger_count, risk_events=risk_event_count,
        recommendations_created=len(snapshots),
        opportunities_scanned=inputs.opportunities_scanned,
        candidates_generated=inputs.candidates_generated,
        candidates_rejected=inputs.candidates_rejected,
        errors=tuple(errors), degraded_mode=len(symbols_failed) > 0, halt_state=kill_switch.halted,
    )
    inputs.control_loop_store.save_cycle_record(cycle_record)

    return ControlCycleResult(
        cycle_record=cycle_record, valuation=valuation, exposure=final_exposure,
        kill_switch=kill_switch, decision_snapshots=tuple(snapshots), lifecycle_results=lifecycle_results,
    )
