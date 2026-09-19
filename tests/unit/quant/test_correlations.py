"""Correlation tests using mathematically exact constructed cases:
identical series -> correlation exactly 1; an exact linear inverse ->
exactly -1; these are not approximations, they're definitional
properties of the Pearson correlation coefficient."""
from __future__ import annotations

import numpy as np
import pytest

from src.quant.correlations import (
    correlation_matrix,
    flag_highly_correlated_pairs,
    returns_from_prices,
)


class TestReturnsFromPrices:
    def test_simple_return_calculation(self):
        prices = np.array([100.0, 110.0, 99.0])
        returns = returns_from_prices(prices)
        assert returns[0] == pytest.approx(0.10)
        assert returns[1] == pytest.approx(99.0 / 110.0 - 1.0)

    def test_too_short_series_rejected(self):
        with pytest.raises(ValueError):
            returns_from_prices(np.array([100.0]))

    def test_non_positive_price_rejected(self):
        with pytest.raises(ValueError):
            returns_from_prices(np.array([100.0, 0.0, 90.0]))


class TestCorrelationMatrixExactCases:
    def test_identical_series_have_correlation_one(self):
        prices = np.array([100.0, 102.0, 101.0, 105.0, 103.0, 108.0])
        symbols, corr = correlation_matrix({"A": prices, "B": prices.copy()})
        assert corr[0, 1] == pytest.approx(1.0, abs=1e-9)

    def test_exact_linear_scale_has_correlation_one(self):
        # B's returns are a positive linear function of A's prices ->
        # same return series -> correlation must be exactly 1.
        a = np.array([100.0, 102.0, 101.0, 105.0, 103.0, 108.0])
        b = a * 2.5
        _, corr = correlation_matrix({"A": a, "B": b})
        assert corr[0, 1] == pytest.approx(1.0, abs=1e-9)

    def test_inverse_price_series_has_correlation_near_negative_one(self):
        a = np.array([100.0, 105.0, 103.0, 110.0, 108.0, 115.0])
        # Construct b as a genuinely inverse price process: b's return
        # each period is the exact negative of a's return.
        a_returns = returns_from_prices(a)
        b = [100.0]
        for r in a_returns:
            b.append(b[-1] * (1 - r))
        b = np.array(b)
        _, corr = correlation_matrix({"A": a, "B": b})
        assert corr[0, 1] == pytest.approx(-1.0, abs=1e-9)

    def test_diagonal_is_always_one(self):
        rng = np.random.default_rng(0)
        a = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 20)))
        b = 50 * np.exp(np.cumsum(rng.normal(0, 0.01, 20)))
        _, corr = correlation_matrix({"A": a, "B": b})
        assert corr[0, 0] == pytest.approx(1.0)
        assert corr[1, 1] == pytest.approx(1.0)

    def test_matrix_is_symmetric(self):
        rng = np.random.default_rng(1)
        series = {sym: 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 30))) for sym in ("A", "B", "C")}
        _, corr = correlation_matrix(series)
        np.testing.assert_allclose(corr, corr.T)

    def test_requires_at_least_two_symbols(self):
        with pytest.raises(ValueError):
            correlation_matrix({"A": np.array([100.0, 101.0, 102.0])})

    def test_mismatched_lengths_rejected(self):
        with pytest.raises(ValueError):
            correlation_matrix({"A": np.array([100.0, 101.0, 102.0]), "B": np.array([100.0, 101.0])})


class TestFlagHighlyCorrelatedPairs:
    def test_identical_series_flagged_above_default_threshold(self):
        prices = np.array([100.0, 102.0, 101.0, 105.0, 103.0, 108.0])
        pairs = flag_highly_correlated_pairs({"A": prices, "B": prices.copy(), "C": prices * 3})
        assert len(pairs) == 3  # A-B, A-C, B-C all perfectly correlated
        assert all(p.correlation == pytest.approx(1.0, abs=1e-9) for p in pairs)

    def test_uncorrelated_independent_series_not_flagged(self):
        rng = np.random.default_rng(42)
        a = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 500)))
        b = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, 500)))
        pairs = flag_highly_correlated_pairs({"A": a, "B": b}, threshold=0.7)
        assert pairs == []

    def test_sorted_by_absolute_correlation_descending(self):
        base = np.array([100.0, 102.0, 101.0, 105.0, 103.0, 108.0, 110.0])
        weakly_related = base + np.array([0.0, 1.0, -2.0, 3.0, -1.0, 2.0, 0.0])
        pairs = flag_highly_correlated_pairs(
            {"A": base, "B": base.copy(), "C": weakly_related}, threshold=0.0
        )
        correlations = [abs(p.correlation) for p in pairs]
        assert correlations == sorted(correlations, reverse=True)

    def test_invalid_threshold_rejected(self):
        with pytest.raises(ValueError):
            flag_highly_correlated_pairs({"A": np.array([1.0, 2.0]), "B": np.array([1.0, 2.0])}, threshold=1.5)
