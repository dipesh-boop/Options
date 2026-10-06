"""PAPER_TRADING_V1.5.13 acceptance test: `scripts/run_validation_cycle.py
--diagnostic-scan` -- a READ-ONLY, operator-facing diagnostic opportunity
scan against (in real use) live Tradier production market data, using the
exact same candidate -> Quant -> Risk pipeline as the official cycle,
WITHOUT counting as a validation day and WITHOUT mutating any
validation/candidate/trade/account/lifecycle/control-loop state.

Runs entirely offline against temporary sqlite files and fake providers
(never a live Tradier token or a real network call) -- reuses the exact
fixture pattern `test_review_only_daily_cycle.py`/`test_run_validation_
cycle_cli.py` already establish (a seeded, already-started cohort;
temporary validation.yaml/universe.yaml/operations.yaml; the real script
loaded via importlib).

Covers items A-O of the PAPER_TRADING_V1.5.13 task spec; see each test
class's own docstring for which item(s) it satisfies.
"""
from __future__ import annotations

import ast
import hashlib
import inspect
import subprocess
import sys
import yaml
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import src.portfolio.operations_config as operations_config_module
from src.data.option_chain import OptionChain, OptionContract, OptionRight
from src.data.provider import DteWindowOptionChainProvider, DteWindowSelectionDiagnostics
from src.data.quotes import UnderlyingQuote
from src.portfolio.market_session import MarketSessionState, ValidationCycleEligibility
from tests.acceptance.test_review_only_daily_cycle import (
    COHORT_ID,
    FakeMarketDataProvider,
    _load_script_module,
    environment,  # noqa: F401 -- reused as a pytest fixture
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "run_validation_cycle.py"

_ALLOWED = ValidationCycleEligibility(
    as_of=None, market_session_state=MarketSessionState.REGULAR_MARKET, is_trading_day=True,
    regular_session_open=None, regular_session_close=None,
    validation_cycle_allowed=True, block_reason=None,
)
_BLOCKED = ValidationCycleEligibility(
    as_of=None, market_session_state=MarketSessionState.PRE_MARKET, is_trading_day=True,
    regular_session_open=None, regular_session_close=None,
    validation_cycle_allowed=False, block_reason="pre-market -- the new-position scan window is not open yet",
)


def _spy_pcs_chain(now: datetime, *, symbol: str = "SPY") -> OptionChain:
    """A realistic, double-dated-prefix-reproducing SPY PUT_CREDIT_SPREAD
    chain (590/587 strikes) -- expiration computed relative to the
    INJECTED `now`, never a fixed calendar date, so this fixture is
    immune to date-rot by construction.

    Strikes/prices (short delta -0.15, long delta -0.08, both within
    `QuantFilterConfig`'s default short-delta screening window and
    liquidity thresholds) are deliberately chosen so the resulting
    candidate doesn't just construct cleanly (the V1.5.12 regression
    this fixture also exercises) but genuinely clears Quant/Risk with a
    positive risk-adjusted return -- verified directly against the real
    `generate_candidates` -> `default_quant_stage` -> `evaluate_trade_
    proposal` pipeline with `internal_paper` broker capabilities before
    being committed here: capital_required=$180, expected_value=+$27.39,
    probability_of_profit=0.691, decision=APPROVE. A shallower OTM
    choice (e.g. a 600/595 spread with 605 spot) reproduces the
    V1.5.12 ValidationError fine but prices to a NEGATIVE risk-adjusted
    return at these IVs/DTE and is correctly never selected as `best` --
    that is real economics, not a fixture bug, so it would be the wrong
    fixture for a test that also needs a surviving candidate."""
    expiration = (now + timedelta(days=24)).date()
    underlying = UnderlyingQuote(symbol=symbol, bid=604.5, ask=605.5, last=605.0, volume=1_000_000, timestamp=now, source="tradier")
    contracts = [
        OptionContract(
            option_symbol=f"{symbol}_P590", underlying=symbol, strike=590.0, expiration=expiration, right=OptionRight.PUT,
            bid=1.90, ask=2.10, last=2.0, volume=500, open_interest=1000, delta=-0.15, iv=0.22,
            underlying_price=605.0, timestamp=now, source="tradier",
        ),
        OptionContract(
            option_symbol=f"{symbol}_P587", underlying=symbol, strike=587.0, expiration=expiration, right=OptionRight.PUT,
            bid=0.75, ask=0.85, last=0.8, volume=500, open_interest=1000, delta=-0.08, iv=0.20,
            underlying_price=605.0, timestamp=now, source="tradier",
        ),
    ]
    return OptionChain(underlying=underlying, contracts=contracts, timestamp=now, source="tradier")


class _DteFakeProvider(DteWindowOptionChainProvider):
    """Implements `DteWindowOptionChainProvider` (so `isinstance`
    checks in `run_diagnostic_scan` take the DTE-aware branch) and
    `MarketDataProvider`'s own two methods. Records every call so tests
    can assert exactly which retrieval path was used."""

    def __init__(self, chain_by_ticker: dict[str, OptionChain]):
        self._chain_by_ticker = chain_by_ticker
        self.dte_window_calls: list[tuple[str, int, int]] = []
        self.plain_calls: list[str] = []
        self.closed = False

    async def get_option_chain_for_dte_window(
        self, symbol: str, *, min_dte: int, max_dte: int, as_of: date, diagnostics: DteWindowSelectionDiagnostics | None = None,
    ) -> OptionChain:
        self.dte_window_calls.append((symbol, min_dte, max_dte))
        return self._chain_by_ticker[symbol]

    async def get_option_chain(self, symbol: str) -> OptionChain:
        self.plain_calls.append(symbol)
        return self._chain_by_ticker[symbol]

    async def get_underlying_quote(self, symbol: str) -> UnderlyingQuote:
        return self._chain_by_ticker[symbol].underlying

    async def close(self) -> None:
        self.closed = True


class _LaggyDteFakeProvider(DteWindowOptionChainProvider):
    """Mirrors `tests/acceptance/test_opportunity_evaluation_timestamp.py
    ::FakeLaggyMarketDataProvider`'s own pattern exactly: every chain is
    stamped with the REAL current instant AT THE MOMENT the fetch call
    executes -- never an artificially fixed offset -- which is naturally
    some nonzero real wall-clock time after the script's own pre-fetch
    `now`, precisely the ordering that broke the pre-V1.5.7 validator.
    `evaluation_as_of` (captured strictly after the whole fetch loop
    completes) must dominate every such chain timestamp by construction,
    not by chance."""

    def __init__(self, symbols: tuple[str, ...]):
        self._symbols = symbols
        self.closed = False

    async def get_option_chain_for_dte_window(
        self, symbol: str, *, min_dte: int, max_dte: int, as_of: date, diagnostics: DteWindowSelectionDiagnostics | None = None,
    ) -> OptionChain:
        return _spy_pcs_chain(datetime.now(timezone.utc), symbol=symbol)

    async def get_option_chain(self, symbol: str) -> OptionChain:
        return _spy_pcs_chain(datetime.now(timezone.utc), symbol=symbol)

    async def get_underlying_quote(self, symbol: str) -> UnderlyingQuote:
        return _spy_pcs_chain(datetime.now(timezone.utc), symbol=symbol).underlying

    async def close(self) -> None:
        self.closed = True


def _write_pcs_universe(environment_path: Path) -> None:
    """Overrides the `environment` fixture's default universe.yaml
    (CASH_SECURED_PUT only) with a PUT_CREDIT_SPREAD-eligible one --
    `load_universe`/`load_universe_strategies` read the file fresh on
    every call, so this takes effect immediately for the next call."""
    (environment_path / "universe.yaml").write_text(
        yaml.safe_dump({"tickers": [{"ticker": "SPY", "sector": "ETF"}], "strategies": ["PUT_CREDIT_SPREAD"]})
    )


async def _record(label: str, called: list) -> bool:
    """Awaited stand-in body for `run_preflight`/`run_validation_cycle`/
    `run_diagnostic_scan` in dispatch tests -- `main()` always calls these
    via `asyncio.run(...)`, so a plain (non-async) lambda returning `True`
    directly fails with "a coroutine was expected, got True". A lambda
    that instead calls this async function returns the coroutine object
    `asyncio.run` needs, recording `label` in `called` once awaited."""
    called.append(label)
    return True


def _body_source_without_docstring(func) -> str:
    """`inspect.getsource(func)` includes the function's own docstring --
    `run_diagnostic_scan`'s docstring deliberately NAMES every forbidden
    store/function (to document that none of them appear in the body),
    which would make a naive substring scan over the whole source find
    its own documentation and false-positive. This strips the leading
    docstring statement (via `ast`) and returns only the executable body
    so item G's negative-capability scan below checks real code, not
    prose about real code."""
    source = inspect.getsource(func)
    tree = ast.parse(source)
    body = tree.body[0].body
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0].value, "value", None), str):
        body = body[1:]
    segments = [ast.get_source_segment(source, stmt) for stmt in body]
    return "\n".join(s for s in segments if s)


