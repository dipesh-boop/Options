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

**Initial strategies:** cash-secured puts, covered calls, put credit spreads.
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
- **Clock/timezone bugs** around market hours and expiration → an exchange
  calendar library (e.g. `pandas_market_calendars`), UTC internally, all
  market-hours logic centralized in one module rather than repeated.
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
