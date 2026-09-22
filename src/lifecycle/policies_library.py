"""Part 15/16: named, RESEARCH-DEFAULT `ManagementPolicy` instances for
every `StrategyKind` this platform supports.

**Every policy here is a research default, never "the correct"
configuration** — Part 1's core principle applies at the library level
too: `PUT_CREDIT_SPREAD` alone has four independently-named policies
below (`PUT_CREDIT_SPREAD_STANDARD`, `PCS_25PCT_14DTE`,
`PCS_DELTA_DEFENSE`, `PCS_HOLD_TO_EXPIRY`) precisely to demonstrate
that this platform never assumes one management policy is universally
superior to another over the same structure. Every `description` below
starts with the literal words "RESEARCH DEFAULT" for the same reason.

**Every policy in `POLICIES_LIBRARY` is validated by
`src.lifecycle.policy.validate_policy_for_strategy` at import time** —
a hand-authored policy that presupposes a structural property its
`strategy_kind` doesn't have fails the moment this module is imported,
not silently at some later runtime evaluation.

**CASH/NO_TRADE never gets a `ManagementPolicy`.** It is not a
`StrategyKind` member at all (`selected=None` in
`src.strategies.selector.SelectionOutcome`) — there is no position for
it to manage, so it is the one item on Part 15's 17-item list this
module deliberately has no entry for, documented here rather than
worked around with a broken enum-membership check (see
`src.lifecycle.policy`'s own history of exactly that mistake, since
fixed).

**`WHEEL_STANDARD` integrates with the stateful Wheel engine rather
than duplicating it** (Part 16's explicit requirement): its fields
govern only the CSP/CC leg-level triggers this platform's
`src.lifecycle.engine` evaluates while a `WheelPosition`
(`src.wheel.models`) is in `CSP_OPEN`/`CC_OPEN`. The wheel_id-level
lifecycle (`WHEEL_CANDIDATE -> CSP_OPEN -> ASSIGNED_SHARES ->
CC_ELIGIBLE -> ... -> WHEEL_COMPLETE`) is owned entirely by
`src.wheel.state`/`src.wheel.lifecycle` and is never touched by this
package's own `PositionLifecycleState` machine — the two state
machines run side by side for the same underlying position without
either duplicating the other.

**Frozen at validation start (Part 26).** Once a validation cohort
begins, nothing in this module may change without the full HYPOTHESIS
-> BACKTEST -> VALIDATION -> OUT-OF-SAMPLE TEST -> RISK COMPARISON ->
HUMAN APPROVAL sequence `src.research.overfitting_guards` already
enforces for strategy-level parameters — this module carries no
special-case exemption from that process.
"""
from __future__ import annotations

from src.lifecycle.policy import ManagementPolicy, validate_policy_for_strategy
from src.strategies.base import StrategyKind

_RD = "RESEARCH DEFAULT (not a claim of optimality) -- "

# ------------------------------------------------- cash-secured put / covered call

CSP_STANDARD = ManagementPolicy(
    name="CSP_STANDARD",
    strategy_kind=StrategyKind.CASH_SECURED_PUT,
    description=_RD + "30-45 DTE entry, 50% profit target, mandatory review at 21 DTE, forced exit at 7 DTE, "
    "delta review at 0.30/close at 0.40 -- assignment is an accepted, planned outcome, not an emergency.",
    minimum_dte=30,
    profit_target_pct=0.50,
    management_dte=21,
    forced_exit_dte=7,
    delta_threshold=0.30,
    delta_close_threshold=0.40,
    assignment_risk_rule="assignment is an acceptable, planned CSP outcome -- reviewed, never treated as a forced-exit trigger by itself",
    early_assignment_rule="review only; no automatic defensive action on early assignment alone",
    earnings_exit_days=3,
    roll_allowed=True,
    adjustment_allowed=False,
)

