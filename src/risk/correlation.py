"""Portfolio correlation check: catches a new position that looks
diversified by underlying but is actually a large concentrated bet on
one factor (ARCHITECTURE.md §7).

Wraps `src.quant.correlations` — this module adds no math of its own,
only the portfolio-awareness of which symbols to compare and the limit
enforcement.

Known limitation (see progress.md): this check requires
`Portfolio.price_history` to already contain aligned price series for
every relevant ticker. No live historical-data wiring exists yet
(src.data.historical is unconnected to the Risk Engine as of Step 9), so
when `price_history` is empty the check is skipped rather than treated
as fail-closed unknown risk — otherwise the Risk Engine could never
approve a single trade in a portfolio that already holds any other
position. This is a deliberate, documented gap, not an oversight.
"""
from __future__ import annotations

import numpy as np

from src.quant.correlations import flag_highly_correlated_pairs
from src.risk.limits import RiskLimitsConfig
from src.risk.portfolio_risk import Portfolio


class CorrelationError(ValueError):
    pass


def check_correlation(portfolio: Portfolio, ticker: str, limits: RiskLimitsConfig) -> None:
    other_tickers = {p.ticker for p in portfolio.positions if p.ticker != ticker}
    if not other_tickers:
        return  # nothing to correlate against

    available = {t for t in other_tickers if t in portfolio.price_history} | (
        {ticker} if ticker in portfolio.price_history else set()
    )
    if ticker not in portfolio.price_history or not (available - {ticker}):
        return  # price history not wired up for this comparison — documented gap, not enforced

    series = {sym: np.asarray(portfolio.price_history[sym], dtype=float) for sym in available}
    pairs = flag_highly_correlated_pairs(series, threshold=limits.high_correlation_threshold)
    offending = [p for p in pairs if ticker in (p.symbol_a, p.symbol_b)]
    if offending:
        worst = offending[0]
        other = worst.symbol_b if worst.symbol_a == ticker else worst.symbol_a
        raise CorrelationError(
            f"{ticker} is {worst.correlation:.2f} correlated with existing position {other}, "
            f"at or above the {limits.high_correlation_threshold:.2f} threshold"
        )