def _tradier_configured(monkeypatch) -> None:
    monkeypatch.setenv("OPTIONS_AGENT_DATA_PROVIDER", "tradier")
    monkeypatch.setenv("OPTIONS_AGENT_TRADIER_TOKEN", "fake-token-for-tests-only")
    monkeypatch.delenv("OPTIONS_AGENT_TRADIER_BASE_URL", raising=False)


# --------------------------------------------------------------- A/B/C/D: CLI dispatch


class TestHelpSafety:
    """Item A: `--help` never runs the diagnostic (or anything else)."""

    def test_help_shows_diagnostic_scan_flag_and_mutates_nothing(self, tmp_path):
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--help"], cwd=tmp_path, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        assert "--diagnostic-scan" in result.stdout
        assert list(tmp_path.glob("**/*.db")) == []

    def test_help_never_calls_run_diagnostic_scan(self, monkeypatch):
        cycle = _load_script_module("_v1513_help_module", SCRIPT_PATH)
        called = []
        monkeypatch.setattr(cycle, "run_diagnostic_scan", lambda **kw: _record("diag", called))
        monkeypatch.setattr(cycle, "run_validation_cycle", lambda **kw: _record("cycle", called))
        monkeypatch.setattr(cycle, "run_preflight", lambda: _record("preflight", called))
        monkeypatch.setattr(sys, "argv", ["run_validation_cycle.py", "--help"])
        with pytest.raises(SystemExit) as exc_info:
            cycle.main()
        assert exc_info.value.code == 0
        assert called == []


