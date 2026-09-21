# CLAUDE.md

Repository-specific guidance for any AI assistant (or human) making
further changes to this codebase. Read this before touching
`src/risk/`, `src/llm/`, `src/brokers/fidelity.py`, or anything
described below as part of the "trusted kernel."

## What this project is

A systematic options-trading research and paper-trading platform:
deterministic Quant Engine → deterministic Risk Engine → optional LLM
multi-agent layer (proposal/critique/CIO roles) → PaperBroker
(simulation) or Fidelity manual-execution ticketing (a human types the
order into Fidelity themselves) → dashboard → 90-day validation
protocol. See `ARCHITECTURE.md` for the full technical picture and
`progress.md` for a running log of every implementation step.

## Non-negotiable invariants

These have been enforced, tested, and re-verified across every
implementation step in this project's history. Do not weaken them to
make a test pass, to "simplify," or because a new feature seems to
need it — if a new feature seems to require weakening one of these,
the feature's design is wrong, not the invariant.

1. **The Risk Engine (`src.risk.engine.evaluate_trade_proposal`) is
   the sole authority on what is allowed to become an order.** It may
   APPROVE, RESIZE (only ever *smaller* than requested, never larger),
   REJECT, or HALT. No LLM output, no strategy-evaluation figure, no
   config value can bypass it.
2. **No LLM ever computes an authoritative price, Greek, probability,
   or risk figure.** Every number an LLM-facing schema carries either
   comes from `src.quant`/`src.risk` as read-only context, or is
   explicitly categorical (e.g. `Conviction`, `RiskLevel`) specifically
   so a model can't fabricate a plausible-looking number. See
   `src.llm.schemas`'s module docstrings for the exact reasoning per
   schema.
3. **Fidelity execution is MANUAL ONLY.** There is no code anywhere in
   this repository, and there must never be any, that: connects to an
   undocumented Fidelity API, scrapes Fidelity's website, automates
   Trader+ or browser clicks, stores Fidelity credentials/MFA/session
   cookies, reverse-engineers Fidelity authentication, or automatically
   submits an order. `src/brokers/fidelity.py` only ever produces a
   `FidelityTradeTicket` — text for a human to read and manually type
   in themselves — and tracks its lifecycle via explicit human-supplied
   `ExecutionConfirmation`. A PaperBroker fill must never be
   interpreted as a Fidelity fill, and vice versa.
4. **A broker's approved strategy set is never inferred.** An
   unlisted/unknown strategy for a given broker in
   `config/brokers.yaml` is `REJECT_ACCOUNT_CAPABILITY`. Never add a
   strategy to that file's `allowed_strategies` until the strategy is
   actually implemented end-to-end and tested — this file is a
   capability declaration, not an aspiration.
5. **Every dollar/probability/Greek figure is computed once, in
   Python, and independently cross-checked**, never trusted verbatim
   from an upstream stage. See `src.risk.trade_risk
   .cross_check_quantitative_analysis`.
6. **Capital preservation and deterministic risk control outrank
   return.** The platform's 12-15% annualized target
   (`src.backtest.engine.DEFAULT_TARGET_LOW/HIGH`) is an aspirational
   research target only, evaluated after the fact against realistic
   (never theoretical-midpoint) execution — never a target to size,
   loosen limits, or manipulate assumptions toward.
7. **No live trading exists in this repository.** Every execution path
   is either `PaperBroker` (in-memory simulation) or Fidelity
   MANUAL_EXECUTION (a human enters the order). There is no
   `execution_mode` other than `MANUAL` and `AUTOMATED`-against-paper
   anywhere in `src/brokers/base.BrokerEnvironment`.

## The multi-leg architecture (Step 20A)

As of Step 20A, `src.llm.schemas.TradeProposal.legs` supports 1-4 legs
(previously capped at 2), and `StrategyType` carries all 16 named
strategies from `src.strategies.base.StrategyKind` — including
`LONG_CALL_BUTTERFLY` (3 legs, 1:-2:1 ratio), `SHORT_IRON_CONDOR` (4
legs, 1:-1:-1:1), and `SHORT_IRON_BUTTERFLY` (4 legs, 1:-1:-1:1, shared
center strike). Key mechanisms to understand before touching any
leg-count-sensitive code:

