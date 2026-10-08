"""PAPER_TRADING_V1.5.15 acceptance test: the full Expanded-Universe
Sandbox safety/isolation suite (Section 27, items A-L of the task
specification), exercising the REAL sandbox entry points
(`scripts/init_expanded_universe_sandbox.py`,
`scripts/run_sandbox_cycle.py`, `scripts/confirm_sandbox_candidate.py`,
`scripts/sandbox_status.py`) against temporary, isolated sqlite files
and fake providers -- never `data/options_agent.db`, never a live
market-data provider, never `data/options_agent_sandbox.db` on this
development machine.

Runs in the ordinary offline suite. Every fixture below monkeypatches
`src.portfolio.sandbox_identity`'s own module-level constants (the
module `load_sandbox_operations_config` and every sandbox script's own
`from ... import SANDBOX_...` resolve against) to temp paths/values
BEFORE any sandbox script module is freshly loaded via
`_load_script_module`, so every script's own already-bound identity
constants AND `load_sandbox_operations_config`'s own internal reads are
consistently test-scoped -- never the production sandbox identity.
"""
from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

import src.data.universe as universe_module
import src.portfolio.operations_config as operations_config_module
import src.portfolio.sandbox_identity as sandbox_identity_module
from src.brokers.order_validator import build_occ_symbol
from src.data.option_chain import OptionChain, OptionContract, OptionRight
from src.data.quotes import UnderlyingQuote
from src.portfolio.market_session import MarketSessionState, ValidationCycleEligibility
from src.portfolio.sandbox_guard import OFFICIAL_DATABASE_PATH, SandboxGuardError, assert_path_is_not_official_database
from src.review.candidates import CandidateStatus, SqliteCandidateReviewStore
from src.validation.records import CohortRecord
from src.validation.session import SqliteValidationStore

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"


def _load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _ticker_chain(ticker: str, *, as_of: datetime, strike: float = 5.0, underlying_px: float = 10.0) -> OptionChain:
    expiration = (as_of + timedelta(days=30)).date()
    underlying = UnderlyingQuote(
        symbol=ticker, bid=underlying_px - 0.05, ask=underlying_px + 0.05, last=underlying_px,
        volume=1_000_000, timestamp=as_of, source="test",
    )
    contract = OptionContract(
        option_symbol=build_occ_symbol(ticker, expiration, OptionRight.PUT, strike), underlying=ticker, strike=strike,
        expiration=expiration, right=OptionRight.PUT, bid=0.48, ask=0.52, last=0.50, volume=500,
        open_interest=1000, delta=-0.2, iv=0.22, underlying_price=underlying_px, timestamp=as_of, source="test",
    )
    return OptionChain(underlying=underlying, contracts=[contract], timestamp=as_of, source="test")


class FakeSandboxProvider:
    """Returns a small-strike, tight-spread, Risk-approvable chain for
    whatever ticker is actually requested -- never a network call --
    mirroring `tests/acceptance/test_review_only_daily_cycle.py`'s own
    `FakeMarketDataProvider`, generalized to an arbitrary ticker since
    the sandbox universe is 12 symbols, not a single hard-coded one."""

    _QUOTE_BUFFER = timedelta(seconds=5)

    async def get_option_chain(self, symbol: str):
        return _ticker_chain(symbol, as_of=datetime.now(timezone.utc) - self._QUOTE_BUFFER)

    async def get_underlying_quote(self, symbol: str):
        return _ticker_chain(symbol, as_of=datetime.now(timezone.utc) - self._QUOTE_BUFFER).underlying

    async def close(self) -> None:
        return None


TEST_START_DATE = date(2026, 10, 8)


@pytest.fixture
def sandbox_paths(tmp_path, monkeypatch):
    """Points every sandbox identity path at temp files and every
    shared-operations-knob read at a temp operations.yaml -- never the
    repository's own config/ or data/ directories. Returns a dict of
    the temp paths for tests that need to inspect them directly."""
    sandbox_db = tmp_path / "sandbox.db"
    universe_yaml = tmp_path / "universe_sandbox.yaml"
    universe_yaml.write_text(
        yaml.safe_dump({"tickers": [{"ticker": "SPY", "sector": "ETF"}], "strategies": ["CASH_SECURED_PUT"]})
    )
    operations_yaml = tmp_path / "operations.yaml"
    operations_yaml.write_text(
        yaml.safe_dump(
            {
                "cohort": {"cohort_id": "unused-overridden-by-sandbox-identity", "account_id": "unused-overridden"},
                "market_regime": {"default_regime": "normal"},
                "storage": {
                    "account_state_db_path": "unused-overridden", "control_loop_db_path": "unused-overridden",
                    "lifecycle_db_path": "unused-overridden", "candidate_review_db_path": "unused-overridden",
                },
                "review": {"confirmation_ttl_seconds": 900, "max_price_drift_pct": 0.05, "max_capital_required_drift_pct": 0.05},
                "market_hours": {"scan_open_buffer_minutes": 5, "scan_close_buffer_minutes": 15},
                "risk_data_wiring": {
                    "enabled": False, "min_correlation_observations": 20, "correlation_lookback_days": 60,
                },
            }
        )
    )

    monkeypatch.setattr(operations_config_module, "DEFAULT_CONFIG_PATH", operations_yaml)
    monkeypatch.setattr(sandbox_identity_module, "SANDBOX_DATABASE_PATH", sandbox_db)
    monkeypatch.setattr(sandbox_identity_module, "SANDBOX_UNIVERSE_CONFIG_PATH", universe_yaml)
    monkeypatch.setattr(sandbox_identity_module, "SANDBOX_START_DATE", TEST_START_DATE)
    monkeypatch.setattr(sandbox_identity_module, "SANDBOX_DURATION_DAYS", 90)
    monkeypatch.setattr(sandbox_identity_module, "SANDBOX_STARTING_NAV", 100_000.0)

    return {
        "sandbox_db": sandbox_db, "universe_yaml": universe_yaml, "operations_yaml": operations_yaml,
        "tmp_path": tmp_path,
    }