class TestPreflightSafety:
    """Item B: `--preflight` remains read-only, and never runs the
    diagnostic path."""

    def test_preflight_never_calls_run_diagnostic_scan(self, monkeypatch):
        cycle = _load_script_module("_v1513_preflight_module", SCRIPT_PATH)
        called = []
        monkeypatch.setattr(cycle, "run_diagnostic_scan", lambda **kw: _record("diag", called))
        monkeypatch.setattr(cycle, "run_preflight", lambda: _record("preflight", called))
        monkeypatch.setattr(sys, "argv", ["run_validation_cycle.py", "--preflight"])
        exit_code = cycle.main()
        assert exit_code == 0
        assert called == ["preflight"]


class TestDiagnosticScanDispatch:
    """Item C: `--diagnostic-scan` invokes ONLY the diagnostic path."""

    def test_diagnostic_scan_calls_only_run_diagnostic_scan(self, monkeypatch):
        cycle = _load_script_module("_v1513_dispatch_module", SCRIPT_PATH)
        called = []
        monkeypatch.setattr(cycle, "run_diagnostic_scan", lambda **kw: _record("diag", called))
        monkeypatch.setattr(cycle, "run_validation_cycle", lambda **kw: _record("cycle", called))
        monkeypatch.setattr(cycle, "run_preflight", lambda: _record("preflight", called))
        monkeypatch.setattr(sys, "argv", ["run_validation_cycle.py", "--diagnostic-scan"])
        exit_code = cycle.main()
        assert exit_code == 0
        assert called == ["diag"]

    def test_diagnostic_scan_mutually_exclusive_with_preflight(self, tmp_path):
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--preflight", "--diagnostic-scan"],
            cwd=tmp_path, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode != 0
        assert list(tmp_path.glob("**/*.db")) == []


class TestOfficialDefaultPathUnchanged:
    """Item D: no arguments still means the existing official,
    state-mutating cycle, and nothing else."""

    def test_no_arguments_calls_only_run_validation_cycle(self, monkeypatch):
        cycle = _load_script_module("_v1513_default_module", SCRIPT_PATH)
        called = []
        monkeypatch.setattr(cycle, "run_diagnostic_scan", lambda **kw: _record("diag", called))
        monkeypatch.setattr(cycle, "run_validation_cycle", lambda **kw: _record("cycle", called))
        monkeypatch.setattr(cycle, "run_preflight", lambda: _record("preflight", called))
        monkeypatch.setattr(sys, "argv", ["run_validation_cycle.py"])
        exit_code = cycle.main()
        assert exit_code == 0
        assert called == ["cycle"]


# --------------------------------------------------------------- E: provider preflight


