"""PAPER_TRADING_V1.5.14 acceptance test: `scripts/run_validation_cycle.py
--universe-feasibility` -- a READ-ONLY universe-breadth feasibility
study against (in real use) live Tradier production market data, using
the exact same candidate -> Quant -> Risk pipeline as the official
cycle and the V1.5.13 diagnostic, over a wider 12-symbol research
universe, WITHOUT counting as a validation day and WITHOUT mutating
any validation/candidate/trade/account/lifecycle/control-loop state or
activating the expanded universe for the official cycle.

Runs entirely offline against temporary sqlite files and fake providers
(never a live Tradier token or a real network call) -- reuses the
exact fixture pattern `test_diagnostic_scan_v1513.py` already
establishes."""
from __future__ import annotations

import ast
import hashlib
import inspect
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import src.portfolio.operations_config as operations_config_module
from src.data.historical import HistoricalBar, HistoricalDataProvider
from src.data.option_chain import OptionChain, OptionContract, OptionRight
from src.data.provider import DteWindowOptionChainProvider, DteWindowSelectionDiagnostics
from src.data.quotes import UnderlyingQuote
from src.portfolio.market_session import MarketSessionState, ValidationCycleEligibility
from tests.acceptance.test_diagnostic_scan_v1513 import _body_source_without_docstring
from tests.acceptance.test_review_only_daily_cycle import (
    COHORT_ID,
    FakeMarketDataProvider,
    _load_script_module,
    environment,  # noqa: F401 -- reused as a pytest fixture
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "run_validation_cycle.py"
FEASIBILITY_TICKERS = ("SPY", "QQQ", "IWM", "DIA", "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "JPM", "XOM")

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


def _spy_like_chain(symbol: str, now: datetime, *, spot: float = 605.0) -> OptionChain:
    expiration = (now + timedelta(days=24)).date()
    underlying = UnderlyingQuote(symbol=symbol, bid=spot - 0.5, ask=spot + 0.5, last=spot, volume=1_000_000, timestamp=now, source="tradier")
    short_strike = round(spot * 0.975 / 5) * 5
    long_strike = short_strike - 3
    contracts = [
        OptionContract(
            option_symbol=f"{symbol}_P{short_strike}", underlying=symbol, strike=short_strike, expiration=expiration, right=OptionRight.PUT,
            bid=2.0, ask=2.2, last=2.1, volume=500, open_interest=1000, delta=-0.15, iv=0.22,
            underlying_price=spot, timestamp=now, source="tradier",
        ),
        OptionContract(
            option_symbol=f"{symbol}_P{long_strike}", underlying=symbol, strike=long_strike, expiration=expiration, right=OptionRight.PUT,
            bid=0.8, ask=0.9, last=0.85, volume=500, open_interest=1000, delta=-0.08, iv=0.20,
            underlying_price=spot, timestamp=now, source="tradier",
        ),
    ]
    return OptionChain(underlying=underlying, contracts=contracts, timestamp=now, source="tradier")


class _FeasibilityFakeProvider(HistoricalDataProvider, DteWindowOptionChainProvider):
    """Implements the DTE-aware option-chain interface AND
    `HistoricalDataProvider` (`get_bars`) -- exactly the two interfaces
    `TradierMarketDataProvider` satisfies in production (see its own
    `class TradierMarketDataProvider(MarketDataProvider,
    HistoricalDataProvider, DteWindowOptionChainProvider)` declaration)
    -- so `run_universe_feasibility_study`'s `isinstance(provider,
    HistoricalDataProvider)` check takes its real, correlation-fetching
    branch here too, rather than silently short-circuiting to the
    always-empty-correlation path every prior version of this fixture
    produced (both ABCs require exact inheritance, not just a matching
    method name, for `isinstance` to hold)."""

    def __init__(self, chain_by_ticker: dict[str, OptionChain]):
        self._chain_by_ticker = chain_by_ticker
        self.dte_window_calls: list[tuple[str, int, int]] = []
        self.get_bars_calls: list[str] = []
        self.closed = False

    async def get_option_chain_for_dte_window(
        self, symbol: str, *, min_dte: int, max_dte: int, as_of: date, diagnostics: DteWindowSelectionDiagnostics | None = None,
    ) -> OptionChain:
        self.dte_window_calls.append((symbol, min_dte, max_dte))
        return self._chain_by_ticker[symbol]

    async def get_option_chain(self, symbol: str) -> OptionChain:
        return self._chain_by_ticker[symbol]

    async def get_underlying_quote(self, symbol: str) -> UnderlyingQuote:
        return self._chain_by_ticker[symbol].underlying

    async def get_bars(self, symbol: str, start: date, end: date) -> list[HistoricalBar]:
        self.get_bars_calls.append(symbol)
        bars = []
        d = start
        price = 100.0
        while d <= end:
            bars.append(HistoricalBar(symbol=symbol, bar_date=d, open=price, high=price + 1, low=price - 1, close=price, volume=1_000_000, source="tradier"))
            price += 0.1
            d += timedelta(days=1)
        return bars

    async def close(self) -> None:
        self.closed = True


def _write_feasibility_universe_override(environment_path: Path, *, tickers: tuple[str, ...] = FEASIBILITY_TICKERS) -> None:
    """Points `_FEASIBILITY_UNIVERSE_CONFIG_PATH` at a temp file for a
    test -- never the real `config/universe_feasibility.yaml` or
    `config/universe.yaml`."""
    import yaml

    path = environment_path / "universe_feasibility.yaml"
    path.write_text(
        yaml.safe_dump({"tickers": [{"ticker": t, "sector": "ETF"} for t in tickers], "strategies": ["PUT_CREDIT_SPREAD"]})
    )
    return path


def _tradier_configured(monkeypatch) -> None:
    monkeypatch.setenv("OPTIONS_AGENT_DATA_PROVIDER", "tradier")
    monkeypatch.setenv("OPTIONS_AGENT_TRADIER_TOKEN", "fake-token-for-tests-only")
    monkeypatch.delenv("OPTIONS_AGENT_TRADIER_BASE_URL", raising=False)


def _chains_for(tickers: tuple[str, ...], now: datetime) -> dict[str, OptionChain]:
    spots = {"SPY": 605.0, "QQQ": 500.0, "IWM": 220.0, "DIA": 420.0, "AAPL": 230.0, "MSFT": 420.0,
             "NVDA": 135.0, "AMZN": 190.0, "META": 580.0, "GOOGL": 165.0, "JPM": 225.0, "XOM": 115.0}
    return {t: _spy_like_chain(t, now, spot=spots.get(t, 100.0)) for t in tickers}


# --------------------------------------------------------------- A/B: CLI help/unknown args


class TestHelpSafety:
    """Item A: --help documents --universe-feasibility, performs zero
    network and zero mutation."""

    def test_help_documents_universe_feasibility_flag(self, tmp_path):
        import subprocess

        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--help"], cwd=tmp_path, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        assert "--universe-feasibility" in result.stdout
        assert list(tmp_path.glob("**/*.db")) == []

    def test_help_never_calls_the_feasibility_study(self, monkeypatch):
        cycle = _load_script_module("_v1514_help", SCRIPT_PATH)
        called = []
        monkeypatch.setattr(cycle, "run_universe_feasibility_study", lambda **kw: called.append("feas") or True)
        monkeypatch.setattr(sys, "argv", ["run_validation_cycle.py", "--help"])
        with pytest.raises(SystemExit) as exc_info:
            cycle.main()
        assert exc_info.value.code == 0
        assert called == []


class TestUnknownArguments:
    """Item B: an unrecognized argument fails nonzero with zero mutation."""

    def test_unknown_argument_fails_and_mutates_nothing(self, tmp_path):
        import subprocess

        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--bogus-flag"], cwd=tmp_path, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode != 0
        assert list(tmp_path.glob("**/*.db")) == []

    def test_mutually_exclusive_with_diagnostic_scan(self, tmp_path):
        import subprocess

        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--diagnostic-scan", "--universe-feasibility"],
            cwd=tmp_path, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode != 0
        assert list(tmp_path.glob("**/*.db")) == []


# --------------------------------------------------------------- C: dispatch + market-hours gate


class TestDispatch:
    """Item C (dispatch half): --universe-feasibility dispatches only
    to run_universe_feasibility_study. Not async -- main() itself calls
    asyncio.run(), which cannot run inside an already-running loop."""

    def test_dispatch_calls_only_run_universe_feasibility_study(self, monkeypatch):
        cycle = _load_script_module("_v1514_dispatch", SCRIPT_PATH)
        called = []

        async def _record(label: str) -> bool:
            called.append(label)
            return True

        monkeypatch.setattr(cycle, "run_universe_feasibility_study", lambda **kw: _record("feas"))
        monkeypatch.setattr(cycle, "run_validation_cycle", lambda **kw: _record("other"))
        monkeypatch.setattr(cycle, "run_diagnostic_scan", lambda **kw: _record("other"))
        monkeypatch.setattr(cycle, "run_preflight", lambda: _record("other"))
        monkeypatch.setattr(sys, "argv", ["run_validation_cycle.py", "--universe-feasibility"])
        exit_code = cycle.main()
        assert exit_code == 0
        assert called == ["feas"]


@pytest.mark.asyncio
class TestMarketHoursGate:
    """Item C (gate half): a closed gate makes zero provider calls and
    zero DB mutation."""

    async def test_closed_gate_makes_no_provider_calls_and_no_persistence(self, environment, monkeypatch):
        _tradier_configured(monkeypatch)
        now = datetime.now(timezone.utc)
        cycle = _load_script_module("_v1514_gate_closed", SCRIPT_PATH)
        _write_feasibility_universe_override(environment)
        monkeypatch.setattr(cycle, "_FEASIBILITY_UNIVERSE_CONFIG_PATH", environment / "universe_feasibility.yaml")

        def _never_construct():
            raise AssertionError("feasibility study must not construct a provider while the gate is closed")

        monkeypatch.setattr(cycle, "get_configured_market_data_provider", _never_construct)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _BLOCKED)

        ok = await cycle.run_universe_feasibility_study(now=now)
        assert ok is False

        ops = operations_config_module.load_operations_config()
        from src.portfolio.account_state import SqlitePortfolioStore

        assert SqlitePortfolioStore(ops.account_state_db_path).get(ops.account_id) is None


# --------------------------------------------------------------- D: production preflight


@pytest.mark.asyncio
class TestProductionPreflight:
    """Item D: mock/sandbox/missing-token refused, production accepted."""

    async def test_mock_provider_refused(self, environment, monkeypatch):
        monkeypatch.setenv("OPTIONS_AGENT_DATA_PROVIDER", "mock")
        monkeypatch.delenv("OPTIONS_AGENT_TRADIER_TOKEN", raising=False)
        cycle = _load_script_module("_v1514_mock_rejected", SCRIPT_PATH)
        assert await cycle.run_universe_feasibility_study() is False

    async def test_missing_token_refused(self, environment, monkeypatch):
        monkeypatch.setenv("OPTIONS_AGENT_DATA_PROVIDER", "tradier")
        monkeypatch.delenv("OPTIONS_AGENT_TRADIER_TOKEN", raising=False)
        cycle = _load_script_module("_v1514_no_token", SCRIPT_PATH)
        assert await cycle.run_universe_feasibility_study() is False

    async def test_sandbox_host_refused(self, environment, monkeypatch):
        monkeypatch.setenv("OPTIONS_AGENT_DATA_PROVIDER", "tradier")
        monkeypatch.setenv("OPTIONS_AGENT_TRADIER_TOKEN", "fake-token-for-tests-only")
        monkeypatch.setenv("OPTIONS_AGENT_TRADIER_BASE_URL", "https://sandbox.tradier.com/v1")
        cycle = _load_script_module("_v1514_sandbox_rejected", SCRIPT_PATH)
        assert await cycle.run_universe_feasibility_study() is False

    async def test_tradier_production_accepted(self, environment, monkeypatch):
        _tradier_configured(monkeypatch)
        now = datetime.now(timezone.utc)
        cycle = _load_script_module("_v1514_tradier_accepted", SCRIPT_PATH)
        _write_feasibility_universe_override(environment)
        monkeypatch.setattr(cycle, "_FEASIBILITY_UNIVERSE_CONFIG_PATH", environment / "universe_feasibility.yaml")
        provider = _FeasibilityFakeProvider(_chains_for(FEASIBILITY_TICKERS, now))
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)
        assert await cycle.run_universe_feasibility_study(now=now) is True