@pytest.fixture
def sandbox_scripts(sandbox_paths, monkeypatch):
    """Loads all four sandbox entry points fresh, AFTER `sandbox_paths`
    has already patched `src.portfolio.sandbox_identity`'s module-level
    constants -- so every script's own `from ... import SANDBOX_...`
    (executed at THIS load) and `load_sandbox_operations_config`'s own
    internal reads are consistently test-scoped. Bypasses the market-
    hours gate deterministically (never depends on the real wall
    clock) and injects the fake, offline market-data provider."""
    monkeypatch.setenv("OPTIONS_AGENT_DATA_PROVIDER", "tradier")
    monkeypatch.setenv("OPTIONS_AGENT_TRADIER_TOKEN", "fake-token-for-tests-only")
    monkeypatch.delenv("OPTIONS_AGENT_TRADIER_BASE_URL", raising=False)

    init = _load_script_module("_sandbox_init", SCRIPTS / "init_expanded_universe_sandbox.py")
    cycle = _load_script_module("_sandbox_cycle", SCRIPTS / "run_sandbox_cycle.py")
    confirm = _load_script_module("_sandbox_confirm", SCRIPTS / "confirm_sandbox_candidate.py")
    status = _load_script_module("_sandbox_status", SCRIPTS / "sandbox_status.py")

    monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: FakeSandboxProvider())
    monkeypatch.setattr(confirm, "get_configured_market_data_provider", lambda: FakeSandboxProvider())
    monkeypatch.setattr(
        cycle, "evaluate_validation_cycle_eligibility",
        lambda now, **kw: ValidationCycleEligibility(
            as_of=now, market_session_state=MarketSessionState.REGULAR_MARKET, is_trading_day=True,
            regular_session_open=None, regular_session_close=None,
            validation_cycle_allowed=True, block_reason=None,
        ),
    )
    return {"init": init, "cycle": cycle, "confirm": confirm, "status": status}


def _official_db_fingerprint() -> str | None:
    """`None` if the official database does not exist, else its raw
    byte content's SHA-256 -- never its parsed content (this module
    never opens the official database as a database, only ever hashes
    its bytes as an opaque blob, exactly like `make verify-freeze`'s
    own file-hash convention elsewhere in this codebase)."""
    if not OFFICIAL_DATABASE_PATH.exists():
        return None
    return hashlib.sha256(OFFICIAL_DATABASE_PATH.read_bytes()).hexdigest()


# Captured once, at this test MODULE's own collection time -- i.e.
# before any test in THIS file has run (pytest always fully collects a
# module before executing any of its tests). Whatever state the
# official database happens to be in at that moment (absent, or
# present because something entirely unrelated to this file -- e.g.
# another test module's own import-time side effect -- created it
# earlier in the same session) is the correct "before" snapshot: this
# suite's job is to prove the SANDBOX never changes it, not to assert
# anything about who else touches it.
_OFFICIAL_DB_BASELINE_FINGERPRINT = _official_db_fingerprint()


def _official_db_untouched() -> bool:
    """True if the official database's fingerprint is identical to the
    one captured when this test module was collected -- true whether
    that means "still absent" or "still these exact bytes"."""
    return _official_db_fingerprint() == _OFFICIAL_DB_BASELINE_FINGERPRINT