@pytest.mark.asyncio
class TestDiagnosticProviderPreflight:
    """Item E: the diagnostic refuses mock/sandbox/missing-token, and
    accepts a correctly-configured Tradier-production environment."""

    async def test_mock_provider_refused(self, environment, monkeypatch):
        monkeypatch.setenv("OPTIONS_AGENT_DATA_PROVIDER", "mock")
        monkeypatch.delenv("OPTIONS_AGENT_TRADIER_TOKEN", raising=False)
        cycle = _load_script_module("_v1513_mock_rejected", SCRIPT_PATH)
        ok = await cycle.run_diagnostic_scan()
        assert ok is False

    async def test_missing_token_refused(self, environment, monkeypatch):
        monkeypatch.setenv("OPTIONS_AGENT_DATA_PROVIDER", "tradier")
        monkeypatch.delenv("OPTIONS_AGENT_TRADIER_TOKEN", raising=False)
        cycle = _load_script_module("_v1513_no_token_rejected", SCRIPT_PATH)
        ok = await cycle.run_diagnostic_scan()
        assert ok is False

    async def test_sandbox_host_refused(self, environment, monkeypatch):
        monkeypatch.setenv("OPTIONS_AGENT_DATA_PROVIDER", "tradier")
        monkeypatch.setenv("OPTIONS_AGENT_TRADIER_TOKEN", "fake-token-for-tests-only")
        monkeypatch.setenv("OPTIONS_AGENT_TRADIER_BASE_URL", "https://sandbox.tradier.com/v1")
        cycle = _load_script_module("_v1513_sandbox_rejected", SCRIPT_PATH)
        ok = await cycle.run_diagnostic_scan()
        assert ok is False

    async def test_tradier_production_accepted(self, environment, monkeypatch):
        _tradier_configured(monkeypatch)
        now = datetime.now(timezone.utc)
        cycle = _load_script_module("_v1513_tradier_accepted", SCRIPT_PATH)
        provider = _DteFakeProvider({"SPY": _spy_pcs_chain(now)})
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)
        ok = await cycle.run_diagnostic_scan(now=now)
        assert ok is True


# --------------------------------------------------------------- F: market-hours gate


@pytest.mark.asyncio
class TestDiagnosticMarketHoursGate:
    """Item F: closed gate -> zero provider calls, zero persistence;
    open gate -> the diagnostic may fetch."""

    async def test_closed_gate_makes_no_provider_calls_and_no_persistence(self, environment, monkeypatch):
        _tradier_configured(monkeypatch)
        now = datetime.now(timezone.utc)
        cycle = _load_script_module("_v1513_gate_closed", SCRIPT_PATH)

        constructed = []

        def _never_construct():
            constructed.append(True)
            raise AssertionError("diagnostic must not construct a provider while the gate is closed")

        monkeypatch.setattr(cycle, "get_configured_market_data_provider", _never_construct)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        ok = await cycle.run_diagnostic_scan(now=now)
        assert ok is False
        assert constructed == []

        ops = operations_config_module.load_operations_config()
        from src.portfolio.account_state import SqlitePortfolioStore

        assert SqlitePortfolioStore(ops.account_state_db_path).get(ops.account_id) is None

    async def test_open_gate_fetches(self, environment, monkeypatch):
        _tradier_configured(monkeypatch)
        now = datetime.now(timezone.utc)
        cycle = _load_script_module("_v1513_gate_open", SCRIPT_PATH)
        provider = _DteFakeProvider({"SPY": _spy_pcs_chain(now)})
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)

        ok = await cycle.run_diagnostic_scan(now=now)
        assert ok is True
        assert len(provider.dte_window_calls) == 1
        assert provider.closed is True


# --------------------------------------------------------------- G: no persistence


class TestDiagnosticSourceNeverReferencesForbiddenPersistence:
    """Item G (static half): a source-level negative-capability check,
    mirroring `src.validation.freeze`'s own `_verify_*` style -- not
    marked `asyncio` since it makes no awaited call."""

    def test_source_never_mentions_forbidden_persistence_paths(self):
        cycle = _load_script_module("_v1513_source_scan", SCRIPT_PATH)
        source = _body_source_without_docstring(cycle.run_diagnostic_scan)
        for forbidden in (
            # Class/store/function NAMES that must never be referenced at
            # all (constructed, imported, or called) -- none of these are
            # ever used as plain English words in this function's own
            # operator-facing print statements, so a bare substring check
            # is safe for them.
            "SqliteControlLoopStore", "SqliteLifecycleStore",
            "SqliteCandidateReviewStore", "SqliteIdempotencyStore", "SqliteValidationStore",
            "SqlitePortfolioStore", "run_outer_cycle", "run_control_cycle",
            # These two DO legitimately appear as plain prose inside this
            # function's own operator-facing print statements (explaining
            # that NEITHER happens) -- so the check below is deliberately
            # for the CALL/CONSTRUCTOR form only, never the bare name.
            "PaperBroker(", "ReviewedCandidate(", "confirm_candidate(",
            ".save(",
        ):
            assert forbidden not in source, f"run_diagnostic_scan must never reference {forbidden!r}"


