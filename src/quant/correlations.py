"""Return-correlation analysis across underlyings — used to catch
"independent-looking" positions that are actually one large concentrated
bet (ARCHITECTURE.md §7: portfolio-level correlation limits).

Pure calculation: given historical price series, compute returns and a
correlation matrix. No portfolio-state awareness and no limit
enforcement — that belongs to Python Risk Engine (not implemented yet).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


def returns_from_prices(prices: np.ndarray) -> np.ndarray:
    """Simple (not log) periodic returns from a price series."""
    arr = np.asarray(prices, dtype=float)
    if arr.ndim != 1 or len(arr) < 2:
        raise ValueError("prices must be a 1-D array of at least 2 values")
    if np.any(arr <= 0):
        raise ValueError("prices must be positive")
    return arr[1:] / arr[:-1] - 1.0


def correlation_matrix(price_series: dict[str, np.ndarray]) -> tuple[list[str], np.ndarray]:
    """Pairwise Pearson correlation of returns across symbols. All
    series must be the same length (same observation dates, already
    aligned by the caller) — this function does not silently truncate or
    interpolate a mismatch."""
    if len(price_series) < 2:
        raise ValueError("need at least two symbols to compute a correlation matrix")
    symbols = list(price_series.keys())
    lengths = {len(np.asarray(v)) for v in price_series.values()}
    if len(lengths) != 1:
        raise ValueError("all price series must be the same length")

    returns = [returns_from_prices(np.asarray(price_series[sym])) for sym in symbols]
    matrix = np.vstack(returns)
    corr = np.corrcoef(matrix)
    return symbols, corr


@dataclass(frozen=True)
class CorrelatedPair:
    symbol_a: str
    symbol_b: str
    correlation: float


def flag_highly_correlated_pairs(
    price_series: dict[str, np.ndarray], threshold: float = 0.7
) -> list[CorrelatedPair]:
    """Every pair whose absolute return correlation is at or above
    `threshold`, highest correlation first — the raw material for a
    portfolio-level concentration check (not itself a limit
    enforcement)."""
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be in [0, 1]")
    symbols, corr = correlation_matrix(price_series)
    pairs: list[CorrelatedPair] = []
    n = len(symbols)
    for i in range(n):
        for j in range(i + 1, n):
            c = float(corr[i, j])
            if abs(c) >= threshold:
                pairs.append(CorrelatedPair(symbol_a=symbols[i], symbol_b=symbols[j], correlation=c))
    pairs.sort(key=lambda p: abs(p.correlation), reverse=True)
    return pairs