CC_STANDARD = ManagementPolicy(
    name="CC_STANDARD",
    strategy_kind=StrategyKind.COVERED_CALL,
    description=_RD + "30-45 DTE entry, 50% profit target, mandatory review at 21 DTE, forced exit at 7 DTE, "
    "delta review at 0.30/close at 0.50 (shares already cover the short call, so more delta room is tolerated "
    "than a naked short) -- being called away is an accepted, planned outcome.",
    minimum_dte=30,
    profit_target_pct=0.50,
    management_dte=21,
    forced_exit_dte=7,
    delta_threshold=0.30,
    delta_close_threshold=0.50,
    assignment_risk_rule="being called away is an acceptable, planned covered-call outcome -- reviewed, never a forced-exit trigger by itself",
    early_assignment_rule="review only; no automatic defensive action on early assignment alone",
    earnings_exit_days=3,
    roll_allowed=True,
    adjustment_allowed=False,
)

# ------------------------------------------------------------------ credit spreads

PUT_CREDIT_SPREAD_STANDARD = ManagementPolicy(
    name="PUT_CREDIT_SPREAD_STANDARD",
    strategy_kind=StrategyKind.PUT_CREDIT_SPREAD,
    description=_RD + "Part 16's named example: 30-60 DTE entry, 50% profit target, mandatory review at 28 DTE, "
    "time exit at 21 DTE, loss review at 1.5x initial credit, forced Risk exit always honored regardless of this "
    "policy's own settings.",
    minimum_dte=30,
    profit_target_pct=0.50,
    management_dte=28,
    forced_exit_dte=21,
    max_loss_multiple_of_credit=1.5,
    delta_threshold=0.30,
    delta_close_threshold=0.45,
    assignment_risk_rule="review short-leg assignment risk once ITM inside the forced-exit DTE window",
    earnings_exit_days=3,
    roll_allowed=True,
    adjustment_allowed=True,
)

PCS_25PCT_14DTE = ManagementPolicy(
    name="PCS_25PCT_14DTE",
    strategy_kind=StrategyKind.PUT_CREDIT_SPREAD,
    description=_RD + "a faster-cycling alternative to PUT_CREDIT_SPREAD_STANDARD over the identical structure: "
    "takes profit earlier (25%) and exits on time later (14 DTE), trading a lower per-trade return for higher "
    "trade frequency -- never assumed superior to the 50%/21DTE variant.",
    minimum_dte=30,
    profit_target_pct=0.25,
    management_dte=21,
    forced_exit_dte=14,
    max_loss_multiple_of_credit=1.5,
    delta_threshold=0.30,
    delta_close_threshold=0.45,
    earnings_exit_days=3,
    roll_allowed=True,
    adjustment_allowed=True,
)

PCS_DELTA_DEFENSE = ManagementPolicy(
    name="PCS_DELTA_DEFENSE",
    strategy_kind=StrategyKind.PUT_CREDIT_SPREAD,
    description=_RD + "over the identical PUT_CREDIT_SPREAD structure, management is delta-driven rather than "
    "profit-driven: no profit_target_pct at all -- the position is held for full theta decay unless delta "
    "deteriorates, in which case it is defended earlier (review at 0.25, close at 0.40) and more generously "
    "loss-tolerant (2.0x credit) than the standard variant.",
    minimum_dte=30,
    forced_exit_dte=7,
    max_loss_multiple_of_credit=2.0,
    delta_threshold=0.25,
    delta_close_threshold=0.40,
    earnings_exit_days=3,
    roll_allowed=True,
    adjustment_allowed=True,
)

PCS_HOLD_TO_EXPIRY = ManagementPolicy(
    name="PCS_HOLD_TO_EXPIRY",
    strategy_kind=StrategyKind.PUT_CREDIT_SPREAD,
    description=_RD + "over the identical PUT_CREDIT_SPREAD structure, no profit target and no mandatory review "
    "DTE at all -- held to the last trading day unless a hard loss or delta-close threshold fires, testing "
    "whether early management actually adds value over simply letting theta run out.",
    minimum_dte=30,
    forced_exit_dte=1,
    max_loss_multiple_of_credit=2.0,
    delta_close_threshold=0.60,
    earnings_exit_days=3,
    roll_allowed=False,
    adjustment_allowed=False,
)

