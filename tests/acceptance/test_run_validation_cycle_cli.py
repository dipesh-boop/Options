"""Step 22.6 (PAPER_TRADING_V1.4.5) acceptance test: the operator CLI and
provider-preflight remediation to `scripts/run_validation_cycle.py`.

Two defects motivated this step, both discovered during local
pre-production acceptance of V1.4.4:

1. The script had no CLI argument parser at all -- `python
   scripts/run_validation_cycle.py --help` silently ignored `--help` and
   ran an actual, state-mutating validation cycle. `TestHelpSafety` and
   `TestUnknownArgumentSafety` prove this is fixed: `--help` exits 0 and
   mutates nothing; an unrecognized argument exits non-zero and mutates
   nothing.
2. The official runner accepted whatever `OPTIONS_AGENT_DATA_PROVIDER`
   happened to be configured (including `mock`) with no check that it
   was Tradier's own production market-data endpoint before mutating
   official validation state. `TestMockProviderRejection` proves a
   `mock`-configured (or otherwise non-Tradier-production-configured)
   environment is refused, before a single line of state is touched;
   `TestTradierProductionProviderPasses` proves a correctly-configured
   environment reaches the exact same behavior V1.4.4's own acceptance
   test (`test_review_only_daily_cycle.py`) already proves.

Runs entirely offline against temporary sqlite files, using
`FakeMarketDataProvider` (never a live Tradier token or a real network
call) -- imported from `test_review_only_daily_cycle`, which already
establishes the exact fixture pattern this file reuses (a seeded,
already-started cohort; temporary `validation.yaml`/`universe.yaml`/
`operations.yaml`; the real script loaded via `importlib`, never just
its library functions).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import src.portfolio.operations_config as operations_config_module
from src.data.factory import OfficialProviderPreflightError, verify_official_provider_is_tradier_production
from tests.acceptance.test_review_only_daily_cycle import (
    COHORT_ID,
    FakeMarketDataProvider,
    _load_script_module,
    environment,  # noqa: F401 -- reused as a pytest fixture
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "run_validation_cycle.py"


# ------------------------------------------------------------- --help / bad args


class TestHelpSafety:
    """`--help` must never run a cycle -- it must not even get as far as
    reading `config/operations.yaml` or opening a database connection.
    Run as a real subprocess (not an in-process import) so this is a
    genuine proof of what an operator typing this command actually gets,
    not just what the loaded module's functions do when called directly."""

    def test_help_exits_zero_and_shows_usage(self, tmp_path, monkeypatch):
        # A directory with no data/ of its own -- if --help touched
        # anything, this is exactly where it would show up.
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--help"],
            cwd=tmp_path, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        assert "usage:" in result.stdout.lower()
        assert "--preflight" in result.stdout

    def test_help_creates_no_database_file(self, tmp_path):
        assert list(tmp_path.glob("**/*.db")) == []
        subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--help"],
            cwd=tmp_path, capture_output=True, text=True, timeout=30,
        )
        assert list(tmp_path.glob("**/*.db")) == [], "python scripts/run_validation_cycle.py --help must create no database file"

    def test_help_does_not_invoke_run_validation_cycle(self, monkeypatch):
        """In-process proof, complementing the subprocess proof above:
        `run_validation_cycle`/`run_preflight` are never even called when
        argparse handles `--help` -- `main()` never reaches past
        `parser.parse_args()`."""
        cycle = _load_script_module("_cli_test_help_module", SCRIPT_PATH)
        called = []
        monkeypatch.setattr(cycle, "run_validation_cycle", lambda: called.append("cycle") or True)
        monkeypatch.setattr(cycle, "run_preflight", lambda: called.append("preflight") or True)
        monkeypatch.setattr(sys, "argv", ["run_validation_cycle.py", "--help"])

        with pytest.raises(SystemExit) as exc_info:
            cycle.main()
        assert exc_info.value.code == 0
        assert called == []


class TestUnknownArgumentSafety:
    def test_unrecognized_argument_exits_nonzero_and_does_not_run(self, tmp_path):
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--this-flag-does-not-exist"],
            cwd=tmp_path, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode != 0
        assert list(tmp_path.glob("**/*.db")) == []

    def test_unrecognized_argument_never_calls_run_validation_cycle(self, monkeypatch):
        cycle = _load_script_module("_cli_test_badarg_module", SCRIPT_PATH)
        called = []
        monkeypatch.setattr(cycle, "run_validation_cycle", lambda: called.append("cycle") or True)
        monkeypatch.setattr(sys, "argv", ["run_validation_cycle.py", "--bogus"])

        with pytest.raises(SystemExit) as exc_info:
            cycle.main()
        assert exc_info.value.code != 0
        assert called == []


# ------------------------------------------------------------- provider preflight


