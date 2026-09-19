"""Shared fixtures for the Strategy Research test suite."""
from __future__ import annotations

from datetime import date, timedelta

from src.backtest.simulator import TradeRecord
from src.llm.schemas import StrategyType
from src.research.performance_breakdown import ResearchTradeObservation, TradeContext


def make_trade(pnl: float = 50.0, *, day_offset: int = 0, holding_days: int = 10, ticker: str = "XYZ", strategy: StrategyType = StrategyType.PUT_CREDIT_SPREAD) -> TradeRecord:
    opened = date(2024, 1, 1) + timedelta(days=day_offset)
    return TradeRecord(
        position_id=f"bt-{day_offset}",
        ticker=ticker,
        strategy=strategy,
        contracts=1,
        opened_at=opened,
        closed_at=opened + timedelta(days=holding_days),
        close_reason="profit_target",
        capital_at_risk=500.0,
        entry_spread_pct=0.05,
        realistic_entry_credit=100.0,
        realistic_exit_debit=-(100.0 - pnl),
        theoretical_entry_credit=100.0,
        theoretical_exit_debit=-(100.0 - pnl),
        commission_paid=1.30,
        realistic_pnl=pnl,
        theoretical_pnl=pnl,
    )


def make_context(**overrides) -> TradeContext:
    base = dict(entry_delta=-0.18, entry_dte=30, iv_percentile=80.0, market_regime="high_iv", sector="tech", profit_target_pct=0.5, management_dte=7)
    base.update(overrides)
    return TradeContext(**base)


def make_observation(pnl: float = 50.0, *, day_offset: int = 0, **context_overrides) -> ResearchTradeObservation:
    return ResearchTradeObservation(trade=make_trade(pnl, day_offset=day_offset), context=make_context(**context_overrides))