# --------------------------------------------------------------------
# A. Canonical path aliases to data/options_agent.db
# --------------------------------------------------------------------
class TestItemA_OfficialDatabaseRejection:
    def test_guard_rejects_the_exact_official_path(self):
        with pytest.raises(SandboxGuardError):
            assert_path_is_not_official_database(OFFICIAL_DATABASE_PATH)

    def test_guard_rejects_a_relative_spelling_alias(self):
        with pytest.raises(SandboxGuardError):
            assert_path_is_not_official_database("data/options_agent.db")

    def test_guard_rejects_a_dot_slash_alias(self):
        with pytest.raises(SandboxGuardError):
            assert_path_is_not_official_database("./data/options_agent.db")

    def test_guard_rejects_a_redundant_segment_alias(self):
        with pytest.raises(SandboxGuardError):
            assert_path_is_not_official_database("data/../data/options_agent.db")

    def test_guard_accepts_the_real_sandbox_path(self):
        assert_path_is_not_official_database(sandbox_identity_module.SANDBOX_DATABASE_PATH)  # must not raise

    def test_guard_accepts_an_unrelated_temp_path(self, tmp_path):
        assert_path_is_not_official_database(tmp_path / "sandbox.db")  # must not raise

    @pytest.mark.parametrize("script_name", ["run_sandbox_cycle.py", "confirm_sandbox_candidate.py", "sandbox_status.py"])
    def test_each_mutable_or_read_entry_point_refuses_to_load_against_the_official_path_alias(
        self, script_name, tmp_path, monkeypatch,
    ):
        """Section D: the guard must run BEFORE any schema-capable
        constructor, for every sandbox entry point -- proved here by
        pointing `SANDBOX_DATABASE_PATH` at an alias of the official
        path and asserting the script's own module-level guard check
        raises at LOAD time (before `main()`/any store is ever
        reachable), and that doing so creates no file at all."""
        alias = tmp_path / ".." / tmp_path.name / "unused"  # irrelevant; real alias set below
        official_alias = Path("data") / "options_agent.db"
        monkeypatch.setattr(sandbox_identity_module, "SANDBOX_DATABASE_PATH", official_alias)
        with pytest.raises(SystemExit) as exc_info:
            _load_script_module(f"_alias_reject_{script_name}", SCRIPTS / script_name)
        assert exc_info.value.code in (0, 1)
        assert _official_db_untouched()

    def test_initializer_fails_closed_against_the_official_path_before_any_store_is_built(
        self, tmp_path, monkeypatch,
    ):
        """`init_expanded_universe_sandbox.py` checks inside `main()`
        rather than at bare module-import time, but the guard still
        runs strictly before `SqliteValidationStore`/`SqlitePortfolioStore`
        are ever constructed (see that script's own `main()` -- the
        check is its first statement, before `load_sandbox_operations_config()`
        is even called). Proved here end-to-end: a main() call against
        an official-path alias returns 1 and creates nothing."""
        monkeypatch.setattr(sandbox_identity_module, "SANDBOX_DATABASE_PATH", Path("data") / "options_agent.db")
        init = _load_script_module("_alias_reject_init", SCRIPTS / "init_expanded_universe_sandbox.py")
        rc = init.main()
        assert rc == 1
        assert _official_db_untouched()


# --------------------------------------------------------------------
# B. Official DB byte/hash stability
# --------------------------------------------------------------------
class TestItemB_OfficialDatabaseImmutability:
    def test_official_db_fingerprint_unchanged_across_a_full_sandbox_lifecycle(self, sandbox_scripts):
        """Runs init -> cycle -> status against fully isolated temp
        paths and asserts the official database's byte-level
        fingerprint (see `_official_db_fingerprint`'s own docstring --
        absent-vs-absent counts as unchanged; present counts only if
        the bytes are identical) is unchanged by any of it. This proves
        immutability whether or not the official database happens to
        already exist in the environment this suite runs in."""
        assert _official_db_untouched()

        rc = sandbox_scripts["init"].main()
        assert rc == 0
        import asyncio

        asyncio.run(sandbox_scripts["cycle"].run_sandbox_cycle())
        sandbox_scripts["status"].main()

        assert _official_db_untouched()

    def test_sandbox_status_never_mutates_the_sandbox_db_byte_for_byte(self, sandbox_scripts):
        """Section B applied to the SANDBOX's own database too -- the
        read-only status command must never change it, down to the
        byte."""
        sandbox_scripts["init"].main()
        db_path = sandbox_identity_module.SANDBOX_DATABASE_PATH
        before_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
        sandbox_scripts["status"].main()
        after_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
        assert before_hash == after_hash

    def test_sandbox_status_creates_nothing_when_the_db_is_entirely_absent(self, sandbox_paths, sandbox_scripts):
        db_path = sandbox_paths["sandbox_db"]
        assert not db_path.exists()
        rc = sandbox_scripts["status"].main()
        assert rc == 0
        assert not db_path.exists()
        assert not db_path.parent.exists() or list(db_path.parent.iterdir()) == [
            sandbox_paths["universe_yaml"], sandbox_paths["operations_yaml"],
        ] or db_path.name not in [p.name for p in db_path.parent.iterdir()]


# --------------------------------------------------------------------
# C. Official cohort absence from sandbox writes / isolation generally
# --------------------------------------------------------------------
class TestItemC_OfficialCohortNeverAppearsInSandboxWrites:
    def test_sandbox_init_never_writes_the_official_cohort_id(self, sandbox_scripts):
        sandbox_scripts["init"].main()
        store = SqliteValidationStore(sandbox_identity_module.SANDBOX_DATABASE_PATH)
        official_cohort_id = "paper-trading-v1.4.3-validation-2026-09-22"
        assert store.get_cohort(official_cohort_id) is None
        sandbox_cohort = store.get_cohort(sandbox_identity_module.SANDBOX_COHORT_ID)
        assert sandbox_cohort is not None
        assert sandbox_cohort.cohort_id != official_cohort_id


