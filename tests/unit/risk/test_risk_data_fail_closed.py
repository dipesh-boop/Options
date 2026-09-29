"""PAPER_TRADING_V1.5.3, Step 3: proves the Risk Engine's sector-
concentration/correlation checks actually enforce fail-closed behavior
when a portfolio has risk-data wiring active (`Portfolio
.risk_data_required=True`), and that the pre-V1.5.3 documented gap
(silent "UNKNOWN" sector bucket / silently-skipped correlation) is
preserved byte-for-byte when wiring is inactive (the default -- the
currently active validation cohort's own behavior).

Uses the existing `tests/unit/risk/conftest.py` scenario builders
(`build_approved_pcs_scenario`, etc.) exactly like every other Risk
Engine test in this suite -- no parallel fixture machinery."""
from __future__ import annotations

from datetime import datetime, timezone

from src.risk.engine import evaluate_trade_proposal
from src.risk.portfolio_risk import PortfolioPosition, PortfolioPositionLeg
from src.risk.reason_codes import ReasonCode, RiskDecision
from tests.unit.risk.conftest import EXPIRATION, NOW, build_approved_pcs_scenario


def _existing_qqq_position(**overrides) -> PortfolioPosition:
    base = dict(
        position_id="pos-qqq-1",
        ticker="QQQ",
        sector="ETF",
        strategy="cash_secured_put",
        expiration=EXPIRATION,
        legs=[PortfolioPositionLeg(right="P", side="sell", strike=500.0, entry_price=2.0)],
        contracts=1,
        capital_at_risk=1000.0,
        max_loss=1000.0,
        opened_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return PortfolioPosition(**base)


class TestSectorFailClosed:
    def test_unclassified_sector_fails_closed_when_wiring_active(self):
        """Item 3: missing required sector data fails closed."""
        scenario = build_approved_pcs_scenario(sector_by_ticker={}, risk_data_required=True)
        result = evaluate_trade_proposal(
            scenario.proposal, scenario.portfolio, scenario.quantitative_analysis,
            scenario.market_data, scenario.broker_capabilities, limits=scenario.limits, now=NOW,
        )
        assert result.decision == RiskDecision.REJECT
        assert result.reason_codes == [ReasonCode.REJECT_SECTOR_DATA_UNAVAILABLE]
        assert "SPY" in result.message

    def test_unclassified_sector_falls_back_to_unknown_when_wiring_inactive(self):
        """Item 15/active-cohort compatibility: the pre-V1.5.3 documented
        gap (silent UNKNOWN bucket, never a data-unavailable REJECT) is
        preserved exactly when risk_data_required is at its default
        False -- an unclassified ticker never blocks the trade for this
        reason alone."""
        scenario = build_approved_pcs_scenario(sector_by_ticker={})
        assert scenario.portfolio.risk_data_required is False
        result = evaluate_trade_proposal(
            scenario.proposal, scenario.portfolio, scenario.quantitative_analysis,
            scenario.market_data, scenario.broker_capabilities, limits=scenario.limits, now=NOW,
        )
        assert result.reason_codes != [ReasonCode.REJECT_SECTOR_DATA_UNAVAILABLE]
        assert result.decision == RiskDecision.APPROVE

    def test_classified_sector_passes_through_normally_when_wiring_active(self):
        """A ticker risk-data wiring COULD classify (present in
        sector_by_ticker) is never blocked by the new fail-closed check
        -- only a genuinely missing classification is."""
        scenario = build_approved_pcs_scenario(sector_by_ticker={"SPY": "ETF"}, risk_data_required=True)
        result = evaluate_trade_proposal(
            scenario.proposal, scenario.portfolio, scenario.quantitative_analysis,
            scenario.market_data, scenario.broker_capabilities, limits=scenario.limits, now=NOW,
        )
        assert result.decision == RiskDecision.APPROVE


class TestCorrelationFailClosed:
    def test_missing_history_fails_closed_when_wiring_active_and_position_exists(self):
        """Items 4/8: missing required historical data fails closed; a
        candidate can never interpret missing history as zero
        correlation."""
        scenario = build_approved_pcs_scenario(
            positions=[_existing_qqq_position()], sector_by_ticker={"SPY": "ETF"}, risk_data_required=True,
        )
        result = evaluate_trade_proposal(
            scenario.proposal, scenario.portfolio, scenario.quantitative_analysis,
            scenario.market_data, scenario.broker_capabilities, limits=scenario.limits, now=NOW,
        )
        assert result.decision == RiskDecision.REJECT
        assert result.reason_codes == [ReasonCode.REJECT_CORRELATION_DATA_UNAVAILABLE]
        assert "QQQ" in result.message

    def test_missing_history_is_silently_skipped_when_wiring_inactive(self):
        """Item 15/active-cohort compatibility: the pre-V1.5.3 documented
        gap (silent skip, never a data-unavailable REJECT) is preserved
        exactly when risk_data_required is at its default False."""
        scenario = build_approved_pcs_scenario(positions=[_existing_qqq_position()], sector_by_ticker={"SPY": "ETF"})
        assert scenario.portfolio.risk_data_required is False
        result = evaluate_trade_proposal(
            scenario.proposal, scenario.portfolio, scenario.quantitative_analysis,
            scenario.market_data, scenario.broker_capabilities, limits=scenario.limits, now=NOW,
        )
        assert result.reason_codes != [ReasonCode.REJECT_CORRELATION_DATA_UNAVAILABLE]
        assert result.decision == RiskDecision.APPROVE

    def test_empty_portfolio_requires_no_correlation_data_even_with_wiring_active(self):
        """The empty-portfolio carve-out (Step 3's own explicit
        requirement): no existing positions means nothing to correlate
        against, so risk_data_required=True must not manufacture a
        correlation-data requirement out of nothing."""
        scenario = build_approved_pcs_scenario(sector_by_ticker={"SPY": "ETF"}, risk_data_required=True)
        assert scenario.portfolio.positions == []
        result = evaluate_trade_proposal(
            scenario.proposal, scenario.portfolio, scenario.quantitative_analysis,
            scenario.market_data, scenario.broker_capabilities, limits=scenario.limits, now=NOW,
        )
        assert result.reason_codes != [ReasonCode.REJECT_CORRELATION_DATA_UNAVAILABLE]
        assert result.decision == RiskDecision.APPROVE

    def test_sufficient_uncorrelated_history_approves_when_wiring_active(self):
        """Item 12: one existing position triggers correlation
        evaluation -- and a genuinely low correlation still approves,
        proving the fail-closed path isn't a blanket reject."""
        low_correlation_spy = [100.0 + (i % 3) for i in range(30)]
        low_correlation_qqq = [200.0 - (i % 5) for i in range(30)]
        scenario = build_approved_pcs_scenario(
            positions=[_existing_qqq_position()], sector_by_ticker={"SPY": "ETF"}, risk_data_required=True,
            price_history={"SPY": low_correlation_spy, "QQQ": low_correlation_qqq},
        )
        result = evaluate_trade_proposal(
            scenario.proposal, scenario.portfolio, scenario.quantitative_analysis,
            scenario.market_data, scenario.broker_capabilities, limits=scenario.limits, now=NOW,
        )
        assert result.reason_codes != [ReasonCode.REJECT_CORRELATION_DATA_UNAVAILABLE]
        assert result.decision == RiskDecision.APPROVE

    def test_sufficient_highly_correlated_history_still_rejects_on_the_limit_itself(self):
        """Item 14: risk-data provider "success" never bypasses Risk --
        once real data IS available, the existing correlation LIMIT
        (config/risk_limits.yaml's high_correlation_threshold, untouched
        by this step) is still enforced exactly as before."""
        trending_series = [100.0 + i for i in range(30)]
        scenario = build_approved_pcs_scenario(
            positions=[_existing_qqq_position()], sector_by_ticker={"SPY": "ETF"}, risk_data_required=True,
            price_history={"SPY": trending_series, "QQQ": [2 * p for p in trending_series]},
        )
        result = evaluate_trade_proposal(
            scenario.proposal, scenario.portfolio, scenario.quantitative_analysis,
            scenario.market_data, scenario.broker_capabilities, limits=scenario.limits, now=NOW,
        )
        assert result.decision == RiskDecision.REJECT
        assert result.reason_codes == [ReasonCode.REJECT_CORRELATION]
