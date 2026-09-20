"""The `/morning-scan` daily workflow (Step 15): everything the slash
command needs beyond what `src.orchestration.pipeline` already provides.

`run_order_pipeline` (Step 12) already implements the required order
pipeline's later stages — Quant Engine, Devil's Advocate, Portfolio
Manager, Risk Engine (which itself evaluates correlation and portfolio
concentration internally, via `src.risk.correlation`/`src.risk
.concentration`), PaperBroker, and FidelityTradeTicket generation — for
one `TradeProposal` at a time. This package supplies what comes before
that: feed/freshness verification, confirmed-Fidelity-position
reconciliation, cash/capital reporting (mostly already in
`src.risk.portfolio_risk`), and universe/liquidity/quant screening to
actually produce the `TradeProposal` candidates the pipeline evaluates —
the first deterministic Strategy Screener this codebase has had, closing
part of a gap flagged since Step 9's progress.md entry.
"""
