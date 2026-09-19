"""Tests for strategy-level risk review — the "RISK COMPARISON" stage,
reusing `src.risk.limits.RiskLimitsConfig` thresholds rather than a
second set of numbers."""
from __future__ import annotations

from datetime import date

import pytest

from src.backtest.metrics import compute_metrics
from src.backtest.simulator import TradeRecord
from src.llm.schemas import StrategyType
from src.risk.limits import RiskLimitsConfig
from src.research.risk_review import MIN_TRADES_FOR_CONFIDENT_RISK_READ, StrategyRiskReviewInputs, review_strategy_risk


def _limits(**overrides) -> RiskLimitsConfig:
    base = dict(
        target_risk_per_trade_pct=0.01,
        absolute_max_risk_per_trade_pct=0.02,
        max_underlying_exposure_pct=0.10,
        max_sector_exposure_pct=0.25,
        min_cash_reserve_pct=0.20,
        normal_max_capital_deployed_pct=0.60,
        absolute_max_capital_deployed_pct=0.70,
        drawdown_warning_pct=0.08,
        drawdown_risk_reduction_pct=0.10,
        drawdown_halt_pct=0.15,
        risk_reduction_sizing_multiplier=0.5,
        min_open_interest=100,
        min_volume=10,
        max_bid_ask_spread_pct=0.15,
        high_correlation_threshold=0.70,
        max_market_data_age_minutes=15.0,
        risk_free_rate=0.04,
        quant_cross_check_tolerance_pct=0.05,
        max_stress_loss_pct_of_nav=0.30,
    )
    base.update(overrides)
    return RiskLimitsConfig(**base)


def _curve_with_drawdown(drawdown_frac: float) -> list[tuple[date, float]]:
    """A minimal 3-point equity curve that peaks at 100 then dips to
    exactly `100 * (1 - drawdown_frac)` before a partial recovery."""
    trough = 100.0 * (1.0 - drawdown_frac)
    return [(date(2024, 1, 1), 100.0), (date(2024, 6, 1), trough), (date(2025, 1, 1), max(trough, 90.0))]


def _trade(pnl: float = 10.0) -> TradeRecord:
    return TradeRecord(
        position_id="p", ticker="XYZ", strategy=StrategyType.PUT_CREDIT_SPREAD, contracts=1,
        opened_at=date(2024, 1, 1), closed_at=date(2024, 1, 11), close_reason="profit_target",
        capital_at_risk=500.0, entry_spread_pct=0.05, realistic_entry_credit=100.0, realistic_exit_debit=-90.0,
        theoretical_entry_credit=100.0, theoretical_exit_debit=-90.0, commission_paid=1.3, realistic_pnl=pnl, theoretical_pnl=pnl,
    )


class TestVerdictThresholds:
    def test_shallow_drawdown_and_ample_trades_pass(self):
        limits = _limits()
        curve = _curve_with_drawdown(0.02)
        trades = [_trade() for _ in range(MIN_TRADES_FOR_CONFIDENT_RISK_READ)]
        metrics = compute_metrics(curve, trades, 100.0, 0.04)
        review = review_strategy_risk(StrategyRiskReviewInputs(realistic_metrics=metrics, theoretical_metrics=metrics, trade_count=len(trades)), limits)
        assert review.verdict == "PASS"
        assert review.reasons == ()

    def test_drawdown_in_risk_reduction_zone_is_concern(self):
        limits = _limits()
        curve = _curve_with_drawdown(0.12)  # between 0.10 and 0.15
        trades = [_trade() for _ in range(MIN_TRADES_FOR_CONFIDENT_RISK_READ)]
        metrics = compute_metrics(curve, trades, 100.0, 0.04)
        review = review_strategy_risk(StrategyRiskReviewInputs(realistic_metrics=metrics, theoretical_metrics=metrics, trade_count=len(trades)), limits)
        assert review.verdict == "CONCERN"
        assert any("risk-reduction zone" in r for r in review.reasons)

    def test_drawdown_at_or_beyond_halt_threshold_is_reject(self):
        limits = _limits()
        curve = _curve_with_drawdown(0.20)  # beyond 0.15 halt
        trades = [_trade() for _ in range(MIN_TRADES_FOR_CONFIDENT_RISK_READ)]
        metrics = compute_metrics(curve, trades, 100.0, 0.04)
        review = review_strategy_risk(StrategyRiskReviewInputs(realistic_metrics=metrics, theoretical_metrics=metrics, trade_count=len(trades)), limits)
        assert review.verdict == "REJECT"
        assert any("halt threshold" in r for r in review.reasons)

    def test_reject_outranks_concern_when_both_apply(self):
        """A REJECT-level drawdown alongside a small-sample CONCERN must
        report REJECT overall, never let the milder concern win."""
        limits = _limits()
        curve = _curve_with_drawdown(0.25)
        trades = [_trade() for _ in range(5)]  # below the confident-read threshold too
        metrics = compute_metrics(curve, trades, 100.0, 0.04)
        review = review_strategy_risk(StrategyRiskReviewInputs(realistic_metrics=metrics, theoretical_metrics=metrics, trade_count=5), limits)
        assert review.verdict == "REJECT"
        assert len(review.reasons) >= 2  # both the drawdown reject and the small-sample concern are recorded

    def test_small_sample_alone_is_concern_not_reject(self):
        limits = _limits()
        curve = _curve_with_drawdown(0.01)
        trades = [_trade() for _ in range(5)]
        metrics = compute_metrics(curve, trades, 100.0, 0.04)
        review = review_strategy_risk(StrategyRiskReviewInputs(realistic_metrics=metrics, theoretical_metrics=metrics, trade_count=5), limits)
        assert review.verdict == "CONCERN"
        assert any("confident risk read" in r for r in review.reasons)

    def test_realistic_metrics_graded_not_theoretical(self):
        """A strategy whose theoretical track looks fine but whose
        realistic track breaches the halt threshold must still REJECT --
        this function grades execution reality, never the frictionless
        fantasy."""
        limits = _limits()
        realistic_curve = _curve_with_drawdown(0.20)
        theoretical_curve = _curve_with_drawdown(0.01)
        trades = [_trade() for _ in range(MIN_TRADES_FOR_CONFIDENT_RISK_READ)]
        realistic_metrics = compute_metrics(realistic_curve, trades, 100.0, 0.04)
        theoretical_metrics = compute_metrics(theoretical_curve, trades, 100.0, 0.04)
        review = review_strategy_risk(StrategyRiskReviewInputs(realistic_metrics=realistic_metrics, theoretical_metrics=theoretical_metrics, trade_count=len(trades)), limits)
        assert review.verdict == "REJECT"

    def test_uses_default_limits_when_none_supplied(self):
        curve = _curve_with_drawdown(0.02)
        trades = [_trade() for _ in range(MIN_TRADES_FOR_CONFIDENT_RISK_READ)]
        metrics = compute_metrics(curve, trades, 100.0, 0.04)
        review = review_strategy_risk(StrategyRiskReviewInputs(realistic_metrics=metrics, theoretical_metrics=metrics, trade_count=len(trades)))
        assert review.verdict in ("PASS", "CONCERN", "REJECT")