class TestOfficialProviderPreflightUnit:
    """Direct unit coverage of `verify_official_provider_is_tradier_production`
    -- the function both the mutating cycle and `--preflight` call."""

    def test_mock_rejected(self):
        from src.data.factory import DataProviderSelection

        with pytest.raises(OfficialProviderPreflightError, match="tradier"):
            verify_official_provider_is_tradier_production(DataProviderSelection(data_provider="mock"))

    def test_alpaca_rejected(self):
        from src.data.factory import DataProviderSelection

        with pytest.raises(OfficialProviderPreflightError):
            verify_official_provider_is_tradier_production(DataProviderSelection(data_provider="alpaca"))

    def test_tradier_without_token_rejected(self):
        from src.data.factory import DataProviderSelection
        from src.data.tradier_provider import TradierConfig

        with pytest.raises(OfficialProviderPreflightError, match="token"):
            verify_official_provider_is_tradier_production(
                DataProviderSelection(data_provider="tradier"), TradierConfig(token=None),
            )

    def test_tradier_sandbox_host_rejected(self):
        from src.data.factory import DataProviderSelection
        from src.data.tradier_provider import TradierConfig

        with pytest.raises(OfficialProviderPreflightError, match="production"):
            verify_official_provider_is_tradier_production(
                DataProviderSelection(data_provider="tradier"),
                TradierConfig(token="x", base_url="https://sandbox.tradier.com/v1"),
            )

    def test_tradier_production_accepted(self):
        from src.data.factory import DataProviderSelection
        from src.data.tradier_provider import TradierConfig

        verify_official_provider_is_tradier_production(
            DataProviderSelection(data_provider="tradier"),
            TradierConfig(token="x", base_url="https://api.tradier.com/v1"),
        )  # no exception -- this is the pass case


@pytest.mark.asyncio
class TestMockProviderRejection:
    """With the official runner configured for `mock` (the exact
    misconfiguration that produced the accidental 2026-09-23 cycle this
    step was written to prevent a recurrence of), the mutating cycle must
    refuse before touching any validation state."""

    @pytest.fixture
    def mock_configured_cycle(self, environment, monkeypatch):
        monkeypatch.setenv("OPTIONS_AGENT_DATA_PROVIDER", "mock")
        monkeypatch.delenv("OPTIONS_AGENT_TRADIER_TOKEN", raising=False)
        cycle = _load_script_module("_cli_test_mock_rejected_module", SCRIPT_PATH)
        # Deliberately NOT monkeypatching get_configured_market_data_provider
        # here -- if provider preflight failed to block the mutating path,
        # this would go on to construct a real _MockMarketDataProvider,
        # which is exactly the bug this test proves does not happen.
        return cycle

    async def test_mock_provider_refused_before_any_mutation(self, mock_configured_cycle):
        cycle = mock_configured_cycle
        ok = await cycle.run_validation_cycle()
        assert ok is False

        ops = operations_config_module.load_operations_config()

        from src.portfolio.persistence import SqliteControlLoopStore
        from src.review.candidates import SqliteCandidateReviewStore
        from src.portfolio.account_state import SqlitePaperAccountStateStore, SqlitePortfolioStore
        from src.validation.protocol import load_validation_config
        from src.validation.session import SqliteValidationStore

        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        cycle_id = f"validation-{__import__('datetime').datetime.now(__import__('datetime').timezone.utc).date().isoformat()}"
        assert control_loop_store.get_cycle_record(cycle_id) is None, "no official cycle record may exist"

        val_config = load_validation_config()
        validation_store = SqliteValidationStore(val_config.db_path)
        snapshots = validation_store.snapshots(cohort_id=COHORT_ID)
        assert len(snapshots) == 1, (
            "only the fixture's own seeded Day-1 snapshot may exist -- no NEW daily snapshot may have been recorded"
        )

        review_store = SqliteCandidateReviewStore(ops.candidate_review_db_path)
        assert review_store.all_candidates(cohort_id=COHORT_ID) == [], "no candidate may have been created or expired"

        assert SqlitePortfolioStore(ops.account_state_db_path).get(ops.account_id) is None, (
            "no Portfolio may have been bootstrapped"
        )
        assert SqlitePaperAccountStateStore(ops.account_state_db_path).get(ops.account_id) is None, (
            "no PaperAccountState may have been bootstrapped"
        )

    async def test_mock_provider_preflight_output_names_tradier(self, mock_configured_cycle, capsys):
        await mock_configured_cycle.run_validation_cycle()
        out = capsys.readouterr().out
        assert "tradier" in out.lower()
        assert "FAIL" in out


