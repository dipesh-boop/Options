"""Portfolio correlation check: catches a new position that looks
diversified by underlying but is actually a large concentrated bet on
one factor (ARCHITECTURE.md §7).

Wraps `src.quant.correlations` — this module adds no math of its own,
only the portfolio-awareness of which symbols to compare and the limit
enforcement.

**Missing-data behavior (PAPER_TRADING_V1.5.3, Step 3).** This check
requires `Portfolio.price_history` to already contain aligned price
series for every relevant ticker — `src.portfolio.risk_data
.apply_correlation_wiring` is what populates it, before this function
ever runs. Two distinct behaviors when that coverage is missing, keyed
on `Portfolio.risk_data_required` (see that field's own docstring):

- `risk_data_required=False` (the default; the currently active
  validation cohort, unless its own operator config explicitly turns
  risk-data wiring on): the pre-V1.5.3 documented gap is preserved
  EXACTLY — the check is silently skipped, never fail-closed. Changing
  this default behavior for an already-running cohort is exactly what
  this step's own "no active-cohort behavior change" requirement
  forbids.
- `risk_data_required=True` (a future cohort that has explicitly
  activated risk-data wiring): missing coverage for a ticker that
  actually needs to be correlated against (the portfolio holds at
  least one other position) raises `CorrelationDataUnavailableError`
  — a fail-closed REJECT, never silently treated as "no correlation
  risk." An empty portfolio (no other positions) is unaffected either
  way: there is nothing to correlate against, so no data is required
  at all, regardless of `risk_data_required`.
"""
from __future__ import annotations

import numpy as np

from src.quant.correlations import flag_highly_correlated_pairs
from src.risk.limits import RiskLimitsConfig
from src.risk.portfolio_risk import Portfolio


class CorrelationError(ValueError):
    pass


class CorrelationDataUnavailableError(CorrelationError):
    """Raised instead of silently skipping the check when
    `Portfolio.risk_data_required` is True, the portfolio holds at
    least one other position, and trustworthy aligned price history
    isn't available for every ticker that needs correlating. Missing
    data is never interpreted as zero correlation."""


def check_correlation(portfolio: Portfolio, ticker: str, limits: RiskLimitsConfig) -> None:
    other_tickers = {p.ticker for p in portfolio.positions if p.ticker != ticker}
    if not other_tickers:
        return  # nothing to correlate against, regardless of risk_data_required

    available = {t for t in other_tickers if t in portfolio.price_history} | (
        {ticker} if ticker in portfolio.price_history else set()
    )
    missing = (other_tickers | {ticker}) - available
    if ticker not in portfolio.price_history or not (available - {ticker}):
        if portfolio.risk_data_required:
            raise CorrelationDataUnavailableError(
                f"correlation evaluation is required ({len(other_tickers)} existing position(s): "
                f"{sorted(other_tickers)}) but trustworthy aligned historical price data is unavailable "
                f"for: {sorted(missing)}"
            )
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