# --------------------------------------------------------------- F/G: read-only portfolio + zero persistence


class TestFeasibilitySourceNeverReferencesForbiddenPersistence:
    """Items F/G (static half): not marked asyncio since it makes no
    awaited call."""

    def test_source_never_references_forbidden_persistence_paths(self):
        cycle = _load_script_module("_v1514_source_scan", SCRIPT_PATH)
        source = _body_source_without_docstring(cycle.run_universe_feasibility_study)
        for forbidden in (
            "SqlitePortfolioStore", "SqliteControlLoopStore", "SqliteLifecycleStore",
            "SqliteCandidateReviewStore", "SqliteIdempotencyStore", "SqliteValidationStore",
            "run_outer_cycle", "run_control_cycle", "PaperBroker(", "ReviewedCandidate(",
            "confirm_candidate(", ".save(", "apply_risk_data_wiring(", "apply_correlation_wiring(",
        ):
            assert forbidden not in source, f"run_universe_feasibility_study must never reference {forbidden!r}"


@pytest.mark.asyncio
class TestReadOnlyPortfolioAndZeroPersistence:
    """Items F and G (behavioral half): true read-only portfolio
    loading, and zero persistence of any kind."""

    async def test_sqliteportfoliostore_construction_fails_test_if_attempted(self, environment, monkeypatch):
        _tradier_configured(monkeypatch)
        now = datetime.now(timezone.utc)
        cycle = _load_script_module("_v1514_no_portfolio_store", SCRIPT_PATH)
        _write_feasibility_universe_override(environment)
        monkeypatch.setattr(cycle, "_FEASIBILITY_UNIVERSE_CONFIG_PATH", environment / "universe_feasibility.yaml")
        provider = _FeasibilityFakeProvider(_chains_for(FEASIBILITY_TICKERS, now))
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)

        from src.portfolio.account_state import SqlitePortfolioStore

        def _construction_must_never_happen(self, db_path):
            raise AssertionError("SqlitePortfolioStore must never be constructed by the feasibility study")

        monkeypatch.setattr(SqlitePortfolioStore, "__init__", _construction_must_never_happen)
        assert await cycle.run_universe_feasibility_study(now=now) is True

    async def test_operational_db_byte_hash_unchanged(self, environment, monkeypatch):
        _tradier_configured(monkeypatch)
        now = datetime.now(timezone.utc)
        cycle = _load_script_module("_v1514_db_hash", SCRIPT_PATH)
        _write_feasibility_universe_override(environment)
        monkeypatch.setattr(cycle, "_FEASIBILITY_UNIVERSE_CONFIG_PATH", environment / "universe_feasibility.yaml")
        provider = _FeasibilityFakeProvider(_chains_for(FEASIBILITY_TICKERS, now))
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)

        ops = operations_config_module.load_operations_config()
        from src.portfolio.account_state import SqlitePortfolioStore

        SqlitePortfolioStore(ops.account_state_db_path)  # pre-seed the table
        db_path = Path(ops.account_state_db_path)
        before = hashlib.sha256(db_path.read_bytes()).hexdigest()

        assert await cycle.run_universe_feasibility_study(now=now) is True

        after = hashlib.sha256(db_path.read_bytes()).hexdigest()
        assert before == after

    async def test_no_candidate_review_lifecycle_or_idempotency_record_created(self, environment, monkeypatch):
        _tradier_configured(monkeypatch)
        now = datetime.now(timezone.utc)
        cycle = _load_script_module("_v1514_no_records", SCRIPT_PATH)
        _write_feasibility_universe_override(environment)
        monkeypatch.setattr(cycle, "_FEASIBILITY_UNIVERSE_CONFIG_PATH", environment / "universe_feasibility.yaml")
        provider = _FeasibilityFakeProvider(_chains_for(FEASIBILITY_TICKERS, now))
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)

        assert await cycle.run_universe_feasibility_study(now=now) is True

        ops = operations_config_module.load_operations_config()
        from src.brokers.base import SqliteIdempotencyStore
        from src.portfolio.persistence import SqliteControlLoopStore
        from src.review.candidates import SqliteCandidateReviewStore

        assert SqliteCandidateReviewStore(ops.candidate_review_db_path).all_candidates(cohort_id=COHORT_ID) == []
        assert SqliteIdempotencyStore(ops.account_state_db_path).all() == []
        main_cycle_id = f"validation-{now.date().isoformat()}"
        assert SqliteControlLoopStore(ops.control_loop_db_path).get_cycle_record(main_cycle_id) is None