@pytest.mark.asyncio
class TestDiagnosticNeverPersists:
    """Item G: the diagnostic never saves a portfolio, account state,
    candidate, cycle record, lifecycle record, validation snapshot, or
    cohort-progress change, and never places/confirms an order."""

    async def test_sqliteportfoliostore_is_never_constructed_at_all(self, environment, monkeypatch):
        """PAPER_TRADING_V1.5.13 acceptance correction, item G: the
        diagnostic must not even CONSTRUCT `SqlitePortfolioStore` -- its
        own `__init__` was found by this release's acceptance audit to
        be capable of writing a missing database/table into existence
        (`CREATE TABLE IF NOT EXISTS`, confirmed empirically to grow a
        0-byte file to 12,288 bytes). Monkeypatching `.save` alone (the
        pre-correction version of this test) was insufficient proof --
        it never ruled out `__init__` itself writing. Monkeypatching
        `__init__` to raise is the strongest available proof that the
        diagnostic never instantiates this class at all."""
        _tradier_configured(monkeypatch)
        now = datetime.now(timezone.utc)
        cycle = _load_script_module("_v1513_no_portfolio_store_construct", SCRIPT_PATH)
        provider = _DteFakeProvider({"SPY": _spy_pcs_chain(now)})
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)

        from src.portfolio.account_state import SqlitePortfolioStore

        def _construction_must_never_happen(self, db_path):
            raise AssertionError("SqlitePortfolioStore must never be constructed by the diagnostic path")

        monkeypatch.setattr(SqlitePortfolioStore, "__init__", _construction_must_never_happen)

        ok = await cycle.run_diagnostic_scan(now=now)
        assert ok is True  # if SqlitePortfolioStore(...) had been called, the monkeypatched raise would have propagated

    async def test_portfolio_store_save_is_never_actually_called(self, environment, monkeypatch):
        _tradier_configured(monkeypatch)
        now = datetime.now(timezone.utc)
        cycle = _load_script_module("_v1513_save_spy", SCRIPT_PATH)
        provider = _DteFakeProvider({"SPY": _spy_pcs_chain(now)})
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)

        from src.portfolio.account_state import SqlitePortfolioStore

        def _save_must_never_be_called(self, account_id, portfolio):
            raise AssertionError("SqlitePortfolioStore.save must never be called by the diagnostic path")

        monkeypatch.setattr(SqlitePortfolioStore, "save", _save_must_never_be_called)

        ok = await cycle.run_diagnostic_scan(now=now)
        assert ok is True  # if .save() had been called, the monkeypatched raise would have propagated

    async def test_no_candidate_review_or_idempotency_record_created(self, environment, monkeypatch):
        _write_pcs_universe(environment)
        _tradier_configured(monkeypatch)
        now = datetime.now(timezone.utc)
        cycle = _load_script_module("_v1513_no_review_record", SCRIPT_PATH)
        provider = _DteFakeProvider({"SPY": _spy_pcs_chain(now)})
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)

        ok = await cycle.run_diagnostic_scan(now=now)
        assert ok is True

        ops = operations_config_module.load_operations_config()
        from src.brokers.base import SqliteIdempotencyStore
        from src.review.candidates import SqliteCandidateReviewStore

        assert SqliteCandidateReviewStore(ops.candidate_review_db_path).all_candidates(cohort_id=COHORT_ID) == []
        assert SqliteIdempotencyStore(ops.account_state_db_path).all() == []


# --------------------------------------------------------------- H: operational DB integrity


