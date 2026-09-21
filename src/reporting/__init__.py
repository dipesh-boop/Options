"""Step 22 Part 10-16: the validation cohort's reporting/export layer.

Confirmed absent as of Step 21's `ACCEPTANCE_TEST_REPORT.md` §22 (no
PDF/XLSX/CSV/JSON export functionality existed anywhere in `src/`) --
re-confirmed here at the start of Step 22 by the same repo-wide grep,
still zero matches outside this new package. This is answer (D) from
Part 10: genuinely absent, not merely missed by Step 21's inspection.

**Scope discipline** (Part 10's own "do NOT build an unnecessarily
elaborate reporting product"): every number this package exports is a
direct read, or a simple, transparent aggregation (sum/mean/count/win
rate), of an already-canonical figure already computed and stored by
the platform's real calculation layers -- `TradeRecord.realistic_pnl`
(from `src.backtest.simulator`/the real pipeline fill), `RiskDecisionResult`,
`QuantitativeAnalysis`, `DailySnapshot.nav` (from a real portfolio
update). Nothing here re-derives a P&L, a Greek, a probability, or a
risk figure a second, different way -- the one invariant CLAUDE.md's
own "every dollar/probability/Greek figure is computed once" rule
would forbid a reporting layer from violating.

Two report sections the Step 22 instruction names -- a full
statistical scorecard (`src.validation.scorecard.build_scorecard`,
which needs bootstrap/Monte-Carlo-derived Sharpe/Sortino/VaR/CVaR over
a completed equity curve) and an external benchmark-index comparison
(`src.validation.benchmarks`, which needs a real market-data benchmark
price series this platform has no live connection to yet, per Step
21's own confirmed gap) -- are correctly reported as
"insufficient data" placeholders rather than fabricated, exactly the
same honesty discipline Step 21 already established for CASH/NO_TRADE,
non-finite quotes, and the (also-absent) export layer itself. Both
`src.validation.scorecard`/`src.validation.benchmarks` remain real,
already-built, already-tested capabilities a future live-data
orchestrator can wire into this package's `ValidationReportBundle`
once a real 90-day run actually has an equity curve and a benchmark
feed to compute them from.
"""