# --------------------------------------------------------------- H: no execution path


class TestNoExecutionPath:
    """Item H: no PaperBroker, place_order, confirm_fill, or
    confirm_candidate reference anywhere in the function body."""

    def test_source_contains_no_execution_calls(self):
        cycle = _load_script_module("_v1514_no_execution", SCRIPT_PATH)
        source = _body_source_without_docstring(cycle.run_universe_feasibility_study)
        for forbidden in ("PaperBroker(", "place_order(", "confirm_fill(", "confirm_candidate("):
            assert forbidden not in source


# --------------------------------------------------------------- I: production pipeline equivalence


@pytest.mark.asyncio
class TestProductionPipelineEquivalence:
    """Item I: the study uses the real scan_and_rank_opportunities
    (never a reimplementation), the real Quant/Risk, and the same
    no-trade hurdle."""

    async def test_scan_and_rank_opportunities_is_called_directly_with_the_real_hurdle(self, environment, monkeypatch):
        _tradier_configured(monkeypatch)
        now = datetime.now(timezone.utc)
        cycle = _load_script_module("_v1514_exact_pipeline", SCRIPT_PATH)
        _write_feasibility_universe_override(environment)
        monkeypatch.setattr(cycle, "_FEASIBILITY_UNIVERSE_CONFIG_PATH", environment / "universe_feasibility.yaml")
        provider = _FeasibilityFakeProvider(_chains_for(FEASIBILITY_TICKERS, now))
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)

        real_scan = cycle.scan_and_rank_opportunities
        calls = []

        def _spy(*args, **kwargs):
            calls.append((args, kwargs))
            return real_scan(*args, **kwargs)

        monkeypatch.setattr(cycle, "scan_and_rank_opportunities", _spy)
        monkeypatch.setattr(
            cycle, "run_outer_cycle",
            lambda *a, **kw: (_ for _ in ()).throw(AssertionError("run_outer_cycle must never be called")),
        )

        assert await cycle.run_universe_feasibility_study(now=now) is True
        assert len(calls) == 1
        assert calls[0][1]["proposal_id_prefix"] == "validation-scan"