@pytest.mark.asyncio
class TestOperationalDbIntegrity:
    """Item H: the operational DB file is byte-identical before and
    after a diagnostic run, when the table it reads already exists
    (exactly the currently-active cohort's real situation)."""

    async def test_db_file_hash_unchanged_across_a_diagnostic_run(self, environment, monkeypatch):
        _tradier_configured(monkeypatch)
        now = datetime.now(timezone.utc)
        cycle = _load_script_module("_v1513_db_hash", SCRIPT_PATH)
        provider = _DteFakeProvider({"SPY": _spy_pcs_chain(now)})
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)

        ops = operations_config_module.load_operations_config()
        from src.portfolio.account_state import SqlitePortfolioStore

        # Pre-seed the table (CREATE TABLE IF NOT EXISTS becomes a true
        # no-op) so this proves zero WRITES, not merely "the table
        # didn't need creating yet" -- the currently-active cohort's
        # real db is already in exactly this state.
        SqlitePortfolioStore(ops.account_state_db_path)

        db_path = Path(ops.account_state_db_path)
        before = hashlib.sha256(db_path.read_bytes()).hexdigest()

        ok = await cycle.run_diagnostic_scan(now=now)
        assert ok is True

        after = hashlib.sha256(db_path.read_bytes()).hexdigest()
        assert before == after, "the operational DB file must be byte-identical before and after a diagnostic run"


# --------------------------------------------------------------- I: V1.5.12 regression


@pytest.mark.asyncio
class TestV1512ProposalIdRegressionViaDiagnostic:
    """Item I: a representative PCS that previously produced a
    >64-character double-dated ID reaches normal construction, with no
    ValidationError, exactly one scan date in its id, and length <= 64."""

    async def test_pcs_constructs_cleanly_with_a_valid_short_proposal_id(self, environment, monkeypatch, capsys):
        _write_pcs_universe(environment)
        _tradier_configured(monkeypatch)
        now = datetime.now(timezone.utc)
        cycle = _load_script_module("_v1513_v1512_regression", SCRIPT_PATH)
        provider = _DteFakeProvider({"SPY": _spy_pcs_chain(now)})
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)

        ok = await cycle.run_diagnostic_scan(now=now)
        assert ok is True

        out = capsys.readouterr().out
        assert "generation exceptions: 0" in out
        assert "ValidationError" not in out
        assert "DIAGNOSTIC CANDIDATE SURVIVED QUANT/RISK" in out

        today_iso = now.date().isoformat()
        assert out.count(today_iso) >= 1
        # Extract the printed proposal_id length and assert it is <= 64.
        for line in out.splitlines():
            if "proposal_id length" in line:
                length = int(line.strip().split(":")[-1].strip())
                assert length <= 64
                break
        else:
            pytest.fail("expected a printed 'proposal_id length' line")


# --------------------------------------------------------------- J: exact pipeline reuse


@pytest.mark.asyncio
class TestExactPipelineReuse:
    """Item J: the diagnostic calls the REAL, unmodified
    `scan_and_rank_opportunities` -- never a reimplemented screen --
    and never `run_outer_cycle`/`run_control_cycle`."""

    async def test_scan_and_rank_opportunities_is_called_directly(self, environment, monkeypatch):
        _write_pcs_universe(environment)
        _tradier_configured(monkeypatch)
        now = datetime.now(timezone.utc)
        cycle = _load_script_module("_v1513_exact_pipeline", SCRIPT_PATH)
        provider = _DteFakeProvider({"SPY": _spy_pcs_chain(now)})
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)

        real_scan = cycle.scan_and_rank_opportunities
        calls = []

        def _spy_scan(*args, **kwargs):
            calls.append((args, kwargs))
            return real_scan(*args, **kwargs)

        monkeypatch.setattr(cycle, "scan_and_rank_opportunities", _spy_scan)
        monkeypatch.setattr(
            cycle, "run_outer_cycle",
            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("run_outer_cycle must never be called by the diagnostic")),
        )

        ok = await cycle.run_diagnostic_scan(now=now)
        assert ok is True
        assert len(calls) == 1
        assert calls[0][1]["proposal_id_prefix"] == "validation-scan"


# --------------------------------------------------------------- K: DTE-aware retrieval


@pytest.mark.asyncio
class TestDteAwareRetrieval:
    """Item K: the diagnostic uses the configured QuantFilterConfig
    min_dte/max_dte via the DTE-window retrieval path, never the
    provider's nearest-N default."""

    async def test_dte_window_called_with_configured_bounds_never_plain_get_option_chain(self, environment, monkeypatch):
        _tradier_configured(monkeypatch)
        now = datetime.now(timezone.utc)
        cycle = _load_script_module("_v1513_dte_bounds", SCRIPT_PATH)
        provider = _DteFakeProvider({"SPY": _spy_pcs_chain(now)})
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)

        ok = await cycle.run_diagnostic_scan(now=now)
        assert ok is True
        assert provider.dte_window_calls == [("SPY", 20, 45)]
        assert provider.plain_calls == []


