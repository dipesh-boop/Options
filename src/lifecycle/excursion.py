"""MFE/MAE excursion tracking (Part 4/5: "track MFE and MAE for every
position, for post-trade analysis of whether exits were too
early/late"; reused by Part 24's post-trade "exit efficiency"
calculation).

MFE (max favorable excursion) and MAE (max adverse excursion) are
running high/low-water marks on a position's unrealized P&L, in
dollars, updated once per lifecycle evaluation from the same
`unrealized_pnl` figure Risk/Quant already computed for that
evaluation. **This module never computes a P&L figure itself** — only
tracks the historical extremes of a number computed elsewhere, in
keeping with CLAUDE.md's "every dollar figure computed once" rule.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator


def _tz_aware(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("timestamp fields must be timezone-aware")
    return v


class ExcursionState(BaseModel):
    """Immutable running MFE/MAE state for one position.

    `mfe`/`mae` are both stored as the actual unrealized-P&L value at
    the extreme, not an absolute value: `mfe >= 0` is not guaranteed
    (a long-premium position that is immediately underwater and never
    recovers has a negative MFE), and `mae <= 0` is not guaranteed
    either (a short-premium position that is profitable from the first
    tick and never gives it back has a positive MAE).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mfe: float
    mfe_at: datetime
    mae: float
    mae_at: datetime
    last_unrealized_pnl: float
    last_updated_at: datetime

    _validate_tz = field_validator("mfe_at", "mae_at", "last_updated_at")(_tz_aware)


def initial_excursion(unrealized_pnl: float, as_of: datetime) -> ExcursionState:
    """The excursion state for a position's very first evaluation
    (immediately after FILLED -> ACTIVE): both MFE and MAE start at the
    same first observed P&L, since there is no history yet to be
    more/less favorable than."""
    return ExcursionState(
        mfe=unrealized_pnl,
        mfe_at=as_of,
        mae=unrealized_pnl,
        mae_at=as_of,
        last_unrealized_pnl=unrealized_pnl,
        last_updated_at=as_of,
    )


def update_excursion(state: ExcursionState, unrealized_pnl: float, as_of: datetime) -> ExcursionState:
    """Returns a NEW `ExcursionState` reflecting one more observation —
    never mutates `state` (frozen model). `as_of` must not be before
    `state.last_updated_at`: the caller (the lifecycle engine) is
    responsible for calling this in chronological order, and this
    function raises rather than silently accepting an out-of-order
    observation that would corrupt the MFE/MAE history."""
    if as_of < state.last_updated_at:
        raise ValueError(
            f"update_excursion called out of order: as_of={as_of!r} is before "
            f"the last recorded observation at {state.last_updated_at!r}"
        )
    if unrealized_pnl > state.mfe:
        new_mfe, new_mfe_at = unrealized_pnl, as_of
    else:
        new_mfe, new_mfe_at = state.mfe, state.mfe_at
    if unrealized_pnl < state.mae:
        new_mae, new_mae_at = unrealized_pnl, as_of
    else:
        new_mae, new_mae_at = state.mae, state.mae_at
    return ExcursionState(
        mfe=new_mfe,
        mfe_at=new_mfe_at,
        mae=new_mae,
        mae_at=new_mae_at,
        last_unrealized_pnl=unrealized_pnl,
        last_updated_at=as_of,
    )


def exit_efficiency(realized_pnl: float, mfe: float) -> float | None:
    """Part 24's "exit efficiency": what fraction of the best paper
    profit ever observed (MFE) was actually captured at exit. Returns
    `None` — never a fabricated 0.0 or 1.0 — when `mfe <= 0`, since the
    position was never profitable at any point and "fraction of peak
    profit captured" is not a meaningful ratio; the caller must report
    that absence explicitly rather than display a misleading number."""
    if mfe <= 0:
        return None
    return realized_pnl / mfe