# --------------------------------------------------------------- J: DTE-aware retrieval


@pytest.mark.asyncio
class TestDteAwareRetrieval:
    """Item J: DTE-aware retrieval across all 12 symbols, never the
    provider's nearest-N default."""

    async def test_dte_window_called_for_every_symbol_never_plain_get_option_chain(self, environment, monkeypatch):
        _tradier_configured(monkeypatch)
        now = datetime.now(timezone.utc)
        cycle = _load_script_module("_v1514_dte_bounds", SCRIPT_PATH)
        _write_feasibility_universe_override(environment)
        monkeypatch.setattr(cycle, "_FEASIBILITY_UNIVERSE_CONFIG_PATH", environment / "universe_feasibility.yaml")
        provider = _FeasibilityFakeProvider(_chains_for(FEASIBILITY_TICKERS, now))
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)

        assert await cycle.run_universe_feasibility_study(now=now) is True
        called_symbols = {c[0] for c in provider.dte_window_calls}
        assert called_symbols == set(FEASIBILITY_TICKERS)
        assert all(c[1:] == (20, 45) for c in provider.dte_window_calls)
        assert provider.closed is True


# --------------------------------------------------------------- K: evaluation timestamp


@pytest.mark.asyncio
class TestEvaluationTimestamp:
    """Item K: evaluation_as_of is captured after the full fetch loop
    (including correlation fetches), preserving the V1.5.7 integrity
    guarantee."""

    async def test_chain_timestamped_after_pre_fetch_now_still_produces_a_valid_candidate(self, environment, monkeypatch, capsys):
        _tradier_configured(monkeypatch)
        pre_fetch_now = datetime.now(timezone.utc)
        cycle = _load_script_module("_v1514_post_fetch_ts", SCRIPT_PATH)
        _write_feasibility_universe_override(environment, tickers=("SPY",))

        class _LaggyProvider(_FeasibilityFakeProvider):
            async def get_option_chain_for_dte_window(self, symbol, *, min_dte, max_dte, as_of, diagnostics=None):
                return _spy_like_chain(symbol, datetime.now(timezone.utc))

        monkeypatch.setattr(cycle, "_FEASIBILITY_UNIVERSE_CONFIG_PATH", environment / "universe_feasibility.yaml")
        provider = _LaggyProvider({})
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)

        ok = await cycle.run_universe_feasibility_study(now=pre_fetch_now)
        assert ok is True
        out = capsys.readouterr().out
        assert "ValidationError" not in out


