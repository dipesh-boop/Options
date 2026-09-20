"""Tests for REJECTED TRADE REVIEW: hypothetical-outcome pricing reused
from src.backtest.execution/assignment, and the statistical small-sample
guard that prevents "this one rejected trade would have won" from being
treated as proof."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.backtest.commissions import CommissionSchedule
from src.backtest.simulator import HistoricalOptionQuote
from src.brokers.paper import FillModel, PaperBrokerConfig
from src.data.option_chain import OptionRight as DataRight
from src.llm.schemas import Conviction, LegSide, OptionLeg, OptionRight, StrategyType, TradeDirection, TradeProposal
from src.workflows.rejected_trade_review import (
    MIN_SAMPLE_SIZE_FOR_CONCLUSIONS,
    HypotheticalOutcome,
    hypothetical_outcome_from_exit_quotes,
    hypothetical_outcome_from_settlement,
    summarize_rejected_outcomes,
)

NOW = datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc)
EXPIRATION = date(2026, 10, 16)
ZERO_FRICTION = PaperBrokerConfig(fill_model=FillModel.MID, commission_per_contract=0.0, slippage_bps=0.0, spread_capture_fraction=0.0)
ZERO_COMMISSION = CommissionSchedule(per_contract=0.0)


def _quote(strike: float, bid: float, ask: float, *, quote_date: date = date(2026, 9, 20), underlying_price: float = 100.0) -> HistoricalOptionQuote:
    return HistoricalOptionQuote(underlying="XYZ", quote_date=quote_date, expiration=EXPIRATION, strike=strike, right=DataRight.PUT, bid=bid, ask=ask, volume=500, open_interest=1000, underlying_price=underlying_price)


def _csp_proposal(**overrides) -> TradeProposal:
    base = dict(
        proposal_id="rej-1", timestamp=NOW, ticker="XYZ", strategy=StrategyType.CASH_SECURED_PUT, market_regime="normal",
        expiration=EXPIRATION, legs=[OptionLeg(right=OptionRight.PUT, strike=95.0, side=LegSide.SELL)], direction=TradeDirection.NEUTRAL,
        contracts_requested=1, target_entry=2.0, profit_target=0.5, management_dte=7, thesis="t", risk_thesis="r",
        confidence=Conviction.MEDIUM, data_sources=["mock"], data_timestamp=NOW, invalidation_conditions=["x"],
    )
    base.update(overrides)
    return TradeProposal(**base)


class TestHypotheticalOutcomeFromExitQuotes:
    def test_computes_round_trip_pnl(self):
        proposal = _csp_proposal()
        entry_quotes = [_quote(95.0, 1.95, 2.05)]
        exit_quotes = [_quote(95.0, 0.45, 0.55, quote_date=date(2026, 10, 1))]
        outcome = hypothetical_outcome_from_exit_quotes(proposal, entry_quotes, exit_quotes, rejected_stage="risk_engine", fill_config=ZERO_FRICTION, commission_schedule=ZERO_COMMISSION)
        # entry credit 2.00, exit debit 0.50 -> pnl = (2.00 - 0.50) * 100
        assert outcome.hypothetical_pnl == pytest.approx(150.0)
        assert outcome.exit_reason == "closed"
        assert outcome.rejected_stage == "risk_engine"
        assert outcome.ticker == "XYZ"

    def test_records_which_stage_rejected_it(self):
        proposal = _csp_proposal()
        entry_quotes = [_quote(95.0, 1.95, 2.05)]
        exit_quotes = [_quote(95.0, 0.45, 0.55, quote_date=date(2026, 10, 1))]
        outcome = hypothetical_outcome_from_exit_quotes(proposal, entry_quotes, exit_quotes, rejected_stage="devils_advocate", fill_config=ZERO_FRICTION, commission_schedule=ZERO_COMMISSION)
        assert outcome.rejected_stage == "devils_advocate"


class TestHypotheticalOutcomeFromSettlement:
    def test_otm_settlement_keeps_full_credit(self):
        proposal = _csp_proposal()
        entry_quotes = [_quote(95.0, 1.95, 2.05)]
        outcome = hypothetical_outcome_from_settlement(proposal, entry_quotes, 110.0, rejected_stage="risk_engine", fill_config=ZERO_FRICTION, commission_schedule=ZERO_COMMISSION)
        assert outcome.hypothetical_pnl == pytest.approx(200.0)
        assert outcome.exit_reason == "expiration_otm"

    def test_itm_settlement_is_assigned_and_reduces_pnl(self):
        proposal = _csp_proposal()
        entry_quotes = [_quote(95.0, 1.95, 2.05)]
        outcome = hypothetical_outcome_from_settlement(proposal, entry_quotes, 80.0, rejected_stage="risk_engine", fill_config=ZERO_FRICTION, commission_schedule=ZERO_COMMISSION)
        # credit 200 collected; a rejected proposal has no pre-existing
        # shares, so the newly-acquired stock is marked immediately at
        # settlement -- the loss is intrinsic value only (95 - 80 =
        # 15/share), not the full 95*100 strike notional.
        assert outcome.hypothetical_pnl == pytest.approx(200.0 - 15.0 * 100.0)
        assert outcome.exit_reason == "assignment"


class TestSummarizeRejectedOutcomes:
    def test_empty_sample(self):
        stats = summarize_rejected_outcomes([])
        assert stats.sample_size == 0
        assert stats.meaningful_sample is False
        assert stats.warning is not None

    def test_small_sample_is_flagged_not_meaningful(self):
        outcomes = [HypotheticalOutcome("p1", "XYZ", "cash_secured_put", "risk_engine", 100.0, "closed")]
        stats = summarize_rejected_outcomes(outcomes)
        assert stats.sample_size == 1
        assert stats.meaningful_sample is False
        assert stats.warning is not None
        assert "single rejected trade" in stats.warning

    def test_meaningful_sample_has_no_warning(self):
        outcomes = [HypotheticalOutcome(f"p{i}", "XYZ", "cash_secured_put", "risk_engine", 10.0 if i % 2 else -5.0, "closed") for i in range(MIN_SAMPLE_SIZE_FOR_CONCLUSIONS)]
        stats = summarize_rejected_outcomes(outcomes)
        assert stats.meaningful_sample is True
        assert stats.warning is None

    def test_hit_rate_and_stats_computed_correctly(self):
        outcomes = [
            HypotheticalOutcome("p1", "XYZ", "s", "risk_engine", 100.0, "closed"),
            HypotheticalOutcome("p2", "XYZ", "s", "risk_engine", -50.0, "closed"),
            HypotheticalOutcome("p3", "XYZ", "s", "risk_engine", 200.0, "closed"),
        ]
        stats = summarize_rejected_outcomes(outcomes, min_sample_size=2)
        assert stats.hit_rate == pytest.approx(2 / 3)
        assert stats.average_hypothetical_pnl == pytest.approx((100.0 - 50.0 + 200.0) / 3)
        assert stats.median_hypothetical_pnl == pytest.approx(100.0)
        assert stats.meaningful_sample is True

    def test_a_single_winning_rejected_trade_never_implies_it_should_have_been_taken(self):
        """The literal instruction: one winner is flagged as
        insufficient evidence, not treated as a conclusion."""
        outcomes = [HypotheticalOutcome("p1", "XYZ", "cash_secured_put", "risk_engine", 500.0, "closed")]
        stats = summarize_rejected_outcomes(outcomes)
        assert stats.hit_rate == 1.0
        assert stats.meaningful_sample is False
        assert stats.warning is not None
