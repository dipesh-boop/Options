# Universe Expansion Activation Plan (PAPER_TRADING_V1.5.14)

**Status: NOT ACTIVATED.** This document describes a FUTURE,
human-approved activation step. Nothing in this plan is executed by
V1.5.14 itself -- `config/universe.yaml` (the official active
universe) remains `SPY`, `QQQ` only, and no `ExperimentVersion` record
for an expanded universe is created by this release.

## Why

The active validation cohort (`paper-trading-v1.4.3-validation-2026-09-22`)
has demonstrated, through the October 7-8 official cycles under
PAPER_TRADING_V1.5.12/V1.5.13, that the production pipeline (Tradier
production data, DTE-aware retrieval, candidate construction, Quant,
Risk, ranking, human confirmation, PaperBroker entry) works correctly
end-to-end. SPY + QQQ alone provides too little independent opportunity
breadth to exercise the full validation lifecycle (entry -> lifecycle
monitoring -> exit -> completed-trade reporting) within a reasonable
number of trading days. PAPER_TRADING_V1.5.14's `--universe-feasibility`
study exists to let the operator decide, from real Tradier production
data, whether and how to expand the official universe -- this plan
describes what happens AFTER that decision is made, never before.

## The two-phase structure

### Phase A (current, unchanged)

- **Universe:** `SPY`, `QQQ` (`config/universe.yaml`, unchanged by this
  release).
- **Strategies:** `CASH_SECURED_PUT`, `COVERED_CALL`, `PUT_CREDIT_SPREAD`.
- **Dates:** 2026-09-22 (cohort start) through the day BEFORE whatever
  future date the operator approves for Phase B.
- **Cohort:** `paper-trading-v1.4.3-validation-2026-09-22` -- its
  history, NAV, cash, positions, completed trades, validation-day
  count, and historical snapshots are never reset, truncated, or
  rewritten by moving to Phase B. The 90-day validation clock keeps
  running across the transition; Phase B is a configuration change
  WITHIN the same cohort, never a new cohort.

### Phase B (future, human-approved, NOT created by this release)

- **Universe:** whatever expanded ticker set the operator approves
  after reviewing a real `--universe-feasibility` run's per-symbol
  suitability classifications and correlation summary -- a subset of,
  or identical to, the 12-symbol research universe
  (`config/universe_feasibility.yaml`: SPY, QQQ, IWM, DIA, AAPL, MSFT,
  NVDA, AMZN, META, GOOGL, JPM, XOM), never a symbol the feasibility
  study never tested.
- **Strategies:** unchanged -- this plan activates universe breadth
  only, never a new strategy. A future, SEPARATE step would be required
  to test strategy breadth, following this same one-variable-at-a-time
  discipline.
- **Activation date:** an explicit, human-chosen future calendar date
  -- never "immediately" and never silently inferred from when the
  feasibility study happened to run.

## Mechanics of the transition, when approved

1. The operator reviews one or more real `--universe-feasibility`
   study outputs (per-symbol suitability, ranked-candidate economics,
   aggregate report, correlation summary) and decides the exact
   expanded ticker set and activation date.
2. **On the approved activation date**, `config/universe.yaml` is
   edited (by a human, or a future dedicated, reviewed change) to list
   the approved expanded ticker set. This is the ONLY file that
   controls what the official cycle scans -- `config/
   universe_feasibility.yaml` is never read by the official path, so
   this edit is the sole activation mechanism.
3. A new `ExperimentVersion` record (`src.validation.experiment_version
   .build_experiment_version`) is computed and recorded, marking the
   configuration change: `universe_config_hash` changes (the file
   content changed), `strategy_activation_stage` is given a new label
   (e.g. `"phase_b_expanded_universe_<activation-date>"`, replacing
   whatever label Phase A used), and `software_freeze_version`/
   `risk_config_hash`/`validation_config_hash`/`market_data_config_hash`
   are recomputed from whatever is current at that time. This produces
   a new, distinct `version_id` -- Phase A and Phase B are therefore
   two DIFFERENT, individually content-addressed experiment
   configurations, never silently merged into one.
4. **Nothing else changes.** The cohort id, account id, starting NAV,
   accumulated cash/positions/completed trades, validation-day count,
   and every historical `DailySnapshot`/`OpportunityRecord` already
   recorded under Phase A remain exactly as they are. The NEXT daily
   cycle run after the activation date simply reads the new
   `config/universe.yaml` and begins scanning the expanded set --
   there is no migration, backfill, or re-evaluation of Phase A's own
   history.
5. Risk/Quant/liquidity/DTE/no-trade-hurdle/ranking/position-sizing
   limits are not touched by this transition. If the operator ALSO
   wants to adjust any of those for the expanded universe, that is a
   separate, explicitly-approved change -- never bundled into the
   universe-activation step itself.

## What this plan deliberately does NOT do

- It does not create the Phase B `ExperimentVersion` record now --
  only describes the mechanism for when the operator approves it.
- It does not edit `config/universe.yaml` now.
- It does not pick the actual expanded ticker subset now -- that is
  the operator's own decision, informed by a REAL (not simulated)
  `--universe-feasibility` run against Tradier production.
- It does not propose a Phase B activation date now.
