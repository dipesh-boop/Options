"""Tests for stages 3-4: confirmed-Fidelity-position reconciliation."""
from __future__ import annotations

from datetime import date

from src.llm.schemas import StrategyType
from src.risk.portfolio_risk import Portfolio, PortfolioPosition, PortfolioPositionLeg
from src.workflows.reconciliation import ConfirmedFidelityPosition, reconcile_portfolio
from tests.unit.workflows.conftest import NOW

EXPIRATION = date(2026, 10, 16)


def _internal_position(**overrides) -> PortfolioPosition:
    base = dict(
        position_id="p1", ticker="XYZ", sector="TECH", strategy=StrategyType.CASH_SECURED_PUT, expiration=EXPIRATION,
        legs=[PortfolioPositionLeg(right="P", side="sell", strike=95.0, entry_price=2.0)], contracts=2,
        capital_at_risk=9500.0, max_loss=9500.0, opened_at=NOW,
    )
    base.update(overrides)
    return PortfolioPosition(**base)


def _confirmed(**overrides) -> ConfirmedFidelityPosition:
    base = dict(
        ticker="XYZ", strategy=StrategyType.CASH_SECURED_PUT, expiration=EXPIRATION, strikes=frozenset({95.0}),
        contracts=2, confirmed_by="dipesh", confirmed_at=NOW,
    )
    base.update(overrides)
    return ConfirmedFidelityPosition(**base)


class TestCleanReconciliation:
    def test_matching_position_produces_no_discrepancy(self):
        portfolio = Portfolio(as_of=NOW, nav=100_000.0, cash=90_000.0, peak_equity=100_000.0, positions=[_internal_position()])
        result = reconcile_portfolio([_confirmed()], portfolio)
        assert result.matched_count == 1
        assert result.clean is True

    def test_no_positions_either_side_is_clean(self):
        portfolio = Portfolio(as_of=NOW, nav=100_000.0, cash=90_000.0, peak_equity=100_000.0)
        result = reconcile_portfolio([], portfolio)
        assert result.matched_count == 0
        assert result.clean is True


class TestDiscrepancies:
    def test_confirmed_position_missing_internally(self):
        portfolio = Portfolio(as_of=NOW, nav=100_000.0, cash=90_000.0, peak_equity=100_000.0)
        result = reconcile_portfolio([_confirmed()], portfolio)
        assert result.matched_count == 0
        assert len(result.discrepancies) == 1
        assert result.discrepancies[0].kind == "missing_from_internal"

    def test_internal_position_not_confirmed_in_fidelity(self):
        portfolio = Portfolio(as_of=NOW, nav=100_000.0, cash=90_000.0, peak_equity=100_000.0, positions=[_internal_position()])
        result = reconcile_portfolio([], portfolio)
        assert result.matched_count == 0
        assert result.discrepancies[0].kind == "missing_from_fidelity"

    def test_quantity_mismatch_reported_distinctly(self):
        portfolio = Portfolio(as_of=NOW, nav=100_000.0, cash=90_000.0, peak_equity=100_000.0, positions=[_internal_position(contracts=2)])
        result = reconcile_portfolio([_confirmed(contracts=3)], portfolio)
        assert result.matched_count == 0
        assert result.discrepancies[0].kind == "quantity_mismatch"
        assert not result.clean

    def test_different_strategy_same_ticker_is_not_a_match(self):
        portfolio = Portfolio(as_of=NOW, nav=100_000.0, cash=90_000.0, peak_equity=100_000.0, positions=[_internal_position(strategy=StrategyType.COVERED_CALL, legs=[PortfolioPositionLeg(right="C", side="sell", strike=105.0, entry_price=2.0)])])
        result = reconcile_portfolio([_confirmed(strategy=StrategyType.CASH_SECURED_PUT)], portfolio)
        # neither matches the other -- one missing_from_internal, one missing_from_fidelity
        assert result.matched_count == 0
        assert {d.kind for d in result.discrepancies} == {"missing_from_internal", "missing_from_fidelity"}

    def test_different_strikes_same_ticker_strategy_expiration_is_not_a_match(self):
        portfolio = Portfolio(as_of=NOW, nav=100_000.0, cash=90_000.0, peak_equity=100_000.0, positions=[_internal_position(legs=[PortfolioPositionLeg(right="P", side="sell", strike=90.0, entry_price=2.0)])])
        result = reconcile_portfolio([_confirmed(strikes=frozenset({95.0}))], portfolio)
        assert result.matched_count == 0
        assert len(result.discrepancies) == 2

    def test_multiple_confirmed_positions_matched_independently(self):
        portfolio = Portfolio(
            as_of=NOW, nav=100_000.0, cash=80_000.0, peak_equity=100_000.0,
            positions=[_internal_position(position_id="p1", ticker="AAA"), _internal_position(position_id="p2", ticker="BBB")],
        )
        confirmed = [_confirmed(ticker="AAA"), _confirmed(ticker="BBB")]
        result = reconcile_portfolio(confirmed, portfolio)
        assert result.matched_count == 2
        assert result.clean is True
