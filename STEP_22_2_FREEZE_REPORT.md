# STEP_22_2_FREEZE_REPORT.md

## Pre-Validation Controlled Amendment — Stateful Wheel Strategy

This report documents Step 22.2: adding a properly modeled, stateful
Wheel strategy to the platform before the 90-day validation begins, and
re-freezing the platform as **PAPER_TRADING_V1.2**. It follows the same
two-commit freeze pattern, and the same "preserve, never overwrite,
prior frozen artifacts" discipline, that STEP_22_FREEZE_REPORT.md
(V1.0) and STEP_22_1_FREEZE_REPORT.md (V1.1) already established.

## 1. Executive Summary

- Added `src/wheel/`, a new package implementing the Wheel as a
  persistent, multi-stage position lifecycle with deterministic state
  transitions, assignment handling, stock ownership, cost-basis
  accounting, option premium accounting, covered-call management, risk
  controls, reporting, backtesting, PaperBroker support, and dashboard
  visibility.
- The Wheel is **never a new order type**. Every order it places is an
  ordinary `TradeProposal` with `strategy=StrategyType.CASH_SECURED_PUT`
  or `COVERED_CALL`, submitted to the exact same unmodified
  `src.risk.engine.evaluate_trade_proposal` every other strategy uses.
- No live or automatic brokerage execution was added anywhere. No
  Fidelity order-submission capability was added. No Alpaca trading API
  was touched (Alpaca remains market-data-only, untouched by this
  amendment). No LLM output can override the Risk Engine — the Wheel's
  own state transitions take no LLM-shaped input at all.
- Naked/uncovered calls remain structurally impossible: `open_cc`
  raises `UncoveredCallError` (checked before any `CcCycle` is even
  constructed, in source order) if `shares_owned < 100 * contracts`,
  and `PaperBroker`'s own independent collateral check refuses an
  uncovered call regardless.
- 207 net new tests added; full suite **2699 passed, 4 skipped, 0
  failed**.
- Re-frozen as **PAPER_TRADING_V1.2**. The original V1.0 and V1.1
  artifacts are preserved, untouched, recoverable at the
  `paper-trading-v1.0`/`paper-trading-v1.1` tags.
- **90-day validation was NOT started.** No cohort was created, no Day
  1 snapshot recorded, no trades generated, starting NAV unaltered, no
  scheduling enabled.

## 2. Starting State

- Repository: `dipesh-boop/Options`, branch
  `claude/options-trading-agent-2b4yi8`.
- Prior freeze: `PAPER_TRADING_V1.1`, tag `paper-trading-v1.1`.
- 90-day validation had not started; live trading disabled; Fidelity
  execution manual-only; Alpaca market-data-only; deterministic Risk
  Engine held final veto authority. All confirmed unchanged by this
  amendment (Sections 13-15 below).

## 3. Architecture Audit

Before writing any Wheel code, the existing platform was audited for:

- **How CSP/covered call orders already flow**: a `TradeProposal` with
  `strategy=CASH_SECURED_PUT`/`COVERED_CALL` goes through
  `src.risk.engine.evaluate_trade_proposal`, which builds an
  `ApprovedOrder` and (for a MANUAL broker) a `FidelityTradeTicket`, or
  (for an AUTOMATED broker) can be converted via
  `src.brokers.order_validator.validate_and_build_order_request` into a
  `PlaceOrderRequest` for `PaperBroker`. This exact, unmodified path is
  what every Wheel CSP/CC leg uses — the Wheel package adds no new
  order-construction logic anywhere.
- **The CLOSE/ROLL gap**: `src.risk.engine._evaluate` already rejects
  every non-`OPEN` `TradeAction` with `REJECT_UNSUPPORTED_ACTION` (the
  TS-004 finding from an earlier security audit) — there is no
  Risk-Engine-routed pipeline for closing an existing option position
  anywhere in this codebase today, Wheel or not. This is a pre-existing,
  documented gap; Step 22.2 does not attempt to close it, and
  `src.wheel.paper_events`/`fidelity_events` document exactly where they
  touch this boundary (a discretionary early close submits a
  `PlaceOrderRequest`/is entered manually in Fidelity, the same way a
  standalone CSP/CC position's early close would have to be handled
  today).