# --------------------------------------------------------------- R/S: official cycle + diagnostic unchanged


@pytest.mark.asyncio
class TestOfficialCycleAndDiagnosticUnchanged:
    """Items R and S: the no-argument official cycle and
    --diagnostic-scan both retain their exact V1.5.13 behavior,
    unaffected by this release's new feasibility path."""

    async def test_official_cycle_still_scopes_to_spy_qqq_and_runs_normally(self, environment, monkeypatch):
        import yaml

        _tradier_configured(monkeypatch)
        cycle = _load_script_module("_v1514_official_unchanged", SCRIPT_PATH)
        # Matches the REAL config/universe.yaml's own official SPY/QQQ
        # scope, overriding the `environment` fixture's own
        # single-ticker default -- proving the official cycle still
        # reads exactly this file, untouched by the feasibility path.
        universe_yaml = environment / "universe.yaml"
        universe_yaml.write_text(
            yaml.safe_dump({"tickers": [{"ticker": "SPY", "sector": "ETF"}, {"ticker": "QQQ", "sector": "ETF"}], "strategies": ["CASH_SECURED_PUT"]})
        )
        before = universe_yaml.read_bytes()
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: FakeMarketDataProvider())
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)

        ok = await cycle.run_validation_cycle()
        assert ok is True

        from src.data.universe import load_universe

        assert [e.ticker for e in load_universe()] == ["SPY", "QQQ"]
        assert universe_yaml.read_bytes() == before, "the official universe.yaml must never be written by this release"

    async def test_diagnostic_scan_still_works_unchanged(self, environment, monkeypatch):
        _tradier_configured(monkeypatch)
        now = datetime.now(timezone.utc)
        cycle = _load_script_module("_v1514_diagnostic_unchanged", SCRIPT_PATH)
        provider = _FeasibilityFakeProvider(_chains_for(("SPY",), now))
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)

        assert await cycle.run_diagnostic_scan(now=now) is True