# --------------------------------------------------------------- L: post-fetch timestamp


@pytest.mark.asyncio
class TestPostFetchEvaluationTimestamp:
    """Item L: the evaluation timestamp is captured after the fetch
    loop completes, so a chain timestamped with a later instant than
    the pre-fetch `now` still produces a valid TradeProposal (the exact
    PAPER_TRADING_V1.5.7 fix, re-proven for the diagnostic path)."""

    async def test_chain_timestamped_after_pre_fetch_now_still_produces_a_valid_candidate(self, environment, monkeypatch, capsys):
        _write_pcs_universe(environment)
        _tradier_configured(monkeypatch)
        pre_fetch_now = datetime.now(timezone.utc)
        cycle = _load_script_module("_v1513_post_fetch_ts", SCRIPT_PATH)
        provider = _LaggyDteFakeProvider(("SPY",))
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)

        ok = await cycle.run_diagnostic_scan(now=pre_fetch_now)
        assert ok is True
        out = capsys.readouterr().out
        assert "ValidationError" not in out
        assert "DIAGNOSTIC CANDIDATE SURVIVED QUANT/RISK" in out


# --------------------------------------------------------------- M: candidate survivor


@pytest.mark.asyncio
class TestCandidateSurvivorIsDiagnosticOnly:
    """Item M: a surviving candidate may be printed, but is never
    persisted, never made confirmable, and no order/fill occurs."""

    async def test_survivor_is_printed_but_not_persisted_or_confirmable(self, environment, monkeypatch, capsys):
        _write_pcs_universe(environment)
        _tradier_configured(monkeypatch)
        now = datetime.now(timezone.utc)
        cycle = _load_script_module("_v1513_survivor", SCRIPT_PATH)
        provider = _DteFakeProvider({"SPY": _spy_pcs_chain(now)})
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)

        ok = await cycle.run_diagnostic_scan(now=now)
        assert ok is True

        out = capsys.readouterr().out
        assert "DIAGNOSTIC CANDIDATE SURVIVED QUANT/RISK (NOT PERSISTED, NOT CONFIRMABLE)" in out
        assert "confirm_candidate.py" not in out

        ops = operations_config_module.load_operations_config()
        from src.brokers.base import SqliteIdempotencyStore
        from src.review.candidates import SqliteCandidateReviewStore

        assert SqliteCandidateReviewStore(ops.candidate_review_db_path).all_candidates(cohort_id=COHORT_ID) == []
        assert SqliteIdempotencyStore(ops.account_state_db_path).all() == []


# --------------------------------------------------------------- N: repeated diagnostic


@pytest.mark.asyncio
class TestRepeatedDiagnosticNeverBlocksTheOfficialCycle:
    """Item N: two diagnostic runs must not consume an idempotency slot
    or prevent the next official daily cycle from running normally."""

    async def test_two_diagnostic_runs_then_the_official_cycle_still_runs_normally(self, environment, monkeypatch):
        _tradier_configured(monkeypatch)
        now = datetime.now(timezone.utc)
        cycle = _load_script_module("_v1513_repeated_then_official", SCRIPT_PATH)
        diag_provider = _DteFakeProvider({"SPY": _spy_pcs_chain(now)})
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: diag_provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)

        assert await cycle.run_diagnostic_scan(now=now) is True
        assert await cycle.run_diagnostic_scan(now=now) is True

        ops = operations_config_module.load_operations_config()
        from src.portfolio.persistence import SqliteControlLoopStore

        control_loop_store = SqliteControlLoopStore(ops.control_loop_db_path)
        main_cycle_id = f"validation-{now.date().isoformat()}"
        assert control_loop_store.get_cycle_record(main_cycle_id) is None, (
            "two diagnostic runs must never create the official cycle's own cycle record"
        )

        # The official cycle, with its own (unmodified) FakeMarketDataProvider,
        # must still run exactly as test_review_only_daily_cycle.py proves.
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: FakeMarketDataProvider())
        ok = await cycle.run_validation_cycle()
        assert ok is True
        assert control_loop_store.get_cycle_record(main_cycle_id) is not None


# --------------------------------------------------------------- O: official behavior equivalence


