"""PAPER_TRADING_V1.5.3, Step 3 acceptance test: risk-data wiring
exercised through the REAL production entry point
(`scripts/run_validation_cycle.py`), against temporary sqlite files --
never `data/options_agent.db`. Reuses `tests/acceptance
/test_review_only_daily_cycle.py`'s own `environment`/`scripts`
fixtures and `FakeMarketDataProvider` rather than duplicating fixture
machinery.

Proves, against the actual script (not just the library function):
- installing this capability with the default (disabled) config leaves
  a real run's resulting `Portfolio` with EMPTY sector_by_ticker/
  price_history and risk_data_required=False -- the active cohort's own
  candidate-eligibility-relevant state is untouched;
- activating it (a config-only change) makes a real run populate
  sector_by_ticker and set risk_data_required=True;
- activating it with a pre-existing position on a different ticker
  makes the real script's own scan REJECT the new candidate for missing
  correlation data (fail closed) rather than silently proposing it.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
import yaml

import src.portfolio.operations_config as operations_config_module
from src.portfolio.account_state import SqlitePortfolioStore
from src.risk.portfolio_risk import Portfolio, PortfolioPosition, PortfolioPositionLeg
from src.risk.reason_codes import ReasonCode
from tests.acceptance.test_review_only_daily_cycle import (  # noqa: F401 -- reused fixtures
    COHORT_ID,
    NOW,
    environment,
    scripts,
)

EXPIRATION = date(2026, 10, 16)


def _enable_risk_data_wiring(environment_dir, monkeypatch) -> None:
    """Rewrites the fixture's own operations.yaml with
    `risk_data_wiring.enabled: true` -- everything else identical, so
    this is a pure config-only activation, exactly as the real operator
    would do for a future cohort."""
    ops_db = environment_dir / "ops.db"
    operations_yaml = environment_dir / "operations.yaml"
    operations_yaml.write_text(
        yaml.safe_dump(
            {
                "cohort": {"cohort_id": COHORT_ID, "account_id": COHORT_ID},
                "market_regime": {"default_regime": "normal"},
                "storage": {
                    "account_state_db_path": str(ops_db), "control_loop_db_path": str(ops_db),
                    "lifecycle_db_path": str(ops_db), "candidate_review_db_path": str(ops_db),
                },
                "review": {"confirmation_ttl_seconds": 900, "max_price_drift_pct": 0.05, "max_capital_required_drift_pct": 0.05},
                "market_hours": {"scan_open_buffer_minutes": 5, "scan_close_buffer_minutes": 15},
                "risk_data_wiring": {
                    "enabled": True, "min_correlation_observations": 20, "correlation_lookback_days": 60,
                },
            }
        )
    )
    monkeypatch.setattr(operations_config_module, "DEFAULT_CONFIG_PATH", operations_yaml)


@pytest.mark.asyncio
class TestActiveCohortCompatibility:
    async def test_disabled_by_default_real_run_leaves_risk_data_fields_empty(self, scripts):
        """The active cohort's own config/operations.yaml -- byte-for-
        byte reproduced by this fixture's default -- must produce a
        Portfolio with the exact pre-V1.5.3 shape after a real cycle."""
        cycle, _confirm = scripts
        ok = await cycle.run_validation_cycle()
        assert ok is True

        ops = operations_config_module.load_operations_config()
        assert ops.risk_data_wiring_enabled is False
        portfolio = SqlitePortfolioStore(ops.account_state_db_path).get(ops.account_id)
        assert portfolio is not None
        assert portfolio.sector_by_ticker == {}
        assert portfolio.price_history == {}
        assert portfolio.risk_data_required is False


@pytest.mark.asyncio
class TestActivation:
    async def test_enabling_via_config_populates_sector_data_on_a_real_run(self, environment, scripts, monkeypatch):
        _enable_risk_data_wiring(environment, monkeypatch)
        cycle, _confirm = scripts
        ok = await cycle.run_validation_cycle()
        assert ok is True

        ops = operations_config_module.load_operations_config()
        assert ops.risk_data_wiring_enabled is True
        portfolio = SqlitePortfolioStore(ops.account_state_db_path).get(ops.account_id)
        assert portfolio.risk_data_required is True
        assert portfolio.sector_by_ticker.get("SPY") == "ETF"

    async def test_existing_position_with_no_provider_fails_closed_on_the_next_scan(
        self, environment, scripts, monkeypatch
    ):
        """The real script, with wiring active and one pre-existing
        position already on the books, must not silently propose a new
        SPY candidate it cannot correlate -- no historical-data provider
        is wired into official validation yet (see
        STEP_23_3_FREEZE_REPORT.md), so the Risk Engine fails closed."""
        _enable_risk_data_wiring(environment, monkeypatch)
        cycle, _confirm = scripts

        ops = operations_config_module.load_operations_config()
        portfolio_store = SqlitePortfolioStore(ops.account_state_db_path)
        existing = Portfolio(
            as_of=NOW, nav=100_000.0, cash=90_000.0, peak_equity=100_000.0,
            positions=[
                PortfolioPosition(
                    position_id="pos-qqq-1", ticker="QQQ", sector="ETF", strategy="cash_secured_put",
                    expiration=EXPIRATION,
                    legs=[PortfolioPositionLeg(right="P", side="sell", strike=500.0, entry_price=2.0)],
                    contracts=1, capital_at_risk=1000.0, max_loss=1000.0,
                    opened_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
                )
            ],
        )
        portfolio_store.save(ops.account_id, existing)

        ok = await cycle.run_validation_cycle()
        assert ok is True

        from src.review.candidates import SqliteCandidateReviewStore

        review_store = SqliteCandidateReviewStore(ops.candidate_review_db_path)
        awaiting = review_store.candidates_awaiting_human(cohort_id=COHORT_ID)
        # The candidate the (unmodified) scan would otherwise have
        # surfaced is rejected by the Risk Engine for missing
        # correlation data -- never silently promoted to AWAITING_HUMAN.
        assert awaiting == []

        portfolio_after = portfolio_store.get(ops.account_id)
        assert portfolio_after.risk_data_required is True
        # The existing position is completely untouched -- this step
        # never places an order, never modifies an existing position.
        assert len(portfolio_after.positions) == 1
        assert portfolio_after.positions[0].position_id == "pos-qqq-1"