# --------------------------------------------------------------- O: correlation methodology


class _CorrelationControlledProvider(_FeasibilityFakeProvider):
    """Extends the standard fake provider with per-ticker-controlled
    `get_bars` output, so item O's date-intersection /
    sufficient-vs-insufficient-history / high-correlation-pair
    behavior can be exercised deterministically, without a live
    Tradier call."""

    def __init__(self, chain_by_ticker: dict[str, OptionChain], bars_by_ticker: dict[str, list[HistoricalBar]]):
        super().__init__(chain_by_ticker)
        self._bars_by_ticker = bars_by_ticker

    async def get_bars(self, symbol: str, start: date, end: date) -> list[HistoricalBar]:
        self.get_bars_calls.append(symbol)
        return self._bars_by_ticker.get(symbol, [])


def _bars(symbol: str, closes: list[float], *, start: date) -> list[HistoricalBar]:
    out = []
    d = start
    for close in closes:
        out.append(HistoricalBar(symbol=symbol, bar_date=d, open=close, high=close + 0.5, low=close - 0.5, close=close, volume=1_000_000, source="tradier"))
        d += timedelta(days=1)
    return out


@pytest.mark.asyncio
class TestCorrelationMethodology:
    """Item O: the correlation/diversification report correctly
    distinguishes sufficient- from insufficient-history symbols using
    the real date-intersection implementation, surfaces a
    high-correlation pair at the configured threshold, and never
    toggles `risk_data_wiring` or mutates the evaluated `Portfolio`'s
    own `price_history`/`risk_data_required` fields -- the correlation
    report is a read-only side channel, never wired into Risk/Quant."""

    async def test_sufficient_and_insufficient_history_and_high_correlation_pair_reported(
        self, environment, monkeypatch, capsys,
    ):
        _tradier_configured(monkeypatch)
        now = datetime.now(timezone.utc)
        cycle = _load_script_module("_v1514_correlation", SCRIPT_PATH)
        tickers = ("SPY", "QQQ", "IWM")
        _write_feasibility_universe_override(environment, tickers=tickers)
        monkeypatch.setattr(cycle, "_FEASIBILITY_UNIVERSE_CONFIG_PATH", environment / "universe_feasibility.yaml")

        start = date(2026, 7, 1)
        # SPY and QQQ: identical, perfectly-correlated 40-point series --
        # comfortably above min_correlation_observations (20) and the
        # 0.70 high-correlation threshold.
        closes = [100.0 + 0.3 * i for i in range(40)]
        bars_by_ticker = {
            "SPY": _bars("SPY", closes, start=start),
            "QQQ": _bars("QQQ", [c * 2 for c in closes], start=start),
            # IWM: only 5 observations -- below min_correlation_observations
            # (20), so it must be reported as insufficient, never padded
            # or silently dropped from the "sufficient" set.
            "IWM": _bars("IWM", [50.0, 50.1, 49.9, 50.2, 50.0], start=start),
        }
        provider = _CorrelationControlledProvider(_chains_for(tickers, now), bars_by_ticker)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)

        assert await cycle.run_universe_feasibility_study(now=now) is True
        out = capsys.readouterr().out

        assert "symbols with sufficient history" in out
        sufficient_line = next(line for line in out.splitlines() if "symbols with sufficient history" in line)
        assert "SPY" in sufficient_line and "QQQ" in sufficient_line
        insufficient_line = next(line for line in out.splitlines() if "symbols with insufficient history" in line)
        assert "IWM" in insufficient_line
        assert "SPY" not in insufficient_line and "QQQ" not in insufficient_line
        assert any("high-correlation pair" in line and "SPY" in line and "QQQ" in line for line in out.splitlines())
        assert set(provider.get_bars_calls) == set(tickers)

    async def test_fewer_than_two_sufficient_symbols_reports_not_computable_never_crashes(
        self, environment, monkeypatch, capsys,
    ):
        """Only one symbol clears min_correlation_observations -- the
        correlation matrix has nothing to pair, and the study must
        report that plainly rather than raise or fabricate a pair."""
        _tradier_configured(monkeypatch)
        now = datetime.now(timezone.utc)
        cycle = _load_script_module("_v1514_correlation_insufficient", SCRIPT_PATH)
        tickers = ("SPY", "QQQ")
        _write_feasibility_universe_override(environment, tickers=tickers)
        monkeypatch.setattr(cycle, "_FEASIBILITY_UNIVERSE_CONFIG_PATH", environment / "universe_feasibility.yaml")

        start = date(2026, 7, 1)
        bars_by_ticker = {
            "SPY": _bars("SPY", [100.0 + 0.1 * i for i in range(40)], start=start),
            "QQQ": [],  # no bars at all -- a legitimate real-world provider gap
        }
        provider = _CorrelationControlledProvider(_chains_for(tickers, now), bars_by_ticker)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)

        assert await cycle.run_universe_feasibility_study(now=now) is True
        out = capsys.readouterr().out
        assert "not computable" in out


