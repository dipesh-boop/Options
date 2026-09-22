# Architecture — Systematic Options Research & Paper-Trading Platform

Status: **design only** — nothing in this document has been implemented yet.
See `IMPLEMENTATION_PLAN.md` for phasing and `progress.md` for live status.

## 1. Goal and constraints

Build a systematic options portfolio management platform pursuing a
**research target of ~12–15% long-term annualized return**, prioritized in
this order:

1. Capital preservation
2. Controlled drawdowns
3. Risk-adjusted return
4. Repeatability
5. Avoidance of catastrophic loss

12–15% is a target the strategy is designed around, not a promise. Every
component below is designed so that a bad LLM output can degrade the
*quality* of a decision but can never produce an *unbounded* loss, an
*unauthorized* order, or a *live* trade.

**Initial strategies:** cash-secured puts, covered calls, put credit spreads
(see §13 for the Step 19A expansion to a 16-item strategy library).
**Initial universe:** SPY, QQQ, IWM, DIA + ~50 liquid large-cap equities
(exact list is config, not code — see `strategies/screener.py` in the plan).
**Explicitly excluded, enforced in code, not just policy:** 0DTE, naked
calls, unfunded naked puts, martingale/loss-doubling sizing, earnings-window
entries, illiquid contracts (min OI / min volume / max spread% gates).
**Trading modes:** `BACKTEST`, `SHADOW`, `PAPER`. **`LIVE` is not built in
this phase** — see §4.

## 2. Governing principle: what the LLM is and isn't trusted for

This is the single most important architectural rule and it constrains
almost every component below, including all four roles inside the
Multi-Agent Layer (§5) — there is no role with elevated privilege over
this table, not even the orchestrating Portfolio Manager.

| Allowed to the LLM (Claude) | Forbidden to the LLM |
|---|---|
| Analyze pre-computed, Python-verified data | Inventing/estimating market data (prices, IV, volume) |
| Research qualitative context (news, sector themes) via read-only tools | Calculating Greeks, P&L, max loss, EV, position size — ever |
| Classify / tag (sector, theme, event risk) | Overriding a deterministic risk-limit rejection |
| Propose a candidate structure, ranking, or ordering | Placing or modifying any order, in any mode |
| Explain a trade's rationale in natural language | Emitting a numeric field the system will trust without recomputation |
| Challenge/red-team a proposed trade ("what could go wrong") | Calling any tool with a side effect (no execute-class tools exist) |

Mechanically this is enforced, not just documented:

- Every tool set in `agent/tools.py` — shared across all four roles —
  contains **only read-only tools** (`get_screened_candidates`,
  `get_portfolio_risk`, `get_market_context`, `get_backtest_summary`, …).
  There is no `place_order`, no `modify_limit`, no `write_*` tool anywhere
  in the agent layer's schema, so it is architecturally incapable of
  causing a side effect, not merely instructed not to.
- Every role's structured output (`agent/schemas.py`, strict Pydantic
  models) has fields like `rationale: str`, `risk_flags: list[RiskFlag]`,
  `conviction: Literal["low","medium","high"]`, `rank: int`, and a
  *declarative* structure intent (symbol/strategy/expiry/strike). None of
  it has a field for max_loss, position size, or any computed risk number
  — there is nothing for the rest of the system to accidentally trust even
  if a model tried to supply one. If a numeric value appears in free text,
  it is never parsed back into a decision path.
- Every candidate the Multi-Agent Layer ever sees has **already passed the
  deterministic Strategy Screener** (liquidity, universe, excluded-
  structure filters) — the agents work within an eligible set, never the
  raw options universe.
- Every proposal the Multi-Agent Layer produces is **independently
  repriced by Python Quant and then gated by Python Risk Engine** (§6, §7)
  before it can become an order in any mode — defense in depth: the
  agents' own output is never the last word on whether a trade proceeds.
- All agent input/output — for all four roles — is persisted to an
  append-only audit table (`agent_decisions`) so every ranking/rationale
  is reviewable after the fact.

## 3. Component map

```
                         ┌───────────────────────┐
                         │ Scheduler / Orchestrator│
                         └────────────┬────────────┘
                                      ▼
                         ┌───────────────────────┐
                         │   Market Data Layer     │
                         │ IBKR / Schwab / mock /  │
                         │      historical         │
                         └────────────┬────────────┘
                                      │ immutable snapshot
                                      ▼
                         ┌───────────────────────┐
                         │   Strategy Screener     │
                         │ universe + liquidity +  │
                         │ CSP/CC/PCS filter        │
                         │ (deterministic, no LLM) │
                         └────────────┬────────────┘
                                      │ eligible candidates
                                      ▼
   ┌───────────────────────────────────────────────────────────────────┐
   │                    Multi-Agent Layer (Claude)                      │
   │                                                                     │
   │                      ┌─────────────────────┐                       │
   │                      │  Portfolio Manager    │                      │
   │                      │  (Opus-tier; synthe-  │                      │
   │                      │  sizes sub-agents into│                      │
   │                      │  a ranked proposal set)│                     │
   │                      └───────────┬───────────┘                     │
   │            ┌─────────────────────┼─────────────────────┐           │
   │            ▼                     ▼                     ▼           │
   │   ┌────────────────┐   ┌──────────────────┐   ┌──────────────────┐│
   │   │  Market Agent    │   │ Strategy Analyst   │   │ Adversarial      ││
   │   │  read-only        │   │ proposes specific   │   │ Reviewer          ││
   │   │  research/context  │   │ structures within    │   │ red-teams each    ││
   │   │                    │   │ the eligible set      │   │ proposal          ││
   │   └────────────────┘   └──────────────────┘   └──────────────────┘│
   │                                                                     │
   │   All output is non-numeric (rationale, risk_flags, conviction,    │
   │   rank, declarative intent) — read-only tools only, no execute tool│
   │   exists anywhere in this layer.                                   │
   └────────────────────────────────┬────────────────────────────────────┘
                                    │ typed TradeProposal
                                    │ (intent only — no trusted numbers)
                                    ▼
                         ┌───────────────────────┐
                         │      Python Quant       │
                         │ recomputes Greeks, P&L, │
                         │ max loss, EV from the   │
                         │ current snapshot — never│
                         │ trusts agent-stated     │
                         │ numbers                 │
                         └────────────┬────────────┘
                                      ▼
                         ┌───────────────────────┐
                         │   Python Risk Engine    │
                         │ RiskGate: sizing,       │
                         │ exposure, correlation,  │
                         │ drawdown, circuit       │
                         │ breaker, versioned      │
                         │ limits — approve/reject │
                         └────────────┬────────────┘
                                      │ approved only
                                      ▼
                         ┌───────────────────────┐
                         │   Trading-mode gate     │
                         │ BACKTEST / SHADOW /     │
                         │ PAPER (LIVE not built)  │
                         └────────────┬────────────┘
                                      ▼
                         ┌───────────────────────┐
                         │ Broker Abstraction Layer│
                         │  → Paper / Backtest      │
                         │    Broker                │
                         └───────────────────────┘

  PostgreSQL persists every stage: snapshots, candidates, agent_decisions
  (full I/O of all four agent roles, append-only), quant/risk evaluations,
  orders, fills, positions, risk_limits (versioned). FastAPI + the
  dashboard read from Postgres only — they compute nothing themselves.
```

## 4. Trading-mode gate (why LIVE "disabled" means *not implemented*)

A config flag that disables live trading is one bug away from being
flipped by accident (bad default, bad env var, bad merge). Instead:

- `TradingMode` is a closed enum: `BACKTEST | SHADOW | PAPER`. There is no
  `LIVE` value in this phase — adding it later requires a code change, a
  new `BrokerClient` capable of live order routing (which doesn't exist
  yet), and an explicit, separate decision; it cannot happen via
  config/env alone.
- `SHADOW`: the full pipeline runs against live market data and produces a
  decision, but the decision is logged only — no order object is even
  constructed.
- `PAPER`: orders are constructed and sent to either (a) a broker's real
  paper-trading endpoint (IBKR supports this natively via a paper account
  on port 7497/4002) or (b) the internal `PaperBroker`, which simulates
  fills against real bid/ask/last data with a slippage+commission model,
  for brokers without a true paper sandbox.
- `BACKTEST`: orders are filled by `BacktestBroker` against historical
  data only; the system must not be able to reach any network broker
  client in this mode (enforced by dependency injection — `BacktestBroker`
  has no network code path at all).
- A separate, independent **kill switch** (DB flag + env var, checked at
  the top of every orchestrator cycle) lets a human halt all trading
  activity in any mode without a deploy.

## 5. Multi-Agent Layer

Refines what was originally a single generic "LLM Agent Layer" into four
distinct roles, matching the intended orchestration design: a Portfolio
Manager coordinating a Market Agent, a Strategy Analyst, and an
Adversarial Reviewer. Every role is bound identically by §2 — read-only
tools only, no output field the rest of the system would trust as a risk
number — and every role's full input/output is written to the
append-only `agent_decisions` table.

- **Market Agent** — read-only research on the screener's eligible
  candidate set: news, macro/sector context, volatility-regime commentary,
  known upcoming events (earnings dates, ex-dividend dates) pulled via
  tools like `get_market_context`. Output is qualitative only — never a
  price, vol, or probability number.