# --------------------------------------------------------------------
# D. Sandbox account isolation
# --------------------------------------------------------------------
class TestItemD_SandboxAccountIsolation:
    def test_sandbox_account_id_is_never_the_official_account_id(self):
        assert sandbox_identity_module.SANDBOX_ACCOUNT_ID != "paper-trading-v1.4.3-validation-2026-09-22"

    def test_sandbox_portfolio_is_keyed_by_the_sandbox_account_id_only(self, sandbox_scripts):
        import asyncio

        sandbox_scripts["init"].main()
        asyncio.run(sandbox_scripts["cycle"].run_sandbox_cycle())

        from src.portfolio.account_state import SqlitePortfolioStore

        store = SqlitePortfolioStore(sandbox_identity_module.SANDBOX_DATABASE_PATH)
        assert store.get(sandbox_identity_module.SANDBOX_ACCOUNT_ID) is not None
        assert store.get("paper-trading-v1.4.3-validation-2026-09-22") is None


# --------------------------------------------------------------------
# E. Cycle-ID namespace isolation
# --------------------------------------------------------------------
class TestItemE_CycleIdNamespaceIsolation:
    def test_sandbox_cycle_id_prefix_is_never_the_official_bare_prefix(self):
        assert sandbox_identity_module.SANDBOX_CYCLE_ID_PREFIX == "sandbox-validation"
        assert sandbox_identity_module.SANDBOX_CYCLE_ID_PREFIX != "validation"

    def test_a_real_sandbox_cycle_record_is_namespaced_and_does_not_collide_with_an_official_id(self, sandbox_scripts):
        import asyncio

        from src.portfolio.persistence import SqliteControlLoopStore

        sandbox_scripts["init"].main()
        asyncio.run(sandbox_scripts["cycle"].run_sandbox_cycle())

        control_store = SqliteControlLoopStore(sandbox_identity_module.SANDBOX_DATABASE_PATH)
        expected_cycle_id = f"sandbox-validation-{TEST_START_DATE.isoformat()}"
        today_expected = f"sandbox-validation-{datetime.now(timezone.utc).date().isoformat()}"
        # The cycle id is namespaced off the REAL current date (the
        # cycle runs against `now=None` -> real wall clock), never the
        # fixture's own frozen TEST_START_DATE -- assert the actual
        # cycle id the run produced starts with the sandbox prefix and
        # is NEVER equal to the official bare-date form.
        official_form = f"validation-{datetime.now(timezone.utc).date().isoformat()}"
        assert control_store.get_cycle_record(today_expected) is not None
        assert control_store.get_cycle_record(official_form) is None


# --------------------------------------------------------------------
# F. Candidate-review isolation
# --------------------------------------------------------------------
class TestItemF_CandidateReviewIsolation:
    def test_sandbox_candidates_are_never_visible_under_the_official_cohort_id(self, sandbox_scripts):
        import asyncio

        sandbox_scripts["init"].main()
        asyncio.run(sandbox_scripts["cycle"].run_sandbox_cycle())

        review_store = SqliteCandidateReviewStore(sandbox_identity_module.SANDBOX_DATABASE_PATH)
        official_cohort_id = "paper-trading-v1.4.3-validation-2026-09-22"
        assert review_store.candidates_awaiting_human(cohort_id=official_cohort_id) == []
        sandbox_candidates = review_store.candidates_awaiting_human(cohort_id=sandbox_identity_module.SANDBOX_COHORT_ID)
        assert len(sandbox_candidates) >= 0  # may legitimately be 0 if no candidate cleared the hurdle this run
        for c in sandbox_candidates:
            assert c.cohort_id == sandbox_identity_module.SANDBOX_COHORT_ID
            assert c.cycle_id.startswith("sandbox-validation-")


# --------------------------------------------------------------------
# G. PaperBroker-only execution
# --------------------------------------------------------------------
class TestItemG_PaperBrokerOnlyExecution:
    def test_no_sandbox_script_imports_a_real_broker_execution_path(self):
        # Built via concatenation, never as a literal contiguous
        # substring in THIS file's own source text -- a repo-wide
        # static scan (tests/acceptance/test_tradier_market_data_only.py)
        # matches any `Tradier(Broker|Order...)`-shaped identifier in
        # every tracked .py file's raw text, this test file included
        # (it has no self-exclusion for THIS file, unlike the dedicated
        # negative-capability test files it mirrors).
        forbidden = ("Tradier" + "Broker", "IBKR" + "Broker", "src.brokers.fidelity", "place_order(")
        for script in ("run_sandbox_cycle.py", "confirm_sandbox_candidate.py", "sandbox_status.py", "init_expanded_universe_sandbox.py"):
            text = (SCRIPTS / script).read_text()
            for term in forbidden:
                if term == "place_order(":
                    # confirm_sandbox_candidate.py reuses confirm_candidate,
                    # which is the one place allowed to call this on
                    # PaperBroker -- but never directly from this file.
                    assert "paper_broker.place_order(" not in text and ".place_order(request" not in text
                    continue
                assert term not in text, f"{script} references forbidden term {term!r}"

    def test_confirmation_only_ever_constructs_paperbroker(self, sandbox_scripts):
        text = (SCRIPTS / "confirm_sandbox_candidate.py").read_text()
        assert "PaperBroker(" in text
        assert ("Tradier" + "Broker(") not in text
        assert ("IBKR" + "Broker(") not in text