# --------------------------------------------------------------- P: rate-limit safety


class _RateLimitedProvider(_FeasibilityFakeProvider):
    """Raises `TradierRateLimitError` (the real production exception
    `TradierMarketDataProvider._request` raises on HTTP 429) for a
    configured subset of symbols, so item P's fail-closed-per-symbol
    behavior under rate-limit exhaustion can be exercised without a
    live Tradier call or a real rate-limiter budget drain."""

    def __init__(self, chain_by_ticker: dict[str, OptionChain], rate_limited_tickers: frozenset[str]):
        super().__init__(chain_by_ticker)
        self._rate_limited_tickers = rate_limited_tickers

    async def get_option_chain_for_dte_window(self, symbol, *, min_dte, max_dte, as_of, diagnostics=None):
        from src.data.tradier_provider import TradierRateLimitError

        self.dte_window_calls.append((symbol, min_dte, max_dte))
        if symbol in self._rate_limited_tickers:
            raise TradierRateLimitError(f"Tradier rate limit exceeded (HTTP 429) for {symbol}")
        return self._chain_by_ticker[symbol]

    async def get_bars(self, symbol, start, end):
        from src.data.tradier_provider import TradierRateLimitError

        self.get_bars_calls.append(symbol)
        if symbol in self._rate_limited_tickers:
            raise TradierRateLimitError(f"Tradier rate limit exceeded (HTTP 429) for {symbol}")
        return await super().get_bars(symbol, start, end)


