"""The canonical, deterministic `ManagementPolicy` (Part 3): the
configurable-fields object that separates STRATEGY (a `StrategyKind` —
the option structure) from MANAGEMENT POLICY (how that structure is
monitored and exited). The same `StrategyKind.PUT_CREDIT_SPREAD` can be
paired with `PCS_50PCT_21DTE` or `PCS_HOLD_TO_EXPIRY`
(`src.lifecycle.policies_library`) — genuinely different, independently
researchable behaviors over the identical structure.

Every field is optional (`None` = "not configured for this policy") —
Part 3's "not every strategy must use every field." `validate_policy_for_strategy`
is where "unsupported combinations must fail validation" is enforced:
a field that presupposes something the named `StrategyKind` structurally
cannot have (e.g. `delta_threshold` on a strategy with no short option
leg, or `assignment_risk_rule` on a pure long-premium strategy that can
never be assigned) raises `UnsupportedPolicyFieldError` rather than
silently being accepted and ignored.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.strategies.base import StrategyKind


class UnsupportedPolicyFieldError(ValueError):
    """Raised by `validate_policy_for_strategy` when a `ManagementPolicy`
    field is set for a `StrategyKind` that structurally cannot use it."""


class InvalidPolicyError(ValueError):
    """Raised for an internally inconsistent policy (e.g. a negative
    DTE, a profit target outside (0, 1])."""


RegimeChangeAction = str  # "review" | "reduce" | "exit" | "no_action" -- see REGIME_CHANGE_ACTIONS below
REGIME_CHANGE_ACTIONS = frozenset({"review", "reduce", "exit", "no_action"})


@dataclass(frozen=True)
class ManagementPolicy:
    """Every field Part 3 names. `name` is the policy's own identity
    (e.g. `"PCS_50PCT_21DTE"`) — the research label this platform tracks
    performance by, separately from `strategy_kind`
    (`src.validation.policy_attribution` groups by the *pair*)."""

    name: str
    strategy_kind: StrategyKind
    description: str

    # Profit management (Part 4).
    profit_target_pct: float | None = None  # fraction of max profit (short-premium) or of debit paid (long-premium)
    profit_target_underlying_price: float | None = None  # long-premium: exit if underlying reaches this price
    profit_target_option_value: float | None = None  # long-premium: exit if the option itself reaches this value
    trailing_profit_rule: str | None = None  # free-text description of a trailing-stop rule; interpretation lives in triggers.py

    # Loss management (Part 5).
    max_loss_pct: float | None = None  # fraction of NAV or of capital_at_risk -- see triggers.py for which
    max_loss_multiple_of_credit: float | None = None  # e.g. 1.5 = exit once loss reaches 1.5x credit received
    underlying_technical_invalidation_price: float | None = None

    # DTE management (Part 6).
    minimum_dte: int | None = None  # entry screening only (used by eligibility, not this engine, but recorded for the policy's own record)
    management_dte: int | None = None  # mandatory review point
    forced_exit_dte: int | None = None  # hard exit regardless of P&L

    # Delta management (Part 7).
    delta_threshold: float | None = None  # |delta| beyond which the short leg is reviewed
    delta_close_threshold: float | None = None  # |delta| beyond which the position is force-closed, not merely reviewed

    # Volatility management (Part 8).
    volatility_trigger: str | None = None  # "iv_contraction_capture" | "iv_expansion_review" -- see triggers.py
    iv_percentile_trigger: float | None = None

    # Regime management (Part 9).
    regime_change_action: RegimeChangeAction | None = None

    # Event management (Part 10).
    earnings_exit_days: int | None = None  # exit N days before earnings unless earnings_exposure_permitted
    earnings_exposure_permitted: bool = False

    # Liquidity (Part 11).
    liquidity_deterioration_threshold: float | None = None  # max acceptable bid/ask spread % before a liquidity-warning trigger
    spread_width_deterioration_pct: float | None = None  # for a spread: max acceptable widening of the spread's own width, as a fraction
    quote_staleness_limit_minutes: float | None = None

    # Assignment (Part 12).
    assignment_risk_rule: str | None = None  # free-text description of when assignment risk requires review; see triggers.py
    early_assignment_rule: str | None = None

    # Rolling / adjustment (Parts 13-14).
    roll_allowed: bool = False
    adjustment_allowed: bool = False
    partial_exit_allowed: bool = False

    underlying_price_trigger: float | None = None  # generic price-level trigger, e.g. for a directional thesis invalidation


# Every StrategyKind's structural shape: whether it has a short leg at
# all (delta/assignment fields need one), a defined credit at entry
# (max_loss_multiple_of_credit needs one), and whether it can ever be
# assigned (a pure long-premium strategy cannot).
_HAS_SHORT_LEG: dict[StrategyKind, bool] = {
    StrategyKind.CASH_SECURED_PUT: True,
    StrategyKind.COVERED_CALL: True,
    StrategyKind.PUT_CREDIT_SPREAD: True,
    StrategyKind.CALL_CREDIT_SPREAD: True,
    StrategyKind.BULL_CALL_SPREAD: False,
    StrategyKind.BEAR_PUT_SPREAD: False,
    StrategyKind.PROTECTIVE_PUT: False,
    StrategyKind.PROTECTIVE_COLLAR: True,
    StrategyKind.LONG_STRADDLE: False,
    StrategyKind.LONG_STRANGLE: False,
    StrategyKind.LONG_CALL: False,
    StrategyKind.LONG_PUT: False,
    StrategyKind.LONG_CALL_BUTTERFLY: True,  # short middle leg
    StrategyKind.SHORT_IRON_CONDOR: True,
    StrategyKind.SHORT_IRON_BUTTERFLY: True,
    StrategyKind.WHEEL: True,
}
_RECEIVES_CREDIT_AT_ENTRY: dict[StrategyKind, bool] = {
    StrategyKind.CASH_SECURED_PUT: True,
    StrategyKind.COVERED_CALL: True,
    StrategyKind.PUT_CREDIT_SPREAD: True,
    StrategyKind.CALL_CREDIT_SPREAD: True,
    StrategyKind.BULL_CALL_SPREAD: False,
    StrategyKind.BEAR_PUT_SPREAD: False,
    StrategyKind.PROTECTIVE_PUT: False,
    StrategyKind.PROTECTIVE_COLLAR: False,
    StrategyKind.LONG_STRADDLE: False,
    StrategyKind.LONG_STRANGLE: False,
    StrategyKind.LONG_CALL: False,
    StrategyKind.LONG_PUT: False,
    StrategyKind.LONG_CALL_BUTTERFLY: False,  # net debit
    StrategyKind.SHORT_IRON_CONDOR: True,
    StrategyKind.SHORT_IRON_BUTTERFLY: True,
    StrategyKind.WHEEL: True,
}
# A pure long-premium structure (no short leg) is never assigned --
# only exercised at the holder's own choice, which this platform never
# does early (see src.strategies.base's own _assignment_and_exercise_risk).
_CAN_BE_ASSIGNED: dict[StrategyKind, bool] = _HAS_SHORT_LEG


def validate_policy_for_strategy(policy: ManagementPolicy) -> None:
    """Fails validation for any field that presupposes a structural
    property `policy.strategy_kind` doesn't have. Called once when a
    named policy is registered (`policies_library.py`) and again
    whenever a policy is attached to a real position -- never silently
    skipped."""
    kind = policy.strategy_kind
    has_short = _HAS_SHORT_LEG.get(kind, False)
    receives_credit = _RECEIVES_CREDIT_AT_ENTRY.get(kind, False)
    can_be_assigned = _CAN_BE_ASSIGNED.get(kind, False)

    if policy.delta_threshold is not None and not has_short:
        raise UnsupportedPolicyFieldError(
            f"{kind.value}: delta_threshold requires a short option leg, which this strategy does not have"
        )
    if policy.delta_close_threshold is not None and not has_short:
        raise UnsupportedPolicyFieldError(
            f"{kind.value}: delta_close_threshold requires a short option leg, which this strategy does not have"
        )
    if policy.max_loss_multiple_of_credit is not None and not receives_credit:
        raise UnsupportedPolicyFieldError(
            f"{kind.value}: max_loss_multiple_of_credit requires a net credit at entry, which this strategy does not receive"
        )
    if policy.assignment_risk_rule is not None and not can_be_assigned:
        raise UnsupportedPolicyFieldError(
            f"{kind.value}: assignment_risk_rule requires a strategy that can be assigned, which this one cannot"
        )
    if policy.early_assignment_rule is not None and not can_be_assigned:
        raise UnsupportedPolicyFieldError(
            f"{kind.value}: early_assignment_rule requires a strategy that can be assigned, which this one cannot"
        )
    # CASH/NO_TRADE is deliberately never a `StrategyKind` member (see
    # `src.strategies.selector.SelectionOutcome` -- it's represented as
    # `selected=None`, not an enum value), so there is no position for it
    # to manage and no `ManagementPolicy` is ever constructed for it in
    # the first place; `policies_library.py` documents this rather than
    # this function guarding against a member that cannot exist.

    _validate_numeric_ranges(policy)


def _validate_numeric_ranges(policy: ManagementPolicy) -> None:
    if policy.profit_target_pct is not None and not (0 < policy.profit_target_pct <= 1.0):
        raise InvalidPolicyError(f"{policy.name}: profit_target_pct must be in (0, 1], got {policy.profit_target_pct!r}")
    if policy.max_loss_pct is not None and not (0 < policy.max_loss_pct <= 1.0):
        raise InvalidPolicyError(f"{policy.name}: max_loss_pct must be in (0, 1], got {policy.max_loss_pct!r}")
    if policy.max_loss_multiple_of_credit is not None and policy.max_loss_multiple_of_credit <= 0:
        raise InvalidPolicyError(f"{policy.name}: max_loss_multiple_of_credit must be positive")
    if policy.management_dte is not None and policy.management_dte < 0:
        raise InvalidPolicyError(f"{policy.name}: management_dte cannot be negative")
    if policy.forced_exit_dte is not None and policy.forced_exit_dte < 0:
        raise InvalidPolicyError(f"{policy.name}: forced_exit_dte cannot be negative")
    if (
        policy.management_dte is not None and policy.forced_exit_dte is not None
        and policy.forced_exit_dte > policy.management_dte
    ):
        raise InvalidPolicyError(
            f"{policy.name}: forced_exit_dte ({policy.forced_exit_dte}) cannot be later (a larger DTE) than "
            f"management_dte ({policy.management_dte}) -- forced exit must come at or after the mandatory review point"
        )
    if policy.delta_threshold is not None and not (0 < policy.delta_threshold <= 1.0):
        raise InvalidPolicyError(f"{policy.name}: delta_threshold must be in (0, 1], got {policy.delta_threshold!r}")
    if policy.delta_close_threshold is not None and not (0 < policy.delta_close_threshold <= 1.0):
        raise InvalidPolicyError(f"{policy.name}: delta_close_threshold must be in (0, 1]")
    if (
        policy.delta_threshold is not None and policy.delta_close_threshold is not None
        and policy.delta_close_threshold < policy.delta_threshold
    ):
        raise InvalidPolicyError(
            f"{policy.name}: delta_close_threshold ({policy.delta_close_threshold}) cannot be smaller than "
            f"delta_threshold ({policy.delta_threshold}) -- close is a more severe action than review"
        )
    if policy.regime_change_action is not None and policy.regime_change_action not in REGIME_CHANGE_ACTIONS:
        raise InvalidPolicyError(
            f"{policy.name}: regime_change_action must be one of {sorted(REGIME_CHANGE_ACTIONS)}, got {policy.regime_change_action!r}"
        )
    if policy.earnings_exit_days is not None and policy.earnings_exit_days < 0:
        raise InvalidPolicyError(f"{policy.name}: earnings_exit_days cannot be negative")
