"""Tests for FIDELITY EXECUTION QUALITY: slippage record construction
from a real (FidelityTradeTicket, ExecutionConfirmation) pair, and the
average/median/by-strategy/by-underlying/by-time-of-day summary."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from src.brokers.fidelity import ExecutionConfirmation, FidelityLegAction, FidelityOrderLeg, FidelityTradeTicket
from src.data.option_chain import OptionRight
from src.workflows.execution_quality import build_slippage_record, summarize_slippage

NOW = datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc)


def _ticket(**overrides) -> FidelityTradeTicket:
    base = dict(
        trade_id="t1", account_alias="OPTIONS_ACCOUNT", ticker="XYZ", strategy="PUT CREDIT SPREAD",
        underlying_price=100.0, expiration=date(2026, 10, 16),
        legs=[
            FidelityOrderLeg(action=FidelityLegAction.SELL_TO_OPEN, put_call=OptionRight.PUT, strike=95.0, expiration=date(2026, 10, 16), contracts=1),
            FidelityOrderLeg(action=FidelityLegAction.BUY_TO_OPEN, put_call=OptionRight.PUT, strike=90.0, expiration=date(2026, 10, 16), contracts=1),
        ],
        quantity=1, limit_price=1.0, minimum_acceptable_price=0.9, estimated_credit_debit=1.0, net_bid=0.95, net_ask=1.05,
        max_profit=100.0, max_loss=400.0, breakeven=94.0, capital_at_risk=400.0, return_on_capital=0.25,
        profit_target=0.5, loss_management_rule="close at 2x credit", DTE_management_rule="manage at 21 DTE", management_dte=21,
        market_data_timestamp=NOW, risk_approval_id="risk-1", status_updated_at=NOW, timestamp=NOW, source="fidelity_manual",
    )
    base.update(overrides)
    return FidelityTradeTicket(**base)


def _confirmation(**overrides) -> ExecutionConfirmation:
    base = dict(confirmed_by="dipesh", confirmation_source="human_manual_entry", filled_quantity=1, fill_price=0.95, confirmed_at=NOW + timedelta(minutes=3))
    base.update(overrides)
    return ExecutionConfirmation(**base)


class TestBuildSlippageRecord:
    def test_computes_slippage_and_delay(self):
        record = build_slippage_record(_ticket(), _confirmation())
        assert record.recommended_limit == pytest.approx(1.0)
        assert record.market_midpoint_at_recommendation == pytest.approx(1.0)
        assert record.actual_fill_price == pytest.approx(0.95)
        assert record.slippage == pytest.approx(0.05)
        assert record.delay_seconds == pytest.approx(180.0)

    def test_recommended_limit_is_always_a_positive_magnitude(self):
        """`FidelityTradeTicket.limit_price` is itself constrained to be
        positive (Field(gt=0)) -- this just confirms the record carries
        that value through unchanged, never re-signed."""
        record = build_slippage_record(_ticket(limit_price=1.25), _confirmation(fill_price=1.1))
        assert record.recommended_limit == pytest.approx(1.25)
        assert record.slippage == pytest.approx(1.25 - 1.1)

    def test_confirmation_before_recommendation_rejected(self):
        with pytest.raises(ValueError):
            build_slippage_record(_ticket(), _confirmation(confirmed_at=NOW - timedelta(minutes=1)))

    def test_time_of_day_bucketing(self):
        early_ticket = _ticket(timestamp=NOW.replace(hour=6), market_data_timestamp=NOW.replace(hour=6), status_updated_at=NOW.replace(hour=6))
        assert build_slippage_record(early_ticket, _confirmation(confirmed_at=NOW.replace(hour=8))).time_of_day == "pre_market"
        assert build_slippage_record(early_ticket, _confirmation(confirmed_at=NOW.replace(hour=10))).time_of_day == "morning"
        assert build_slippage_record(early_ticket, _confirmation(confirmed_at=NOW.replace(hour=12))).time_of_day == "midday"
        assert build_slippage_record(early_ticket, _confirmation(confirmed_at=NOW.replace(hour=15))).time_of_day == "afternoon"
        assert build_slippage_record(early_ticket, _confirmation(confirmed_at=NOW.replace(hour=17))).time_of_day == "after_hours"


class TestSummarizeSlippage:
    def test_empty_records(self):
        summary = summarize_slippage([])
        assert summary.count == 0
        assert summary.average_slippage == 0.0

    def test_average_and_median(self):
        records = [
            build_slippage_record(_ticket(), _confirmation(fill_price=0.90)),  # slippage 0.10
            build_slippage_record(_ticket(), _confirmation(fill_price=0.95)),  # slippage 0.05
            build_slippage_record(_ticket(), _confirmation(fill_price=1.00)),  # slippage 0.00
        ]
        summary = summarize_slippage(records)
        assert summary.count == 3
        assert summary.average_slippage == pytest.approx(0.05)
        assert summary.median_slippage == pytest.approx(0.05)

    def test_grouped_by_strategy_and_underlying(self):
        r1 = build_slippage_record(_ticket(ticker="AAA", strategy="cash_secured_put"), _confirmation(fill_price=0.90))
        r2 = build_slippage_record(_ticket(ticker="BBB", strategy="covered_call"), _confirmation(fill_price=1.00))
        summary = summarize_slippage([r1, r2])
        assert summary.by_strategy == {"cash_secured_put": pytest.approx(0.10), "covered_call": pytest.approx(0.0)}
        assert summary.by_underlying == {"AAA": pytest.approx(0.10), "BBB": pytest.approx(0.0)}

    def test_grouped_by_time_of_day(self):
        early_ticket = _ticket(timestamp=NOW.replace(hour=6), market_data_timestamp=NOW.replace(hour=6), status_updated_at=NOW.replace(hour=6))
        r1 = build_slippage_record(early_ticket, _confirmation(confirmed_at=NOW.replace(hour=10), fill_price=0.90))
        r2 = build_slippage_record(early_ticket, _confirmation(confirmed_at=NOW.replace(hour=15), fill_price=1.00))
        summary = summarize_slippage([r1, r2])
        assert summary.by_time_of_day == {"morning": pytest.approx(0.10), "afternoon": pytest.approx(0.0)}
