"""Strategy Research (Step 14): discovers testable hypotheses about the
platform's three approved strategies from already-run backtests. This
package never modifies a production rule and cannot promote a strategy
to production on its own — see `src.research.promotion.promote_strategy`
for the single, human-gated choke point that can.

Observation -> Hypothesis -> Experimental Strategy Version -> Backtest ->
Validation -> Out-of-Sample Test -> Risk Comparison -> Human Review.

This package only ever reads outputs `src.backtest` already produced
(`src.backtest.simulator.TradeRecord`, `src.backtest.metrics.PerformanceMetrics`)
and `src.risk.limits` thresholds — it runs no backtest and gates no
trade itself.
"""