class TestOfficialBehaviorEquivalence:
    """Item O: adding `--diagnostic-scan` (and the purely-additive
    `on_generation_exception` parameter it threads through
    `generate_candidates`/`scan_and_rank_opportunities`) must not change
    candidate ranking, Quant/Risk decisions, or any other part of the
    official path's own behavior. The full acceptance suites
    (`test_review_only_daily_cycle.py`, `test_run_validation_cycle_cli.py`,
    `test_opportunity_evaluation_timestamp.py`, etc.) already exercise
    the official CLI entry points end-to-end and continue to pass
    unmodified -- this test additionally proves, directly at the real
    library call sites `run_diagnostic_scan`/`run_validation_cycle` both
    route through, that supplying the new optional parameter is
    byte-for-byte behavior-neutral: every official call site omits it
    (passes `None`), so this is the exact comparison that matters."""

    def test_scan_and_rank_opportunities_is_unchanged_by_the_new_optional_parameter(self):
        from src.llm.schemas import StrategyType
        from src.portfolio.opportunity_scan import scan_and_rank_opportunities
        from src.risk.broker_constraints import load_broker_capabilities
        from src.risk.limits import get_default_limits
        from src.risk.portfolio_risk import Portfolio
        from src.workflows.candidate_generation import QuantFilterConfig, UniverseEntry

        now = datetime.now(timezone.utc)
        universe = [UniverseEntry(ticker="SPY", sector="ETF")]
        chains = {"SPY": _spy_pcs_chain(now)}
        limits = get_default_limits()
        portfolio = Portfolio(as_of=now, nav=100_000.0, cash=100_000.0, peak_equity=100_000.0)
        caps = load_broker_capabilities("internal_paper")

        without_callback = scan_and_rank_opportunities(
            universe, chains, [StrategyType.PUT_CREDIT_SPREAD], QuantFilterConfig(), limits, portfolio,
            "normal", caps, now=now, proposal_id_prefix="validation-scan",
        )
        seen: list[tuple[str, str, str]] = []
        with_callback = scan_and_rank_opportunities(
            universe, chains, [StrategyType.PUT_CREDIT_SPREAD], QuantFilterConfig(), limits, portfolio,
            "normal", caps, now=now, proposal_id_prefix="validation-scan",
            on_generation_exception=lambda t, s, exc: seen.append((t, s, repr(exc))),
        )

        assert len(without_callback.scanned) == len(with_callback.scanned) == 1
        assert without_callback.scanned[0].candidate.proposal.proposal_id == with_callback.scanned[0].candidate.proposal.proposal_id
        assert without_callback.scanned[0].risk_decision == with_callback.scanned[0].risk_decision
        assert without_callback.scanned[0].risk_adjusted_return == with_callback.scanned[0].risk_adjusted_return
        assert without_callback.best is not None and with_callback.best is not None
        assert without_callback.best.candidate.proposal.proposal_id == with_callback.best.candidate.proposal.proposal_id
        assert without_callback.no_trade_reason == with_callback.no_trade_reason == None
        assert seen == [], "the new callback must never fire for a candidate that generates without error"


# --------------------------------------------------------------- I: official cycle still uses SqlitePortfolioStore


@pytest.mark.asyncio
class TestOfficialCycleStillUsesSqlitePortfolioStoreUnchanged:
    """Acceptance-correction item I: the official, no-argument
    `run_validation_cycle()` path must keep bootstrapping/persisting
    the portfolio through the existing, durable `SqlitePortfolioStore`
    exactly as before -- the new read-only loader is diagnostic-only,
    and must never be substituted into the official path's own
    behavior merely to satisfy this release's safety guarantee."""

    async def test_official_cycle_constructs_and_saves_through_sqliteportfoliostore(self, environment, monkeypatch):
        _tradier_configured(monkeypatch)
        cycle = _load_script_module("_v1513_official_still_sqlite", SCRIPT_PATH)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: FakeMarketDataProvider())
        monkeypatch.setattr(
            cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED,
        )

        from src.portfolio.account_state import SqlitePortfolioStore

        constructed = []
        real_init = SqlitePortfolioStore.__init__

        def _spy_init(self, db_path):
            constructed.append(db_path)
            return real_init(self, db_path)

        monkeypatch.setattr(SqlitePortfolioStore, "__init__", _spy_init)

        ok = await cycle.run_validation_cycle()
        assert ok is True
        assert len(constructed) >= 1, "the official cycle must still construct SqlitePortfolioStore exactly as before"

        ops = operations_config_module.load_operations_config()
        persisted = SqlitePortfolioStore(ops.account_state_db_path).get(ops.account_id)
        assert persisted is not None, "the official cycle must still persist a Portfolio via SqlitePortfolioStore.save"
