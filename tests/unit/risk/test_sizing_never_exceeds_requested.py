"""Direct proof, at the src.risk.trade_risk.size_trade level (not just
observed indirectly through the full engine), that the Risk Engine can
only ever reduce a requested contract count, never increase it — the
core guarantee Step 9 calls out by name."""
from __future__ import annotations

from src.quant.expected_value import StrategyEconomics
from src.risk.limits import load_risk_limits
from src.risk.trade_risk import size_trade
from tests.unit.risk.conftest import make_portfolio


def _economics(max_loss: float, capital_required: float) -> StrategyEconomics:
    return StrategyEconomics(
        max_profit=100.0,
        max_loss=max_loss,
        breakeven=100.0,
        capital_required=capital_required,
        return_on_capital=100.0 / capital_required,
        annualized_roc=1.0,
        probability_of_profit=0.6,
        expected_value=10.0,
    )


class TestSizingNeverExceedsRequested:
    def test_plenty_of_budget_still_caps_at_requested(self):
        portfolio = make_portfolio(nav=10_000_000.0, cash=9_000_000.0, peak_equity=10_000_000.0)
        limits = load_risk_limits()
        result = size_trade(3, _economics(max_loss=10.0, capital_required=10.0), portfolio, limits)
        assert result.contracts == 3
        assert result.capped_by == "requested"

    def test_tight_budget_reduces_below_requested(self):
        portfolio = make_portfolio(nav=100_000.0, cash=90_000.0, peak_equity=100_000.0)
        limits = load_risk_limits()
        result = size_trade(1000, _economics(max_loss=500.0, capital_required=500.0), portfolio, limits)
        assert result.contracts < 1000
        assert result.contracts >= 0

    def test_risk_reduction_multiplier_only_ever_tightens(self):
        portfolio = make_portfolio(nav=100_000.0, cash=90_000.0, peak_equity=100_000.0)
        limits = load_risk_limits()
        normal = size_trade(1000, _economics(max_loss=200.0, capital_required=200.0), portfolio, limits, risk_multiplier=1.0)
        tightened = size_trade(1000, _economics(max_loss=200.0, capital_required=200.0), portfolio, limits, risk_multiplier=0.5)
        assert tightened.contracts <= normal.contracts

    def test_zero_requested_yields_zero_never_a_positive_surprise(self):
        portfolio = make_portfolio(nav=1_000_000.0, cash=900_000.0, peak_equity=1_000_000.0)
        limits = load_risk_limits()
        result = size_trade(0, _economics(max_loss=10.0, capital_required=10.0), portfolio, limits)
        assert result.contracts == 0
