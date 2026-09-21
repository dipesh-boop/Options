"""Validation cohort management (Step 19A): determines whether a
strategy-library expansion can simply continue the current validation
run, or must close it and start a fresh, separately-tracked cohort —
per the explicit rule this step was given:

- If the formal 90-day validation cohort has NOT started: finish the
  expansion, run all tests, freeze strategy versions/prompts/risk
  configuration, create a new `VALIDATION_MANIFEST`, then start Day 1.
- If it HAS already started: close/preserve the current cohort as
  `PRE_EXPANSION_VALIDATION` and start a new `MULTI_STRATEGY_VALIDATION_V1`
  cohort with its own Day 1. Results from materially different strategy
  architectures are never mixed into one cohort's numbers.

"Has the cohort started" is answered from the `ValidationStore` itself
(a real trade, snapshot, rejected-trade outcome, or logged violation
recorded against it) — never from a manifest's mere existence, since a
manifest can be built and frozen before Day 1 without a single day of
the run actually having happened yet.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

from src.validation.protocol import StrategyVersionManifest, ValidationPeriod, build_validation_manifest
from src.validation.session import ValidationStore

PRE_EXPANSION_COHORT_LABEL = "PRE_EXPANSION_VALIDATION"
MULTI_STRATEGY_COHORT_LABEL = "MULTI_STRATEGY_VALIDATION_V1"

CohortAction = Literal["START_FRESH_COHORT", "CLOSE_AND_START_NEW_COHORT"]


@dataclass(frozen=True)
class ValidationCohort:
    manifest: StrategyVersionManifest
    store: ValidationStore


def has_cohort_started(store: ValidationStore | None) -> bool:
    """The factual test: has this store recorded a single real event
    yet (a trade, a snapshot, a rejected-trade outcome, or a logged
    violation)? A manifest existing, or a store existing but empty, is
    NOT "started" — Day 1 is defined by the first recorded event, not by
    when the store or manifest was created."""
    if store is None:
        return False
    return bool(store.trades() or store.snapshots() or store.rejected_outcomes() or store.violations())


@dataclass(frozen=True)
class CohortTransitionPlan:
    action: CohortAction
    previous_cohort_label: str | None
    new_cohort_label: str
    reason: str


def decide_cohort_transition(
    existing_store: ValidationStore | None,
    existing_manifest: StrategyVersionManifest | None = None,
    *,
    new_cohort_label: str = MULTI_STRATEGY_COHORT_LABEL,
    pre_expansion_label: str = PRE_EXPANSION_COHORT_LABEL,
) -> CohortTransitionPlan:
    started = has_cohort_started(existing_store)
    if not started:
        return CohortTransitionPlan(
            action="START_FRESH_COHORT",
            previous_cohort_label=None,
            new_cohort_label=new_cohort_label,
            reason=(
                "no formal 90-day validation cohort has recorded any trade, snapshot, rejected-trade outcome, "
                "or violation yet -- the strategy-library expansion can freeze directly into a new manifest and "
                "start Day 1 without closing anything"
            ),
        )
    previous_label = existing_manifest.cohort_label if existing_manifest is not None else "unknown"
    return CohortTransitionPlan(
        action="CLOSE_AND_START_NEW_COHORT",
        previous_cohort_label=previous_label if previous_label != "default" else pre_expansion_label,
        new_cohort_label=new_cohort_label,
        reason=(
            f"the existing cohort ({previous_label!r}) has already recorded real trades/snapshots -- it is "
            "preserved as-is (never silently mixed with post-expansion results) and a new cohort with its own "
            "Day 1 is required for the expanded, materially different strategy architecture"
        ),
    )


def start_new_cohort(
    *,
    manifest_id: str,
    start_date: date,
    duration_days: int,
    frozen_at: datetime,
    starting_nav: float,
    strategy_versions: dict[str, str],
    cohort_label: str,
    store: ValidationStore,
    notes: str = "",
) -> ValidationCohort:
    period = ValidationPeriod(
        start_date=start_date, end_date=start_date + timedelta(days=duration_days), duration_days=duration_days
    )
    manifest = build_validation_manifest(
        manifest_id=manifest_id, period=period, frozen_at=frozen_at, starting_nav=starting_nav,
        strategy_versions=strategy_versions, notes=notes, cohort_label=cohort_label,
    )
    return ValidationCohort(manifest=manifest, store=store)