- **`StrategyKind` vs. `StrategyType`**: `StrategyKind`
  (`src.strategies.base`) is the strategy-comparison enum;
  `StrategyType` (`src.llm.schemas`) is the order-construction enum
  used by `TradeProposal`. Three pre-Step-20A `StrategyKind` members
  (`LONG_CALL_BUTTERFLY`, `SHORT_IRON_CONDOR`, `SHORT_IRON_BUTTERFLY`)
  already established the precedent of a `StrategyKind` member with no
  `StrategyType` counterpart (evaluation-only, until Step 20A wired
  them to real orders). `StrategyKind.WHEEL` follows this same pattern,
  except permanently — a Wheel is never itself submitted as an order,
  by design, not as a staged rollout.

## 4. Wheel State Machine (Part 2)

`src/wheel/state.py` defines 14 `WheelState` members exactly matching
the required diagram: `WHEEL_CANDIDATE -> CSP_OPEN -> {CSP_EXPIRED |
CSP_CLOSED | ASSIGNED_SHARES}`; `ASSIGNED_SHARES -> CC_ELIGIBLE ->
CC_OPEN -> {CC_EXPIRED -> CC_ELIGIBLE | CC_CLOSED -> CC_ELIGIBLE |
SHARES_CALLED_AWAY -> WHEEL_COMPLETE}`; plus `WHEEL_EXITED`,
`WHEEL_HALTED`, `WHEEL_REJECTED`. `VALID_TRANSITIONS` is the single
source of truth; `transition()` is the one choke point every state
change in `src.wheel.lifecycle` goes through — an illegal transition
raises `InvalidWheelTransitionError` rather than silently happening.
`WHEEL_HALTED`/`WHEEL_EXITED` are reachable from any non-terminal state
(escape hatches); `WHEEL_REJECTED` only from `WHEEL_CANDIDATE`.

Every `WheelPosition` carries a persistent, unique `wheel_id`; every
`CspCycle`, `CcCycle`, `WheelEvent`, and `WheelStateTransitionRecord`
references the same `wheel_id` (proven in
`tests/unit/wheel/test_lifecycle.py::TestWheelIdIntegrityAcrossEverything`).

## 5. Initial CSP Entry, Assignment, Covered Calls (Parts 3, 6, 7, 8)

- `src.wheel.lifecycle.open_csp` reuses the existing CSP infrastructure
  — the resulting order is an ordinary `CASH_SECURED_PUT`
  `TradeProposal`; a CSP reserves `strike * 100 * contracts` of capital
  (`src.wheel.accounting.csp_opened`), never trusting premium received
  to justify exceeding deterministic buying-power/risk limits.
- Assignment (`lifecycle.csp_assigned`) creates `100 * contracts` shares
  at cost = the put's strike (Part 6), tracked via
  `WheelAccounting.gross_stock_acquisition_cost`/
  `acquisition_basis_per_share` (tax/accounting-style, weighted-averaged
  across multiple assignment cycles) and, separately,
  `economic_basis_per_share` (acquisition basis reduced by every dollar
  of net Wheel premium collected per share held) — the two bases are
  never conflated; every report shows both.
- Covered calls (`lifecycle.open_cc`) may only be sold against shares
  actually owned: `UncoveredCallError` raises if
  `shares_owned < 100 * contracts`, checked (in source order, not just
  at runtime — see `tests/acceptance/test_wheel_security.py`) before any
  `CcCycle` is constructed. `PaperBroker`'s own independent collateral
  check refuses an uncovered call regardless, as defense in depth.
- Below-basis calls (Part 8): `src.wheel.accounting.below_basis_flags`
  computes `BELOW_ACQUISITION_BASIS`/`BELOW_ECONOMIC_BASIS`
  deterministically and attaches them to the `CcCycle`; they are
  surfaced to the dashboard, the Devil's Advocate, and the Portfolio
  Manager, never silently forbidden (legitimate loss-management calls
  exist) and never silently allowed either.

## 6. Underlying Eligibility (Parts 4, 5)