CALL_CREDIT_SPREAD_STANDARD = ManagementPolicy(
    name="CALL_CREDIT_SPREAD_STANDARD",
    strategy_kind=StrategyKind.CALL_CREDIT_SPREAD,
    description=_RD + "structural mirror of PUT_CREDIT_SPREAD_STANDARD for the bearish/call side: 30-60 DTE "
    "entry, 50% profit target, mandatory review at 28 DTE, time exit at 21 DTE, loss review at 1.5x initial credit.",
    minimum_dte=30,
    profit_target_pct=0.50,
    management_dte=28,
    forced_exit_dte=21,
    max_loss_multiple_of_credit=1.5,
    delta_threshold=0.30,
    delta_close_threshold=0.45,
    assignment_risk_rule="review short-leg assignment risk once ITM inside the forced-exit DTE window",
    earnings_exit_days=3,
    roll_allowed=True,
    adjustment_allowed=True,
)

# ------------------------------------------------------------------- debit spreads

BULL_CALL_SPREAD_STANDARD = ManagementPolicy(
    name="BULL_CALL_SPREAD_STANDARD",
    strategy_kind=StrategyKind.BULL_CALL_SPREAD,
    description=_RD + "defined-risk debit spread: exit at 75% of max profit, review at 21 DTE, forced exit at "
    "7 DTE, accept up to 50% of the max-loss (the debit paid) before a loss-threshold review.",
    minimum_dte=30,
    profit_target_pct=0.75,
    max_loss_pct=0.50,
    management_dte=21,
    forced_exit_dte=7,
    earnings_exit_days=3,
)

BEAR_PUT_SPREAD_STANDARD = ManagementPolicy(
    name="BEAR_PUT_SPREAD_STANDARD",
    strategy_kind=StrategyKind.BEAR_PUT_SPREAD,
    description=_RD + "structural mirror of BULL_CALL_SPREAD_STANDARD for the bearish side: exit at 75% of max "
    "profit, review at 21 DTE, forced exit at 7 DTE, accept up to 50% of max loss before review.",
    minimum_dte=30,
    profit_target_pct=0.75,
    max_loss_pct=0.50,
    management_dte=21,
    forced_exit_dte=7,
    earnings_exit_days=3,
)

# --------------------------------------------------------------- hedged/protective

PROTECTIVE_PUT_STANDARD = ManagementPolicy(
    name="PROTECTIVE_PUT_STANDARD",
    strategy_kind=StrategyKind.PROTECTIVE_PUT,
    description=_RD + "a hedge, not a profit vehicle -- no profit_target: management is purely time-based, "
    "reviewing at 21 DTE and rolling the protection forward (roll_allowed) rather than letting coverage lapse, "
    "with a forced review at 7 DTE if it hasn't already been rolled.",
    management_dte=21,
    forced_exit_dte=7,
    roll_allowed=True,
)

PROTECTIVE_COLLAR_STANDARD = ManagementPolicy(
    name="PROTECTIVE_COLLAR_STANDARD",
    strategy_kind=StrategyKind.PROTECTIVE_COLLAR,
    description=_RD + "the short call side is managed like a covered call's short leg (delta review at 0.30, "
    "close at 0.45) while the long put side is rolled forward like a standalone protective put -- reviewed "
    "together at 21 DTE, forced review at 7 DTE.",
    management_dte=21,
    forced_exit_dte=7,
    delta_threshold=0.30,
    delta_close_threshold=0.45,
    assignment_risk_rule="short-call assignment is an acceptable collar outcome -- reviewed, not a forced exit by itself",
    roll_allowed=True,
)

# ------------------------------------------------------------------- long premium