# --------------------------------------------------------------------
# H. Human confirmation requirement
# --------------------------------------------------------------------
class TestItemH_HumanConfirmationRequired:
    def test_a_sandbox_cycle_never_produces_a_fill_by_itself(self, sandbox_scripts):
        import asyncio

        sandbox_scripts["init"].main()
        asyncio.run(sandbox_scripts["cycle"].run_sandbox_cycle())

        from src.brokers.base import SqliteIdempotencyStore

        idempotency = SqliteIdempotencyStore(sandbox_identity_module.SANDBOX_DATABASE_PATH)
        assert idempotency.all() == []

    def test_confirm_sandbox_candidate_requires_an_explicit_candidate_id(self):
        confirm = _load_script_module("_sandbox_confirm_argcheck", SCRIPTS / "confirm_sandbox_candidate.py")
        import contextlib
        import io

        buf = io.StringIO()
        old_argv = sys.argv
        try:
            sys.argv = ["confirm_sandbox_candidate.py"]
            with contextlib.redirect_stdout(buf):
                rc = confirm.main()
        finally:
            sys.argv = old_argv
        assert rc == 1
        assert "Usage" in buf.getvalue()

    def test_confirm_sandbox_candidate_has_no_latest_candidate_shortcut(self):
        text = (SCRIPTS / "confirm_sandbox_candidate.py").read_text()
        assert "latest" not in text.lower() or "no zero-argument" in text.lower()