`src/wheel/eligibility.py::check_wheel_eligibility` deterministically
screens: approved universe, underlying price/volume, option open
interest/volume/bid-ask spread, DTE range, delta range (computed via
Black-Scholes when the provider doesn't supply one — `resolve_put_delta`
— never fabricated; `DATA_INSUFFICIENT`-shaped when IV itself is
unavailable), earnings proximity (`EarningsProximityStatus` — a
`DATA_UNAVAILABLE` status blocks eligibility exactly like
`EARNINGS_IN_WINDOW` does, per Part 5's "missing data must not be
silently interpreted as no earnings"; ETF Wheels flag earnings as
informational-only, per Part 5), cash sufficiency, and reuse (not
reimplementation) of `src.risk.concentration`/`src.risk.correlation`.
IV context (when supplied) is surfaced as both a premium opportunity
and a risk warning, never a standalone reason to proceed (Part 4).

## 7. Assignment Modeling, Called-Away Economics (Parts 6, 9, 10)

`src.wheel.accounting.called_away` realizes stock P&L against the
tax-style acquisition basis (never the economic basis, which would
misstate real gain/loss — see
`tests/unit/wheel/test_accounting.py::TestCalledAwayAccounting
::test_called_away_never_uses_economic_basis_for_the_stock_pnl`).
`summarize_wheel_economics`/`WheelEconomicsSummary` assembles every
Part 9 metric (total premium, realized/unrealized stock and option P&L,
capital committed/max committed, return on committed capital and its
annualized figure, days in Wheel, days holding stock, cycle counts) in
one place, so the dashboard, Fidelity context, and validation reporting
never independently recompute a different number for the same figure.
An unassigned CSP expiring worthless or bought to close (Part 10)
releases reserved cash and returns the underlying to strategy
competition — the Wheel does not automatically sell another put; a new
`wheel_id` must again pass the full pipeline.

## 8. Rolling (Part 11)

No blind automatic rolling exists anywhere. `csp_bought_to_close`/
`cc_bought_to_close` always realize the actual closing fill's P&L first;
a subsequent new option position is a fully independent proposal through
the same eligibility/Quant/Risk pipeline. `ROLL_FROM_TRADE_ID`/
`ROLL_TO_TRADE_ID`-style tracking is expressed through each cycle's own
`proposal_id`/`position_id` fields rather than a separate roll-specific
field, since this codebase has no Risk-Engine-routed roll pipeline to
begin with (see Section 3's CLOSE/ROLL gap note) — "no roll" (a plain
close) is always valid, and `src.validation.wheel_attribution`'s
`roll_count` detects the observable signature (a bought-to-close cycle)
for reporting.

## 9. Risk Model (Part 12)

`src/wheel/risk.py::stress_test_wheel` reprices whatever the Wheel
currently holds across `WHEEL_STRESS_SPOT_SHOCKS = (-0.05, -0.10,
-0.20, -0.30, -0.50)` — Part 12's exact required grid, deeper than the
platform's general `src.risk.stress.STRESS_SPOT_SHOCKS` (+-5/10/20%),
since a Wheel's live risk is entirely on the downside once a put is
short or shares are held; plus the existing `WHEEL_STRESS_VOL_SHOCKS`
grid. `compute_wheel_aggregate_exposure` aggregates reserved CSP cash,
owned shares' current market value, and covered-call obligation shares,
and feeds `total_capital_at_risk` into the exact same
`underlying_exposure_pct`/`sector_exposure_pct` functions every other
strategy's capital-at-risk already goes through — proven in
`tests/acceptance/test_wheel_security.py::TestWheelNeverExemptFromPortfolioLimits`
that no separate, looser Wheel-only concentration rule exists anywhere,
that `config/brokers.yaml`/`config/risk_limits.yaml`/
`src/risk/engine.py` never mention "wheel"/"WHEEL", and that
`src.risk.engine.evaluate_trade_proposal` itself never gets a
Wheel-specific branch.

## 10. Dividends / Early Assignment (Part 13)

Not fabricated: no dividend calendar exists anywhere in this codebase
(a pre-existing, documented gap — `SECURITY_AUDIT.md`'s OP-005 already
names it for covered calls generally). The Wheel does not claim to
model early-assignment risk around ex-dividend dates it has no data
for; `PaperBroker.settle_expiration` (reused unmodified) already
supports deterministic assignment-at-expiration scenarios for testing,
which is what `src.wheel.paper_events`/`backtest_engine` build on.

