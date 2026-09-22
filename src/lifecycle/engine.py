"""The main Strategy Lifecycle Management Engine orchestrator: one
evaluation, `evaluate_position`, that ties `src.lifecycle.triggers`
(Parts 4-12) + `src.lifecycle.precedence` (Part 18) +
`src.lifecycle.excursion` (MFE/MAE) + `src.lifecycle.state` (Part 2's
state machine) + `src.lifecycle.snapshot` (Part 17) together into one
deterministic, auditable step.

**This engine never places, closes, or modifies an order.** Its output
is a `PositionLifecycleState` transition plus an immutable
`LifecycleDecisionSnapshot` — nothing more. Turning a `RISK_EXIT_REQUIRED`/
`*_TRIGGERED` outcome into an actual close/roll/adjustment order is the
job of `src.lifecycle.rolling`/`adjustment` (Part 13/14, Task #234) and
`src.lifecycle.paper_events`/`fidelity_events` (Part 20/21, Task #236)
— each of which still must pass back through
`src.risk.engine.evaluate_trade_proposal` like every other proposal in
this codebase. `evaluate_position` itself performs no I/O, so it can be
called identically from live monitoring, backtesting, or a unit test.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

from src.lifecycle import triggers as trg
from src.lifecycle.excursion import ExcursionState, update_excursion
from src.lifecycle.policy import ManagementPolicy
from src.lifecycle.precedence import ResolvedAction, resolve_action
from src.lifecycle.snapshot import LifecycleDecisionSnapshot, build_snapshot
from src.lifecycle.state import PositionLifecycleState, transition
from src.strategies.base import StrategyKind


def _tz_aware(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("timestamp fields must be timezone-aware")
    return v


class PositionMonitoringInput(BaseModel):
    """Every number one lifecycle evaluation needs, gathered once by
    the caller from `src.quant`/`src.risk`/`src.data`/`src.strategies`
    — this model carries no logic and computes nothing; it is the
    read-only snapshot-of-inputs the deterministic trigger functions
    compare against policy thresholds. Every field the position's
    configured `ManagementPolicy` doesn't need may be left `None`;
    every field it DOES need being `None` produces a `DATA_INSUFFICIENT`
    finding rather than a silently skipped check (Part 19)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    as_of: datetime
    dte: int | None = None

    unrealized_pnl: float
    realized_pnl: float = 0.0
    profit_capture_denominator: float | None = None
    max_loss_dollars: float | None = None
    initial_credit: float | None = None

    entry_underlying_price: float | None = None
    underlying_price: float | None = None
    entry_option_value: float | None = None
    current_option_value: float | None = None

    position_delta_abs: float | None = None
    iv: float | None = None
    iv_percentile: float | None = None

    entry_regime: str | None = None
    current_regime: str | None = None

    earnings_data_available: bool = True
    days_to_earnings: int | None = None

    spread_pct: float | None = None
    quote_age_minutes: float | None = None

    short_leg_is_itm: bool | None = None
    short_leg_extrinsic_value: float | None = None

    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    option_prices: dict[str, float] = {}
    portfolio_exposure_pct: float | None = None
    drawdown_pct: float | None = None

    risk_halt_active: bool = False
    risk_status: str = "ok"

    _validate_tz = field_validator("as_of")(_tz_aware)