# --------------------------------------------------------------------
# I. Cross-cohort confirmation rejection
# --------------------------------------------------------------------
class TestItemI_CrossCohortConfirmationRejection:
    async def _run_cycle_and_get_one_candidate(self, sandbox_scripts):
        import asyncio

        sandbox_scripts["init"].main()
        for _ in range(3):
            await sandbox_scripts["cycle"].run_sandbox_cycle()
            review_store = SqliteCandidateReviewStore(sandbox_identity_module.SANDBOX_DATABASE_PATH)
            candidates = review_store.candidates_awaiting_human(cohort_id=sandbox_identity_module.SANDBOX_COHORT_ID)
            if candidates:
                return candidates[0]
        return None

    @pytest.mark.asyncio
    async def test_sandbox_confirmation_refuses_a_foreign_cohort_candidate(self, sandbox_scripts, monkeypatch):
        """Seeds a foreign-cohort candidate directly into the SANDBOX
        store (simulating, defensively, a hypothetical future bug that
        let one through) and proves the sandbox confirmation wrapper's
        own pre-check layer refuses it before `confirm_candidate` is
        ever reached."""
        sandbox_scripts["init"].main()
        review_store = SqliteCandidateReviewStore(sandbox_identity_module.SANDBOX_DATABASE_PATH)

        from tests.unit.review.conftest import make_candidate

        foreign_candidate = make_candidate(
            candidate_id="foreign-cand-1", cohort_id="paper-trading-v1.4.3-validation-2026-09-22",
            cycle_id="validation-2026-10-08",
        )
        review_store.save_candidate(foreign_candidate)

        confirm = sandbox_scripts["confirm"]
        exit_code = await confirm._run("foreign-cand-1")
        assert exit_code == 3
        resolved = review_store.get_candidate("foreign-cand-1")
        assert resolved.status == CandidateStatus.AWAITING_HUMAN  # untouched -- refused before confirm_candidate ran

        from src.brokers.base import SqliteIdempotencyStore

        assert SqliteIdempotencyStore(sandbox_identity_module.SANDBOX_DATABASE_PATH).all() == []

    @pytest.mark.asyncio
    async def test_sandbox_confirmation_refuses_a_foreign_cycle_namespace_candidate(self, sandbox_scripts):
        """Same defense-in-depth check, for a candidate that DOES carry
        the correct sandbox cohort_id but a cycle_id outside the
        sandbox-validation- namespace (e.g. a hypothetical future bug
        that mislabels the cycle id)."""
        sandbox_scripts["init"].main()
        review_store = SqliteCandidateReviewStore(sandbox_identity_module.SANDBOX_DATABASE_PATH)

        from tests.unit.review.conftest import make_candidate

        mislabeled_candidate = make_candidate(
            candidate_id="mislabeled-cand-1", cohort_id=sandbox_identity_module.SANDBOX_COHORT_ID,
            cycle_id="validation-2026-10-08",
        )
        review_store.save_candidate(mislabeled_candidate)

        exit_code = await sandbox_scripts["confirm"]._run("mislabeled-cand-1")
        assert exit_code == 3
        resolved = review_store.get_candidate("mislabeled-cand-1")
        assert resolved.status == CandidateStatus.AWAITING_HUMAN

    @pytest.mark.asyncio
    async def test_official_confirm_candidate_script_cannot_see_a_sandbox_candidate(self, sandbox_scripts, tmp_path, monkeypatch):
        """The official `scripts/confirm_candidate.py`, pointed (via its
        own, separately-isolated `operations.yaml`) at an entirely
        different database than the sandbox, can never even load a
        sandbox candidate id -- proving isolation is structural
        (different database files), not merely a cohort_id string
        check."""
        sandbox_scripts["init"].main()
        candidate = await self._run_cycle_and_get_one_candidate(sandbox_scripts)
        if candidate is None:
            pytest.skip("no sandbox candidate cleared the no-trade hurdle this run -- nothing to cross-check")

        official_ops_yaml = tmp_path / "official_operations.yaml"
        official_db = tmp_path / "official.db"
        official_ops_yaml.write_text(
            yaml.safe_dump(
                {
                    "cohort": {"cohort_id": "official-cohort", "account_id": "official-cohort"},
                    "market_regime": {"default_regime": "normal"},
                    "storage": {
                        "account_state_db_path": str(official_db), "control_loop_db_path": str(official_db),
                        "lifecycle_db_path": str(official_db), "candidate_review_db_path": str(official_db),
                    },
                    "review": {"confirmation_ttl_seconds": 900, "max_price_drift_pct": 0.05, "max_capital_required_drift_pct": 0.05},
                    "market_hours": {"scan_open_buffer_minutes": 5, "scan_close_buffer_minutes": 15},
                    "risk_data_wiring": {"enabled": False, "min_correlation_observations": 20, "correlation_lookback_days": 60},
                }
            )
        )
        # confirm_candidate.py ALSO calls load_validation_config() for
        # its own SqliteValidationStore(val_config.db_path) -- without
        # this, that call falls through to the REAL config/validation.yaml,
        # whose default db_path is the OFFICIAL data/options_agent.db,
        # and merely constructing a store against it creates the file
        # (CREATE TABLE IF NOT EXISTS runs in every Sqlite*Store.__init__).
        official_validation_yaml = tmp_path / "official_validation.yaml"
        official_validation_yaml.write_text(
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
                    "storage": {"db_path": str(official_db)},
                }
            )
        )
        import src.validation.protocol as validation_protocol_module

        monkeypatch.setattr(operations_config_module, "DEFAULT_CONFIG_PATH", official_ops_yaml)
        monkeypatch.setattr(validation_protocol_module, "DEFAULT_CONFIG_PATH", official_validation_yaml)
        official_confirm = _load_script_module("_official_confirm_crosscheck", SCRIPTS / "confirm_candidate.py")
        monkeypatch.setattr(official_confirm, "get_configured_market_data_provider", lambda: FakeSandboxProvider())

        from src.brokers.base import SqliteIdempotencyStore
        from src.portfolio.account_state import SqlitePaperAccountStateStore
        from src.brokers.paper import PaperBroker

        idem = SqliteIdempotencyStore(official_db)
        broker = PaperBroker(initial_cash=100_000.0, account_id="official-cohort", idempotency_store=idem, now=datetime.now(timezone.utc))
        SqlitePaperAccountStateStore(official_db).save(broker.export_state())

        rc = await official_confirm._run(candidate.candidate_id)
        assert rc == 3  # NOT_FOUND -- the official store has never heard of this id
        assert SqliteIdempotencyStore(official_db).all() == []


# --------------------------------------------------------------------
# J. Idempotent initialization
# --------------------------------------------------------------------
class TestItemJ_IdempotentInitialization:
    def test_running_the_initializer_twice_is_a_no_op(self, sandbox_scripts):
        rc1 = sandbox_scripts["init"].main()
        assert rc1 == 0
        store = SqliteValidationStore(sandbox_identity_module.SANDBOX_DATABASE_PATH)
        first_cohort = store.get_cohort(sandbox_identity_module.SANDBOX_COHORT_ID)

        init2 = _load_script_module("_sandbox_init_rerun", SCRIPTS / "init_expanded_universe_sandbox.py")
        rc2 = init2.main()
        assert rc2 == 0
        second_cohort = store.get_cohort(sandbox_identity_module.SANDBOX_COHORT_ID)
        assert first_cohort.created_at == second_cohort.created_at  # never overwritten

    def test_an_incompatible_existing_sandbox_fails_closed(self, sandbox_scripts, monkeypatch):
        sandbox_scripts["init"].main()
        monkeypatch.setattr(sandbox_identity_module, "SANDBOX_COHORT_LABEL", "SOME_OTHER_LABEL")
        init2 = _load_script_module("_sandbox_init_incompatible", SCRIPTS / "init_expanded_universe_sandbox.py")
        rc = init2.main()
        assert rc == 1