- **`OptionLeg.quantity_ratio`** (default 1): a leg's contract count
  relative to `TradeProposal.contracts_requested`. Every strategy
  before Step 20A used a flat 1:1 ratio on every leg; only
  `LONG_CALL_BUTTERFLY`'s middle (short) leg uses `quantity_ratio=2`.
  Every place that turns a `TradeProposal`/`OptionLeg` into an actual
  order quantity must multiply by this ratio (`src.risk.engine
  ._build_quant_position`/`_build_approved_order`, and
  `src.risk.trade_risk.compute_trade_greeks`).
- **`base_combo_quantity`** (`src.brokers.paper`): the smallest
  per-leg `OrderLeg.quantity` across an order — "1 unit of the
  combo." `PaperBroker`'s fill/collateral/partial-fill logic all
  anchor on this rather than an arbitrary leg's (e.g. `legs[0]`'s)
  quantity, since `TradeProposal.legs` carries no guaranteed
  submission order.
- **Generic payoff engine**: `src.quant.monte_carlo.payoff_profile`
  computes exact max profit/max loss/breakeven(s) for *any* `Position`
  (any leg count, any quantity ratio) via piecewise-linear analysis —
  prefer this over a new hand-derived formula for any future
  multi-leg strategy.
- **Two breakevens, not a list**: `breakeven` +
  `breakeven_upper: float | None` (not `breakevens: list[float]`) is
  the platform's structural representation, since no strategy in the
  16-item library has more than two. It appears on
  `StrategyEconomics`, `QuantitativeAnalysis`, `QuantitativeAnalysisContext`,
  `ApprovedOrder`/`FidelityTradeTicket`, and `OpportunityView`. Never
  silently drop `breakeven_upper` when propagating economics between
  these types — grep for `breakeven_upper` before adding a new
  propagation point.
- **Defined-risk multi-leg collateral**: `PaperBroker
  ._required_collateral` and `src.backtest.engine
  ._estimate_capital_at_risk` both special-case the butterfly (debit
  only, no extra collateral) and the two iron structures
  (`max(put_wing_width, call_wing_width)`, never the sum of both
  widths and never a naked-short-style full-strike reservation)
  *before* falling through to the generic same-right short/long
  pairing logic, which cannot recognize a 2x-ratio leg or two
  same-right short legs on its own.

## Testing discipline

- Run the full suite (`python -m pytest -q` or `make test`) before
  considering any change complete — not just the tests for the file
  you touched.
- Never delete a failing test, weaken an assertion, loosen a numeric
  tolerance without a stated mathematical justification, bypass the
  Risk Engine to make a test pass, or change a risk limit merely to
  make a trade approve.
- A test asserting a now-reversed architectural decision (e.g. the
  Step 19A "Tier2 strategies stay evaluation-only" split that Step 20A
  deliberately reverses) should be *updated* to assert the new,
  intentional behavior — not deleted and not left failing.
- New strategy-shaped functionality gets: structure-validation tests
  (valid + every named malformed-structure rejection), an economics/
  dispatch test, and an end-to-end Risk Engine integration test,
  following the pattern in `tests/unit/risk/test_multileg_strategies.py`.

## Conventions

- Every module/function docstring in this codebase explains *why*, not
  just *what* — match that style rather than writing terse comments.
- Config values (risk limits, broker capabilities, LLM model routing,
  validation protocol parameters) live in `config/*.yaml`, never
  hardcoded in `src/`. Every numeric config value has a matching
  `..._env` override key for ops-time changes without a deploy.
- Update `progress.md` at the end of any implementation step: what was
  built, what was tested (with counts), what bugs were caught and how,
  and what remains open. This file is the project's institutional
  memory — read it before starting new work, and don't let it go
  stale.
