"""End-to-end tests for build_weekly_review_report/render_weekly_review_report,
the research hand-off, and the structural "cannot modify production
strategy" guarantee."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.backtest.simulator import TradeRecord
from src.data.earnings import EarningsEvent
from src.llm.schemas import StrategyType
from src.research.hypothesis import Hypothesis, HypothesisRegistry
from src.research.performance_breakdown import ResearchTradeObservation, TradeContext
from src.risk.limits import get_default_limits
from src.risk.portfolio_risk import Portfolio, PortfolioPosition, PortfolioPositionLeg
from src.risk.reason_codes import RiskDecision
from src.workflows import weekly_review
from src.workflows.decision_quality import DecisionQualityInputs
from src.workflows.execution_quality import FidelitySlippageRecord
from src.workflows.rejected_trade_review import HypotheticalOutcome
from src.workflows.weekly_review import WeeklyReviewInputs, build_weekly_review_report, render_weekly_review_report

NOW = datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc)


def _trade(pnl: float = 58.7, **overrides) -> TradeRecord:
    base = dict(
        position_id="p", ticker="XYZ", strategy=StrategyType.PUT_CREDIT_SPREAD, contracts=1,
        opened_at=date(2026, 9, 1), closed_at=date(2026, 9, 15), close_reason="profit_target", capital_at_risk=500.0,
        entry_spread_pct=0.05, realistic_entry_credit=100.0, realistic_exit_debit=-(100.0 - pnl),
        theoretical_entry_credit=100.0, theoretical_exit_debit=-(100.0 - pnl), commission_paid=1.3,
        realistic_pnl=pnl, theoretical_pnl=pnl,
    )
    base.update(overrides)
    return TradeRecord(**base)


def _portfolio(**overrides) -> Portfolio:
    base = dict(
        as_of=NOW, nav=109_500.0, cash=90_000.0, peak_equity=110_000.0, sector_by_ticker={"XYZ": "TECH"},
        positions=[
            PortfolioPosition(
                position_id="pp1", ticker="XYZ", sector="TECH", strategy=StrategyType.CASH_SECURED_PUT,
                expiration=date(2026, 9, 25), legs=[PortfolioPositionLeg(right="P", side="sell", strike=95.0, entry_price=2.0)],
                contracts=1, capital_at_risk=9500.0, max_loss=9500.0, opened_at=NOW,
            )
        ],
    )
    base.update(overrides)
    return Portfolio(**base)


def _base_inputs(**overrides) -> WeeklyReviewInputs:
    trade = _trade()
    obs = [ResearchTradeObservation(trade=trade, context=TradeContext(entry_delta=-0.2, entry_dte=25, iv_percentile=70.0, market_regime="normal", sector="TECH", profit_target_pct=0.5, management_dte=7))]
    dq_inputs = [DecisionQualityInputs(risk_decision=RiskDecision.APPROVE, devils_advocate_verdict="PASS", probability_of_profit=0.7, realistic_pnl=trade.realistic_pnl)]
    base = dict(
        now=NOW,
        equity_curve=[(date(2026, 1, 1), 100_000.0), (date(2026, 9, 13), 108_000.0), (date(2026, 9, 20), 109_500.0)],
        week_start=date(2026, 9, 13), month_start=date(2026, 9, 1), year_start=date(2026, 1, 1), risk_free_annual_rate=0.04,
        portfolio=_portfolio(), trades=[trade], trade_observations=obs, decision_quality_inputs=dq_inputs,
        rejected_outcomes=[], slippage_records=[], committee_hypotheses=[], hypothesis_registry=HypothesisRegistry(),
        limits=get_default_limits(),
    )
    base.update(overrides)
    return WeeklyReviewInputs(**base)


class TestEndToEndReport:
    def test_all_sections_populate(self):
        report = build_weekly_review_report(_base_inputs())
        assert report.performance.starting_nav == 100_000.0
        assert report.trade_count == 1
        assert len(report.breakdowns) == 8
        assert report.decision_quality_summary.total == 1

    def test_render_includes_every_named_section_header(self):
        text = render_weekly_review_report(build_weekly_review_report(_base_inputs()))
        for header in (
            "PORTFOLIO PERFORMANCE", "RISK", "TRADE STATISTICS", "BREAK DOWN PERFORMANCE",
            "DECISION QUALITY", "REJECTED TRADE REVIEW", "FIDELITY EXECUTION QUALITY", "RESEARCH", "NEXT WEEK",
        ):
            assert header in text

    def test_no_trades_period_renders_honestly(self):
        report = build_weekly_review_report(_base_inputs(trades=[], trade_observations=[], decision_quality_inputs=[]))
        text = render_weekly_review_report(report)
        assert "(no closed trades this period)" in text
        assert report.trade_count == 0


class TestDecisionQualitySection:
    def test_good_decision_bad_outcome_reported_distinctly(self):
        trade = _trade(pnl=-40.0)
        dq_inputs = [DecisionQualityInputs(risk_decision=RiskDecision.APPROVE, devils_advocate_verdict="PASS", probability_of_profit=0.7, realistic_pnl=-40.0)]
        report = build_weekly_review_report(_base_inputs(trades=[trade], decision_quality_inputs=dq_inputs))
        assert report.decision_quality[0].label == "good_decision_bad_outcome"
        text = render_weekly_review_report(report)
        assert "GOOD DECISION / BAD OUTCOME: 1" in text


class TestRejectedTradeReviewSection:
    def test_small_sample_warning_rendered(self):
        outcomes = [HypotheticalOutcome("r1", "AAA", "cash_secured_put", "risk_engine", 500.0, "closed")]
        report = build_weekly_review_report(_base_inputs(rejected_outcomes=outcomes))
        text = render_weekly_review_report(report)
        assert "WARNING" in text
        assert "single rejected trade" in text


class TestExecutionQualitySection:
    def test_slippage_summary_rendered(self):
        records = [FidelitySlippageRecord("s1", "XYZ", "PUT CREDIT SPREAD", 1.0, 1.0, 0.95, NOW, NOW + timedelta(minutes=3), 0.05, 180.0, "afternoon")]
        report = build_weekly_review_report(_base_inputs(slippage_records=records))
        text = render_weekly_review_report(report)
        assert "Confirmed fills: 1" in text
        assert "0.0500" in text

    def test_no_confirmed_fills_renders_honestly(self):
        report = build_weekly_review_report(_base_inputs())
        text = render_weekly_review_report(report)
        assert "(no confirmed Fidelity fills this period)" in text


class TestResearchHandOff:
    def test_hypothesis_registered_into_the_shared_registry(self):
        registry = HypothesisRegistry()
        hyp = Hypothesis(hypothesis_id="h1", statement="Test hypothesis", dimensions=("delta",), parameter_family_key="fam1", created_at=NOW)
        report = build_weekly_review_report(_base_inputs(committee_hypotheses=[hyp], hypothesis_registry=registry))
        assert len(report.registered_hypotheses) == 1
        # actually landed in the registry the Strategy Research Agent reads from, not a private copy
        assert registry.get("h1").hypothesis.statement == "Test hypothesis"

    def test_no_hypotheses_renders_honestly(self):
        report = build_weekly_review_report(_base_inputs())
        text = render_weekly_review_report(report)
        assert "(no new hypotheses identified this week)" in text


class TestNextWeekSection:
    def test_expiration_within_seven_days_flagged(self):
        report = build_weekly_review_report(_base_inputs())
        assert any("expires in" in r for r in report.next_week.expiration_risks)
        assert any("expiration within" in a for a in report.next_week.positions_requiring_attention)

    def test_earnings_within_window_flagged(self):
        event = EarningsEvent(symbol="XYZ", earnings_date=date(2026, 9, 24), timing="after_market", confirmed=True, timestamp=NOW, source="mock")
        report = build_weekly_review_report(_base_inputs(upcoming_earnings={"XYZ": event}))
        assert any("earnings" in r for r in report.next_week.earnings_risks)

    def test_earnings_outside_window_not_flagged(self):
        event = EarningsEvent(symbol="XYZ", earnings_date=date(2026, 3, 1), timing="after_market", confirmed=True, timestamp=NOW, source="mock")
        report = build_weekly_review_report(_base_inputs(upcoming_earnings={"XYZ": event}))
        assert report.next_week.earnings_risks == ()

    def test_drawdown_risk_reduction_zone_flagged(self):
        portfolio = _portfolio(nav=100_000.0, peak_equity=112_000.0)  # drawdown ~10.7%, in risk-reduction zone
        report = build_weekly_review_report(_base_inputs(portfolio=portfolio))
        assert any("drawdown" in r for r in report.next_week.portfolio_risks)

    def test_no_limits_supplied_skips_portfolio_risk_checks_without_erroring(self):
        report = build_weekly_review_report(_base_inputs(limits=None))
        assert report.next_week.portfolio_risks == ()

    def test_known_economic_events_pass_through(self):
        report = build_weekly_review_report(_base_inputs(known_economic_events=("FOMC Wednesday",)))
        assert report.next_week.known_economic_events == ("FOMC Wednesday",)


class TestCannotModifyProductionStrategy:
    """Structural proof: this module never imports src.research.promotion
    and has no way to call promote_strategy."""

    WEEKLY_REVIEW_SOURCE = Path(weekly_review.__file__).read_text(encoding="utf-8")

    def test_module_does_not_import_promotion(self):
        pattern = re.compile(r"^\s*(from\s+src\.research\.promotion|import\s+src\.research\.promotion)\b", re.MULTILINE)
        assert pattern.search(self.WEEKLY_REVIEW_SOURCE) is None

    def test_module_does_not_call_promote_strategy(self):
        assert "promote_strategy(" not in self.WEEKLY_REVIEW_SOURCE

    def test_module_performs_no_filesystem_write(self):
        forbidden_patterns = [r'open\([^)]*["\']w', r'open\([^)]*["\']a', r"\.write_text\(", r"\.write_bytes\(", r"yaml\.dump\(", r"yaml\.safe_dump\("]
        for pattern in forbidden_patterns:
            assert re.search(pattern, self.WEEKLY_REVIEW_SOURCE) is None, f"matched forbidden pattern {pattern!r}"

    def test_promotion_module_not_a_live_dependency(self):
        for name, value in vars(weekly_review).items():
            assert "promotion" not in repr(getattr(value, "__module__", ""))