def evaluate_all_triggers(policy: ManagementPolicy, inp: PositionMonitoringInput) -> list[trg.TriggerFinding]:
    """Calls every Part 4-12 trigger function once, in no particular
    order (order doesn't matter — `src.lifecycle.precedence` resolves
    the final action, not the order findings were produced in), and
    returns every finding any of them produced."""
    findings: list[trg.TriggerFinding] = []

    def _add(f: trg.TriggerFinding | None) -> None:
        if f is not None:
            findings.append(f)

    def _add_all(fs: list[trg.TriggerFinding]) -> None:
        findings.extend(fs)

    _add(trg.check_profit_target_pct(policy, unrealized_pnl=inp.unrealized_pnl, profit_capture_denominator=inp.profit_capture_denominator))
    _add(
        trg.check_profit_target_underlying_price(
            policy, entry_underlying_price=inp.entry_underlying_price, underlying_price=inp.underlying_price
        )
    )
    _add(
        trg.check_profit_target_option_value(
            policy, entry_option_value=inp.entry_option_value, current_option_value=inp.current_option_value
        )
    )
    _add(trg.check_max_loss_pct(policy, unrealized_pnl=inp.unrealized_pnl, max_loss_dollars=inp.max_loss_dollars))
    _add(
        trg.check_max_loss_multiple_of_credit(policy, unrealized_pnl=inp.unrealized_pnl, initial_credit=inp.initial_credit)
    )
    _add(
        trg.check_underlying_technical_invalidation(
            policy, entry_underlying_price=inp.entry_underlying_price, underlying_price=inp.underlying_price
        )
    )
    _add_all(trg.check_dte(policy, dte=inp.dte))
    _add(trg.check_delta(policy, position_delta_abs=inp.position_delta_abs))
    _add(trg.check_volatility(policy, iv_percentile=inp.iv_percentile))
    _add(trg.check_regime_change(policy, entry_regime=inp.entry_regime, current_regime=inp.current_regime))
    _add(trg.check_earnings(policy, earnings_data_available=inp.earnings_data_available, days_to_earnings=inp.days_to_earnings))
    _add_all(trg.check_liquidity(policy, spread_pct=inp.spread_pct, quote_age_minutes=inp.quote_age_minutes))
    _add(
        trg.check_assignment_risk(
            policy,
            short_leg_is_itm=inp.short_leg_is_itm,
            short_leg_extrinsic_value=inp.short_leg_extrinsic_value,
            dte=inp.dte,
        )
    )
    return findings


class EvaluationResult(BaseModel):
    """The engine's full output for one evaluation: the new state (may
    equal the old one), the new excursion state, the resolved action,
    and the immutable snapshot recording all of it."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    new_state: PositionLifecycleState
    excursion: ExcursionState
    resolved: ResolvedAction
    snapshot: LifecycleDecisionSnapshot


def evaluate_position(
    *,
    trade_id: str,
    wheel_id: str | None,
    strategy_kind_for_snapshot: StrategyKind,
    policy: ManagementPolicy,
    current_state: PositionLifecycleState,
    excursion: ExcursionState,
    inp: PositionMonitoringInput,
) -> EvaluationResult:
    """One full lifecycle evaluation. Pure function: given the same
    arguments, always produces the same result (backtest determinism,
    Part 27's "no look-ahead" requirement carried into this engine
    too). `strategy_kind_for_snapshot` must equal `policy.strategy_kind`
    — passed explicitly rather than reading it off `policy` so a caller
    can never silently record the wrong strategy in the snapshot if it
    passes a mismatched policy by mistake."""
    if strategy_kind_for_snapshot != policy.strategy_kind:
        raise ValueError(
            f"strategy_kind_for_snapshot ({strategy_kind_for_snapshot!r}) does not match "
            f"policy.strategy_kind ({policy.strategy_kind!r}) -- refusing to evaluate a position "
            "against a policy built for a different strategy"
        )

    new_excursion = update_excursion(excursion, inp.unrealized_pnl, inp.as_of)
    findings = evaluate_all_triggers(policy, inp)
    resolved = resolve_action(findings, risk_halt_active=inp.risk_halt_active)

    if resolved.target_state == current_state:
        new_state = current_state
    else:
        new_state = transition(current_state, resolved.target_state)

    snapshot = build_snapshot(
        trade_id=trade_id,
        wheel_id=wheel_id,
        timestamp=inp.as_of,
        strategy=policy.strategy_kind,
        management_policy_name=policy.name,
        current_state=new_state,
        underlying_price=inp.underlying_price,
        option_prices=inp.option_prices,
        delta=inp.delta,
        gamma=inp.gamma,
        theta=inp.theta,
        vega=inp.vega,
        iv=inp.iv,
        dte=inp.dte,
        mfe=new_excursion.mfe,
        mae=new_excursion.mae,
        unrealized_pnl=inp.unrealized_pnl,
        realized_pnl=inp.realized_pnl,
        portfolio_exposure_pct=inp.portfolio_exposure_pct,
        drawdown_pct=inp.drawdown_pct,
        market_regime=inp.current_regime,
        findings=findings,
        resolved=resolved,
        risk_status=inp.risk_status,
        data_is_fresh=(resolved.category != "system_data_safety"),
    )

    return EvaluationResult(new_state=new_state, excursion=new_excursion, resolved=resolved, snapshot=snapshot)