- **Strategy Analyst** — proposes specific structures (symbol, strategy
  type, expiry, strike(s), a target size as a *stated intent*, e.g. "sell
  the ~30-delta put, ~30 DTE") drawn only from the screener's already-
  liquid, already-in-universe candidates. The intent is declarative, not a
  computed number the system trusts — Python Quant recomputes the real
  numbers for whatever specific contract this resolves to.
- **Adversarial Reviewer** — a dedicated red-team role, explicitly
  prompted to argue against each proposal: concentration risk, thesis
  weaknesses, event risk the Strategy Analyst may have missed, correlation
  with existing positions. It may attach a `do_not_advance` flag — an
  **advisory quality filter** that saves downstream attention on a
  proposal the agent layer itself distrusts. It has no authority to
  approve anything either, and its absence or presence never changes what
  Python Risk Engine does.
- **Portfolio Manager** — the orchestrating role. An Opus-tier model is
  recommended here specifically because this role synthesizes three
  sub-agent outputs into a judgment call, where stronger reasoning earns
  its cost; the three sub-agent roles can reasonably run on a
  faster/cheaper tier — a config choice, not fixed in this document. The
  Portfolio Manager produces the single ranked shortlist that leaves the
  Multi-Agent Layer: a list of typed `TradeProposal` objects, each with
  only non-numeric fields (`rationale`, `risk_flags`, `conviction`,
  `rank`, plus the Strategy Analyst's declarative structure intent).

A `TradeProposal` is an **intent object**, nothing more — it carries no
field the rest of the pipeline is allowed to trust as a computed risk
number. It goes to Python Quant next, which prices it from the current
market snapshot exactly as it would if the agent layer didn't exist.

## 6. Python Quant (per-trade calculation, no policy)

Pure, stateless, deterministic calculation for a single proposed
structure — identical math whether that structure came from the
Multi-Agent Layer, a manual API call, or a backtest replay:

- **Greeks & pricing** — Black-Scholes as the base model; since these are
  American-style equity options, early-exercise/dividend risk is handled
  as an explicit flag (assignment-risk check) rather than a full binomial
  tree in v1 (documented approximation, revisited if it proves material).
- **P&L** — realized/unrealized, for the proposed structure and existing
  positions.
- **Max loss / defined risk** — per strategy: CSP = strike − premium (× 100
  × contracts, cash-secured so this is the true worst case); covered call
  = cost basis − premium (stock-to-zero case), upside capped at strike;
  put credit spread = width − credit received. Every strategy in the
  initial set is defined-risk or cash-secured by construction — a
  universe constraint, not just a calculation.
- **Expected value / probability of profit** — delta-approximated,
  cross-checked against a Monte Carlo estimate under the fitted vol
  surface for sanity.

Python Quant never sees or trusts any number the agent layer may have
mentioned in free text; it recomputes from the live/current snapshot only.
It has no concept of limits, NAV, or portfolio state, so it cannot itself
approve or reject anything — its output is an input to Python Risk Engine.

## 7. Python Risk Engine (portfolio policy & gating — the trusted kernel)

Pure Python, deterministic, no LLM calls, the highest test-coverage bar in
the system (unit tests + property-based tests via `hypothesis`). Takes
Python Quant's numbers plus current portfolio state and decides what's
allowed:

- **Position sizing** — fixed-fractional / vol-adjusted sizing against
  portfolio NAV, with hard per-position and per-underlying caps.
- **Portfolio exposure** — net delta/theta/vega, notional exposure vs NAV,
  concentration by underlying and sector.
- **Correlation** — return-correlation matrix across held/candidate
  underlyings to prevent stacking correlated short-premium risk (e.g. 10
  correlated tech CSPs behaving like one large position in a selloff).
- **Drawdown monitor** — trailing high-water mark, current drawdown %,
  trips the circuit breaker at a configured threshold.
- **RiskGate** — the single chokepoint every candidate/order passes
  through, in every mode, **twice** for agent-sourced proposals: once
  informally when the Strategy Screener builds the eligible set the agents
  are allowed to see, and once authoritatively here, after Python Quant
  has priced the agents' actual proposal. Limits are stored in a
  versioned DB table (`risk_limits`), never hardcoded, and every change to
  them is itself logged.
- **Circuit breaker** — kept as simple and heavily tested as possible,
  since a bug here is a silent failure of the platform's core safety
  promise; backed by the independent kill switch from §4, which does not
  depend on the breaker's own logic being correct.

## 8. Broker Abstraction Layer

A single `BrokerClient` interface (`connect`, `get_account`,
`get_positions`, `get_option_chain`, `place_order`, `cancel_order`,
`get_order_status`, `stream_quotes`) implemented by:

- `IBKRBroker` — `ib_insync` against TWS/IB Gateway. Paper account is a
  distinct port (7497/4002), not a flag on the live client.
- `SchwabBroker` — REST + OAuth2. **Open question, not assumed:** Schwab's
  public API does not offer a true paper-trading sandbox equivalent to
  IBKR's; Phase 4 starts with research into current Schwab
  developer-account capabilities before deciding whether Schwab is
  data-only (quotes/chains) with fills simulated by `PaperBroker`, or has
  a usable sandbox. This is called out explicitly so it isn't silently
  assumed away.
- `PaperBroker` — simulates fills against real quotes (mid ± modeled
  slippage, commission schedule) for any broker lacking a native paper
  account.
- `BacktestBroker` — fills against historical bars/chains only; no network
  access; deterministic given a fixed random seed for any modeled
  slippage.

## 9. Data flow (one orchestrator cycle)

1. **Scheduler** triggers a cycle (daily and/or intraday, mode-dependent).
2. **Market Data Layer** pulls quotes/chains (or, in `BACKTEST`, reads the
   historical store) → persisted as immutable, timestamped snapshots in
   Postgres. Snapshots are the source of truth for everything downstream
   in that cycle — no component re-fetches live data mid-cycle, which
   would break determinism and point-in-time correctness.
3. **Strategy Screener** reads the snapshot + universe/liquidity rules
   (min OI, min volume, max spread%, min market cap, DTE window, no
   earnings-week entries) → an eligible candidate list. Deterministic, no
   LLM.
4. **Multi-Agent Layer** (§5): Market Agent and Strategy Analyst work the
   eligible set in parallel; the Adversarial Reviewer critiques each
   resulting proposal; the Portfolio Manager synthesizes all three into a
   ranked list of typed `TradeProposal` objects — non-numeric only.
5. **Python Quant** (§6) independently reprices every proposed structure
   from the current snapshot — Greeks, P&L, max loss, EV — ignoring any
   number the agent layer may have stated in free text.
6. **Python Risk Engine** (§7) gates each priced proposal against current
   portfolio state and the versioned `risk_limits`: position sizing,
   exposure, correlation, drawdown/circuit-breaker status. Anything
   violating a hard limit is rejected here, regardless of the agent
   layer's conviction or ranking.
7. **Orchestrator** applies the trading-mode gate (§4) to whatever
   survives: `BACKTEST` fills via `BacktestBroker`; `SHADOW` logs the
   intended action only; `PAPER` routes the approved proposal to a
   `BrokerClient`.
8. Fills/positions are persisted; portfolio state, exposure, correlation,
   and drawdown are recomputed; the circuit breaker re-evaluates.
9. All Multi-Agent Layer I/O, every Python Quant/Risk Engine evaluation
   (including rejections and why), and every orchestrator decision are
   written to an append-only audit log.
10. The FastAPI + dashboard layer reads Postgres to present chain/risk
    views, portfolio state, backtest results, and the full agent
    rationale trail — it does not compute anything itself.

## 10. Security risks

- **Secrets** (Anthropic API key, IBKR gateway creds, Schwab OAuth
  tokens/refresh tokens) — env vars / secrets manager only, never
  committed, never logged, **never included in any LLM prompt or tool
  response**.
- **Prompt injection** — any ingested external text (news, filings,
  social sentiment) fed to the Market Agent or any other role is
  untrusted input; the strict typed output schema and the absence of
  execute-class tools are the primary defense (an injected instruction has
  no side-effecting tool to invoke, in any of the four roles).
- **Excess LLM privilege** — mitigated structurally per §2 and §5, not by
  instruction-following alone. No role, including the Portfolio Manager,
  has a path to a trusted numeric field or a side-effecting tool.
- **SQL injection** — ORM/parameterized queries only, no raw string
  interpolation into SQL.
- **Broker credential compromise** — minimally scoped tokens; paper vs.
  live credentials (where they exist) are never the same secret or the
  same config path, so a leaked paper credential cannot touch a live
  account.
- **Supply-chain risk** — pinned dependencies + lockfile, `pip-audit` (or
  equivalent) in CI.
- **Audit log integrity** — append-only table for agent decisions,
  quant/risk evaluations, and orchestrator actions; no update/delete path
  in the application layer.
- **Accidental live trading** — architecturally prevented in this phase
  because no live order-routing code exists (§4), not just configured off.
- **Broker API rate limits** — backoff/retry with jitter (`tenacity`),
  respecting documented rate limits, to avoid account restrictions that
  would themselves become an operational risk.
- **Data at rest** — encrypt sensitive columns (tokens, credentials);
  restrict DB roles (the API/web layer should hold a read-mostly DB role
  distinct from the orchestrator's read-write role).

## 11. Trading-system failure modes

- **Stale/erroneous market data** → freshness checks on every snapshot;
  reject-and-skip rather than compute risk on stale quotes.
- **Broker disconnect mid-order** → idempotent order keys, persisted order
  state machine, reconciliation against the broker's actual state on
  reconnect/startup — never blind-retry a submit.
- **Partial fills on multi-leg spreads (legging risk)** → a partially
  filled put credit spread is treated as naked exposure by Python Risk
  Engine until the fill completes or is unwound; this is itself a hard
  limit check, not an afterthought.
- **Clock/timezone bugs** around market hours and expiration →
  **resolved in Step 22**: `src.data.market_calendar` is the single,
  centralized module for every market-hours/holiday question (is
  today a trading day, is the market open right now, regular/early
  close time, next trading day/open), computed from stable NYSE
  observance rules (not a hardcoded date table) using stdlib
  `zoneinfo` for correct Eastern/UTC/DST handling — deliberately not a
  third-party exchange-calendar dependency (see that module's own
  docstring for why: this codebase has never taken a `pandas`
  dependency, and `pandas_market_calendars` would pull in `pandas`
  plus several unrelated calendar packages for a single-market need).
  Every function rejects a naive datetime outright. See
  `tests/unit/data/test_market_calendar.py` for the full deterministic
  test suite (holidays, early closes, weekends, DST transitions,
  UTC/Eastern conversion).
- **Corporate actions** (splits, dividends, ticker/OCC symbol changes)
  breaking contract identity → an instrument-mapping/adjustment layer;
  screener, quant, and risk engine must not silently misprice a
  post-split contract.
- **Early assignment risk** (American-style options, esp. covered calls
  near ex-dividend) → explicit assignment-risk flag from Python Quant,
  not left implicit in the Greeks.
- **LLM hallucination / injected false context** → no numeric trust path
  (§2, §5), full audit logging, and — at least through the PAPER phase —
  a human-in-the-loop review point before Multi-Agent Layer output changes
  what gets shown as top-of-list.
- **Multi-agent groupthink** (Portfolio Manager, Strategy Analyst, and
  Adversarial Reviewer converging on the same blind spot since they share
  a model family/prompt lineage) → the Adversarial Reviewer's prompt is
  deliberately adversarial rather than collaborative, and neither its
  presence nor its silence changes what Python Risk Engine independently
  allows — the deterministic gate is the real backstop, not agent
  consensus.
- **Config/limit drift** → `risk_limits` table is versioned; every change
  logged with who/when/old→new.
- **Backtest overfitting / lookahead bias** → strict point-in-time data
  discipline (only data available as-of the simulated date is visible to
  the backtest engine), walk-forward validation, out-of-sample holdout.
- **Correlated tail risk** → portfolio-level correlation limits so the
  system can't unknowingly build one large concentrated bet out of many
  "independent" positions.
- **Liquidity evaporation** → liquidity screened at entry *and*
  continuously monitored, position size capped relative to OI/volume, not
  just checked once at entry.
- **Silent circuit-breaker failure** → the circuit breaker/drawdown
  monitor gets the highest test-coverage priority in the codebase, plus
  the independent kill switch from §4 as a backstop that doesn't depend on
  the breaker's own logic being correct.
- **Duplicate/double execution** on retry or crash-restart → idempotency
  keys on every order, persisted state machine, startup reconciliation.
- **Wrong-mode trading** → covered structurally in §4; additionally, mode
  is logged on every decision row so a post-hoc audit can always show
  which mode produced which action.

## 12. Open questions (deliberately not decided yet)

These are flagged rather than silently assumed, per the instruction not to
invent facts:

1. **Historical options data for backtesting** — IBKR/Schwab do not provide
   deep historical options chain history well-suited to backtesting. A
   vendor decision (e.g. CBOE DataShop, ORATS, Polygon.io, ivolatility) or
   a documented synthetic-pricing approximation (historical underlying +
   fitted vol surface) is needed before Phase 2 can start in earnest.
2. **Schwab paper-trading capability** — needs direct research against
   current Schwab developer docs before Phase 4; not assumed here.
3. **Sector/classification data source** for the correlation and
   concentration checks (e.g. GICS sector mapping for the ~50-name
   universe) — vendor or static mapping TBD.
4. **Exact universe list** (the ~50 large-cap names) — to be finalized as
   a config artifact with explicit liquidity criteria, not hardcoded
   ad hoc.
5. **Model tier per Multi-Agent Layer role** — Opus-tier is recommended
   for the Portfolio Manager; which tier each of Market Agent, Strategy
   Analyst, and Adversarial Reviewer should run on (cost vs. quality) is
   a config decision to be tuned empirically, not fixed here.
6. **Resolved by Step 20A** (§14): `TradeProposal`'s leg cap was widened
   from 2 to 4, and `src.risk.engine`/`src.risk.trade_risk`/
   `src.brokers.fidelity`'s leg-count assumptions were extended
   end-to-end, so `LONG_CALL_BUTTERFLY`/`SHORT_IRON_CONDOR`/
   `SHORT_IRON_BUTTERFLY` are now order-eligible like the other 12.
   Left open by Step 20A: the backtest engine's `BacktestLeg
   .quantity_ratio` field (added so `compute_fill`'s existing ratio-
   aware pricing applies to a backtested butterfly) has no candidate-
   generation call site actually constructing a `LONG_CALL_BUTTERFLY`
   `EntrySignal` yet — `run_backtest` takes fully-formed entries from
   its caller (there is no internal per-strategy candidate generator to
   extend), so this is a "someone must supply the entries" gap, not a
   pricing-correctness one.

## 13. Strategy Competition Engine (Step 19A)

Step 19A expanded the original 3-strategy set into a 16-item library
(15 real `StrategyKind` members plus NO_TRADE/CASH, which is
deliberately not a `StrategyKind` member at all — see below) and
replaced any implicit "one regime maps to one strategy" assumption with
an explicit competition: for every opportunity, generate every strategy
a market/volatility view and the portfolio's current holdings make
*viable*, price all of them identically, and select the single best
risk-adjusted candidate — never the one with the largest theoretical
profit.

**New top-level package: `src/strategies/`.** Sibling to `src/llm`,
`src/quant`, `src/data`, `src/brokers`, `src/risk`, `src/workflows`,
`src/validation` — the `src/options_platform` packaging question from
§12/`IMPLEMENTATION_PLAN.md` §6 remains open and is not resolved by this
addition. One module per strategy (`cash_secured_put.py`,
`covered_call.py`, `put_credit_spread.py`, `call_credit_spread.py`,
`bull_call_spread.py`, `bear_put_spread.py`, `protective_put.py`,
`protective_collar.py`, `long_call.py`, `long_put.py`,
`long_straddle.py`, `long_strangle.py`, `long_call_butterfly.py`,
`iron_condor.py`, `iron_butterfly.py`), each a thin leg-builder around
a single shared assembler (`base.py::build_strategy_evaluation`) that
performs **no independent financial calculation of its own** — every
dollar/probability/Greek figure it emits comes from `src.quant`
(`payoff_profile`, `net_greeks`, `monte_carlo_pop_and_ev`,
`stress_test`), the same "Python computes, LLM never invents a number"
rule from §2, now also enforced strategy-by-strategy rather than only
proposal-by-proposal.

**Tier1/Tier2 split — reversed by Step 20A.** Through Step 19A/14B, 12
strategies fit `TradeProposal`'s 1-2 leg cap and were wired all the way
through the trusted kernel, while the remaining 3
(`LONG_CALL_BUTTERFLY`, `SHORT_IRON_CONDOR`, `SHORT_IRON_BUTTERFLY`,
needing 3-4 legs) were evaluation-only. Step 20A (§14) extended the
trusted kernel's leg-count assumptions end-to-end, so all 15
`StrategyKind` members are now order-eligible —
`src.strategies.base.TRADE_PROPOSAL_ELIGIBLE` includes all 15, with
zero logic changes needed there (it was built in Step 19A specifically
to require none once `StrategyType` gained the 3 new members).

**Explicitly excluded from the library, per Step 19A's own instruction,
same as the original universe-exclusion list in §1:** naked short
calls, any undefined-risk short call, unfunded naked puts,
martingale/loss-doubling sizing, and any other unlimited-risk
structure — none of the 16 items has unbounded downside; the only
unbounded-*upside* members (`LONG_CALL`, `LONG_STRADDLE`,
`LONG_STRANGLE`, `PROTECTIVE_PUT`) are long-premium, defined-*loss*-at-
entry structures, and `QuantitativeAnalysis.max_profit` is the one
field in the Risk Engine's Pydantic contract permitted to be `+inf`
(NaN and `-inf` remain rejected) specifically to represent them
honestly rather than forcing a fabricated cap.

**Strategy families (`StrategyFamily`, 9 members: INCOME, BULLISH,
BEARISH, NEUTRAL, VOLATILITY_EXPANSION, VOLATILITY_CONTRACTION,
PORTFOLIO_PROTECTION, TAIL_RISK_HEDGE, CAPITAL_PRESERVATION).**
Deliberately multi-valued per strategy (`STRATEGY_FAMILIES: dict[StrategyKind,
tuple[StrategyFamily, ...]]`) — a covered call is both INCOME and
mildly BULLISH-capped; a protective collar is PORTFOLIO_PROTECTION,
TAIL_RISK_HEDGE, and CAPITAL_PRESERVATION at once. Never collapsed to a
single label.

**Regime-to-candidate mapping is a guideline table, not a trading
rule.** `src.strategies.regime_mapping.CANDIDATE_STRATEGIES_BY_VIEW`
maps each of 8 `MarketView` values to a *tuple* of strategies worth
constructing and pricing — order within a tuple is not a ranking.
Nothing here decides a trade; every constructed candidate still goes
through suitability filtering, quant pricing, Devil's Advocate,
Portfolio Manager, and the Risk Engine before anything can be selected.

**Position-aware suitability (`src.strategies.suitability`).** A
share-requiring strategy (covered call, protective put, protective
collar) is never even *constructed* unless the portfolio already holds
enough shares of the underlying (100 × contracts) — the structural
guarantee behind "never misclassify a bare short call as a covered
call": an unsuitable strategy is filtered out before pricing, not
flagged after the fact.

**Comparison and selection (`src.strategies.comparison`,
`src.strategies.selector`).** `risk_adjusted_score = expected_value /
maximum_loss` is the ranking metric — expected value *per dollar of
defined risk*, deliberately not raw expected return, so that a
25%-return/large-max-loss candidate does not automatically outrank a
14%-return/small-max-loss one (Step 19A's own worked example).
`rank_candidates` never collapses the full comparison table
(`ComparisonRow`, one row per candidate) into just the winning score —
the table is preserved for counterfactual tracking, the same
"structurally incapable of collapsing into one number" discipline
`src.validation.scorecard` already applies to the 90-day report.
**NO_TRADE/CASH is not a `StrategyKind` member at all** — it is
represented structurally as `SelectionOutcome.selected is None`, and it
wins whenever no surviving candidate's `risk_adjusted_score` clears a
configurable `no_trade_hurdle` (default `0.0`) or every candidate is
rejected by Devil's Advocate, the Risk Engine, or portfolio-fit
screening (`src.strategies.portfolio_fit`, reusing `src.risk
.portfolio_risk`'s concentration/correlation helpers directly).

**Volatility engine (`src.strategies.volatility_engine`).** Compares
implied vs. realized volatility and computes required-move percentages
for straddles/strangles against the market's own implied expected move
and (where supplied) a historical move distribution. Every function
here returns a comparison figure only (a spread, a ratio, a required-
move percentage) — never a "buy"/"sell"/"approved" verdict. This is the
literal enforcement of "do not buy volatility simply because an event
exists" and "do not sell volatility merely because IV is elevated": the
module structurally cannot emit a trading verdict, so that judgment
stays where §2 already puts it — the qualitative agent roles and the
deterministic Risk Engine, never a Python heuristic pretending to be
one.

**Validation-cohort integration (`src.validation.cohort`,
`src.validation.counterfactual`).** Extends, never replaces, Step 19's
validation system. `has_cohort_started`/`decide_cohort_transition`
implement the rule this step was given verbatim: if no formal 90-day
cohort has ever recorded a trade/snapshot/rejected-outcome/violation,
finish the expansion and start a fresh cohort directly; if one has,
preserve it under its own label (or `PRE_EXPANSION_VALIDATION` if none
was ever explicitly given) and start a new, separately-tracked cohort
— results from materially different strategy architectures are never
mixed into one cohort's numbers. `src.validation.counterfactual` prices
every non-selected alternative for a concluded opportunity under the
same realistic execution assumptions (never hindsight at construction
time) and aggregates Selection Regret / effectiveness-by-regime only
once a sample crosses a configured minimum size — a single trade is
never enough to judge the selector.

**Cross-regime robustness (`src.backtest.regime_scenarios`).** 8 named
regimes (BULL/BEAR/SIDEWAYS/HIGH_VOLATILITY/LOW_VOLATILITY/
VOLATILITY_EXPANSION/VOLATILITY_CONTRACTION/MARKET_CRASH) as a
deterministic, seeded test-fixture generator — `src.backtest.engine`
was already strategy-agnostic and needed no new logic. The property
actually proven is "every defined-risk strategy's `maximum_loss` bound
holds against every simulated terminal price in every regime," never a
P&L-sign or win-rate requirement in any regime — no strategy is
required to win everywhere.

**Step 14B refinements** (same system, no rebuild — every change below
is additive or a targeted bug fix over the §13 architecture above):

- `StrategyFamily` renamed to Step 14B's own exact vocabulary
  (`DIRECTIONAL_BULLISH`/`DIRECTIONAL_BEARISH`/`NEUTRAL_RANGE` replacing
  `BULLISH`/`BEARISH`/`NEUTRAL`), keeping `TAIL_RISK_HEDGE` as a
  documented 9th value beyond the 8 Step 14B names, since neither step
  said the classification must be *exactly* 8 and no more.
- The ranking rule (`risk_adjusted_score`/`rank_candidates`) moved into
  its own module, `src.strategies.ranking` — the file Step 14B names
  explicitly, separate from `src.strategies.comparison`'s metrics-table
  builder, so the ranking formula is independently testable.
- A 9th `MarketView`, `LOW_IV_EXPANSION_EXPECTED`, added alongside the
  original 8: distinct from `LARGE_MOVE_EXPECTED` (direction uncertain)
  by explicitly allowing directional candidates (long call/put, debit
  spreads), matching Step 14B's own "low IV, expecting expansion" list.
- **Two capital-requirement bugs found and fixed**, both pre-dating
  Step 14B (present since the original 3-strategy platform, only
  becoming reachable in practice once Step 19A's spread strategies
  existed to trigger them): (1) `PaperBroker._required_collateral` and
  `src.backtest.engine._estimate_capital_at_risk` both charged a debit
  vertical spread (bull call spread, bear put spread) the same
  full-strike-width collateral a *credit* spread of that width needs —
  wrong, since a debit spread's maximum loss is already the premium
  paid; both now use the same strike-ordering rule
  (`_is_credit_pairing`, imported into the backtest engine rather than
  reimplemented) `TradeProposal`'s own leg validators already encode.
  (2) Both also silently reported **zero** capital at risk for any
  pure-long position (long call/put, long straddle/strangle, a fresh
  protective put) — fixed by using the actual debit paid instead of an
  empty short-strikes sum. `PaperBroker` additionally gained a
  preflight cash-affordability check in `attempt_fill` (a debit that
  would take the account's cash negative is now rejected, never
  silently filled) — a gap that existed for every debit-only strategy
  and was closed at the same time as the collateral fix.
- `src/validation/strategy_attribution.py` (new): per-strategy
  performance tracking — trades/wins/losses/win rate/net P&L/return on
  capital/expectancy/profit factor/a per-trade Sharpe-like ratio
  (explicitly documented as an approximation, not the rigorous
  equity-curve Sharpe `src.backtest.metrics` computes portfolio-wide)/a
  drawdown-*contribution* figure/average holding period/average
  slippage/performance by regime — reusing
  `src.research.performance_breakdown.breakdown_by` directly rather
  than a second grouping implementation. Answers Step 14B's six named
  attribution questions (which strategies generated profit, reduced
  losses, consumed capital without value, are regime-specific, improved
  drawdown, increased tail risk, generated excessive trading costs)
  with plain deterministic threshold rules over those numbers — never
  an LLM judgment call. Hedge strategies (protective put/collar) are
  judged by their family classification, never standalone P&L sign,
  per Step 19A's own rule.
- A final system test (`tests/unit/strategies/test_final_system_scenarios.py`)
  runs the full MARKET DATA → REGIME → CANDIDATES → QUANT → COMPARISON
  → verdicts (supplied) → SELECTION-OR-NO_TRADE pipeline across the 8
  scenarios Step 14B names (STRONG/MODERATE BULL, SIDEWAYS LOW
  VOL/HIGH IV, STRONG BEAR, VOLATILITY EXPANSION/CONTRACTION, PORTFOLIO
  CRASH), asserting only that a valid outcome exists and every
  candidate's own defined-risk bound holds against that scenario's
  simulated terminal prices — never which strategy wins.

**Step 19A gap-check re-ask.** A follow-up restatement of Step 19A's
own objective, closely overlapping what §13 above already delivers.
Re-inspecting every named component (Quant/Risk Engines, Market Regime
Agent, Portfolio Manager, Devil's Advocate, PaperBroker, Backtesting
Engine, Fidelity constraints) against this restatement surfaced two
genuine, previously-unbuilt pieces, both additive:

- `ManagementConditionType` (`src.strategies.base`, 9 values —
  `PROFIT_TARGET`/`MAX_LOSS`/`DTE_EXIT`/`THESIS_INVALIDATION`/
  `DELTA_THRESHOLD`/`VOLATILITY_CHANGE`/`ROLL_EVALUATION`/
  `ASSIGNMENT_MANAGEMENT`/`EXPIRATION_MANAGEMENT`) plus
  `MANAGEMENT_CONDITION_TYPES`, a per-`StrategyKind` table auto-wired
  into `build_strategy_evaluation` (no per-strategy module changes
  needed, the same pattern `STRATEGY_FAMILIES` already established).
  `StrategyEvaluation.entry_rules`/`exit_rules`/`adjustment_rules`/
  `invalidation_rules` were free-text prose only — descriptive, but not
  backed by a closed type. This enum is the actual mechanism behind
  "LLMs may interpret conditions, they may NOT improvise risk rules":
  the *categories* of management logic that apply to a given strategy
  are now a closed, Python-determined set, not something free text
  alone could be trusted to constrain.
- `src.validation.strategy_attribution.funnel_counts_by_strategy` +
  `StrategyFunnelCounts` (opportunities_considered/trades_proposed/
  trades_rejected/trades_entered) and a new `avg_capital_deployed`
  field on `StrategyPerformanceSummary` — the decision-time funnel this
  step names explicitly, distinct from that module's existing
  completed-trade statistics. Sourced from `src.validation
  .counterfactual.StrategyAlternativeRecord` (already captured at
  decision time for every serious candidate, selected or not) rather
  than a second decision-tracking mechanism.

**Three dedicated reports, built on the tracking above.**

- **Multi-Strategy Attribution Report**
  (`src.validation.strategy_attribution.build_multi_strategy_attribution_report`)
  — one row per all 15 `StrategyKind` values always, funnel counts and
  completed-trade performance (now including a `volatility_regime`
  breakdown, a 13th `src.research.performance_breakdown
  .AnalysisDimension` added alongside Step 14's original 12) merged
  side by side, `INSUFFICIENT_SAMPLE`-labeled per row via a dedicated
  20/50-trade threshold pair (deliberately smaller than the whole
  90-day run's own 50/100, since a single strategy's slice needs a
  lower bar).
- **Strategy Selection Report**
  (`src.validation.selection_report.build_strategy_selection_report`)
  — whether the Selector itself adds value: selection frequency,
  risk-adjusted value (expectancy per dollar deployed), and a new
  `dynamic_vs_fixed_strategy_comparison` (`src.validation.counterfactual`)
  answering "is dynamic selection beating a simpler always-use-this-
  strategy baseline" — `None`, never a guessed answer, below a
  meaningful sample on either side. Four of its six named questions
  reuse `answer_attribution_questions` directly rather than
  re-answering them.
- **Hedge Effectiveness Report** (`src.strategies.hedge_effectiveness`)
  — protective put/collar evaluated by a **paired** Monte Carlo
  comparison against a synthetic unhedged all-shares baseline, both
  drawn from the identical simulated terminal-price sample (new
  `simulated_terminal_payoffs`/`tail_mean_payoff` in `src.quant
  .monte_carlo`, also now the single implementation
  `src.strategies.base.build_strategy_evaluation`'s own expected-
  shortfall calculation calls, replacing what used to be inline,
  duplicated logic). Reports hedge cost, drawdown avoided, tail loss
  avoided, CVaR reduction, portfolio volatility reduction, upside
  sacrificed, and net hedge benefit — structurally incapable of
  emitting a success/failure verdict, since neither
  `HedgeEffectivenessReport` nor its aggregate has a field for one,
  directly enforcing "never label a hedge unsuccessful merely because
  its standalone P&L is negative."

## 14. Multi-leg structural support (Step 20A)

Step 20A widened the trusted kernel end-to-end so `LONG_CALL_BUTTERFLY`,
`SHORT_IRON_CONDOR`, and `SHORT_IRON_BUTTERFLY` (§13) are order-eligible,
not just evaluable. Audit finding that shaped the implementation: most of
the "trusted kernel" already supported 4 legs at the Pydantic-field level
before this step (`src.brokers.base.PlaceOrderRequest.legs`,
`src.brokers.fidelity`'s `ApprovedOrder.legs`/`FidelityTradeTicket.legs`,
`src.risk.portfolio_risk.PortfolioPositionLeg`-list, `src.dashboard.schemas
.leg_quotes` were all already `max_length=4`) — the actual blockers were
`src.llm.schemas.TradeProposal.legs` (`max_length=2`), the missing
`StrategyType` members, and several places that assumed a flat 1:1
per-leg quantity ratio.

**`TradeProposal.legs`**: widened to `max_length=4`. `StrategyType`
gained `LONG_CALL_BUTTERFLY`/`SHORT_IRON_CONDOR`/`SHORT_IRON_BUTTERFLY`.
`TradeProposal._validate_legs_match_strategy` gained one `elif` branch
per new strategy enforcing its exact leg count, strike ordering, side,
and quantity ratio (fail-closed: malformed structures — missing wing,
wrong option type, wrong strike order, wrong quantities, mismatched
center strike, a long-iron-butterfly-shaped payload — are rejected at
the Pydantic layer before reaching Python Quant at all).

**Quantity ratios**: `OptionLeg.quantity_ratio` (default 1, additive)
represents a leg's contract count relative to `contracts_requested` —
needed because `LONG_CALL_BUTTERFLY`'s middle strike is short *2x* each
wing's quantity (1:-2:1), the first non-uniform ratio this platform has
had to represent. Every downstream consumer that turns a leg into an
actual order/position quantity multiplies by this ratio:
`src.risk.engine._build_quant_position`/`_build_approved_order`,
`src.risk.trade_risk.compute_trade_greeks`, and (on the execution side)
`src.brokers.paper.base_combo_quantity`/`compute_fill`/`_apply_fill`,
which anchor PaperBroker's fill quantity, net-price weighting, and
partial-fill tracking on the smallest per-leg quantity across an order
("1 combo unit") rather than an arbitrary leg's own quantity — necessary
because `TradeProposal.legs`/`ApprovedOrder.legs` carry no guaranteed
submission order, so "the first leg" is not a safe proxy for "the
combo's base size."

**Economics**: three new functions in `src.quant.expected_value`
(`long_call_butterfly_economics`, `short_iron_condor_economics`,
`short_iron_butterfly_economics`) build a `Position` and delegate to the
existing generic `payoff_profile` engine (§6/§13) for max profit/max
loss/breakevens — an exact, piecewise-linear computation, not a new
hand-derived formula — plus a full Monte Carlo simulation for
probability-of-profit/expected-value, since these structures' profit
zone sits *between* two breakevens (unlike every prior strategy's
single-sided or symmetric-tail zone), so the existing
probability_above/probability_below closed forms don't apply.
`src.risk.trade_risk.compute_trade_economics`/`resolve_credit`/
`check_collateral` dispatch to these via a new `_legs_sorted_by_strike`
helper (sorting `(strike, put-before-call)` deterministically recovers
each leg's structural role, since `_matching_by_right`'s single-match
lookup is ambiguous once a strategy has two same-right legs).

**Two breakevens, not a list**: `breakeven_upper: float | None`
(additive, mirrors the pre-existing straddle/strangle field) now
actually propagates all the way to `ApprovedOrder`/`FidelityTradeTicket`
and `OpportunityView`, not just `StrategyEconomics` — a genuine
pre-existing gap (straddle/strangle's second breakeven was computed but
silently dropped before reaching a human-readable ticket) that Step 20A's
"never discard extra breakevens" requirement caught and fixed for all
four two-breakeven strategies at once.

**Collateral**: `PaperBroker._required_collateral` and
`src.backtest.engine._estimate_capital_at_risk` both gained explicit
shape-detection for the butterfly (debit paid only, zero extra
collateral — the previous fallback would have double-charged the short
middle leg as if naked) and the two iron structures
(`max(put_wing_width, call_wing_width)`, standard margin treatment —
the previous fallback either summed both widths or reserved the full
short-strike notional, both wrong) *before* falling through to the
generic same-right short/long pairing logic, which cannot recognize a
2x-ratio leg or two same-right short legs.

**PaperBroker fill model**: `compute_fill` now separately tracks
per-share leg prices (`raw_signs`, unweighted — used for actual cash
flow/position updates) from the combo's net, per-unit price
(`weighted_signs`, scaled by each leg's `quantity_ratio` — used for
limit-price satisfaction and fillable-quantity checks), so a butterfly's
net debit correctly reflects its middle leg counting double without
distorting any individual leg's own per-share transaction price.
Commission is charged per actual contract filled (so the middle leg
pays 2x), consistent with a standard per-contract-per-leg commission
schedule. Atomic-vs-per-leg model: this platform simulates all legs of
one order filling together in a single `attempt_fill` pass (never a
partial subset of an order's legs) — there is no intermediate state
where a multi-leg position holds some legs and not others, so a
multi-leg position can never become unintentionally naked through
partial execution.

**Backtest engine**: `BacktestLeg` gained the same `quantity_ratio`
field (default 1), threaded through `_to_order_leg`/`flip_legs` so
`compute_fill`'s ratio-aware pricing applies identically to a
backtested butterfly. `src.backtest.simulator.EntrySignal` already took
fully-formed legs from its caller (no internal per-strategy candidate
generator exists to extend), so this is sufficient for correctness —
see §12 open question 6 for what's still missing (a caller that
actually constructs one).

**Regime mapping**: `src.strategies.regime_mapping` gained
`LONG_CALL_BUTTERFLY` under `NEUTRAL_RANGE_BOUND` and
`HIGH_IV_CONTRACTION_EXPECTED` (a genuine gap — `SHORT_IRON_CONDOR`/
`SHORT_IRON_BUTTERFLY` were already present in both views since Step
19A, but the butterfly was never added to either).

**Config**: `config/brokers.yaml` added all 3 strategies to every
broker's `allowed_strategies` — done last, after the full pipeline was
proven end-to-end by `tests/unit/risk/test_multileg_strategies.py` and
`tests/unit/brokers/test_paper_broker_multileg.py`, per this file's own
standing rule (never list a capability before it's backed up).

**Not touched, deliberately**: `src.strategies.selector`/`comparison`/
`portfolio_fit`/`suitability` needed no changes — they were already
generic over `StrategyEvaluation` lists with no `StrategyKind`-specific
branching. `src.validation.strategy_attribution` already iterates all
15 `StrategyKind` values (built that way in Step 14B specifically so a
future Tier2-to-Tier1 promotion would need no further changes there).

## 15. Alpaca market-data-only provider (pre-validation amendment, "Step 22.1")

Adds Alpaca (`src/data/alpaca_provider.py`, `src/data/alpaca_historical.py`)
as a third `MarketDataProvider` implementation alongside `mock` and `ibkr`,
so the 90-day validation can run against real current U.S. equity and
options market data (OPRA, when entitled) without requiring an IBKR
account. **Structurally market-data-only**: `AlpacaMarketDataProvider`
implements `src.data.provider.MarketDataProvider`'s two read methods
(`get_option_chain`, `get_underlying_quote`) and nothing else — it is not
a `Broker` subclass, defines no order-submission/cancellation/modification
method, and never imports `alpaca.trading` (Alpaca's separate,
order-submission client) anywhere. See
`tests/acceptance/test_alpaca_market_data_only.py` for the full
structural/security proof (repo-wide `alpaca.trading` import grep,
public-surface-equals-`MarketDataProvider` check, credential-leakage
checks, socket-level no-network-when-faked proof).

**Official SDK**: `alpaca-py` (pinned in `requirements.txt`). Only two
client classes/methods are called: `StockHistoricalDataClient
.get_stock_latest_quote` and `OptionHistoricalDataClient.get_option_chain`
(plus `.get_stock_bars` for the separate, minimal historical-bars adapter).
Confirmed directly against the SDK source (not assumed) before
implementation.

**OCC symbol parsing, not a second Alpaca endpoint**: Alpaca's option
chain response is keyed by standard OCC-format option symbols (root +
YYMMDD expiration + C/P + zero-padded strike), which are fully
self-describing — `parse_occ_option_symbol` decodes strike/expiration/
right directly from the key string rather than calling Alpaca's separate
option-contracts-metadata endpoint (which lives on the *trading* API this
module deliberately never imports).

**OPRA vs indicative, never silently substituted**: `AlpacaConfig
.options_feed` (`"opra"` or `"indicative"`) is explicit config, requested
exactly as configured — if Alpaca's account isn't entitled to the
requested feed, `AlpacaFeedEntitlementError` raises immediately rather
than silently falling back to the other feed. Every canonical
`OptionContract`/`UnderlyingQuote`'s own `source` field records exactly
which feed served it (e.g. `"alpaca_opra"`, `"alpaca_indicative"`,
`"alpaca_sip"`, `"alpaca_iex"`) — reusing the pre-existing, deliberately
open-string `source` field (`src.data.provider`'s own docstring: "not a
closed enum... locking the type now would force a premature choice")
rather than adding a new canonical-schema field for feed type.

**Provider selection** (`src/data/factory.py`, new): the first place in
this codebase that actually turns `OPTIONS_AGENT_DATA_PROVIDER`
(`mock`/`ibkr`/`alpaca`) into a constructed provider instance — nothing
before this selected a provider at runtime; `/morning-scan` (a Claude
Code skill) and `src.dashboard.service`/`src.workflows.feed_health` all
take already-fetched `OptionChain`s as plain parameters. Fails closed on
a misconfigured/uncredentialed real provider — never silently falls back
to `mock`.

**Provider health check** (`src/data/provider_health.py`, new): probes
the configured provider with one real (or synthetic, for `mock`) request
each for equity/options data and reports REAL_DATA_CONNECTED / MOCK_DATA
/ REAL_DATA_UNAVAILABLE, auth status, feed type, OPRA entitlement (when
determinable), market-open state (`src.data.market_calendar`), and
freshness — surfaced read-only via `GET /api/data-provider-health` and a
small dashboard panel, so the owner never has to guess what kind of data
is powering the system.

**Dead code discovered, not touched**: `app/` (a very early
`app/data/mock_provider.py`/`app/config.py` prototype) is not imported by
anything under `src/` — `scripts/start.sh` runs `src.dashboard.app:app`
exclusively. Left as-is; removing unrelated dead code was out of scope
for this amendment.

## 16. Stateful Wheel strategy (pre-validation amendment, "Step 22.2")

Adds `src/wheel/`, a new package implementing the Wheel as a persistent,
multi-stage position lifecycle rather than a label glued onto a
standalone cash-secured put and covered call. **The Wheel never becomes
its own order type**: every order it places is an ordinary
`TradeProposal` with `strategy=StrategyType.CASH_SECURED_PUT` or
`COVERED_CALL` — the ~15-year-old two strategies this platform has
supported since Step 9, completely unmodified — submitted to the exact
same unmodified `src.risk.engine.evaluate_trade_proposal`. `src.wheel`
tracks which `wheel_id` a CSP/CC belongs to and what lifecycle state that
Wheel is in; it adds no new Risk Engine code path, no new PaperBroker
order type, and (per `src/strategies/base.py`'s
`TRADE_PROPOSAL_ELIGIBLE` set) `StrategyKind.WHEEL` is deliberately never
order-eligible — joining `LONG_CALL_BUTTERFLY`/`SHORT_IRON_CONDOR`/
`SHORT_IRON_BUTTERFLY`'s pre-Step-20A precedent of a `StrategyKind`
member that exists for comparison/evaluation only, except permanently
rather than as a staged rollout.

**State machine** (`src/wheel/state.py`): 14 named `WheelState` values
(`WHEEL_CANDIDATE`, `CSP_OPEN`, `CSP_EXPIRED`, `CSP_CLOSED`,
`ASSIGNED_SHARES`, `CC_ELIGIBLE`, `CC_OPEN`, `CC_EXPIRED`, `CC_CLOSED`,
`SHARES_CALLED_AWAY`, `WHEEL_COMPLETE`, `WHEEL_EXITED`, `WHEEL_HALTED`,
`WHEEL_REJECTED`) with a single source-of-truth `VALID_TRANSITIONS`
table and one `transition()` choke point every state change in
`src.wheel.lifecycle` goes through — the same "illegal transition raises,
never silently happens" discipline `src.brokers.fidelity`'s
`TicketStatus` state machine already established. `WHEEL_HALTED`/
`WHEEL_EXITED` are reachable from any non-terminal state (the escape
hatches a kill-switch/drawdown halt or a manual exit need); `WHEEL_REJECTED`
only from `WHEEL_CANDIDATE` (once a real order exists, unwinding it is an
exit, never a "rejection").

**Two bases, always kept apart** (`src/wheel/accounting.py`):
`acquisition_basis_per_share` (tax/accounting-style — the strike paid at
assignment, unadjusted) and `economic_basis_per_share` (acquisition basis
reduced by every dollar of net Wheel premium collected per share held).
Every report (dashboard, `src.validation.wheel_attribution`) shows both,
and `called_away`'s realized-P&L calculation always uses the tax-style
acquisition basis, never the economic one, for what was actually
gained/lost on the shares themselves.

**Below-basis covered calls are flagged, never forbidden**
(`src.wheel.accounting.below_basis_flags`): `BELOW_ACQUISITION_BASIS`/
`BELOW_ECONOMIC_BASIS` are attached to the `CcCycle` and surfaced to the
dashboard, the Devil's Advocate, and the Portfolio Manager — Part 8's
"must require explicit deterministic justification and cannot occur
merely to generate premium" is enforced by *visibility*, not a hard
block, since legitimate loss-management calls exist.

**PaperBroker integration** (`src/wheel/paper_events.py`): opens are
submitted only from an already Risk-Engine-approved `RiskDecisionResult`
(`WheelOrderNotApprovedError` if not APPROVE/RESIZE), converted to a
`PlaceOrderRequest` via the existing `src.brokers.order_validator
.validate_and_build_order_request` — no new order-construction logic.
Expiration settlement reads `PaperBroker.settle_expiration`'s own
`ExpirationSettlement.assigned_or_exercised` to decide `csp_assigned`
vs. `csp_expires_worthless` (and the covered-call equivalent) — never a
second, independently-computed assignment decision. A discretionary
early close (`buy_to_close_csp`/`_cc`) submits a `PlaceOrderRequest`
directly, since this platform's Risk Engine has no CLOSE/ROLL pipeline
capability at all yet (`src.risk.engine`'s own TS-004 comment) — a
pre-existing, documented gap this amendment does not newly introduce or
attempt to close.

**Fidelity integration** (`src/wheel/fidelity_events.py`): opens reuse
the unmodified `FidelityManualProvider`/`ApprovedOrder`/
`FidelityTradeTicket` machinery exactly as-is (a `WheelFidelityTicket`
dataclass pairs an unmodified `FidelityTradeTicket` with its `wheel_id`
externally, rather than adding a field to the trusted-kernel schema
itself). Assignment is recorded as a plain reconciliation event
(`record_wheel_csp_assignment`/`record_wheel_cc_assignment`, forwarding
straight to `src.wheel.lifecycle`) — never a fabricated order, per Part
20's explicit requirement.

**Risk analytics** (`src/wheel/risk.py`): `stress_test_wheel` reprices
whatever the Wheel currently holds (the short put while `CSP_OPEN`;
shares plus any open short call while holding stock) across
`WHEEL_STRESS_SPOT_SHOCKS = (-0.05, -0.10, -0.20, -0.30, -0.50)`, the
exact down-only grid Part 12 names (deeper than
`src.risk.stress.STRESS_SPOT_SHOCKS`'s general +-5/10/20% grid, since a
Wheel's live risk is entirely on the downside once assigned).
`compute_wheel_aggregate_exposure` feeds its `total_capital_at_risk`
(reserved CSP cash, or shares' current market value once held) into the
exact same `underlying_exposure_pct`/`sector_exposure_pct` functions
every other strategy's capital-at-risk goes through — no separate,
looser Wheel-only concentration rule exists anywhere.

**Persistence** (`src/wheel/persistence.py`): `WheelPosition` (which
already carries its complete nested history — every cycle, every state
transition, every audit event — in one Pydantic object) round-trips
through `model_dump_json`/`model_validate_json` as a single row per
`wheel_id`, the same pattern `src.brokers.base.SqliteIdempotencyStore`
already uses for `Order`. `WHEEL_DATABASE_SCHEMA_VERSION` is tracked
independently of `src.validation.session.DATABASE_SCHEMA_VERSION`.

**Stateful backtest** (`src/wheel/backtest_engine.py`): unlike
`src.backtest.engine.run_backtest` (every position an independent round
trip — correct for the platform's other 16 strategies), this engine
drives `src.wheel.lifecycle` through a real day-by-day loop so a CSP
assignment feeds directly into the covered-call phase within the same
run, reusing `src.backtest.execution`/`src.backtest.assignment` for all
actual pricing (no second pricing model). Every quote lookup is piped
through `assert_no_lookahead_options` exactly like the general engine;
a missing settlement quote raises `WheelBacktestDataInsufficientError`
rather than fabricating one.

**LLM review context** (`src/llm/context.WheelReviewContext`,
`src/wheel/review_context.py`): optional, Python-computed context wired
into `DevilsAdvocateInputs`/`PortfolioManagerInputs` only when the
proposal under review is a Wheel leg — the Part 17 (10 named failure
modes) and Part 18 (CSP-phase/CC-phase question sets) checklists as
reference data the model must work through, never a schema requirement
change (`DevilsAdvocateReview.failure_scenarios` already required
`min_length=3` for every review before this amendment existed).

**Validation reporting** (`src/validation/wheel_attribution.py`):
cohort-level Wheel metrics (assignment rate, below-basis call rate,
expectancy, a pseudo-equity-curve max-drawdown figure, tail-loss
average, capital efficiency) explicitly excludes `win_rate` as a field
at all, per Part 22's "do not judge Wheel quality solely on win rate."

**Dashboard** (`GET /api/wheels`, `GET /api/wheels/{wheel_id}`): the only
two new routes, both read-only, both added to the existing route
allowlist test (`tests/unit/dashboard/test_app_security.py`) that fails
loudly on any unlisted or execution-shaped route. No POST route exists
for a Wheel anywhere — opening/closing a Wheel's legs happens exclusively
through the ordinary Risk-Engine-gated PaperBroker/Fidelity paths above,
never through the dashboard.

**Security proof**: `tests/acceptance/test_wheel_security.py` — repo-wide
greps for a live trading client import or a live-shaped order-submission
method name anywhere in `src/wheel/`, proof no deterministic Wheel module
imports `src.llm.client`/`src.llm.router`, proof `open_cc`'s
`UncoveredCallError` check precedes any `CcCycle` construction in source
order (not just at runtime), proof `WHEEL` never appears in
`config/brokers.yaml`/`config/risk_limits.yaml`/`src/risk/engine.py`, and
proof every Wheel Pydantic model rejects an unrecognized field
(`extra="forbid"`).

## 17. Deterministic Strategy Lifecycle Management Engine (Step 22.3)

Adds `src/lifecycle/`, a new package managing every strategy this
platform supports (all 16 `StrategyKind` members, including the Wheel by
delegation) through ENTRY -> ACTIVE -> MONITORING -> MANAGEMENT DECISION
-> EXIT/EXPIRATION/ASSIGNMENT/ADJUSTMENT -> POST-TRADE ANALYSIS. Like
every other package in this codebase, it remains strictly subordinate to
`src.risk.engine`: nothing in `src/lifecycle/` places, cancels, or
modifies a live brokerage order, and a roll or adjustment's new OPEN leg
is a genuinely new `TradeProposal` that must independently clear the
full existing approval pipeline — this package grants no shortcut
through it.

**Core principle: STRATEGY is separate from MANAGEMENT POLICY**
(`src/lifecycle/policy.py`, `policies_library.py`). `StrategyKind
.PUT_CREDIT_SPREAD` is the structure; `ManagementPolicy` instances named
`PUT_CREDIT_SPREAD_STANDARD`, `PCS_25PCT_14DTE`, `PCS_DELTA_DEFENSE`, and
`PCS_HOLD_TO_EXPIRY` are four independently-researchable ways to manage
that identical structure — `policies_library.py` names at least one
research-default policy per `StrategyKind` (19 total), every one
labeled "RESEARCH DEFAULT" and validated by `validate_policy_for_strategy`
at import time so a hand-authored policy that presupposes a structural
property (e.g. `delta_threshold` on a strategy with no short leg) fails
immediately, not silently at runtime. CASH/NO_TRADE — never a
`StrategyKind` member at all — gets no policy, documented rather than
guarded against with a broken enum check.

**Position state machine** (`src/lifecycle/state.py`): 28 named
`PositionLifecycleState` values (11 pre-fill, mirroring but never
importing `src.brokers.fidelity.TicketStatus`'s own vocabulary, plus 17
post-fill monitoring/exit states), one `VALID_TRANSITIONS` table, one
`transition()` choke point. `RISK_EXIT_REQUIRED` is reachable from
*every* monitoring state (not only `ACTIVE`) and has no path back to
`ACTIVE` at all — not even indirectly through `HALTED` (a deliberately
closed two-hop loophole) — the state-machine-level encoding of Part
18's "nothing outranks Risk."

**Deterministic triggers** (`src/lifecycle/triggers.py`, Parts 4-12):
pure comparison functions — profit target, loss, DTE, delta, volatility,
regime change, earnings/event, liquidity deterioration, and assignment
risk — each returning zero or more `TriggerFinding`s tagged with one of
Part 18's 11 precedence categories. Every function computes nothing
itself; a caller-supplied number missing where a configured policy field
needs it produces a `DATA_INSUFFICIENT` finding (`category=
"system_data_safety"`, the top precedence tier) rather than a silently
skipped check — Part 19's fail-safe posture applied uniformly, including
"missing earnings data is never interpreted as no earnings."

**Action precedence** (`src/lifecycle/precedence.py`): `resolve_action`
picks whichever of Part 18's 11 categories is highest-ranked among the
findings that actually fired — full stop, regardless of the lower
category's own mandatory flag — and within the winning category prefers
a `mandatory=True` finding over an advisory one.

**MFE/MAE excursion tracking** (`src/lifecycle/excursion.py`): an
immutable, monotonically-updated `ExcursionState` (mfe/mae are the
actual signed P&L values at the extreme, never forced non-negative) plus
`exit_efficiency`, which returns `None` — never a fabricated 0.0/1.0 —
for a position that was never profitable.

**Immutable decision record** (`src/lifecycle/snapshot
.LifecycleDecisionSnapshot`, Part 17): every field a lifecycle
evaluation observed plus every candidate action and the one resolved —
persisted append-only (never overwritten) so a position's full history
is reconstructable after the fact.

**Orchestrator** (`src/lifecycle/engine.py`): `evaluate_position` is a
pure function (`PositionMonitoringInput` in, `EvaluationResult` out) —
same inputs always produce the same outputs, satisfying the backtest
determinism requirement every other engine in this codebase already
meets. It performs no I/O and carries no LLM-shaped parameter anywhere
in its signature.

**Rolling** (`src/lifecycle/rolling.py`, Part 13): a roll is modeled as
exactly two transactions, `record_roll_close` (realizes P&L immediately,
tracks cumulative chain loss honestly) then `complete_roll` (records the
new leg's id and net credit/debit once the new position has independently
cleared the full pipeline) — never one operation, and a large realized
loss is never netted away by a favorable-looking roll credit.

**Adjustment** (`src/lifecycle/adjustment.py`, Part 14): a closed set of
seven named `AdjustmentType`s (including Part 14's own worked example,
`CLOSE_UNCHALLENGED_SIDE`), each producing an `AdjustmentProposal` with
explicit before/after max loss, capital requirement, breakevens, and
Greeks — packaging numbers `src.quant`/`src.risk` already computed, never
re-deriving option math, and never itself constituting approval.

**PaperBroker integration** (`src/lifecycle/paper_events.py`, Part 20):
`close_position` builds the correctly-reversed closing leg(s) (BUY to
close a short, SELL to close a long) and submits them through the
unmodified `PaperBroker.place_order`; `settle_lifecycle_expiration` is a
thin pass-through to `PaperBroker.settle_expiration`, already fully
generic across assignment/exercise/call-away. No function in this module
opens a new position — verified structurally in
`tests/acceptance/test_lifecycle_security.py`.

**Fidelity integration** (`src/lifecycle/fidelity_events.py`, Part 21):
`LifecycleClosingTicket` is a deliberately separate, simpler shape from
`ApprovedOrder`/`FidelityTradeTicket` — it carries no `max_profit`/
`max_loss`/`breakeven` (fields with no real meaning for a closing order)
— rendered as plain human-readable text, never submitted anywhere.
`FILLED` only ever follows an explicit `record_ticket_filled` call
carrying a human-reported fill.

**Persistence** (`src/lifecycle/persistence.py`): `LifecyclePositionRecord`
(mutable, REPLACE-on-save, keyed by `trade_id`) and
`LifecycleDecisionSnapshot` (append-only) live in separate sqlite tables
for exactly the reason `src.wheel.persistence` documents for its own
shape — a position's *current* state changes on every evaluation, its
*history* never should. Restart recovery is proven by re-opening a fresh
`SqliteLifecycleStore` against the same file mid-test-suite.

**Alerts** (`src/lifecycle/alerts.py`, Part 23): eleven named
`AlertType`s (Part 23's ten plus `VOLATILITY_CHANGE`, since Part 8's
volatility management has no home in the literal ten — the same "at
minimum" spirit Part 23 states explicitly). `raise_alert_if_new` checks
the caller-supplied set of a trade's existing alerts and returns `None`
for a still-unresolved duplicate condition — safe to call every
evaluation.

**Post-trade analysis and counterfactuals** (`src/validation
.post_trade_analysis.py`, Part 24): `ClosedPositionAnalysis` records the
actual outcome (realized P&L, return on risk/capital, MFE/MAE, exit
efficiency, commissions) plus a separate `counterfactuals` tuple ("what
if held to expiration/exited at a different point") built by reusing
`src.backtest.execution.execute_exit`/`src.backtest.assignment
.settle_position` exactly as `src.workflows.rejected_trade_review`
already does for a rejected proposal — the counterfactual list never
alters `.realized_pnl` or any historical field.

**Policy-level attribution** (`src/validation.policy_attribution.py`,
Part 25): every metric computed at both `strategy_level_performance`
(e.g. all `PUT_CREDIT_SPREAD` trades) and `strategy_policy_level
_performance` (e.g. `PUT_CREDIT_SPREAD` + `PCS_50PCT_21DTE` specifically)
— Sharpe/Sortino/max-drawdown/CVaR computed against a synthetic,
trade-level equity curve for the group via the existing
`src.backtest.metrics` implementations (never re-derived), and every
group carries `sample_size_warning` from the existing
`src.research.overfitting_guards.check_small_sample` threshold rather
than a second one.

**Dashboard** (`GET /api/lifecycle`, `GET /api/lifecycle/{trade_id}`):
read-only, added to the existing route allowlist test. Twelve
`LifecycleStatusIndicator` values (Part 22's ten plus `REGIME_REVIEW`/
`ASSIGNMENT_REVIEW`, the display-layer counterpart of the alert list's
same "at minimum" extension) derived purely from a `ResolvedAction`'s
own category — no execution control anywhere on this view or route.

**Security proof**: `tests/acceptance/test_lifecycle_security.py` —
repo-wide greps for a live trading client/network/browser-automation
import or a live-shaped order-submission method name anywhere in
`src/lifecycle/`, proof no credential-shaped identifier exists in the
package, proof `paper_events.py` exposes no function that opens a
position, proof no deterministic lifecycle module imports
`src.llm.client`/`src.llm.router` or accepts an LLM-verdict-shaped
parameter, proof `fidelity_events.py` only ever sets `FILLED` from
`record_ticket_filled`, and proof `rolling.py`/`adjustment.py` never
import `src.risk.engine` (a roll/adjustment proposal is packaged here,
evaluated by Risk elsewhere, by the caller).

## 18. Tradier real-time market data + deterministic Portfolio Control
## Loop (pre-validation amendment, "Step 22.4")

Adds a second real market-data provider, `src/data/tradier_provider.py`
(alongside the existing `src/data/alpaca_provider.py`, neither removed),
and a new package, `src/portfolio/`, that continuously turns real
market data into an auditable, deterministic picture of the PAPER
portfolio without ever gaining the ability to execute a trade. The
hierarchy this amendment enforces, top to bottom: MARKET DATA ->
CANONICAL DATA MODELS -> PYTHON QUANT ENGINE -> LIFECYCLE ENGINE ->
PORTFOLIO CONTROL LOOP -> DETERMINISTIC RISK ENGINE -> RECOMMENDATION ->
HUMAN -> FIDELITY MANUAL EXECUTION -> HUMAN-CONFIRMED FILL -> PORTFOLIO
RECONCILIATION. `src/portfolio/control_loop.py` is explicitly not a
second Risk Engine or a second Lifecycle Engine — it orchestrates the
existing ones, unmodified, and never overrides what either returns.

**Tradier provider** (`src/data/tradier_provider.py`): the same
dependency-injection pattern `AlpacaMarketDataProvider` already
establishes (`http_client` constructor override for tests, no real
network/token needed), the same "missing means missing, never
fabricated" mapping discipline, and the same "provider Greeks are
reference data, Python Quant is authoritative" doctrine `OptionContract`
already documents. Structurally MARKET_DATA_ONLY: `_request` (the one
HTTP choke point) accepts no `method` parameter at all — every call is a
hardcoded GET against `/v1/markets/*`; there is no
`TradierBroker`/`TradierOrderClient`/`TradierExecutionProvider` anywhere
in the repository, proven by `tests/acceptance
/test_tradier_market_data_only.py`. `get_underlying_quotes` batches
multiple symbols into one request (never one request per symbol), and
every request carries a `RateLimitPriority` (`src/data/rate_limiter.py`)
gating it against the provider's own most-recently-observed rate-limit
headers — P0 (open-position risk monitoring) is throttled only by the
hard "zero requests remaining" floor, never by a softer utilization
ceiling, so a busy cycle degrades opportunity scanning long before it
ever touches risk monitoring.

**Data-quality gate** (`src/data/quality_gate.py`): deterministic
per-contract/per-chain validation (crossed/zero/excessively-wide
markets, staleness, symbol/expiration consistency, non-finite values on
fields Pydantic's own constraints don't already reject) that isolates
one bad contract from an otherwise-good chain rather than discarding the
whole fetch — the concrete mechanism behind "a malformed provider
response must not crash the control loop."

**Portfolio revaluation** (`src/portfolio/revaluation.py`): every open
position's mark-to-market P&L and Greeks, computed once per cycle from
the quality-gated chain. Never trusts a provider-reported Greek or a
theoretical Black-Scholes price for P&L — the mark is the contract's
actual `mid`, and Greeks come from an independently-solved
`src.quant.volatility.implied_volatility` feeding `src.quant.greeks`,
exactly the same two functions `src.risk.trade_risk`/`src.orchestration
.pipeline.default_quant_stage` already use for a proposal under review.
A position whose legs can't all be matched to a fresh, non-stale
contract is `DATA_INSUFFICIENT` — its P&L/Greeks are `None`, never a
fabricated number, and it's excluded from (never silently folded into)
the portfolio-level aggregate, which always lists which positions it
had to exclude.

**Portfolio exposure** (`src/portfolio/exposure.py`): underlying/
sector/strategy/expiration concentration (reusing `src.risk
.portfolio_risk`'s existing per-ticker/per-sector helpers, no new
concentration math), directional/volatility exposure labels from
caller-supplied Greeks, Wheel cash commitment (an active Wheel is any
non-terminal `src.wheel.state.WheelState`, so a halted-but-not-exited
Wheel still counts as committed capital), owned-share exposure, and
covered-call encumbrance. Assignment risk is read from the caller
(supplied from `src.lifecycle.triggers.check_assignment_risk`'s own
output), never recomputed here — this module adds no second assignment
determination.

**Control-loop action vocabulary** (`src/portfolio/actions.py`):
`ControlLoopAction`'s 16 members are the outward recommendation label a
human/dashboard sees; `action_from_lifecycle_category` is a total,
one-to-one relabeling of `src.lifecycle.triggers.PRECEDENCE_ORDER`'s 11
categories (a `KeyError` on an unmapped category, never a silent
default) — never a second precedence decision layered on top of
`src.lifecycle.precedence.resolve_action`'s own.

**Decision snapshot, cycle record, persistence** (`src/portfolio
/decision_snapshot.py`, `cycle_record.py`, `persistence.py`):
`PortfolioControlDecisionSnapshot` (one per decision point, position or
opportunity) and `ControlCycleRecord` (one per cycle) are both
immutable/append-only, stored via the same `InMemory*`/`Sqlite*`
dual-implementation pattern `src.lifecycle.persistence`/`src.wheel
.persistence` already establish (restart survival verified by reopening
a fresh store against the same file mid-test-suite). `ControlLoopAlert`
(Part 32) is persisted in the same store, REPLACE-on-save keyed by
`alert_id` — the one field that ever mutates is `resolved`/`resolved_at`.

**Core orchestrator** (`src/portfolio/control_loop.py`):
`run_control_cycle` performs, per cycle: quality-gate the already-
fetched market data (this module does no I/O of its own, the same
"caller already fetched it" convention `src.workflows.morning_scan`
established) -> revalue the portfolio -> build the exposure snapshot ->
evaluate every open position through the unmodified `src.lifecycle
.engine.evaluate_position` (lazily initializing a fresh
`LifecyclePositionRecord` the first time a position is seen, from a
caller-supplied management-policy name — never guessing one) ->
`src.risk.kill_switch.check_kill_switch` -> a
`PortfolioControlDecisionSnapshot` per position. One position's bad
market data, missing policy configuration, or unknown policy name
isolates to a `DATA_INSUFFICIENT` decision for that position alone,
never an aborted cycle; a portfolio halt (manual or drawdown-triggered)
overrides every open position's action to `PORTFOLIO_HALT`, never a
softer per-position recommendation. New-opportunity scanning and
pending-ticket monitoring are deliberately not phases of this function —
their already-computed counts are accepted as plain input fields, so a
thin outer wrapper runs those separate, independently-testable stages
and merges the counts into one `ControlCycleRecord`.

**Pending-ticket monitoring and fill reconciliation**
(`src/portfolio/ticket_monitor.py`): fill reconciliation itself was
already complete before this amendment
(`src.brokers.fidelity.confirm_fill`/`transition`,
`src.workflows.reconciliation.reconcile_portfolio`) — this module's
actual job is the one real gap, Part 21: every cycle, a ticket still
sitting at `AWAITING_HUMAN`/`ORDER_ENTERED` is checked against the
current market for staleness, price drift beyond a configurable
threshold, or a now-unreachable `minimum_acceptable_price` (the same
net-credit/net-debit sign convention `src.brokers.fidelity`'s own
validator uses); a bad finding calls the *existing* `transition()` to
`REPRICE_REQUIRED` — this module never derives a new price itself, a
fresh ticket must come from a fresh, independently-Risk-approved
`ApprovedOrder`, exactly like the original.

**Portfolio-aware opportunity scanning** (`src/portfolio
/opportunity_scan.py`): the lighter, no-LLM sibling of
`src.workflows.morning_scan.run_morning_scan` — reuses
`src.workflows.candidate_generation.generate_candidates`,
`src.orchestration.pipeline.default_quant_stage`, and
`src.risk.engine.evaluate_trade_proposal` unmodified; Devil's
Advocate/Portfolio Manager remain `run_morning_scan`'s own separate,
deliberately-invoked daily review, never re-run every cycle. Ranks only
Risk-approved/resized candidates by risk-adjusted return
(expected-value per dollar of capital); `best=None` (CASH/NO_TRADE) is
the explicit, tested outcome when nothing clears a configurable hurdle
— even a Risk-*approved* candidate with negative risk-adjusted EV still
yields no trade, Part 25's "not maximize raw profit" made concrete.

**Alerts** (`src/portfolio/alerts.py`, Part 32): `ControlLoopAlert`
generalizes `src.lifecycle.alerts.Alert`'s dedup-by-condition mechanism
from a mandatory `trade_id` to an arbitrary `scope` string (`"portfolio"`,
a provider name, a position_id, a ticket's trade_id, an opportunity's
proposal_id) — the dimension Part 32's own list (Risk halt, drawdown,
provider outage, a stale pending ticket, a new opportunity) needs and
`Alert` structurally cannot express. `AlertSeverity` is Part 32's exact
four-level vocabulary (INFO/REVIEW/WARNING/CRITICAL), one fixed severity
per alert type. `generate_cycle_alerts` is dedup-safe to call every
cycle unconditionally.

**Dashboard** (`GET /api/control-loop/status`, `/exposure`, `/alerts`):
read-only, GET-only, added to the existing route allowlist test — no
POST/PUT route of any kind for the control loop anywhere in this
package; starting, stopping, or reconfiguring a cycle happens wherever
the loop is actually scheduled (outside this dashboard), never through
it.

**Security proof**: `tests/acceptance/test_tradier_market_data_only.py`
— repo-wide grep proving no Tradier order/trading-shaped class exists
anywhere, `_request`'s signature has no `method` parameter, no POST/
PUT/DELETE verb is issued against the injected HTTP client, `config
/brokers.yaml` lists no `tradier` entry, the Risk Engine module never
imports Tradier, and the bearer token never appears in a raised
exception's text.

## 19. Outer Portfolio Control Loop orchestrator + dashboard projection
## (acceptance-gap remediation, "Step 22.4A")

`src/portfolio/control_loop.py`'s own module docstring called for a
"thin outer wrapper" to run new-opportunity scanning and pending-ticket
monitoring around it and merge their counts in — Step 22.4 built those
two stages (`src.portfolio.opportunity_scan`/`ticket_monitor`) as
independently-testable modules but never built that wrapper, so neither
stage was ever invoked from a production entry point and
`ControlCycleRecord.opportunities_scanned`/`candidates_generated`/
`candidates_rejected` stayed permanently zero. `src/portfolio/orchestrator.py`
(`run_outer_cycle`) is that wrapper.

**Still not a second Risk Engine or Lifecycle Engine.** `run_outer_cycle`
calls `run_control_cycle` (unmodified), `scan_and_rank_opportunities`
(which itself calls the unmodified `src.risk.engine.evaluate_trade_proposal`),
and `monitor_pending_tickets` (which itself calls the unmodified
`src.brokers.fidelity.transition`) — never overriding any of their
verdicts, never importing `src.risk.engine`/`src.lifecycle.engine`
directly, and never calling `confirm_fill` (so a Risk approval or a
scanned opportunity can never be silently treated as an executed fill).

**Priority: existing-position/risk monitoring > pending-ticket safety
monitoring > new-opportunity scanning.** `run_control_cycle` itself
carries no rate-limit gate in this module at all — whatever market data
the caller already fetched for open positions is evaluated every cycle,
unconditionally. Only the two stages this module adds are ever skipped
under provider-capacity constraints: pending-ticket monitoring is gated
at `RateLimitPriority.P3_PENDING_TICKET_REPRICING`, opportunity scanning
at `P4_OPPORTUNITY_SCANNING`. Since `src.data.rate_limiter`'s existing
per-priority utilization ceilings throttle P4 before P3 before P0-P2 as
headroom shrinks, opportunity scanning is always sacrificed first,
ticket monitoring second, and existing-position/risk monitoring never at
all.

**Exposure persistence gap closed**: `ControlLoopStore` gained
`save_exposure_snapshot`/`get_exposure_snapshot` (Step 22.4's
`run_control_cycle` computed a fresh `PortfolioExposureSnapshot` every
cycle but was never given the job of persisting it) — `run_outer_cycle`
saves it immediately after the cycle completes.

**Dashboard projection** (`src/dashboard/control_loop_projection.py`,
`load_latest_control_loop_state`): the one production-safe read from a
persisted `ControlLoopStore` into `DashboardState.latest_cycle_record`/
`latest_exposure`/`control_loop_alerts` — never fabricates; an empty
store leaves every field at its honest default. `src.dashboard.app
.set_state` gained an optional `control_loop_store` keyword that invokes
this projection at session-initialization time, so the dashboard is
capable of showing the most recently completed cycle right after normal
application startup. Three states are kept structurally distinct, never
collapsed: no session at all (503, `get_state`'s pre-existing behavior,
unchanged), a session with no cycle yet (404, the control-loop routes'
pre-existing honest behavior, unchanged), and a session with real
persisted cycle data (200, now actually reachable).

**Tradier production smoke test** (`scripts/smoke_tradier_market_data.py`):
operator-run, GET-only, market-data-only (one quote, the expirations
list, one option chain), never places/previews/cancels an order, never
starts validation, never writes to any store, never prints the bearer
token — documented in README.md §19.

**Security proof**: `tests/acceptance/test_orchestrator_security.py` —
the orchestrator never imports `src.risk.engine`/`src.lifecycle.engine`
directly or calls `confirm_fill`/an order-submission method; the
dashboard projection module has exactly one public function and no
route can resolve a `ControlLoopAlert`; the P4>P3>P0 rate-limit priority
ordering holds structurally, re-verified independently by
`src/validation/freeze.py`'s new `orchestrator_cannot_bypass_risk_or_lifecycle`/
`opportunity_scan_never_outranks_risk_monitoring`/
`dashboard_cannot_execute_trades` checks on every `make verify-freeze`
run.