def _long_option_standard(kind: StrategyKind, name: str) -> ManagementPolicy:
    """Part 16's `LONG_OPTION_STANDARD` family: identical philosophy
    (100% profit target on the debit paid, accept up to 50% loss of
    debit, mandatory time-decay review at 21 DTE, forced exit at 7 DTE)
    applied per-`StrategyKind`, since `ManagementPolicy` is bound to
    one `strategy_kind`. Thesis-invalidation is inherently per-trade
    (it depends on the specific underlying and setup), so
    `underlying_technical_invalidation_price` is deliberately left
    unset here; a caller attaches a real trade's invalidation level via
    `dataclasses.replace(LONG_CALL_STANDARD, underlying_technical_invalidation_price=...)`
    rather than this module guessing one."""
    return ManagementPolicy(
        name=name,
        strategy_kind=kind,
        description=_RD + "the LONG_OPTION_STANDARD family: 100% profit target on the debit paid, accept up to "
        "50% loss of the debit before review, mandatory time-decay review at 21 DTE, forced exit at 7 DTE. "
        "Thesis-invalidation price is trade-specific and left unset in this library default.",
        profit_target_pct=1.0,
        max_loss_pct=0.50,
        management_dte=21,
        forced_exit_dte=7,
        earnings_exit_days=None,
        earnings_exposure_permitted=True,  # a directional long-premium thesis is often earnings-driven by design
    )


LONG_CALL_STANDARD = _long_option_standard(StrategyKind.LONG_CALL, "LONG_CALL_STANDARD")
LONG_PUT_STANDARD = _long_option_standard(StrategyKind.LONG_PUT, "LONG_PUT_STANDARD")

LONG_STRADDLE_STANDARD = ManagementPolicy(
    name="LONG_STRADDLE_STANDARD",
    strategy_kind=StrategyKind.LONG_STRADDLE,
    description=_RD + "a long-vega structure: reviewed for profit-taking when IV percentile expands above 0.70 "
    "(volatility expansion is favorable here, unlike a short-premium structure), 100% profit target, accept up "
    "to 60% loss of the debit, forced exit at 10 DTE ahead of accelerating theta decay.",
    profit_target_pct=1.0,
    max_loss_pct=0.60,
    management_dte=21,
    forced_exit_dte=10,
    volatility_trigger="iv_expansion_review",
    iv_percentile_trigger=0.70,
)

LONG_STRANGLE_STANDARD = ManagementPolicy(
    name="LONG_STRANGLE_STANDARD",
    strategy_kind=StrategyKind.LONG_STRANGLE,
    description=_RD + "structural mirror of LONG_STRADDLE_STANDARD (wider, cheaper strikes, same management "
    "philosophy): IV-expansion profit review above 0.70 percentile, 100% profit target, accept up to 60% loss "
    "of the debit, forced exit at 10 DTE.",
    profit_target_pct=1.0,
    max_loss_pct=0.60,
    management_dte=21,
    forced_exit_dte=10,
    volatility_trigger="iv_expansion_review",
    iv_percentile_trigger=0.70,
)

LONG_CALL_BUTTERFLY_STANDARD = ManagementPolicy(
    name="LONG_CALL_BUTTERFLY_STANDARD",
    strategy_kind=StrategyKind.LONG_CALL_BUTTERFLY,
    description=_RD + "max profit is realized AT the center strike at expiration, so this structure is managed "
    "closer to expiration than a typical spread: 50% of max profit target, mandatory review at 14 DTE, forced "
    "exit at 3 DTE, accept up to 70% loss of the debit paid.",
    profit_target_pct=0.50,
    max_loss_pct=0.70,
    management_dte=14,
    forced_exit_dte=3,
)

# ----------------------------------------------------------------------- iron structures

IRON_CONDOR_STANDARD = ManagementPolicy(
    name="IRON_CONDOR_STANDARD",
    strategy_kind=StrategyKind.SHORT_IRON_CONDOR,
    description=_RD + "Part 16's named example: 50% profit target, time exit at 21 DTE, challenged-short delta "
    "review at 0.35, critical-short delta close at 0.50.",
    profit_target_pct=0.50,
    management_dte=28,
    forced_exit_dte=21,
    max_loss_multiple_of_credit=1.5,
    delta_threshold=0.35,
    delta_close_threshold=0.50,
    assignment_risk_rule="review whichever short leg is challenged once ITM inside the forced-exit DTE window",
    earnings_exit_days=5,
    roll_allowed=True,
    adjustment_allowed=True,
)