@pytest.mark.asyncio
class TestTradierProductionProviderPasses:
    """A correctly-configured environment (provider=tradier, token
    present, production base URL) must clear the preflight and reach the
    exact same Review-Only cycle behavior V1.4.4 already established --
    proven here via the same `FakeMarketDataProvider` substitution
    `test_review_only_daily_cycle.py` uses for the real fetch step,
    downstream of (never bypassing) the preflight check itself."""

    @pytest.fixture
    def tradier_configured_cycle(self, environment, monkeypatch):
        monkeypatch.setenv("OPTIONS_AGENT_DATA_PROVIDER", "tradier")
        monkeypatch.setenv("OPTIONS_AGENT_TRADIER_TOKEN", "fake-token-for-tests-only")
        monkeypatch.delenv("OPTIONS_AGENT_TRADIER_BASE_URL", raising=False)
        cycle = _load_script_module("_cli_test_tradier_accepted_module", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: FakeMarketDataProvider())
        return cycle

    async def test_tradier_production_reaches_the_mutating_cycle(self, tradier_configured_cycle):
        ok = await tradier_configured_cycle.run_validation_cycle()
        assert ok is True

        ops = operations_config_module.load_operations_config()
        from src.review.candidates import SqliteCandidateReviewStore

        review_store = SqliteCandidateReviewStore(ops.candidate_review_db_path)
        awaiting = review_store.candidates_awaiting_human(cohort_id=COHORT_ID)
        assert len(awaiting) == 1, "the exact same one-candidate outcome V1.4.4's own acceptance test proves"

        from src.brokers.base import SqliteIdempotencyStore

        idempotency = SqliteIdempotencyStore(ops.account_state_db_path)
        assert idempotency.all() == [], "still zero orders placed from the daily-cycle path"


# ------------------------------------------------------------- --preflight mode


@pytest.mark.asyncio
class TestPreflightMode:
    async def test_preflight_with_mock_provider_fails_and_mutates_nothing(self, environment, monkeypatch):
        monkeypatch.setenv("OPTIONS_AGENT_DATA_PROVIDER", "mock")
        monkeypatch.delenv("OPTIONS_AGENT_TRADIER_TOKEN", raising=False)
        cycle = _load_script_module("_cli_test_preflight_mock_module", SCRIPT_PATH)

        ok = await cycle.run_preflight()
        assert ok is False

        ops = operations_config_module.load_operations_config()
        from src.portfolio.account_state import SqlitePaperAccountStateStore, SqlitePortfolioStore
        from src.review.candidates import SqliteCandidateReviewStore

        assert SqlitePortfolioStore(ops.account_state_db_path).get(ops.account_id) is None
        assert SqlitePaperAccountStateStore(ops.account_state_db_path).get(ops.account_id) is None
        assert SqliteCandidateReviewStore(ops.candidate_review_db_path).all_candidates(cohort_id=COHORT_ID) == []

    async def test_preflight_with_tradier_production_passes_and_prints_the_required_banner(
        self, environment, monkeypatch, capsys,
    ):
        monkeypatch.setenv("OPTIONS_AGENT_DATA_PROVIDER", "tradier")
        monkeypatch.setenv("OPTIONS_AGENT_TRADIER_TOKEN", "fake-token-for-tests-only")
        monkeypatch.delenv("OPTIONS_AGENT_TRADIER_BASE_URL", raising=False)
        cycle = _load_script_module("_cli_test_preflight_tradier_module", SCRIPT_PATH)

        ok = await cycle.run_preflight()
        assert ok is True

        out = capsys.readouterr().out
        assert "PREFLIGHT ONLY -- NO VALIDATION STATE MUTATED" in out

        ops = operations_config_module.load_operations_config()
        from src.portfolio.account_state import SqlitePaperAccountStateStore, SqlitePortfolioStore
        from src.review.candidates import SqliteCandidateReviewStore

        assert SqlitePortfolioStore(ops.account_state_db_path).get(ops.account_id) is None, (
            "--preflight must never bootstrap a Portfolio even on success"
        )
        assert SqlitePaperAccountStateStore(ops.account_state_db_path).get(ops.account_id) is None
        assert SqliteCandidateReviewStore(ops.candidate_review_db_path).all_candidates(cohort_id=COHORT_ID) == []

    async def test_preflight_never_calls_the_configured_provider_factory(self, environment, monkeypatch):
        """`--preflight` must not even construct a provider instance --
        only read `DataProviderSelection`/`TradierConfig`."""
        monkeypatch.setenv("OPTIONS_AGENT_DATA_PROVIDER", "tradier")
        monkeypatch.setenv("OPTIONS_AGENT_TRADIER_TOKEN", "fake-token-for-tests-only")
        cycle = _load_script_module("_cli_test_preflight_no_provider_module", SCRIPT_PATH)

        def _boom():
            raise AssertionError("run_preflight must never call get_configured_market_data_provider")

        monkeypatch.setattr(cycle, "get_configured_market_data_provider", _boom)
        ok = await cycle.run_preflight()
        assert ok is True
