from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from src.risk.reason_codes import RiskDecision
from src.validation.consistency import (
    build_weekly_consistency,
    check_rule_compliance,
    correlation_summary,
    summarize_consistency,
)
from src.validation.session import RuleViolationRecord

from .conftest import _trade


class TestBuildWeeklyConsistency:
    def test_buckets_trades_into_7_day_windows_from_period_start(self):
        period_start = date(2026, 1, 1)
        trades = [
            _trade(position_id="a", closed_at=date(2026, 1, 3)),  # week 0
            _trade(position_id="b", closed_at=date(2026, 1, 5)),  # week 0
            _trade(position_id="c", closed_at=date(2026, 1, 9)),  # week 1
        ]
        records = build_weekly_consistency(trades, period_start=period_start)
        assert len(records) == 2
        assert records[0].trade_count == 2
        assert records[1].trade_count == 1

    def test_win_rate_computed_per_week(self):
        period_start = date(2026, 1, 1)
        trades = [
            _trade(position_id="a", closed_at=date(2026, 1, 2), realistic_pnl=100.0),
            _trade(position_id="b", closed_at=date(2026, 1, 3), realistic_pnl=-50.0),
        ]
        records = build_weekly_consistency(trades, period_start=period_start)
        assert records[0].win_rate == pytest.approx(0.5)

    def test_no_trades_returns_empty_list(self):
        assert build_weekly_consistency([], period_start=date(2026, 1, 1)) == []


class TestSummarizeConsistency:
    def test_no_active_weeks_returns_zeroed_summary(self):
        summary = summarize_consistency([])
        assert summary.weeks_with_trades == 0
        assert summary.profitable_week_pct == 0.0

    def test_profitable_week_pct_and_streak(self):
        period_start = date(2026, 1, 1)
        trades = [
            _trade(position_id="a", closed_at=date(2026, 1, 2), realistic_pnl=100.0),   # week 0: +100
            _trade(position_id="b", closed_at=date(2026, 1, 9), realistic_pnl=-50.0),   # week 1: -50
            _trade(position_id="c", closed_at=date(2026, 1, 16), realistic_pnl=-30.0),  # week 2: -30
            _trade(position_id="d", closed_at=date(2026, 1, 23), realistic_pnl=200.0),  # week 3: +200
        ]
        records = build_weekly_consistency(trades, period_start=period_start)
        summary = summarize_consistency(records)
        assert summary.weeks_with_trades == 4
        assert summary.profitable_week_count == 2
        assert summary.profitable_week_pct == pytest.approx(0.5)
        assert summary.longest_losing_week_streak == 2
        assert summary.worst_week_pnl == -50.0
        assert summary.best_week_pnl == 200.0


class TestCorrelationSummary:
    def test_perfectly_correlated_pair_flagged(self):
        price_history = {"AAA": [100.0, 101.0, 102.0, 103.0, 104.0], "BBB": [50.0, 50.5, 51.0, 51.5, 52.0]}
        pairs = correlation_summary(price_history, threshold=0.9)
        assert len(pairs) == 1
        assert pairs[0].correlation == pytest.approx(1.0, abs=1e-6)

    def test_uncorrelated_pair_not_flagged_at_high_threshold(self):
        rng = np.random.default_rng(1)
        price_history = {
            "AAA": list(np.cumsum(rng.normal(size=50)) + 100),
            "BBB": list(np.cumsum(rng.normal(size=50)) + 100),
        }
        pairs = correlation_summary(price_history, threshold=0.999)
        assert pairs == []


class TestCheckRuleCompliance:
    def test_no_violations_when_all_trades_approved(self):
        trades = [(_trade(position_id="a"), RiskDecision.APPROVE), (_trade(position_id="b"), RiskDecision.RESIZE)]
        summary = check_rule_compliance(trades, logged_violations=[])
        assert summary.violations_found == 0
        assert summary.compliance_pct == 1.0

    def test_trade_with_reject_decision_is_a_derived_violation(self):
        """The core rule-violation scenario Step 19 explicitly asks
        tests to cover: a trade that somehow closed despite a REJECT
        decision must be caught, not silently passed as compliant."""
        trades = [(_trade(position_id="a"), RiskDecision.REJECT)]
        summary = check_rule_compliance(trades, logged_violations=[])
        assert summary.violations_found == 1
        assert summary.compliance_pct == 0.0
        assert summary.violations[0].rule_name == "risk_engine_approval_required"

    def test_missing_decision_is_also_a_violation(self):
        trades = [(_trade(position_id="a"), None)]
        summary = check_rule_compliance(trades, logged_violations=[])
        assert summary.violations_found == 1

    def test_logged_violations_are_included_alongside_derived_ones(self):
        from datetime import datetime, timezone

        logged = [
            RuleViolationRecord(
                violation_id="v-1", occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                rule_name="manifest_drift", description="config changed mid-run",
            )
        ]
        trades = [(_trade(position_id="a"), RiskDecision.APPROVE)]
        summary = check_rule_compliance(trades, logged_violations=logged)
        assert summary.violations_found == 1
        assert summary.violations[0].rule_name == "manifest_drift"

    def test_empty_trades_is_fully_compliant(self):
        summary = check_rule_compliance([], logged_violations=[])
        assert summary.compliance_pct == 1.0
        assert summary.total_trades_checked == 0