IRON_BUTTERFLY_STANDARD = ManagementPolicy(
    name="IRON_BUTTERFLY_STANDARD",
    strategy_kind=StrategyKind.SHORT_IRON_BUTTERFLY,
    description=_RD + "the shared center strike leaves less room than an Iron Condor's wider short strikes, so "
    "this variant reviews/closes earlier: 50% profit target, time exit at 21 DTE, delta review at 0.30, delta "
    "close at 0.45.",
    profit_target_pct=0.50,
    management_dte=28,
    forced_exit_dte=21,
    max_loss_multiple_of_credit=1.5,
    delta_threshold=0.30,
    delta_close_threshold=0.45,
    assignment_risk_rule="review whichever short leg is challenged once ITM inside the forced-exit DTE window",
    earnings_exit_days=5,
    roll_allowed=True,
    adjustment_allowed=True,
)

# ------------------------------------------------------------------------- wheel

WHEEL_STANDARD = ManagementPolicy(
    name="WHEEL_STANDARD",
    strategy_kind=StrategyKind.WHEEL,
    description=_RD + "governs only the CSP/CC leg-level triggers evaluated while a WheelPosition is in "
    "CSP_OPEN/CC_OPEN -- the wheel_id-level state machine itself is owned entirely by src.wheel and is never "
    "touched by this policy or by src.lifecycle.engine. 50% profit target per leg, review at 21 DTE, forced "
    "exit at 7 DTE, delta review at 0.30/close at 0.45; assignment and being called away are the Wheel's own "
    "planned mechanics, reviewed by src.wheel rather than exited early by this policy.",
    profit_target_pct=0.50,
    management_dte=21,
    forced_exit_dte=7,
    delta_threshold=0.30,
    delta_close_threshold=0.45,
    assignment_risk_rule="assignment/being called away are the Wheel's own planned mechanics -- reviewed by src.wheel, not exited early by this policy",
    earnings_exit_days=3,
    roll_allowed=True,
    adjustment_allowed=False,
)

# --------------------------------------------------------------------------- registry

POLICIES_LIBRARY: dict[str, ManagementPolicy] = {
    p.name: p
    for p in (
        CSP_STANDARD,
        CC_STANDARD,
        PUT_CREDIT_SPREAD_STANDARD,
        PCS_25PCT_14DTE,
        PCS_DELTA_DEFENSE,
        PCS_HOLD_TO_EXPIRY,
        CALL_CREDIT_SPREAD_STANDARD,
        BULL_CALL_SPREAD_STANDARD,
        BEAR_PUT_SPREAD_STANDARD,
        PROTECTIVE_PUT_STANDARD,
        PROTECTIVE_COLLAR_STANDARD,
        LONG_CALL_STANDARD,
        LONG_PUT_STANDARD,
        LONG_STRADDLE_STANDARD,
        LONG_STRANGLE_STANDARD,
        LONG_CALL_BUTTERFLY_STANDARD,
        IRON_CONDOR_STANDARD,
        IRON_BUTTERFLY_STANDARD,
        WHEEL_STANDARD,
    )
}


def get_policy(name: str) -> ManagementPolicy:
    """Deterministic exact-name lookup -- never fuzzy-matches. Raises
    `KeyError` (with the full set of valid names) for anything not in
    the library."""
    try:
        return POLICIES_LIBRARY[name]
    except KeyError:
        raise KeyError(f"no such management policy {name!r}; known policies: {sorted(POLICIES_LIBRARY)}") from None


def policies_for_strategy(kind: StrategyKind) -> list[ManagementPolicy]:
    """Every named policy in the library for one `StrategyKind`,
    demonstrating Part 1's "never assume one management policy is
    universally superior" for strategies (like `PUT_CREDIT_SPREAD`)
    that have more than one."""
    return [p for p in POLICIES_LIBRARY.values() if p.strategy_kind == kind]


def _validate_library() -> None:
    for policy in POLICIES_LIBRARY.values():
        validate_policy_for_strategy(policy)
    covered = {p.strategy_kind for p in POLICIES_LIBRARY.values()}
    missing = set(StrategyKind) - covered
    if missing:
        raise AssertionError(f"policies_library.py has no named policy for: {sorted(k.value for k in missing)}")


_validate_library()
