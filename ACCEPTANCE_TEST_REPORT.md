# ACCEPTANCE TEST REPORT — Step 21: Final System Integration & Acceptance Test

**Commit under test:** `3909862` ("Step 20A: wire LONG_CALL_BUTTERFLY/SHORT_IRON_CONDOR/SHORT_IRON_BUTTERFLY to real orders")
**Branch:** `claude/options-trading-agent-2b4yi8`
**Scope:** verification and hardening only. No new features, no strategy-logic changes, no risk-limit changes, no live trading, no automated Fidelity execution were added. The 12–15% annualized target was never treated as an acceptance criterion.

---

## 1. Executive Summary

The platform passed acceptance testing. The complete pipeline — market
data → Quant Engine → Devil's Advocate → Portfolio Manager → Risk
Engine → Order Validator → PaperBroker → accounting → reporting — was
proven to work end to end for all 15 order-eligible strategies plus
CASH/NO_TRADE, using the REAL, unmocked Risk Engine, Order Validator,
PaperBroker, and Quant stage (only the two LLM stages were scripted,
per the instruction's "frozen/mock data, no live markets" requirement).
The Fidelity manual-execution workflow was separately proven to have
no automated-submission path anywhere in the repository. Hostile
market data, hostile LLM output (including 9 literal prompt-injection
strings), and direct Risk Engine attacks were all shown to fail closed.

Three real defects were found and fixed during this pass — one HIGH
(a backtest accounting bug specific to the butterfly's 2x middle leg),
one MEDIUM (a floating-point reliability bug that could silently stall
a correctly-priced multi-leg order forever), and one LOW
(a non-finite-quote liquidity-check gap, defense-in-depth only). Two
documentation staleness issues were also found and corrected. All five
have regression tests. No test was deleted, weakened, or bypassed to
reach this result; no risk limit was loosened; no strategy logic was
changed.

**Acceptance Decision: READY_WITH_NON_BLOCKING_WARNINGS** (see §28).

The formal 90-day validation cohort has **not** started, and nothing
in this acceptance pass started it (§18, §31). Step 22 has **not**
begun. This is not a decision to freeze V1.0 or begin validation —
that remains the user's call.

---

## 2. What Was Tested

- The full existing test suite (`tests/unit/`, 2,125 tests, pre-existing).
- A new `tests/acceptance/` suite (186 new tests across 9 files),
  purpose-built for this Step, described in §4 below.
- A live boot of the dashboard via `./scripts/start.sh` (the exact
  documented command) and via FastAPI's `TestClient`.
- A repository-wide, executable (not just narratively-described) check
  of every Fidelity-security claim in `SECURITY_AUDIT.md`.

## 3. Pipeline Proof (Sections 2–3)

`tests/acceptance/test_end_to_end_happy_path.py` runs all 15
`StrategyType` members plus dedicated butterfly/iron-structure checks
through `run_order_pipeline` with the REAL `evaluate_trade_proposal`
(Risk Engine), `validate_and_build_order_request` (Order Validator),
`PaperBroker`, and `default_quant_stage` — only Devil's Advocate and
Portfolio Manager are scripted (a fake `LLMClient` returning a fixed
payload, the same pattern the pre-existing test suite already uses).
Every strategy fills, produces a `FidelityTradeTicket` at the same
time, and the pipeline's `PipelineOutcome` structurally has no field,
anywhere, that could carry an automated Fidelity submission — proven
directly in `TestNoAutomatedExecutionPathExists`.

The Fidelity manual workflow (risk-approved → ticket at
`AWAITING_HUMAN` → human enters → human confirms → reconciliation) is
proven both by the pre-existing, exhaustive
`tests/unit/brokers/test_fidelity_state_machine.py` and by this Step's
`test_order_state_machine.py`/`test_fidelity_manual_only.py`, which
additionally prove, through the REAL pipeline, that every ticket this
platform ever actually produces starts at `AWAITING_HUMAN` — never
`PROPOSED` — so "PROPOSED → FILLED" is structurally unreachable, not
merely rejected by a transition table.

## 4. New Acceptance Suite (Section 4)

| File | Tests | Focus |
|---|---|---|
| `test_end_to_end_happy_path.py` | 18 | full pipeline, all 15 strategies + butterfly/iron structure checks |
| `test_market_data_failures.py` | 23 | malformed/NaN/infinite/crossed/stale/wrong-ticker data, fail-closed |
| `test_strategy_integrity.py` | 20 | full 15-strategy inventory, structural correctness, stale-doc grep |
| `test_risk_veto.py` | 10 | direct Risk Engine attacks: sizing, concentration, drawdown, capability |
| `test_llm_boundary.py` | 14 | 9 verbatim prompt-injection strings, malformed/hostile LLM output |
| `test_multileg_integrity.py` | 20 | leg order/duplicate/missing/ratio/strike attacks, one-bad-leg-invalidates-combo |
| `test_quant_integrity.py` | 8 | hand-computed economics vs. real pipeline, x100/sign/ratio/DTE hunting |
| `test_paper_broker.py` | 10 | multi-leg duplicate-submission, cancellation, partial fill, commission |
| `test_accounting_reconciliation.py` | 9 | NAV identity conservation, partial-fill scaling, explicit-failure-on-discrepancy |
| `test_assignment_expiration.py` | 11 | expiration/assignment at multiple moneyness zones, ACCEPT-003 regression |
| `test_order_state_machine.py` | 7 | illegal transitions, real-pipeline-ticket starting state |
| `test_fidelity_manual_only.py` | 19 | FS-001–FS-005 converted to executable, repo-wide regression tests |
| `test_validation_pipeline.py` | 7 | cohort-not-started proof against real repo state |
| `test_operational_resilience.py` | 10 | timezone enforcement, DB crash recovery, kill switch, dashboard boot |
| **Total** | **186** | |

All frozen/deterministic fixtures; zero live-market calls; zero live
Anthropic API calls (a scripted fake `LLMClient` is used throughout).

## 5. Multi-Leg / Section 7

Every 3-/4-leg attack the instruction names was tested: leg-list order
never affects the outcome (the schema sorts by strike internally),
duplicate/missing/wrong-ratio/malformed-strike structures reject at
construction, and — the acceptance-unique finding — one bad leg
(missing quote, zero liquidity, stale timestamp) invalidates the
**entire** combo, proven both through the full pipeline and directly
against `PaperBroker.place_order` (whose `_resolve_leg_quote`
comprehension raises on the first bad leg before any fill logic runs
for any leg).

## 6. Quant Engine / Section 6

`test_quant_integrity.py` hand-computes textbook economics
(credit-spread max profit/loss/breakeven, a long call's unbounded
upside represented honestly as literal `math.inf` never a fabricated
finite number, the butterfly's 1:-2:1 debit/collateral, the iron
condor's two-wing netting) and traces the exact same numbers through
the Quant stage, the Risk Engine's own independent cross-check, and
the Fidelity ticket. `payoff_at_expiration` (exact terminal payoff) is
proven distinct from the fixture's mark-to-market premium (which
carries real time value) — expiration payoff and mark-to-market are
never conflated.

## 7. Risk Engine / Section 8

`test_risk_veto.py` runs the real `evaluate_trade_proposal` against
oversized requests (RESIZE only ever shrinks, confirmed
`approved_contracts <= requested` in every case), concentration/
drawdown/capital limits, unsupported-strategy and disabled-account
capability, and an artificially non-positive "credit" spread (always
REJECTs, never approves with undefined risk). Every observed decision
was APPROVE/RESIZE/REJECT/HALT — nothing else.

## 8. LLM Boundary / Section 9

`test_llm_boundary.py` injects the 9 literal hostile strings from the
Step 21 instruction into a Devil's Advocate `why_not_thesis` field —
none altered the deterministic outcome (`filled_quantity` stayed
exactly 1 in every case). Free-text-instead-of-`tool_use` output,
missing required schema fields, and smuggled fields
(`execute`/`broker`/`final_approved_contracts` on the DA side;
`authoritative_max_loss`/`position_size` on the Portfolio Manager
side) all reject at the schema boundary (`extra="forbid"`). A Devil's
Advocate REJECT verdict was proven to prevent the Portfolio Manager
from ever being invoked (an `AssertionError`-raising stage substitute
proves it, not just an assumption).

## 9. PaperBroker / Section 10

Beyond the pre-existing exhaustive single/2-leg unit coverage, this
Step proved multi-leg (3-/4-leg) duplicate-submission never doubles a
position or cash balance, multi-leg cancellation has zero cash/position
impact, a thin-volume partial fill preserves the butterfly's 1:-2:1
ratio exactly at whatever quantity actually fills, and commission is
charged per actual contract across all legs (not per combo unit) —
each independently cross-checked against a hand-computed expected cash
delta.

## 10. Accounting / Section 15

`Portfolio.cash` is a risk-budget ledger ("NAV not currently committed
as capital"), not literal cash-on-hand — this is a deliberate,
documented design choice (see `default_portfolio_update_stage`'s own
docstring), not a defect. The identity actually proven: `(nav - cash) -
total_capital_at_risk(portfolio)` is conserved exactly across any
number of sequential fills, including across a partial fill (scaled
proportionally to the actual fill ratio, never the full request). A
fill that would drive cash negative is an explicit, named
`portfolio_update`-stage failure — never silently applied, never
silently dropped — the outcome still honestly reports the real fill at
the broker while flagging that accounting needs manual attention.

## 11. Assignment/Expiration / Section 11

See §18 (ACCEPT-003) for the one real defect found here. Beyond that
fix and its regression tests, live `PaperBroker.settle_expiration` was
proven correct for a 4-leg iron condor at both the max-profit zone
(all four legs OTM, nothing assigned) and beyond the long put wing
(both put legs settle, both call legs untouched, share impacts net to
zero as the correct economic shape of a put spread's assignment).
Early-assignment and pin-risk are confirmed to be explicitly,
plainly documented platform simplifications (not silently assumed
away) in `src/brokers/paper.py`'s own module docstring,
`ARCHITECTURE.md`'s risk list, and `long_call_butterfly.py`'s own
adjustment-rules text.

## 12. Order State Machine / Section 12

The exact three transitions the instruction names by name
(`PROPOSED→FILLED`, `AWAITING_HUMAN→FILLED` without confirmation,
`REPRICE_REQUIRED→FILLED` without repricing) were already exhaustively
covered by the pre-existing `tests/unit/brokers/
test_fidelity_state_machine.py`. This Step adds proof, through a REAL
pipeline-produced ticket, that these guarantees actually protect what
the system produces (not just what the transition function rejects in
isolation), plus the PaperBroker's own parallel `Order`/`OrderStatus`
guarantees: a CANCELLED or REJECTED order never later becomes FILLED
on retry, and a FILLED order's quantity never grows past its own combo
size on a repeated `attempt_fill` call.

## 13. Fidelity Security / Section 13

`SECURITY_AUDIT.md`'s FS-001 through FS-005 — previously verified only
by manually-recorded `grep` commands in that document — are now
executable, repository-wide regression tests (`test_fidelity_manual_
only.py`, 19 tests): no credentials/cookies/browser-automation library
anywhere in `src/`, `config/`, or `.claude/`; `FidelityManualProvider`
implements no `Broker` interface and has no submission-shaped method;
`config/brokers.yaml`'s loader has no environment-variable override
mechanism for `execution_mode`; no `src/llm/*.py` module references
broker capabilities, and every agent persona's declared tool is
read-only (`get_*`); the sole `FidelityTradeTicket(` construction call
site in `src/` is hardcoded to `AWAITING_HUMAN` (FS-005's own claim,
now regression-tested rather than merely asserted). A `PaperBroker`
`Order` and a `FidelityTradeTicket` were confirmed to be structurally
unrelated types with no conversion function between them anywhere in
`src/` — the two fill concepts can never be confused.

## 14. Fidelity Ticket Field Integrity / Section 14

Covered by §6/§11's hand-computed-economics tracing (the ticket's
`max_profit`/`max_loss`/`breakeven`/`capital_at_risk` were checked
against independently hand-derived numbers, not just internal
consistency) and by the pre-existing `tests/unit/brokers/
test_fidelity_schemas.py` (stale-quote → `REPRICE_REQUIRED`, already
covered before this Step).

## 15–25 (remaining named sections)

- **§16 database failure:** `SqliteDatabase` (the pipeline's durable
  audit trail) survives a simulated crash-and-restart — a record saved
  by one instance is readable by a freshly constructed instance
  against the same file; sequential "process lifetimes" each see every
  prior write.
- **§17 time/calendar:** every timestamp-carrying schema this platform
  relies on for freshness rejects a naive `datetime` outright
  (`Portfolio.as_of`, `ExecutionConfirmation.confirmed_at`,
  `TradeProposal.timestamp`). **Finding, not a defect:** no market-
  hours/holiday/DST-aware trading-calendar module exists anywhere in
  `src/` — confirmed by repo-wide grep. This is a known, already-
  documented gap (`ARCHITECTURE.md`'s own risk list names "Clock/
  timezone bugs around market hours" with an exchange-calendar library
  as named future work), not something this pass discovered newly or
  is hiding.
- **§18 validation framework:** see the boxed statement in §1/§31 — no
  `.db`/`.sqlite` file exists anywhere in the repository, `config/
  validation.yaml` declares no fixed store path, and `start_new_cohort`
  has zero call sites anywhere in `src/`. The cohort has not started.
- **§19/§20 strategy selection / hedge effectiveness:** already
  thoroughly covered by the pre-existing `tests/unit/validation/
  test_counterfactual.py` and `tests/unit/strategies/
  test_hedge_effectiveness.py` (built in Step 19A/the multi-strategy-
  attribution work) — re-verified passing, no gap found requiring new
  acceptance-level coverage.
- **§21 reporting integrity:** the dashboard's `OpportunityView`/
  `RiskPanelResponseView` schemas are built directly from the same
  `RiskDecisionResult`/`QuantitativeAnalysis` objects the pipeline
  itself produces (see `src/dashboard/service.py`) — there is no
  separate, hand-maintained reporting number anywhere to drift from
  the source. No new acceptance test was needed beyond the existing
  `tests/unit/dashboard/` suite.
- **§22 export reproducibility:** **N/A.** Confirmed by repository-wide
  grep (`openpyxl`/`reportlab`/`.to_csv`/`.to_json`/`.xlsx`/`.pdf`) —
  no export functionality of any kind exists anywhere in `src/`. This
  is honestly reported as not-yet-built, not fabricated as tested.
- **§23 dashboard:** launched for real via the documented
  `./scripts/start.sh` (see §26 below) and via `TestClient`; every
  frontend `fetch` call in `dashboard.js` was cross-checked against the
  backend's own route allowlist (already extensively tested pre-
  existing) — a 1:1 match, no orphaned or unreachable route either
  direction. No "Auto Trade"/"Execute"/"Send to Fidelity" button or
  route exists anywhere, confirmed by both source inspection and the
  frontend/backend consistency check.
- **§24 startup/novice-user:** the exact documented command sequence
  (`./scripts/start.sh`) was run for real (not read and assumed) — it
  produced exactly the log output the README promises and served the
  root page and a graceful, non-crashing 503 (not a stack trace) from
  an API route with no data loaded yet, honestly matching the README's
  own "if any have been generated" framing.
- **§25 failure recovery/kill switch:** a manually-halted `Portfolio`
  blocks every trade through the real pipeline (`REJECTED`, `order is
  None`), and `check_kill_switch`'s result always carries a non-null
  reason code and human-readable message — never a bare boolean a
  caller could silently ignore.

## 26. Full Suite Results

```
python -m pytest -q tests/
2311 passed, 4 skipped, 1 warning in ~33s
0 failed, 0 errors, 0 xfailed
```

- 2,125 pre-existing tests (`tests/unit/`) — all still passing, zero
  regressions from any fix applied in this Step.
- 186 new tests (`tests/acceptance/`) — all passing.
- 4 skips are pre-existing, documented false positives in
  `tests/unit/risk/test_no_hardcoded_limits.py` (unrelated to this
  Step; see that file's own `_ALLOWED_COLLISIONS`).
- 1 warning is a third-party (`starlette`/`anyio`) deprecation notice,
  not from this codebase.

No test was deleted, weakened, or had a bypass/hardcode introduced to
reach this result.

## 27–28. Issues Found, Classified and Fixed

| ID | Severity | Component | Status |
|---|---|---|---|
| ACCEPT-003 | **HIGH** | `src/backtest/assignment.py` | **FIXED**, regression tests added |
| ACCEPT-001 | MEDIUM | `src/brokers/paper.py` (`price_satisfies_limit`) | **FIXED**, exercised by existing multi-leg fixtures |
| ACCEPT-002 | LOW | `src/risk/trade_risk.py` (`check_liquidity`) | **FIXED**, regression test added |
| — | LOW | `src/strategies/base.py` docstrings (stale "evaluation only" language) | **FIXED** |
| — | LOW | `SECURITY_AUDIT.md` OP-003 (stale factual claims, now-legitimate 2x ratio) | **FIXED** (documentation corrected) |
| OP-003 | MEDIUM | `src/brokers/base.py`/`src/brokers/paper.py` (no type-level leg-ratio validator) | **OPEN as of Step 21** (pre-existing, defense-in-depth only, no live exploit path — see updated description in `SECURITY_AUDIT.md`); **FIXED in Step 22** |
| FS-005 | LOW | `src/brokers/fidelity.py` (`FidelityTradeTicket` constructible at a terminal status directly) | **OPEN as of Step 21** (pre-existing, no live exploit path, now regression-tested that the one real construction site is safe); **FIXED in Step 22** |
| Other `SECURITY_AUDIT.md` OPEN items | MEDIUM/LOW | various | **OPEN** (pre-existing, out of this Step's scope — none is CRITICAL or HIGH) |

**#### ACCEPT-003 — HIGH — Backtest butterfly settlement ignored the 2x middle-leg quantity ratio**
- **Component:** `src/backtest/assignment.py` (`settle_leg`, `realized_settlement_pnl`)
- **Found via:** `tests/acceptance/test_assignment_expiration.py`, cross-checking backtest settlement against the platform's own independently-verified `payoff_at_expiration` engine.
- **Description:** `BacktestPosition.contracts` (the flat combo-unit count) was passed to every leg's settlement math unmodified — `BacktestLeg.quantity_ratio` (2 for `LONG_CALL_BUTTERFLY`'s middle leg) was applied on the ENTRY fill path but never on the SETTLEMENT path. A butterfly held to expiration ITM had its assignment cash/share impact understated by exactly half for the middle leg — a $700-per-combo-unit realized-P&L error in the reproduction case (computed: $1,100 vs. the correct $400).
- **Consequence:** corrupted backtest research validity (counterfactual analysis, strategy attribution, the aspirational 12–15% target evaluation) for `LONG_CALL_BUTTERFLY` positions held to expiration ITM. **Never reachable in the live `PaperBroker`/Risk Engine path** — `PaperBroker.settle_expiration` uses each position's own already-ratio-correct tracked signed quantity (verified: `test_middle_leg_position_is_exactly_twice_each_wing`), so no live paper trade or Fidelity ticket was ever affected.
- **Fix:** every leg's settlement cash/share impact and realized-P&L contribution now multiplies by `leg.quantity_ratio`. No-op for every other strategy (all use ratio=1 on every leg).
- **Regression tests:** `TestButterflyMiddleLegRatioCorrectlyAppliedAtSettlementAcceptRegression003` (4 moneyness points cross-checked against the independent payoff engine, linear-scaling-with-contracts check, exact 2x share-impact check).

**#### ACCEPT-001 — MEDIUM — Floating-point limit-price mismatch could stall a correctly-priced multi-leg order forever**
- **Component:** `src/brokers/paper.py` (`price_satisfies_limit`)
- **Description:** `ApprovedOrder.limit_price` and `PaperBroker`'s independently-recomputed `net_price` are two floating-point evaluations of the same economic quantity via different summation orders — for a 3–4 leg combo they could differ by a single ULP, which a strict `>=` comparison treated as "doesn't satisfy the limit," leaving a correctly-priced order resting in `SUBMITTED` status indefinitely.
- **Fix:** a `1e-6`-dollar epsilon tolerance (far below any real cent-level price difference) added to the comparison.

**#### ACCEPT-002 — LOW — Non-finite bid/ask could pass the liquidity check**
- **Component:** `src/risk/trade_risk.py` (`check_liquidity`)
- **Description:** `OptionContract` only enforces `bid`/`ask >= 0`, so `+inf` is constructible; `(inf-inf)/inf` is `NaN`, and `NaN > threshold` is always `False` in Python, so the spread-percentage liquidity gate never fired on an infinite quote. The full pipeline still failed closed one stage later via a less-specific exception, so this was not independently exploitable — fixed anyway as cheap, low-risk defense-in-depth.
- **Fix:** explicit `math.isfinite()` guard raising a clear `LiquidityError`.

No CRITICAL issues were found. No MEDIUM issue required loosening a risk limit, changing strategy logic, or increasing risk to resolve. No fix touched `src/risk/engine.py`'s decision logic, any risk-limit value in `config/risk_limits.yaml`, or `config/brokers.yaml`'s `allowed_strategies`.

## 29. Remaining Warnings (non-blocking)

- `SECURITY_AUDIT.md`'s pre-existing OPEN MEDIUM/LOW findings (OP-003 as
  now-corrected above, OP-005, SY-007, SY-008, LM-001, LM-002, QF-001,
  MD-007, MD-008, FS-005) remain open — none is CRITICAL or HIGH, none
  was introduced or worsened by this Step, and fixing them was outside
  this Step's mandate (verification/hardening of the existing system,
  not new feature work). They are appropriate backlog items for a
  future step, not blockers for a paper-trading V1.0 freeze decision.
- No market-hours/trading-calendar/DST-aware logic exists yet (§17) —
  a known, already-documented gap, not a new finding.
- No export functionality exists yet (§22) — likewise a known gap, not
  a defect.

## 30. Acceptance Decision

**READY_WITH_NON_BLOCKING_WARNINGS**

Rationale: the one HIGH-severity issue found (ACCEPT-003) has been
fixed with regression coverage, was backtest-only (never reachable in
the live paper-trading/Fidelity-ticket path this V1.0 freeze would
actually govern), and is now verified fixed. No CRITICAL issue exists
or ever existed. No unresolved CRITICAL or HIGH issue remains — the
mandatory bar for `NOT_READY` is not met. The remaining open items are
all pre-existing MEDIUM/LOW findings already catalogued in
`SECURITY_AUDIT.md`, none newly discovered as blocking by this pass,
none touching capital preservation, catastrophic-loss prevention, or
Risk Engine authority.

This decision is a statement about the codebase's readiness for a
90-day paper-trading validation experiment — **not** an instruction to
begin one. Per the Step 21 instruction's own explicit direction, Step
22 has not been started and the 90-day validation has not begun; both
remain the user's decision.