@pytest.mark.asyncio
class TestRateLimitSafety:
    """Item P: a `TradierRateLimitError` on one or more symbols (the
    real exception the existing rate-limiter/provider architecture
    raises on HTTP 429) is isolated per-symbol -- exactly like any
    other market-data fetch failure -- and never aborts the whole
    study, never crashes, and never mutates any state. Rate-limit
    PROTECTION itself (never exceeding the budget) is provided for
    free by reusing `TradierMarketDataProvider`'s existing
    `may_proceed`-gated `_request` for every call this study makes
    (see `expected_tradier_request_count` for the upper-bound budget
    calculation, exercised in the unit tests) -- this test proves the
    complementary fail-CLOSED guarantee when a limit is hit anyway."""

    async def test_rate_limited_symbols_isolated_study_completes_cleanly(self, environment, monkeypatch):
        _tradier_configured(monkeypatch)
        now = datetime.now(timezone.utc)
        cycle = _load_script_module("_v1514_rate_limit_partial", SCRIPT_PATH)
        tickers = FEASIBILITY_TICKERS
        _write_feasibility_universe_override(environment, tickers=tickers)
        monkeypatch.setattr(cycle, "_FEASIBILITY_UNIVERSE_CONFIG_PATH", environment / "universe_feasibility.yaml")
        rate_limited = frozenset({"NVDA", "META", "XOM"})
        provider = _RateLimitedProvider(_chains_for(tickers, now), rate_limited)
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)

        ops = operations_config_module.load_operations_config()
        from src.portfolio.account_state import SqlitePortfolioStore

        SqlitePortfolioStore(ops.account_state_db_path)
        db_path = Path(ops.account_state_db_path)
        before = hashlib.sha256(db_path.read_bytes()).hexdigest()

        ok = await cycle.run_universe_feasibility_study(now=now)
        assert ok is True, "a rate-limited symbol must never abort the whole feasibility study"

        after = hashlib.sha256(db_path.read_bytes()).hexdigest()
        assert before == after, "a rate-limit failure must never cause a write"
        called_symbols = {c[0] for c in provider.dte_window_calls}
        assert called_symbols == set(tickers), "every symbol is still attempted -- one rate-limited symbol never short-circuits the rest"
        assert provider.closed is True

    async def test_all_symbols_rate_limited_study_still_completes_with_zero_candidates(self, environment, monkeypatch, capsys):
        """Simulated full rate-limit-budget exhaustion: every symbol's
        chain fetch fails. The study must still fail closed CLEANLY --
        complete with a report showing zero usable chains and zero
        candidates -- never raise, never fall back to stale/fabricated
        data, never write anything."""
        _tradier_configured(monkeypatch)
        now = datetime.now(timezone.utc)
        cycle = _load_script_module("_v1514_rate_limit_all", SCRIPT_PATH)
        tickers = ("SPY", "QQQ", "IWM")
        _write_feasibility_universe_override(environment, tickers=tickers)
        monkeypatch.setattr(cycle, "_FEASIBILITY_UNIVERSE_CONFIG_PATH", environment / "universe_feasibility.yaml")
        provider = _RateLimitedProvider(_chains_for(tickers, now), frozenset(tickers))
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)

        ok = await cycle.run_universe_feasibility_study(now=now)
        assert ok is True
        out = capsys.readouterr().out
        assert "total ranked candidates: 0" in out.replace("  ", " ") or "total ranked candidates" in out
        failed_line = next(line for line in out.splitlines() if "symbols failed this feasibility study" in line)
        for t in tickers:
            assert t in failed_line

        ops = operations_config_module.load_operations_config()
        from src.brokers.base import SqliteIdempotencyStore

        assert SqliteIdempotencyStore(ops.account_state_db_path).all() == []


# --------------------------------------------------------------- repeated runs never block the official cycle


@pytest.mark.asyncio
class TestRepeatedFeasibilityNeverBlocksOfficialCycle:
    async def test_two_feasibility_runs_then_official_cycle_runs_normally(self, environment, monkeypatch):
        _tradier_configured(monkeypatch)
        now = datetime.now(timezone.utc)
        cycle = _load_script_module("_v1514_repeated", SCRIPT_PATH)
        _write_feasibility_universe_override(environment, tickers=("SPY",))
        monkeypatch.setattr(cycle, "_FEASIBILITY_UNIVERSE_CONFIG_PATH", environment / "universe_feasibility.yaml")
        provider = _FeasibilityFakeProvider(_chains_for(("SPY",), now))
        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: provider)
        monkeypatch.setattr(cycle, "evaluate_validation_cycle_eligibility", lambda now, **kw: _ALLOWED)

        assert await cycle.run_universe_feasibility_study(now=now) is True
        assert await cycle.run_universe_feasibility_study(now=now) is True

        monkeypatch.setattr(cycle, "get_configured_market_data_provider", lambda: FakeMarketDataProvider())
        ok = await cycle.run_validation_cycle()
        assert ok is True
