"""PAPER_TRADING_V1.5.6 acceptance test: proves `scripts/run_validation_cycle.py`'s
fetch-loop branching (not just the isolated `TradierMarketDataProvider`
unit) actually takes the new DTE-windowed path for universe tickers when
the configured provider implements `DteWindowOptionChainProvider`, and
that a ticker which is simultaneously an existing position AND in the
scan universe gets BOTH its near-term lifecycle chain and its DTE-windowed
scan chain, merged via `src.data.option_chain.merge_option_chains` --
never one at the expense of the other.

Runs entirely offline against a `FakeDteWindowProvider` (never a network
call), reusing the exact `environment`/`scripts` fixture machinery already
established in `tests/acceptance/test_review_only_daily_cycle.py`.
`tests/acceptance/test_review_only_daily_cycle.py` itself already proves
the OTHER half of backward compatibility: its own `FakeMarketDataProvider`
does NOT implement `DteWindowOptionChainProvider`, and that whole file
continues to pass unmodified -- proving a provider that only implements
`MarketDataProvider` is completely unaffected by this hotfix.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

import src.data.universe as universe_module
import src.portfolio.operations_config as operations_config_module
import src.validation.protocol as validation_protocol_module
from src.data.provider import DteWindowOptionChainProvider, DteWindowSelectionDiagnostics
from src.portfolio.market_session import MarketSessionState, ValidationCycleEligibility
from src.risk.portfolio_risk import Portfolio, PortfolioPosition, PortfolioPositionLeg
from src.validation.cohort import start_new_cohort
from src.validation.records import CohortRecord
from src.validation.session import DailySnapshot, SqliteValidationStore
from tests.unit.review.conftest import EXPIRATION, make_chain

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)
COHORT_ID = "acceptance-test-cohort-dte-window"


def _load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeDteWindowProvider(DteWindowOptionChainProvider):
    """Implements BOTH `MarketDataProvider.get_option_chain` (near-term,
    used for existing-position lifecycle monitoring) AND
    `DteWindowOptionChainProvider.get_option_chain_for_dte_window`
    (DTE-windowed, used for universe opportunity scanning) -- records
    every call it receives so the test can assert which path the runner
    script actually took, for which ticker."""

    _QUOTE_BUFFER = timedelta(seconds=5)

    def __init__(self) -> None:
        self.get_option_chain_calls: list[str] = []
        self.get_option_chain_for_dte_window_calls: list[tuple[str, int, int]] = []

    async def get_option_chain(self, symbol: str):
        self.get_option_chain_calls.append(symbol)
        return make_chain(as_of=datetime.now(timezone.utc) - self._QUOTE_BUFFER)

    async def get_option_chain_for_dte_window(
        self, symbol: str, *, min_dte: int, max_dte: int, as_of: date,
        diagnostics: DteWindowSelectionDiagnostics | None = None,
    ):
        self.get_option_chain_for_dte_window_calls.append((symbol, min_dte, max_dte))
        chain = make_chain(as_of=datetime.now(timezone.utc) - self._QUOTE_BUFFER)
        if diagnostics is not None:
            diagnostics.provider_expirations_returned = 1
            diagnostics.expirations_in_window = 1
            diagnostics.expirations_selected = 1
        return chain

    async def get_underlying_quote(self, symbol: str):
        return make_chain(as_of=datetime.now(timezone.utc) - self._QUOTE_BUFFER).underlying

    async def close(self) -> None:
        return None


def test_fake_provider_satisfies_the_capability_interface():
    assert isinstance(FakeDteWindowProvider(), DteWindowOptionChainProvider)


@pytest.fixture
def with_open_position(request) -> bool:
    return getattr(request, "param", False)


@pytest.fixture
def environment(tmp_path, monkeypatch, with_open_position: bool):
    """Mirrors `tests/acceptance/test_review_only_daily_cycle.py`'s own
    `environment` fixture exactly, with one addition: when
    `with_open_position` is true, a Portfolio carrying one open SPY
    position is pre-seeded into the account-state store before the cycle
    ever runs -- so SPY is simultaneously an existing position AND (via
    `config/universe.yaml`, below) in the scan universe, exercising the
    merge branch."""
    validation_db = tmp_path / "validation.db"
    ops_db = tmp_path / "ops.db"

    store = SqliteValidationStore(validation_db)
    cohort = start_new_cohort(
        manifest_id="acceptance-manifest", start_date=date(2026, 9, 22), duration_days=90,
        frozen_at=NOW, starting_nav=100_000.0, strategy_versions={"cash_secured_put": "v1"},
        store=store, cohort_label=COHORT_ID,
    )
    store.record_cohort(
        CohortRecord(cohort_id=COHORT_ID, cohort_name="ACCEPTANCE_TEST", status="active", created_at=NOW, manifest=cohort.manifest, started_at=NOW)
    )
    store.record_snapshot(
        DailySnapshot(snapshot_date=date(2026, 9, 22), nav=100_000.0, cash=100_000.0, capital_deployed_pct=0.0, open_position_count=0, drawdown_pct=0.0, per_strategy_nav={}, recorded_at=NOW),
        cohort_id=COHORT_ID,
    )

    validation_yaml = tmp_path / "validation.yaml"
    validation_yaml.write_text(
        yaml.safe_dump(
            {
                "validation_period": {"duration_days": 90, "checkpoint_days": [30, 60]},
                "sample_size": {"minimum_completed_trades": 50, "preferred_completed_trades": 100},
                "starting_capital": {"default_nav": 100_000.0},
                "research_targets": {
                    "annual_return_low_pct": 0.12, "annual_return_high_pct": 0.15,
                    "reference_min_sharpe": 1.0, "reference_max_acceptable_drawdown_pct": 0.15,
                },
                "statistics": {
                    "bootstrap_iterations": 100, "bootstrap_confidence_pct": 0.90,
                    "monte_carlo_iterations": 100, "var_confidence_pct": 0.95, "random_seed": 1,
                },
                "decision_quality": {"min_probability_of_profit": 0.50, "rejected_trade_min_sample_size": 20},
                "alerts": {"consecutive_loss_alert_count": 5, "weekly_loss_alert_pct": 0.05},
                "storage": {"db_path": str(validation_db)},
            }
        )
    )
    universe_yaml = tmp_path / "universe.yaml"
    universe_yaml.write_text(yaml.safe_dump({"tickers": [{"ticker": "SPY", "sector": "ETF"}], "strategies": ["CASH_SECURED_PUT"]}))
    operations_yaml = tmp_path / "operations.yaml"
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
                    "enabled": False, "min_correlation_observations": 20, "correlation_lookback_days": 60,
                },
            }
        )
    )

    monkeypatch.setattr(validation_protocol_module, "DEFAULT_CONFIG_PATH", validation_yaml)
    monkeypatch.setattr(universe_module, "DEFAULT_CONFIG_PATH", universe_yaml)
    monkeypatch.setattr(operations_config_module, "DEFAULT_CONFIG_PATH", operations_yaml)

    if with_open_position:
        from src.portfolio.account_state import SqlitePortfolioStore

        portfolio = Portfolio(
            as_of=NOW, nav=100_000.0, cash=95_000.0, peak_equity=100_000.0,
            positions=[
                PortfolioPosition(
                    position_id="pos-spy-1", ticker="SPY", sector="ETF", strategy="cash_secured_put",
                    expiration=EXPIRATION, legs=[PortfolioPositionLeg(right="P", side="sell", strike=5.0, entry_price=0.5)],
                    contracts=1, capital_at_risk=500.0, max_loss=500.0, opened_at=NOW,
                )
            ],
        )
        SqlitePortfolioStore(ops_db).save(COHORT_ID, portfolio)

    return tmp_path


@pytest.fixture
def scripts(environment, monkeypatch):
    monkeypatch.setenv("OPTIONS_AGENT_DATA_PROVIDER", "tradier")
    monkeypatch.setenv("OPTIONS_AGENT_TRADIER_TOKEN", "fake-token-for-tests-only")
    monkeypatch.delenv("OPTIONS_AGENT_TRADIER_BASE_URL", raising=False)

    cycle = _load_script_module("_acceptance_dte_window_run_validation_cycle", REPO_ROOT / "scripts" / "run_validation_cycle.py")
    provider = FakeDteWindowProvider()
    monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
    monkeypatch.setattr(
        cycle,
        "evaluate_validation_cycle_eligibility",
        lambda now, **kw: ValidationCycleEligibility(
            as_of=now, market_session_state=MarketSessionState.REGULAR_MARKET, is_trading_day=True,
            regular_session_open=None, regular_session_close=None,
            validation_cycle_allowed=True, block_reason=None,
        ),
    )
    return cycle, provider


@pytest.mark.asyncio
class TestDteWindowBranchTakenForUniverseTickers:
    @pytest.mark.parametrize("with_open_position", [False], indirect=True)
    async def test_universe_only_ticker_uses_dte_windowed_fetch_not_nearest_n(self, scripts, with_open_position):
        cycle, provider = scripts
        ok = await cycle.run_validation_cycle()
        assert ok is True

        # SPY is in the universe but has no open position this run -- the
        # DTE-windowed capability must be used, exclusively, for it.
        assert provider.get_option_chain_for_dte_window_calls == [("SPY", 20, 45)]
        assert provider.get_option_chain_calls == []

    @pytest.mark.parametrize("with_open_position", [True], indirect=True)
    async def test_ticker_both_position_and_universe_gets_both_fetches_merged(self, scripts, with_open_position):
        cycle, provider = scripts
        ok = await cycle.run_validation_cycle()
        assert ok is True

        # SPY is simultaneously an existing position AND in the universe --
        # both the near-term lifecycle fetch and the DTE-windowed scan
        # fetch must have been made, never only one.
        assert provider.get_option_chain_calls == ["SPY"]
        # PAPER_TRADING_V1.5.10: `_fetch_existing_position_chain` ALSO
        # requests the position's own actual held expiration exactly
        # (min_dte=max_dte=that position's real DTE) -- a second,
        # intentional call alongside the universe scan's own [20, 45]
        # window, never a replacement for it. The position's DTE is
        # computed dynamically here (never hardcoded) because `EXPIRATION`
        # (tests/unit/review/conftest.py) is a fixed calendar date whose
        # DTE relative to the real wall clock drifts over time -- this
        # assertion must stay correct however many days that drift has
        # reached when the suite happens to run.
        position_dte = (EXPIRATION - datetime.now(timezone.utc).date()).days
        assert provider.get_option_chain_for_dte_window_calls == [
            ("SPY", 20, 45), ("SPY", position_dte, position_dte),
        ]

        # And the merge succeeded well enough for the cycle to evaluate
        # the existing position through the (unmodified) Lifecycle Engine.
        assert ok is True
