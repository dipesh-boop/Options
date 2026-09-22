"""Deterministic lifecycle triggers (Parts 4-12): profit management,
loss management, DTE management, delta management, volatility
management, regime-change management, earnings/event management,
liquidity deterioration, and assignment risk.

**Every function in this module is pure arithmetic and comparison
against an already-computed number.** No function here computes a
price, Greek, probability, or P&L figure — every input is a number
`src.quant`/`src.risk`/`src.data` already produced once, passed in by
the caller (the lifecycle engine, `src.lifecycle.engine`, Part 233).
This module's only job is comparing those numbers against the
deterministic thresholds recorded on a `ManagementPolicy`
(`src.lifecycle.policy`) and producing zero or more `TriggerFinding`
records — never a silent decision, never a fabricated HOLD, and (Part
19) never a fabricated evaluation result when a required input is
missing.

**A missing input a configured policy field needs is `DATA_INSUFFICIENT`,
never "assume the trigger didn't fire."** e.g. if `policy.earnings_exit_days`
is set but earnings-date data is unavailable, `check_earnings` returns a
`DATA_INSUFFICIENT` finding rather than silently returning `None` — Part
10's "never interpret missing data as 'no earnings'" applies exactly as
much to this module as to the data layer itself.

**Precedence categories** (`PRECEDENCE_ORDER`, Part 18) are attached to
every finding here so `src.lifecycle.precedence` never has to
re-derive which of Part 18's 11 levels a given trigger belongs to —
that assignment is made once, here, next to the rule that produces it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.lifecycle.policy import ManagementPolicy
from src.lifecycle.state import PositionLifecycleState

TriggerCategory = Literal[
    "system_data_safety",
    "risk_halt",
    "hard_loss_exposure",
    "assignment_expiration",
    "event_risk",
    "liquidity_risk",
    "time_exit",
    "profit_target",
    "delta_volatility_review",
    "optional_adjustment",
    "hold",
]

# Index 0 = highest priority. `risk_halt` is never produced by this
# module (it comes from `src.risk`'s own portfolio-level halt signal,
# merged in by the engine) but is listed here so precedence.py has one
# single canonical ordering to import, matching Part 18's numbering
# exactly (SYSTEM/DATA SAFETY=1 ... HOLD=11).
PRECEDENCE_ORDER: tuple[TriggerCategory, ...] = (
    "system_data_safety",
    "risk_halt",
    "hard_loss_exposure",
    "assignment_expiration",
    "event_risk",
    "liquidity_risk",
    "time_exit",
    "profit_target",
    "delta_volatility_review",
    "optional_adjustment",
    "hold",
)


@dataclass(frozen=True)
class TriggerFinding:
    """One deterministic candidate action a single rule produced.
    `mandatory=True` means the rule leaves no discretion (e.g. a hard
    loss limit, a stale-data safety stop, an imminent ITM assignment);
    `mandatory=False` means the rule surfaces a review/adjustment
    candidate without forcing an immediate exit (e.g. the 28 DTE
    management checkpoint, a delta *review* — as opposed to *close* —
    threshold)."""

    trigger_name: str
    category: TriggerCategory
    target_state: PositionLifecycleState
    mandatory: bool
    reason: str


def _data_insufficient(trigger_name: str, reason: str) -> TriggerFinding:
    return TriggerFinding(
        trigger_name=trigger_name,
        category="system_data_safety",
        target_state=PositionLifecycleState.DATA_INSUFFICIENT,
        mandatory=True,
        reason=reason,
    )


# ------------------------------------------------------------- Part 4: profit

def check_profit_target_pct(
    policy: ManagementPolicy, *, unrealized_pnl: float, profit_capture_denominator: float | None
) -> TriggerFinding | None:
    """`profit_target_pct` is a fraction of `profit_capture_denominator`
    — the dollar figure "100% of target" means for this specific
    position (max profit for a short-premium structure, debit paid for
    a long-premium structure targeting a percentage return; computed
    once by the caller, never here)."""
    if policy.profit_target_pct is None:
        return None
    if profit_capture_denominator is None:
        return _data_insufficient(
            "profit_target_pct", "profit_target_pct is configured but the profit-capture reference amount is unavailable"
        )
    if profit_capture_denominator <= 0:
        return _data_insufficient(
            "profit_target_pct",
            f"profit_capture_denominator must be positive to evaluate profit_target_pct, got {profit_capture_denominator!r}",
        )
    captured_fraction = unrealized_pnl / profit_capture_denominator
    if captured_fraction >= policy.profit_target_pct:
        return TriggerFinding(
            trigger_name="profit_target_pct",
            category="profit_target",
            target_state=PositionLifecycleState.PROFIT_TARGET_REACHED,
            mandatory=False,
            reason=(
                f"unrealized P&L captures {captured_fraction:.1%} of the profit-target reference, "
                f"meeting or exceeding the configured {policy.profit_target_pct:.1%} target"
            ),
        )
    return None


def check_profit_target_underlying_price(
    policy: ManagementPolicy, *, entry_underlying_price: float | None, underlying_price: float | None
) -> TriggerFinding | None:
    if policy.profit_target_underlying_price is None:
        return None
    if entry_underlying_price is None or underlying_price is None:
        return _data_insufficient(
            "profit_target_underlying_price", "profit_target_underlying_price is configured but underlying price data is unavailable"
        )
    target = policy.profit_target_underlying_price
    bullish = target >= entry_underlying_price
    reached = underlying_price >= target if bullish else underlying_price <= target
    if reached:
        return TriggerFinding(
            trigger_name="profit_target_underlying_price",
            category="profit_target",
            target_state=PositionLifecycleState.PROFIT_TARGET_REACHED,
            mandatory=False,
            reason=f"underlying price {underlying_price!r} reached the configured target price {target!r}",
        )
    return None


def check_profit_target_option_value(
    policy: ManagementPolicy, *, entry_option_value: float | None, current_option_value: float | None
) -> TriggerFinding | None:
    if policy.profit_target_option_value is None:
        return None
    if entry_option_value is None or current_option_value is None:
        return _data_insufficient(
            "profit_target_option_value", "profit_target_option_value is configured but option value data is unavailable"
        )
    target = policy.profit_target_option_value
    appreciating = target >= entry_option_value
    reached = current_option_value >= target if appreciating else current_option_value <= target
    if reached:
        return TriggerFinding(
            trigger_name="profit_target_option_value",
            category="profit_target",
            target_state=PositionLifecycleState.PROFIT_TARGET_REACHED,
            mandatory=False,
            reason=f"option value {current_option_value!r} reached the configured target value {target!r}",
        )
    return None


# --------------------------------------------------------------- Part 5: loss

def check_max_loss_pct(
    policy: ManagementPolicy, *, unrealized_pnl: float, max_loss_dollars: float | None
) -> TriggerFinding | None:
    if policy.max_loss_pct is None:
        return None
    if max_loss_dollars is None or max_loss_dollars <= 0:
        return _data_insufficient(
            "max_loss_pct", "max_loss_pct is configured but the position's dollar max-loss figure is unavailable"
        )
    loss_limit = -(policy.max_loss_pct * max_loss_dollars)
    if unrealized_pnl <= loss_limit:
        return TriggerFinding(
            trigger_name="max_loss_pct",
            category="hard_loss_exposure",
            target_state=PositionLifecycleState.LOSS_THRESHOLD_REACHED,
            mandatory=True,
            reason=f"unrealized P&L {unrealized_pnl!r} breached {policy.max_loss_pct:.1%} of max loss ({loss_limit!r})",
        )
    return None


def check_max_loss_multiple_of_credit(
    policy: ManagementPolicy, *, unrealized_pnl: float, initial_credit: float | None
) -> TriggerFinding | None:
    if policy.max_loss_multiple_of_credit is None:
        return None
    if initial_credit is None:
        return _data_insufficient(
            "max_loss_multiple_of_credit", "max_loss_multiple_of_credit is configured but initial_credit is unavailable"
        )
    if initial_credit <= 0:
        return _data_insufficient(
            "max_loss_multiple_of_credit", f"initial_credit must be positive to evaluate this rule, got {initial_credit!r}"
        )
    loss_limit = -(policy.max_loss_multiple_of_credit * initial_credit)
    if unrealized_pnl <= loss_limit:
        return TriggerFinding(
            trigger_name="max_loss_multiple_of_credit",
            category="hard_loss_exposure",
            target_state=PositionLifecycleState.LOSS_THRESHOLD_REACHED,
            mandatory=True,
            reason=(
                f"unrealized P&L {unrealized_pnl!r} breached {policy.max_loss_multiple_of_credit!r}x "
                f"the initial credit received ({loss_limit!r})"
            ),
        )
    return None


def check_underlying_technical_invalidation(
    policy: ManagementPolicy, *, entry_underlying_price: float | None, underlying_price: float | None
) -> TriggerFinding | None:
    if policy.underlying_technical_invalidation_price is None:
        return None
    if entry_underlying_price is None or underlying_price is None:
        return _data_insufficient(
            "underlying_technical_invalidation_price",
            "underlying_technical_invalidation_price is configured but underlying price data is unavailable",
        )
    threshold = policy.underlying_technical_invalidation_price
    # The invalidation level is on the opposite side of entry from a
    # profit target's direction -- below entry invalidates a bullish
    # thesis, above entry invalidates a bearish one.
    bearish_invalidation = threshold <= entry_underlying_price
    invalidated = underlying_price <= threshold if bearish_invalidation else underlying_price >= threshold
    if invalidated:
        return TriggerFinding(
            trigger_name="underlying_technical_invalidation_price",
            category="hard_loss_exposure",
            target_state=PositionLifecycleState.LOSS_THRESHOLD_REACHED,
            mandatory=True,
            reason=f"underlying price {underlying_price!r} breached the technical invalidation level {threshold!r}",
        )
    return None


# ---------------------------------------------------------------- Part 6: DTE

def check_dte(policy: ManagementPolicy, *, dte: int | None) -> list[TriggerFinding]:
    if dte is None:
        needs_data = policy.management_dte is not None or policy.forced_exit_dte is not None
        if needs_data:
            return [_data_insufficient("dte", "a DTE-based rule is configured but current DTE is unavailable")]
        return []
    findings: list[TriggerFinding] = []
    if policy.forced_exit_dte is not None and dte <= policy.forced_exit_dte:
        findings.append(
            TriggerFinding(
                trigger_name="forced_exit_dte",
                category="time_exit",
                target_state=PositionLifecycleState.TIME_EXIT_TRIGGERED,
                mandatory=True,
                reason=f"DTE {dte} reached the forced-exit threshold of {policy.forced_exit_dte}",
            )
        )
    elif policy.management_dte is not None and dte <= policy.management_dte:
        findings.append(
            TriggerFinding(
                trigger_name="management_dte",
                category="time_exit",
                target_state=PositionLifecycleState.TIME_EXIT_TRIGGERED,
                mandatory=False,
                reason=f"DTE {dte} reached the mandatory review point of {policy.management_dte}",
            )
        )
    return findings


# -------------------------------------------------------------- Part 7: delta

def check_delta(policy: ManagementPolicy, *, position_delta_abs: float | None) -> TriggerFinding | None:
    if policy.delta_threshold is None and policy.delta_close_threshold is None:
        return None
    if position_delta_abs is None:
        return _data_insufficient("delta", "a delta-based rule is configured but current position delta is unavailable")
    if policy.delta_close_threshold is not None and position_delta_abs >= policy.delta_close_threshold:
        return TriggerFinding(
            trigger_name="delta_close_threshold",
            category="delta_volatility_review",
            target_state=PositionLifecycleState.DELTA_TRIGGERED,
            mandatory=True,
            reason=f"|delta| {position_delta_abs!r} reached the close threshold {policy.delta_close_threshold!r}",
        )
    if policy.delta_threshold is not None and position_delta_abs >= policy.delta_threshold:
        return TriggerFinding(
            trigger_name="delta_threshold",
            category="delta_volatility_review",
            target_state=PositionLifecycleState.DELTA_TRIGGERED,
            mandatory=False,
            reason=f"|delta| {position_delta_abs!r} reached the review threshold {policy.delta_threshold!r}",
        )
    return None


# --------------------------------------------------------- Part 8: volatility

def check_volatility(policy: ManagementPolicy, *, iv_percentile: float | None) -> TriggerFinding | None:
    if policy.volatility_trigger is None:
        return None
    if policy.iv_percentile_trigger is None:
        return _data_insufficient(
            "volatility_trigger", "volatility_trigger is configured but iv_percentile_trigger has no numeric threshold"
        )
    if iv_percentile is None:
        return _data_insufficient("volatility_trigger", "volatility_trigger is configured but current IV percentile is unavailable")
    if policy.volatility_trigger == "iv_contraction_capture":
        fired = iv_percentile <= policy.iv_percentile_trigger
        reason = f"IV percentile {iv_percentile!r} contracted to/below the capture threshold {policy.iv_percentile_trigger!r}"
    elif policy.volatility_trigger == "iv_expansion_review":
        fired = iv_percentile >= policy.iv_percentile_trigger
        reason = f"IV percentile {iv_percentile!r} expanded to/above the review threshold {policy.iv_percentile_trigger!r}"
    else:
        raise ValueError(f"unrecognized volatility_trigger {policy.volatility_trigger!r}")
    if fired:
        return TriggerFinding(
            trigger_name="volatility_trigger",
            category="delta_volatility_review",
            target_state=PositionLifecycleState.VOLATILITY_TRIGGERED,
            mandatory=False,
            reason=reason,
        )
    return None


# ------------------------------------------------------------ Part 9: regime

def check_regime_change(
    policy: ManagementPolicy, *, entry_regime: str | None, current_regime: str | None
) -> TriggerFinding | None:
    if policy.regime_change_action is None:
        return None
    if entry_regime is None or current_regime is None:
        return _data_insufficient(
            "regime_change_action", "regime_change_action is configured but entry/current regime classification is unavailable"
        )
    if entry_regime == current_regime:
        return None
    if policy.regime_change_action == "no_action":
        return None
    return TriggerFinding(
        trigger_name="regime_change_action",
        category="delta_volatility_review",
        target_state=PositionLifecycleState.REGIME_CHANGE_TRIGGERED,
        mandatory=(policy.regime_change_action == "exit"),
        reason=(
            f"market regime changed from {entry_regime!r} to {current_regime!r}; "
            f"configured action is {policy.regime_change_action!r}"
        ),
    )


# ----------------------------------------------------- Part 10: earnings/events

def check_earnings(
    policy: ManagementPolicy, *, earnings_data_available: bool, days_to_earnings: int | None
) -> TriggerFinding | None:
    if policy.earnings_exit_days is None or policy.earnings_exposure_permitted:
        return None
    if not earnings_data_available:
        return _data_insufficient(
            "earnings_exit_days",
            "earnings_exit_days is configured (earnings exposure not permitted) but earnings-date data is unavailable "
            "-- this is never interpreted as 'no earnings'",
        )
    if days_to_earnings is None:
        return _data_insufficient("earnings_exit_days", "earnings data is marked available but days_to_earnings is missing")
    if days_to_earnings <= policy.earnings_exit_days:
        return TriggerFinding(
            trigger_name="earnings_exit_days",
            category="event_risk",
            target_state=PositionLifecycleState.TIME_EXIT_TRIGGERED,
            mandatory=True,
            reason=(
                f"earnings are {days_to_earnings} day(s) away, at/inside the configured "
                f"{policy.earnings_exit_days}-day exit window and earnings_exposure_permitted is False"
            ),
        )
    return None


# --------------------------------------------------------- Part 11: liquidity

def check_liquidity(
    policy: ManagementPolicy,
    *,
    spread_pct: float | None,
    quote_age_minutes: float | None,
) -> list[TriggerFinding]:
    findings: list[TriggerFinding] = []
    if policy.quote_staleness_limit_minutes is not None:
        if quote_age_minutes is None:
            findings.append(
                _data_insufficient(
                    "quote_staleness_limit_minutes", "quote_staleness_limit_minutes is configured but quote age is unavailable"
                )
            )
        elif quote_age_minutes > policy.quote_staleness_limit_minutes:
            findings.append(
                _data_insufficient(
                    "quote_staleness_limit_minutes",
                    f"quote age {quote_age_minutes!r} minutes exceeds the configured staleness limit "
                    f"{policy.quote_staleness_limit_minutes!r} minutes",
                )
            )
    if policy.liquidity_deterioration_threshold is not None:
        if spread_pct is None:
            findings.append(
                _data_insufficient(
                    "liquidity_deterioration_threshold", "liquidity_deterioration_threshold is configured but spread % is unavailable"
                )
            )
        elif spread_pct > policy.liquidity_deterioration_threshold:
            findings.append(
                TriggerFinding(
                    trigger_name="liquidity_deterioration_threshold",
                    category="liquidity_risk",
                    target_state=PositionLifecycleState.ADJUSTMENT_CANDIDATE,
                    mandatory=False,
                    reason=(
                        f"bid/ask spread {spread_pct!r} exceeds the configured liquidity-deterioration "
                        f"threshold {policy.liquidity_deterioration_threshold!r}"
                    ),
                )
            )
    return findings


# ------------------------------------------------------- Part 12: assignment

def check_assignment_risk(
    policy: ManagementPolicy,
    *,
    short_leg_is_itm: bool | None,
    short_leg_extrinsic_value: float | None,
    dte: int | None,
) -> TriggerFinding | None:
    if policy.assignment_risk_rule is None and policy.early_assignment_rule is None:
        return None
    if short_leg_is_itm is None:
        return _data_insufficient(
            "assignment_risk_rule", "an assignment-risk rule is configured but short-leg ITM status is unavailable"
        )
    if not short_leg_is_itm:
        return None
    # Imminent expiration with a short leg already ITM is a mandatory
    # review regardless of extrinsic value -- Part 12: "assignment must
    # be modeled explicitly ... never assume it occurs only at
    # expiration," which starts with never being surprised by it.
    if dte is not None and dte <= 0:
        return TriggerFinding(
            trigger_name="assignment_risk_imminent_expiration",
            category="assignment_expiration",
            target_state=PositionLifecycleState.ADJUSTMENT_CANDIDATE,
            mandatory=True,
            reason="short leg is in-the-money at or past expiration DTE -- assignment is expected",
        )
    if short_leg_extrinsic_value is None:
        return _data_insufficient(
            "assignment_risk_rule", "the short leg is ITM but its extrinsic value is unavailable to assess early-assignment risk"
        )
    if short_leg_extrinsic_value <= 0.05:
        return TriggerFinding(
            trigger_name="assignment_risk_low_extrinsic",
            category="assignment_expiration",
            target_state=PositionLifecycleState.ADJUSTMENT_CANDIDATE,
            mandatory=False,
            reason=(
                f"short leg is in-the-money with extrinsic value {short_leg_extrinsic_value!r} near zero -- "
                "elevated early-assignment risk"
            ),
        )
    return None