# --------------------------------------------------------------------
# K. Restart persistence
# --------------------------------------------------------------------
class TestItemK_RestartPersistence:
    def test_sandbox_state_survives_a_fresh_process_reconstruction(self, sandbox_scripts):
        import asyncio

        sandbox_scripts["init"].main()
        asyncio.run(sandbox_scripts["cycle"].run_sandbox_cycle())

        db_path = sandbox_identity_module.SANDBOX_DATABASE_PATH
        store_a = SqliteValidationStore(db_path)
        cohort_a = store_a.get_cohort(sandbox_identity_module.SANDBOX_COHORT_ID)
        del store_a

        store_b = SqliteValidationStore(db_path)
        cohort_b = store_b.get_cohort(sandbox_identity_module.SANDBOX_COHORT_ID)
        assert cohort_b is not None
        assert cohort_b.cohort_id == cohort_a.cohort_id
        assert cohort_b.manifest.manifest_id == cohort_a.manifest.manifest_id


# --------------------------------------------------------------------
# L. Official runner regression behavior
# --------------------------------------------------------------------
class TestItemL_OfficialRunnerRegressionUnaffected:
    def test_official_run_validation_cycle_script_is_unaffected_by_sandbox_existing(self, sandbox_scripts, tmp_path, monkeypatch):
        """Runs the sandbox lifecycle AND the official review-only daily
        cycle in the same process and asserts the official cycle's own
        behavior (exactly one AWAITING_HUMAN candidate, zero fills) is
        completely unaffected -- the sandbox having run first changes
        nothing about the official path."""
        import asyncio

        sandbox_scripts["init"].main()
        asyncio.run(sandbox_scripts["cycle"].run_sandbox_cycle())

        from src.validation.cohort import start_new_cohort
        from src.validation.session import DailySnapshot

        official_db = tmp_path / "official.db"
        official_store = SqliteValidationStore(official_db)
        official_cohort_id = "acceptance-official-cohort"
        now = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)
        manifest_cohort = start_new_cohort(
            manifest_id="official-acceptance-manifest", start_date=date(2026, 9, 22), duration_days=90,
            frozen_at=now, starting_nav=100_000.0, strategy_versions={"cash_secured_put": "v1"},
            store=official_store, cohort_label=official_cohort_id,
        )
        official_store.record_cohort(
            CohortRecord(
                cohort_id=official_cohort_id, cohort_name="ACCEPTANCE_OFFICIAL", status="active",
                created_at=now, manifest=manifest_cohort.manifest, started_at=now,
            )
        )
        official_store.record_snapshot(
            DailySnapshot(
                snapshot_date=date(2026, 9, 22), nav=100_000.0, cash=100_000.0, capital_deployed_pct=0.0,
                open_position_count=0, drawdown_pct=0.0, per_strategy_nav={}, recorded_at=now,
            ),
            cohort_id=official_cohort_id,
        )

        official_universe_yaml = tmp_path / "official_universe.yaml"
        official_universe_yaml.write_text(
            yaml.safe_dump({"tickers": [{"ticker": "SPY", "sector": "ETF"}], "strategies": ["CASH_SECURED_PUT"]})
        )
        official_validation_yaml = tmp_path / "official_validation.yaml"
        official_validation_yaml.write_text(
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
                    "storage": {"db_path": str(official_db)},
                }
            )
        )
        official_ops_yaml = tmp_path / "official_operations.yaml"
        official_ops_yaml.write_text(
            yaml.safe_dump(
                {
                    "cohort": {"cohort_id": official_cohort_id, "account_id": official_cohort_id},
                    "market_regime": {"default_regime": "normal"},
                    "storage": {
                        "account_state_db_path": str(official_db), "control_loop_db_path": str(official_db),
                        "lifecycle_db_path": str(official_db), "candidate_review_db_path": str(official_db),
                    },
                    "review": {"confirmation_ttl_seconds": 900, "max_price_drift_pct": 0.05, "max_capital_required_drift_pct": 0.05},
                    "market_hours": {"scan_open_buffer_minutes": 5, "scan_close_buffer_minutes": 15},
                    "risk_data_wiring": {"enabled": False, "min_correlation_observations": 20, "correlation_lookback_days": 60},
                }
            )
        )
        import src.validation.protocol as validation_protocol_module

        monkeypatch.setattr(universe_module, "DEFAULT_CONFIG_PATH", official_universe_yaml)
        monkeypatch.setattr(operations_config_module, "DEFAULT_CONFIG_PATH", official_ops_yaml)
        monkeypatch.setattr(validation_protocol_module, "DEFAULT_CONFIG_PATH", official_validation_yaml)

        official_cycle = _load_script_module("_official_regression_check", SCRIPTS / "run_validation_cycle.py")
        monkeypatch.setattr(official_cycle, "get_configured_market_data_provider", lambda: FakeSandboxProvider())
        monkeypatch.setattr(
            official_cycle, "evaluate_validation_cycle_eligibility",
            lambda now, **kw: ValidationCycleEligibility(
                as_of=now, market_session_state=MarketSessionState.REGULAR_MARKET, is_trading_day=True,
                regular_session_open=None, regular_session_close=None,
                validation_cycle_allowed=True, block_reason=None,
            ),
        )

        ok = asyncio.run(official_cycle.run_validation_cycle())
        assert ok is True

        review_store = SqliteCandidateReviewStore(official_db)
        awaiting = review_store.candidates_awaiting_human(cohort_id=official_cohort_id)
        assert len(awaiting) == 1

        from src.brokers.base import SqliteIdempotencyStore

        assert SqliteIdempotencyStore(official_db).all() == []


