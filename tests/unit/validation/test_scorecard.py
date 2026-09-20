from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.validation.consistency import ConsistencySummary, RuleComplianceSummary
from src.validation.decision_quality import ValidationDecisionQualityReport
from src.validation.execution_quality import OptionsExecutionMetrics
from src.validation.protocol import SampleSizeStatus, ValidationPeriod
from src.validation.regime_analysis import RegimeCoverageSummary
from src.validation.scorecard import ReturnInputs, RiskInputs, ScorecardCategory, ValidationScorecard, build_scorecard
from src.workflows.decision_quality import DecisionQualitySummary
from src.workflows.execution_quality import SlippageSummary
from src.workflows.rejected_trade_review import RejectedTradeStatistics


def _scorecard(*, sample_status=SampleSizeStatus.PREFERRED_SAMPLE, sharpe=1.5, since_inception_return=0.08) -> ValidationScorecard:
    period = ValidationPeriod(start_date=date(2026, 1, 1), end_date=date(2026, 4, 1), duration_days=90)
    return build_scorecard(
        generated_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        period=period,
        sample_status=sample_status,
        completed_trades=110,
        return_inputs=ReturnInputs(
            since_inception_return=since_inception_return, annualized_return_projected=0.30,
            excess_vs_spy=0.02, excess_vs_risk_free=0.04, excess_vs_cash=0.08,
        ),
        risk_inputs=RiskInputs(sharpe=sharpe, sortino=1.8, max_drawdown=0.05, current_drawdown_pct=0.02, var_95=0.02, cvar_95=0.03),
        decision_quality=ValidationDecisionQualityReport(
            combined=DecisionQualitySummary(counts={"good_decision_good_outcome": 80, "good_decision_bad_outcome": 20,
                                                      "bad_decision_good_outcome": 5, "bad_decision_bad_outcome": 5}, total=110),
            by_strategy={},
        ),
        rejected_trades=RejectedTradeStatistics(sample_size=25, hit_rate=0.4, average_hypothetical_pnl=10.0,
                                                  median_hypothetical_pnl=5.0, total_hypothetical_pnl=250.0,
                                                  meaningful_sample=True, warning=None),
        slippage=SlippageSummary(count=10, average_slippage=0.02, median_slippage=0.01, by_strategy={}, by_underlying={}, by_time_of_day={}),
        options_execution=OptionsExecutionMetrics(total_closed_trades=110, assignment_count=5, assignment_rate=0.045,
                                                    early_close_count=90, early_close_rate=0.818, expiration_otm_count=15,
                                                    expiration_otm_rate=0.136, attempted_trades=120, fill_rate=110 / 120),
        consistency=ConsistencySummary(weeks_with_trades=12, profitable_week_count=9, profitable_week_pct=0.75,
                                        average_weekly_pnl=200.0, weekly_pnl_stdev=150.0, worst_week_pnl=-100.0,
                                        best_week_pnl=600.0, longest_losing_week_streak=1),
        regime_coverage=RegimeCoverageSummary(regimes_observed=("bull_trending", "range_bound_low_vol"),
                                               regimes_not_observed=("bear_trending", "range_bound_high_vol", "crisis_tail_event"),
                                               trade_count_by_regime={"bull_trending": 80, "range_bound_low_vol": 30},
                                               single_regime_dominant=False),
        compliance=RuleComplianceSummary(total_trades_checked=110, violations_found=0, compliance_pct=1.0, violations=()),
    )


class TestScorecardStructure:
    def test_never_collapses_into_a_single_overall_score(self):
        """The core structural guarantee: no field anywhere on
        ValidationScorecard or ScorecardCategory resembles a single
        composite score."""
        scorecard = _scorecard()
        top_level_fields = {"generated_at", "period", "categories"}
        assert set(ValidationScorecard.__dataclass_fields__.keys()) == top_level_fields
        category_fields = set(ScorecardCategory.__dataclass_fields__.keys())
        assert category_fields == {"name", "metrics", "qualitative_note"}
        forbidden_names = {"overall_score", "score", "total_score", "composite_score", "grade"}
        for f in top_level_fields | category_fields:
            assert f.lower() not in forbidden_names

    def test_has_exactly_the_seven_named_categories(self):
        scorecard = _scorecard()
        names = {c.name for c in scorecard.categories}
        assert names == {"RETURN", "RISK", "TRADE_QUALITY", "EXECUTION", "CONSISTENCY", "COMPLIANCE", "SAMPLE_ADEQUACY"}

    def test_category_lookup_by_name(self):
        scorecard = _scorecard()
        assert scorecard.category("RETURN").metrics["since_inception_return"] == pytest.approx(0.08)

    def test_unknown_category_raises(self):
        scorecard = _scorecard()
        with pytest.raises(KeyError):
            scorecard.category("NOT_A_CATEGORY")

    def test_sample_adequacy_category_reflects_sample_status(self):
        scorecard = _scorecard(sample_status=SampleSizeStatus.INSUFFICIENT_SAMPLE)
        assert scorecard.category("SAMPLE_ADEQUACY").metrics["sample_status"] == "insufficient_sample"