## 11. PaperBroker Support (Part 14)

`src/wheel/paper_events.py` wires every required auditable event: CSP
opened/bought-back/expires/assigned, stock position created, covered
call opened/bought-back/expires/assigned, shares called away, Wheel
closed/halted — each recorded as a `WheelEvent` on the `WheelPosition`
itself (`src.wheel.models.WheelEventType`), auditable via
`wheel.events`. Opens are submitted only from an already
Risk-Engine-approved `RiskDecisionResult`
(`WheelOrderNotApprovedError` otherwise) via the existing
`validate_and_build_order_request`; expiration settlement reads
`PaperBroker.settle_expiration`'s own `assigned_or_exercised` flag —
never a second, independently-guessed outcome. Assignments create/
remove actual simulated share positions through `PaperBroker`'s own
unmodified position book. No synthetic P&L shortcuts: every dollar
figure recorded on a `CspCycle`/`CcCycle` traces back to a real
`PaperBroker` fill/settlement fact.

## 12. Backtest Support (Part 15)

`src/wheel/backtest_engine.py::run_wheel_backtest` is genuinely
stateful — unlike `src.backtest.engine.run_backtest` (every position an
independent round trip), this engine drives `src.wheel.lifecycle`
through a real day-by-day loop so a CSP assignment feeds directly into
the covered-call phase within the same run (proven in
`tests/unit/wheel/test_backtest_engine.py::TestFullCycleStatefulness`).
Every quote lookup is piped through `assert_no_lookahead_options` before
use, exactly like the general engine. A missing settlement quote raises
`WheelBacktestDataInsufficientError` rather than fabricating one (Part
15's explicit requirement) — proven by
`TestDataInsufficiency::test_missing_settlement_quote_raises_not_silently_skips`.

## 13. Strategy Competition (Part 16)

`StrategyKind.WHEEL` (16th member) plus
`src.strategies.wheel.evaluate_wheel_candidate` let the Wheel compete
in `src.strategies.selector`/`comparison` against every other named
strategy and CASH — reusing the CSP entry's own priced economics
(`build_cash_secured_put_position`) rather than a fabricated
speculative multi-cycle projection, since what happens after entry
(assignment, how many covered-call cycles) is genuinely unknown at
candidate time. The Wheel is never preferentially selected merely for
generating premium — `selector`/`comparison` apply the same
risk-adjusted ranking to every candidate, Wheel included.

## 14. Devil's Advocate Review (Part 17)

`DevilsAdvocateReview.failure_scenarios` already required
`min_length=3` for every review before this amendment — Part 17's "at
least three failure modes" was already a structural schema guarantee,
not newly added. `src/wheel/review_context.py` supplies the 10 named
Wheel-specific failure prompts as reference content
(`WHEEL_DEVILS_ADVOCATE_FAILURE_PROMPTS`), wired into
`DevilsAdvocateInputs.wheel_context` (optional; present only for a
Wheel leg) and the `devil_advocate.md` persona was updated with the
checklist verbatim. Devil's Advocate retains its existing PASS/CAUTION/
REJECT/REPRICE_REQUIRED verdicts and no approval authority.

## 15. Portfolio Manager Review (Part 18)

`src/wheel/review_context.py::WHEEL_PM_CSP_PHASE_QUESTIONS`/
`WHEEL_PM_CC_PHASE_QUESTIONS` supply Part 18's exact question sets
(entry-phase and covered-call-phase, selected by the caller's
`is_new_wheel_candidate` flag), wired into
`PortfolioManagerInputs.wheel_context` and the `portfolio_manager.md`
persona. The Portfolio Manager retains no override authority over the
deterministic Risk Engine, Wheel or not.

## 16. Dashboard (Part 19)

Two new read-only routes, `GET /api/wheels` and
`GET /api/wheels/{wheel_id}` (`src/dashboard/app.py`), both added to
the existing route allowlist test
(`tests/unit/dashboard/test_app_security.py`) that fails loudly on any
unlisted or execution-shaped route. The new "Wheels" panel
(`index.html`/`dashboard.js`/`dashboard.css`) is explicitly labeled
"RESEARCH / PAPER — read only, no execution here" and shows wheel_id,
ticker, state, active CSP/CC strike/expiration/premium, shares owned,
acquisition/economic basis, current price, unrealized P&L, accumulated
premium, total P&L, days active, capital committed, next decision, and
risk status. No POST route exists for a Wheel anywhere.

## 17. Fidelity Manual Tickets (Part 20)

`src/wheel/fidelity_events.py` reuses the unmodified
`FidelityManualProvider`/`ApprovedOrder`/`FidelityTradeTicket`
machinery as-is for CSP/CC opens (SELL TO OPEN, generated by the exact
same Risk-Engine-driven path every other MANUAL-broker strategy uses).
Assignment is recorded as a plain reconciliation event
(`record_wheel_csp_assignment`/`record_wheel_cc_assignment`, forwarding
straight to `src.wheel.lifecycle`) — never a fabricated order, proven
structurally in
`tests/acceptance/test_wheel_security.py::TestNoFidelityAutoExecution`
(neither function's signature accepts anything order-shaped, and
neither ever constructs or touches a `FidelityTradeTicket`).
Discretionary BUY TO CLOSE tickets are documented as a pre-existing
platform gap (Section 3) rather than force-fitting fabricated "max
profit" economics onto `ApprovedOrder`'s opening-trade-shaped schema —
a human closes early in Trader+ the same way they would for a
standalone CSP/CC position today, and reports the fill back via
`record_wheel_buy_to_close_fill`. Risk-approved still does not mean
executed; FILLED still requires explicit human confirmation via the
unmodified `ExecutionConfirmation`/`confirm_fill` mechanism.

## 18. Database / Persistence (Part 21)

`src/wheel/persistence.py::SqliteWheelStore` persists the complete
`WheelPosition` (every cycle, event, and state transition already
nested inside it) as one JSON row per `wheel_id`, round-tripping through
`model_dump_json`/`model_validate_json` — the same pattern
`src.brokers.base.SqliteIdempotencyStore` already uses for `Order`.
`WHEEL_DATABASE_SCHEMA_VERSION` is tracked independently of
`src.validation.session.DATABASE_SCHEMA_VERSION`; no existing V1.1
database table is touched or migrated (the Wheel store is an
entirely new, additive sqlite file/table). Restart recovery is
explicitly tested: create a Wheel with a full CSP+CC history, persist
it, drop the store instance, build a fresh one against the same file,
and prove exact structural equality on reload
(`tests/unit/wheel/test_persistence.py::TestSqliteWheelStoreRestartRecovery`).

## 19. Validation Reporting (Part 22)

`src/validation/wheel_attribution.py::build_wheel_cohort_attribution`
computes: Wheels initiated/assigned, assignment rate, average CSP/CC
premium, average total Wheel return and its annualized figure, average
duration, average/max capital committed, return on committed capital,
a pseudo-equity-curve max-drawdown figure, average stock loss after
assignment, called-away frequency, below-basis call count/rate, roll
count (detected structurally, Section 8), losses avoided/not avoided by
calls, expectancy per Wheel, a tail-loss (worst 20%) average, and
capital efficiency — plus pass-through comparison figures
(`WheelComparisonBenchmarks`: buy-and-hold, standalone-CSP, cash, SPY
benchmark) the caller supplies from this platform's existing backtest/
benchmark machinery. **`win_rate` is deliberately not a field on
`WheelCohortAttribution` at all** (`test_no_win_rate_field`), per Part
22's explicit "do not judge Wheel quality solely on win rate."

## 20. Testing (Part 23)

207 net new tests. State transitions (every named edge + every illegal
edge + terminal-state completeness), accounting (premium/commission/
basis math, called-away P&L, multiple CC cycles), risk (stress scenarios
at the required grid, aggregate exposure, concentration reuse),
assignment (ITM/OTM expiration via a real `PaperBroker`, called-away
accounting), restart/persistence (explicit create-persist-restart-
reload-and-compare proof), and security (no live trading client import,
no Fidelity auto-execution, no Alpaca import, no LLM Risk override, no
hidden naked-option path — 20 dedicated acceptance tests) are all
covered. Full repository suite: **2699 passed, 4 skipped, 0 failed.**
No test was deleted, weakened, or bypassed to make this pass; 3
pre-existing tests that hardcoded "15 total StrategyKind members" were
updated (not deleted) to assert 16, consistent with CLAUDE.md's guidance
for a deliberate architectural expansion.

## 21. Documentation (Part 24)

`README.md` gained a new plain-English §15 ("How the Wheel strategy
works (optional, advanced)"), stating explicitly: "The Wheel is not a
guaranteed-income strategy... premium received does not eliminate stock
downside risk — it only partially offsets it," and that the Wheel may
underperform buy-and-hold or cash. `ARCHITECTURE.md` gained a full §16
technical section. `progress.md` gained a complete Step 22.2 entry.

## 22. Pre-Validation Re-Freeze (Part 25)

`FREEZE_NAME` bumped from `PAPER_TRADING_V1.1` to `PAPER_TRADING_V1.2`
(`MANIFEST_VERSION` `1.1.0` -> `1.2.0`) in place, exactly as V1.1 bumped
V1.0 — the original V1.0 and V1.1 manifests/reports remain untouched
and recoverable at the `paper-trading-v1.0`/`paper-trading-v1.1` tags.
`VALIDATION_MANIFEST.json` gained `wheel_module_hash` and
`wheel_strategy_kind_trade_proposal_eligible` (always `False`).
`verify_freeze()` gained two new standing, independently-executable
structural checks: `wheel_no_live_trading_client` (repo-wide regex grep
of `src/wheel/` for a live trading-client import) and
`wheel_never_becomes_its_own_order_type` (re-derives
`StrategyKind.WHEEL not in TRADE_PROPOSAL_ELIGIBLE` live and compares
against what the manifest claims). `make verify-freeze` now runs 33
checks total, all passing.

## 23. Git Commit, Tag, and Manifest Hash (Part 26)

- **Code commit:** `dac6055efc9fea74c8ae6e617076266bc446dea6` — "Step
  22.2: add a stateful Wheel strategy (pre-validation amendment)".
- **Manifest's recorded `git_commit`:** exactly the SHA above (the
  manifest was generated immediately after this commit, before it was
  itself committed — the same one-commit lag V1.0/V1.1 document as
  deliberate, not a bug).
- **Manifest hash:** `11a3a5972eef325a60889cf466a66d8bfd1d794fefb99dd21b2600e01b4930ea`.
- **Freeze commit** (this report + `VALIDATION_MANIFEST.json` +
  `progress.md`'s Step 22.2 entry together): the commit immediately
  following the code commit in `git log` — see that commit's own message
  for its exact SHA.
- **Git tag:** `paper-trading-v1.2`, applied to the freeze commit.
- **Remote push and tag verification**: reported honestly in the final
  owner-facing summary for this task, exactly as it was for
  `paper-trading-v1.0`/`paper-trading-v1.1` — this report does not claim
  a remote outcome it did not itself independently verify via
  `git ls-remote`.

## 24. Confirmations (Part 27)

- Live trading: **DISABLED** — structurally unchanged (`BrokerEnvironment`
  still exactly `[PAPER]`).
- Automatic Fidelity execution: **DISABLED** — unchanged; assignment is
  a reconciliation event, never a fabricated order (Section 17).
- Fidelity execution mode: **MANUAL_EXECUTION** — unchanged.
- Alpaca: **MARKET_DATA_ONLY** — untouched by this amendment.
- Deterministic Risk Engine: **sole authority**, unchanged — no
  Wheel-specific branch exists anywhere in `src/risk/engine.py`.
- Naked/uncovered calls: **structurally impossible**, proven both at
  the `src.wheel.lifecycle` layer and independently at the
  `PaperBroker` layer.
- 90-day validation: **NOT STARTED.** No cohort created, no Day 1
  snapshot, no trades generated, starting NAV unaltered, no scheduling
  enabled.

**PAPER_TRADING_V1.2: FROZEN. 90_DAY_VALIDATION: NOT_STARTED.
LIVE_TRADING: DISABLED. FIDELITY_EXECUTION: MANUAL_ONLY. ALPACA:
MARKET_DATA_ONLY.**