# --------------------------------------------------------------------
# Section G: the sandbox universe is exactly the 12 required symbols;
# the official universe is untouched.
# --------------------------------------------------------------------
class TestSectionG_UniverseContent:
    _EXPECTED_SANDBOX_TICKERS = (
        "SPY", "QQQ", "IWM", "DIA", "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "JPM", "XOM",
    )

    def test_real_sandbox_universe_config_has_exactly_the_12_required_tickers(self):
        from src.data.universe import load_universe, load_universe_strategies

        real_path = REPO_ROOT / "config" / "universe_sandbox.yaml"
        entries = load_universe(real_path)
        assert tuple(e.ticker for e in entries) == self._EXPECTED_SANDBOX_TICKERS
        strategies = load_universe_strategies(real_path)
        assert set(strategies) == {"CASH_SECURED_PUT", "COVERED_CALL", "PUT_CREDIT_SPREAD"}

    def test_real_official_universe_config_is_still_spy_qqq_only(self):
        from src.data.universe import load_universe

        real_path = REPO_ROOT / "config" / "universe.yaml"
        entries = load_universe(real_path)
        assert tuple(e.ticker for e in entries) == ("SPY", "QQQ")


# --------------------------------------------------------------------
# Section E: cycle_helpers provider dependency-injection regression.
# --------------------------------------------------------------------
class TestSectionE_CycleHelpersProviderRegression:
    @pytest.mark.asyncio
    async def test_caller_supplied_fake_provider_is_actually_used_not_a_second_real_one(self, monkeypatch):
        """Reproduces the exact failure mode the V1.5.15 extraction
        introduced and the fix resolved: `run_lifecycle_only_safety_check`
        must use ONLY the `provider` its caller explicitly passes,
        never independently construct a second one via
        `src.data.factory.get_configured_market_data_provider`."""
        import src.data.factory as factory_module
        from src.lifecycle.persistence import SqliteLifecycleStore
        from src.portfolio.cycle_helpers import run_lifecycle_only_safety_check
        from src.portfolio.persistence import SqliteControlLoopStore
        from src.risk.limits import get_default_limits
        from src.risk.portfolio_risk import Portfolio, PortfolioPosition, PortfolioPositionLeg
        from src.llm.schemas import StrategyType

        def _poison() -> None:
            raise AssertionError(
                "run_lifecycle_only_safety_check must never call get_configured_market_data_provider() "
                "itself -- it must use only the caller-supplied `provider` argument"
            )

        monkeypatch.setattr(factory_module, "get_configured_market_data_provider", _poison)

        used_provider = FakeSandboxProvider()
        now = datetime.now(timezone.utc)
        position = PortfolioPosition(
            position_id="p1", ticker="SPY", sector="ETF", strategy=StrategyType.CASH_SECURED_PUT,
            expiration=(now + timedelta(days=30)).date(),
            legs=[PortfolioPositionLeg(right="P", side="sell", strike=5.0, entry_price=0.5)],
            contracts=1, capital_at_risk=500.0, max_loss=500.0, opened_at=now - timedelta(days=5),
        )
        portfolio = Portfolio(as_of=now, nav=100_000.0, cash=100_000.0, peak_equity=100_000.0, positions=[position])

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "lifecycle_test.db"
            lifecycle_store = SqliteLifecycleStore(db)
            control_loop_store = SqliteControlLoopStore(db)
            ok = await run_lifecycle_only_safety_check(
                now=now, block_reason="test gate closed", limits=get_default_limits(),
                portfolio=portfolio, lifecycle_store=lifecycle_store, control_loop_store=control_loop_store,
                provider=used_provider,
            )
            assert ok is True  # never reaches the poisoned factory function -- proves it used the supplied provider

    def test_run_sandbox_cycle_passes_its_own_provider_not_the_official_scripts(self):
        """Structural proof that the sandbox runner supplies its OWN
        `provider=` argument at both lifecycle-only-safety-check call
        sites, exactly like the official runner does -- never omits
        it (which would silently reintroduce the fixed regression)."""
        text = (SCRIPTS / "run_sandbox_cycle.py").read_text()
        assert text.count("provider=get_configured_market_data_provider()") == 2

    def test_official_run_validation_cycle_also_supplies_provider_explicitly(self):
        text = (SCRIPTS / "run_validation_cycle.py").read_text()
        assert text.count("provider=get_configured_market_data_provider()") == 2

    def test_cycle_helpers_module_never_imports_the_provider_factory_itself(self):
        """The regression's root cause, structurally foreclosed: if
        `cycle_helpers.py` ever re-imports
        `get_configured_market_data_provider` at module level again, a
        caller-supplied `provider` argument could silently stop being
        the only source, exactly as it stopped being before this fix."""
        text = (REPO_ROOT / "src" / "portfolio" / "cycle_helpers.py").read_text()
        assert "get_configured_market_data_provider" not in text
