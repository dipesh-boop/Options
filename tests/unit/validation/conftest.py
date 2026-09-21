from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.backtest.simulator import TradeRecord
from src.llm.schemas import StrategyType
from src.risk.limits import RiskLimitsConfig
from src.validation.protocol import ValidationConfig


def _trade(**overrides) -> TradeRecord:
    defaults = dict(
        position_id="val-1",
        ticker="XYZ",
        strategy=StrategyType.CASH_SECURED_PUT,
        contracts=1,
        opened_at=date(2026, 1, 2),
        closed_at=date(2026, 1, 16),
        close_reason="profit_target",
        capital_at_risk=9500.0,
        entry_spread_pct=0.05,
        realistic_entry_credit=200.0,
        realistic_exit_debit=-100.0,
        theoretical_entry_credit=200.0,
        theoretical_exit_debit=-100.0,
        commission_paid=1.30,
        realistic_pnl=98.70,
        theoretical_pnl=100.0,
    )
    defaults.update(overrides)
    return TradeRecord(**defaults)


def _limits(**overrides) -> RiskLimitsConfig:
    defaults = dict(
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
        max_bid_ask_spread_pct=0.10,
        high_correlation_threshold=0.7,
        max_market_data_age_minutes=15.0,
        risk_free_rate=0.04,
        quant_cross_check_tolerance_pct=0.05,
        max_stress_loss_pct_of_nav=0.30,
    )
    defaults.update(overrides)
    return RiskLimitsConfig(**defaults)


def _validation_config(**overrides) -> ValidationConfig:
    defaults = dict(
        duration_days=90,
        checkpoint_days=(30, 60),
        minimum_completed_trades=50,
        preferred_completed_trades=100,
        default_starting_nav=100_000.0,
        research_target_annual_return_low_pct=0.12,
        research_target_annual_return_high_pct=0.15,
        research_reference_min_sharpe=1.0,
        research_reference_max_acceptable_drawdown_pct=0.15,
        bootstrap_iterations=200,
        bootstrap_confidence_pct=0.90,
        monte_carlo_iterations=200,
        var_confidence_pct=0.95,
        random_seed=42,
        min_probability_of_profit=0.50,
        rejected_trade_min_sample_size=20,
        consecutive_loss_alert_count=5,
        weekly_loss_alert_pct=0.05,
        db_path="data/options_agent_test.db",
    )
    defaults.update(overrides)
    return ValidationConfig(**defaults)


NOW = datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def limits() -> RiskLimitsConfig:
    return _limits()


@pytest.fixture
def validation_config() -> ValidationConfig:
    return _validation_config()
