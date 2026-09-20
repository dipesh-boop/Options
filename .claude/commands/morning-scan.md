---
description: Run the complete daily portfolio research cycle — verify data, reconcile the portfolio, screen the approved universe, and surface Risk-Engine-approved Fidelity trade tickets for human review. Never places an order.
---

# /morning-scan

Perform the complete daily portfolio research cycle by running
`src.workflows.morning_scan.run_morning_scan` and rendering its result
with `render_morning_scan_report`. This command **never places a
Fidelity order** — every ticket it can possibly produce comes out of
`run_order_pipeline`'s existing Risk-Engine-driven MANUAL path and is
`AWAITING_HUMAN` by construction. Do not create a trade simply because
this command was run: **cash is a valid position**, and a session where
nothing clears every gate ends in a clearly stated `NO TRADE`, not a
forced candidate.

## What this command does, in order

1. **Verify market-data feeds** — for every ticker in the approved
   universe, confirm a chain fetch was attempted and either succeeded or
   recorded a specific failure. Use
   `src.workflows.feed_health.verify_market_data_feeds`.
2. **Verify data freshness** — for every successfully fetched chain,
   check its age against `src.data.provider.DEFAULT_MAX_QUOTE_AGE` (or
   the caller's own max age). Use
   `src.workflows.feed_health.verify_data_freshness`. A stale or failed
   feed excludes that ticker from candidate generation this run — it is
   reported, never silently substituted with cached or estimated data.
3. **Load confirmed Fidelity positions** — this platform never logs
   into, scrapes, or automates Fidelity in any way (it is
   `MANUAL_EXECUTION` forever). "Loading" here means asking the human
   running this command to confirm what is actually open in their real
   Fidelity account right now, and representing that confirmation as
   `src.workflows.reconciliation.ConfirmedFidelityPosition` entries. If
   the human hasn't supplied any, proceed with an empty list and note
   that reconciliation is unconfirmed this run — do not guess at what
   might be open.
4. **Reconcile internal portfolio** — compare confirmed positions
   against the internal `Portfolio` with
   `src.workflows.reconciliation.reconcile_portfolio`. Surface every
   discrepancy; do not silently resolve one in either direction.
5. **Check cash and capital deployment** — `src.risk.portfolio_risk
   .capital_deployed_pct`/`cash_reserve_pct`, plus current drawdown from
   `Portfolio.nav`/`peak_equity`.
6. **Analyze current market regime** — this platform's `Market Regime`
   agent (`.claude/agents/market_regime.md`) classifies regime from
   Python-supplied reference data (index levels, realized vol, notable
   events); no dedicated `src.llm.market_regime` orchestration module
   exists yet (a documented gap — see `progress.md`), so today this
   step means: supply a `MarketRegimeAssessment` (invoke the Market
   Regime agent with current index/vol data if you have it, or state
   plainly that regime classification is a placeholder for this run).
   Never invent a regime label without basis.
7. **Review VIX/volatility conditions** — pass whatever current VIX
   level/commentary is available into `MorningScanInputs.vix_level`; if
   none is available, leave it `None` and say so in the output rather
   than guessing a level.
8. **Review economic events** — pass known upcoming events (FOMC, CPI,
   NFP, etc.) into `MorningScanInputs.notable_economic_events`. Only
   events you actually know about; an empty list is a legitimate answer.
9. **Review earnings calendar** — for every universe ticker with a known
   upcoming earnings date, supply an `EarningsEvent` via
   `MorningScanInputs.earnings_by_ticker`; `run_morning_scan` screens out
   any candidate whose expiration falls within the configured earnings
   window (`src.data.earnings.is_within_earnings_window`) automatically.
10. **Scan approved universe** — supply the universe as a list of
    `src.workflows.candidate_generation.UniverseEntry(ticker, sector)`.
    There is no final, curated ~50-name universe file yet (a documented
    open decision) — use whatever universe list the human running this
    command provides, and say so if it's a placeholder.
11. **Apply liquidity filters** — automatic, inside
    `generate_candidates` (`src.workflows.candidate_generation
    .passes_liquidity_filter`), reusing `config/risk_limits.yaml`'s own
    `min_open_interest`/`min_volume`/`max_bid_ask_spread_pct` — never a
    second set of thresholds.
12. **Run quantitative filters** — automatic, inside
    `generate_candidates`: target-delta-range screening per
    `QuantFilterConfig`.
13. **Generate candidate strategies** — automatic: up to one
    `TradeProposal` per requested strategy per ticker (cash-secured put,
    covered call — only for tickers with >= 100 shares already held,
    put credit spread), ranked by stated credit and capped at
    `MorningScanInputs.max_candidates`.
14. **Run Python Quant Engine** — via `run_order_pipeline`'s
    `quant_stage` (`default_quant_stage`), unchanged from Step 12.
15. **Run Devil's Advocate** — via `run_order_pipeline`'s
    `devils_advocate_stage` (`src.llm.devils_advocate.evaluate_trade_risk`).
16. **Run Portfolio Manager** — via `run_order_pipeline`'s
    `portfolio_manager_stage` (`src.llm.portfolio_manager.evaluate_proposal`).
17. **Evaluate correlation** and **18. Evaluate portfolio impact** — both
    already happen inside the Risk Engine stage
    (`src.risk.engine.evaluate_trade_proposal`, which calls
    `src.risk.correlation`/`src.risk.concentration` itself) — no separate
    call is needed or made here.
19. **Run deterministic Risk Engine** — via `run_order_pipeline`'s
    `risk_engine_stage` (`src.risk.engine.evaluate_trade_proposal`), the
    sole authority on APPROVE/RESIZE/REJECT/HALT.
20. **Generate PaperBroker candidates** — via `run_order_pipeline`'s
    `paper_broker` stage, unchanged.
21. **Generate FidelityTradeTickets for Risk-Approved trades** — via
    `run_order_pipeline`'s companion MANUAL-capability call, unchanged —
    every ticket defaults to `AWAITING_HUMAN`.

Steps 1-13 are `src.workflows.morning_scan.run_morning_scan`'s own code
(new in Step 15); steps 14-21 are one `run_order_pipeline` call per
candidate, entirely reused from Step 12.

## Output

Render the result with
`src.workflows.morning_scan.render_morning_scan_report(report)`, which
produces, in this exact order:

- **MARKET REGIME** — regime, commentary, VIX (if known), notable
  events, plus any feed/freshness/reconciliation/earnings-screening
  issues worth flagging up front.
- **PORTFOLIO** — NAV, cash, capital deployed, current drawdown, net
  delta/theta/vega (reported as "not tracked" when the caller didn't
  supply them — never fabricated), sector concentration.
- **TOP OPPORTUNITIES** — one block per candidate that passed screening,
  with ticker, strategy, expiration/DTE, strikes, delta, IV, probability
  of profit, expected value, capital required, max profit/loss, ROC,
  and thesis.
- **DEVIL'S ADVOCATE** — each candidate's verdict and `why_not_thesis`.
- **RISK ENGINE** — each candidate's APPROVE/RESIZE/REJECT verdict and
  reason (or, for a candidate rejected before reaching the Risk Engine,
  which earlier stage rejected it and why).
- **FIDELITY TRADE TICKET** — every ticket a Risk-Approved candidate
  produced, in the platform's standard ticket format
  (`render_ticket_text`) plus probability of profit, the exit/DTE
  management rules, and the Risk Engine's own verdict. **If no candidate
  produced a ticket, this section is `NO TRADE` with a plain-language
  explanation of why — never a trade manufactured to have something to
  show.**

## Hard constraints

- **Never place a Fidelity order.** This command has no path to do so —
  don't add one, and don't describe a ticket's status as anything other
  than `AWAITING_HUMAN` (or a later state a human has since confirmed
  through the existing `transition`/`confirm_fill` functions elsewhere
  in this codebase, never through this command).
- **Do not create a trade simply because `/morning-scan` was run.** A
  clean `NO TRADE` with a clear explanation is a complete, successful
  run of this command, not a failure to find something.
- **Cash is a valid position.** Never let ranking/urgency framing imply
  otherwise.
- Run this command's supporting tests (`tests/unit/workflows/`) after
  any change to the code it depends on, and keep `progress.md` current.
