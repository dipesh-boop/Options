from __future__ import annotations

import pytest

from src.risk.reason_codes import RiskDecision
from src.validation.decision_quality import (
    ValidationDecisionRecord,
    classify_validation_trade,
    summarize_validation_decision_quality,
    summarize_validation_rejected_trades,
)
from src.workflows.rejected_trade_review import HypotheticalOutcome

from .conftest import _trade


def _record(**overrides) -> ValidationDecisionRecord:
    defaults = dict(
        trade=_trade(),
        risk_decision=RiskDecision.APPROVE,
        devils_advocate_verdict="PASS",
        probability_of_profit=0.65,
    )
    defaults.update(overrides)
    return ValidationDecisionRecord(**defaults)


class TestClassifyValidationTrade:
    def test_good_process_good_outcome(self):
        result = classify_validation_trade(_record(), min_probability_of_profit=0.5)
        assert result.label == "good_decision_good_outcome"

    def test_good_process_bad_outcome_from_a_loss_stays_good_decision(self):
        record = _record(trade=_trade(realistic_pnl=-75.0))
        result = classify_validation_trade(record, min_probability_of_profit=0.5)
        assert result.good_decision is True
        assert result.good_outcome is False

    def test_rejected_trade_that_somehow_has_a_realized_pnl_is_bad_decision(self):
        record = _record(risk_decision=RiskDecision.REJECT, trade=_trade(realistic_pnl=200.0))
        result = classify_validation_trade(record, min_probability_of_profit=0.5)
        assert result.good_decision is False
        assert result.good_outcome is True
        assert result.label == "bad_decision_good_outcome"


class TestSummarizeValidationDecisionQuality:
    def test_combined_and_per_strategy_never_collapsed(self):
        records = [
            _record(trade=_trade(position_id="a", strategy=_trade().strategy)),
            _record(trade=_trade(position_id="b", strategy=_trade().strategy, realistic_pnl=-10.0)),
        ]
        report = summarize_validation_decision_quality(records, min_probability_of_profit=0.5)
        assert report.combined.total == 2
        assert sum(s.total for s in report.by_strategy.values()) == 2
        # Both fields exist independently -- never merged into one number.
        assert isinstance(report.combined.counts, dict)
        assert isinstance(report.by_strategy, dict)

    def test_per_strategy_buckets_are_separate_from_each_other(self):
        from src.llm.schemas import StrategyType

        csp = _record(trade=_trade(position_id="a", strategy=StrategyType.CASH_SECURED_PUT))
        cc = _record(trade=_trade(position_id="b", strategy=StrategyType.COVERED_CALL, realistic_pnl=-50.0))
        report = summarize_validation_decision_quality([csp, cc], min_probability_of_profit=0.5)
        assert report.by_strategy["cash_secured_put"].total == 1
        assert report.by_strategy["covered_call"].total == 1
        assert report.by_strategy["covered_call"].counts["good_decision_bad_outcome"] == 1


class TestSummarizeValidationRejectedTrades:
    def test_below_threshold_is_not_meaningful(self):
        outcomes = [
            HypotheticalOutcome(proposal_id=f"p{i}", ticker="XYZ", strategy="cash_secured_put",
                                 rejected_stage="risk_engine", hypothetical_pnl=100.0, exit_reason="closed")
            for i in range(5)
        ]
        stats = summarize_validation_rejected_trades(outcomes, min_sample_size=20)
        assert stats.meaningful_sample is False
        assert stats.warning is not None

    def test_at_threshold_is_meaningful(self):
        outcomes = [
            HypotheticalOutcome(proposal_id=f"p{i}", ticker="XYZ", strategy="cash_secured_put",
                                 rejected_stage="risk_engine", hypothetical_pnl=100.0, exit_reason="closed")
            for i in range(20)
        ]
        stats = summarize_validation_rejected_trades(outcomes, min_sample_size=20)
        assert stats.meaningful_sample is True
        assert stats.warning is None
