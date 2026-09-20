---
description: Convene the weekly Investment Committee, chaired by the Portfolio Manager, to review portfolio performance, risk, decision quality, rejected trades, and Fidelity execution quality, and hand identified hypotheses to the Strategy Research Agent. Never modifies production strategy.
---

# /weekly-review

Convene the weekly Investment Committee. **The Portfolio Manager chairs
this review** — synthesize what `src.workflows.weekly_review
.build_weekly_review_report` computed into committee-quality narrative,
but never restate a number differently than what that report says, and
never treat this command as a place to change how the platform trades.
**This command may not modify production strategy** — the only thing it
may do with a new idea is register it as a `Hypothesis` for the
Strategy Research Agent to independently test later; it has no path to
promote anything (`src.research.promotion` is never imported here,
exactly as `/weekly-review`'s Python backing never imports it).

## What this command does

Call `src.workflows.weekly_review.build_weekly_review_report` with a
`WeeklyReviewInputs` assembled from this week's actual data, then render
it with `render_weekly_review_report`. Every section below names which
part of that report it comes from.

### PORTFOLIO PERFORMANCE
Starting/ending NAV, weekly/MTD/YTD/since-inception/annualized return,
and SPY/risk-free benchmarks — `performance_review.build_portfolio_performance`,
built on the already-tested `src.backtest.metrics.cagr` and
`src.backtest.benchmark.compare_to_benchmarks`. Supply the real
since-inception `equity_curve` (there is no persisted equity ledger yet —
say so plainly if you're working from an incomplete series) and, when
available, SPY `HistoricalBar`s for the benchmark comparison.

### RISK
Sharpe, Sortino, max/current drawdown, cash, capital deployed, sector
and underlying concentration — `performance_review.build_risk_section`,
reusing `src.risk.portfolio_risk`'s existing aggregation helpers.
Portfolio-level delta/theta/vega render as "not tracked" unless you
supply them — there is no Greeks aggregator across positions yet; never
fabricate a number here.

### TRADE STATISTICS
Win rate, average win/loss, profit factor, expectancy — `src.backtest
.metrics.trade_statistics`, applied to this period's closed trades
(`TradeRecord`s), unmodified from how it grades a backtest.

### BREAK DOWN PERFORMANCE
By strategy, market regime, delta, DTE, IV percentile, ticker, sector,
and holding period — `src.research.performance_breakdown.breakdown_by`,
the exact same dimension bucketing Step 14's Strategy Research Agent
uses, applied here to live trades instead of backtest trades.

### DECISION QUALITY
Classify every closed trade as GOOD/BAD DECISION x GOOD/BAD OUTCOME via
`decision_quality.classify_decision_quality`. **This never looks at
P&L to decide whether the decision was good** — only the Risk Engine's
decision, the Devil's Advocate's verdict, and the stated probability of
profit at entry, all as they stood *before* the trade's outcome was
known. Outcome quality is the only thing P&L decides. When you narrate
this section, preserve that distinction explicitly — a good decision
that lost money and a bad decision that made money are both meaningful
findings, not a report to smooth over.

### REJECTED TRADE REVIEW
`rejected_trade_review.summarize_rejected_outcomes` over a sample of
`HypotheticalOutcome`s (each priced by replaying the rejected proposal
through `src.backtest.execution`/`assignment` against real subsequent
market data — never estimated). **If the sample is below the
statistical-significance threshold, the report says so explicitly, and
you must not draw a conclusion from it anyway** — "this one rejected
trade would have won" is not evidence the rejection was wrong; only the
aggregate, once the sample is meaningful, is.

### FIDELITY EXECUTION QUALITY
`execution_quality.summarize_slippage` over confirmed
`(FidelityTradeTicket, ExecutionConfirmation)` pairs — recommended limit
vs. market midpoint at recommendation vs. actual confirmed fill vs. the
delay between them, averaged/medianed and broken down by strategy,
underlying, and time of day. Present **MODEL** (the Quant Engine's
stated expected value/theoretical figures), **PAPER** (the simulated
fill `TradeRecord`s actually realized), and **ACTUAL CONFIRMED FIDELITY**
(this section) as three distinct performance lines — do not blend them
into one number. When something looks off, attribute it to one of:
strategy performance (the edge itself), execution performance (fill
quality), human execution delay (`delay_seconds`), or market-data
differences (midpoint move between recommendation and confirmation) —
never a single undifferentiated "performance was worse."

### RESEARCH
Identify hypotheses worth testing from this week's review — patterns in
the breakdown, decision-quality quadrants, or rejected-trade statistics
that suggest something concrete and falsifiable (in the spirit of
Step 14's own example: *"15-20 delta put credit spreads outperform
25-30 delta spreads during high-IV regimes"*). Register each one via
`weekly_review.register_committee_hypotheses` into the same
`HypothesisRegistry` the Strategy Research Agent reads from. **Do not
modify production strategy from this section under any circumstance** —
a hypothesis is a question for the Research Agent to test, never a
change to make.

### NEXT WEEK
`weekly_review.build_next_week_section`: known economic events (as
supplied — never invented), earnings risks and expiration risks for
currently open positions (`src.data.earnings.is_within_earnings_window`,
DTE checks), portfolio risks (drawdown/concentration thresholds compared
against `config/risk_limits.yaml`, the same numbers the Risk Engine
itself uses), and positions requiring attention. **Do not forecast
market direction as certainty** — state known risks and dates as facts;
never phrase next week's outlook as a prediction of where the market is
headed.

## Hard constraints

- **Do not judge decision quality solely by P&L** — the classifier
  structurally cannot; don't override it with a P&L-based narrative.
- **Do not automatically conclude a rejected winning trade should have
  been accepted** — respect the sample-size warning when it's present.
- **This command may not modify production strategy.** Its only output
  toward future strategy is a hypothesis handed to the Strategy Research
  Agent's registry — never a parameter change, never a promotion.
- **Do not forecast market direction as certainty** in the NEXT WEEK
  section.
- Update the database/report store this platform eventually persists
  results to (a real one doesn't exist yet — note that plainly rather
  than pretending a write happened).
- Run `tests/unit/workflows/` after any change to the code this command
  depends on, and keep `progress.md` current.
