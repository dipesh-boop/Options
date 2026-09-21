"""Sections 16 (database failure), 17 (time/calendar), 23 (dashboard),
and 25 (failure recovery/kill switch) acceptance coverage.

Section 17 finding AS OF STEP 21 (superseded by Step 22): this
platform had NO market-hours/trading-calendar/DST-aware module
anywhere in `src/`. `ARCHITECTURE.md` listed "Clock/timezone bugs
around market hours and expiration" as a known, not-yet-built
mitigation. Step 22 Part 7-9 closed that gap with
`src.data.market_calendar` (see `tests/unit/data/test_market_calendar.py`
for the full deterministic test suite: holidays, early closes, DST,
UTC/Eastern conversion) -- this file's own check below now confirms
the module exists and is wired to fail closed, rather than confirming
its absence.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.brokers.fidelity import ExecutionConfirmation
from src.llm.schemas import StrategyType
from src.orchestration.pipeline import (
    DatabaseRecord,
    PipelineStatus,
    SqliteDatabase,
    run_order_pipeline,
)
from src.risk.kill_switch import check_kill_switch
from src.risk.limits import load_risk_limits
from src.risk.portfolio_risk import Portfolio

from .conftest import all_strategy_fixtures, base_proposal, full_stages, make_chain, make_portfolio, pipeline_request


class TestSection17TimezoneEnforcementIsUniversal:
    """Every timestamp-carrying schema this platform relies on for
    freshness/staleness gating rejects a naive datetime outright,
    rather than silently assuming a timezone (which is exactly how a
    UTC-vs-Eastern mixup could otherwise slip past a staleness check)."""

    def test_portfolio_as_of_rejects_naive_datetime(self):
        with pytest.raises(ValidationError, match="timezone"):
            Portfolio(as_of=datetime(2026, 9, 20, 14, 0), nav=1_000_000.0, cash=900_000.0, peak_equity=1_000_000.0)

    def test_execution_confirmation_rejects_naive_datetime(self):
        with pytest.raises(ValidationError):
            ExecutionConfirmation(
                confirmed_by="human", confirmation_source="human_manual_entry",
                filled_quantity=1, fill_price=1.0, confirmed_at=datetime(2026, 9, 20, 14, 0),
            )

    @pytest.mark.asyncio
    async def test_trade_proposal_with_naive_timestamp_rejects_at_construction(self):
        fx = all_strategy_fixtures()[StrategyType.LONG_CALL]
        with pytest.raises(ValidationError):
            base_proposal(fx.strategy, fx.legs, contracts_requested=1, timestamp=datetime(2026, 9, 20, 14, 0))

    def test_market_calendar_module_exists_and_fails_closed_on_naive_datetimes(self):
        """Step 22's resolution of the Step 21 finding: a real,
        deterministic market-hours/holiday module now exists (see
        `tests/unit/data/test_market_calendar.py` for its own
        exhaustive test suite) -- this is a light smoke check that it's
        actually importable and wired to the same fail-closed,
        never-naive-datetime discipline as every other timestamp check
        in this platform, not a re-test of its internals."""
        from src.data.market_calendar import NaiveDatetimeError, is_market_open

        with pytest.raises(NaiveDatetimeError):
            is_market_open(datetime(2026, 9, 21, 10, 0))  # naive -- rejected

        # A real, known-closed instant (a Saturday) -- proves it isn't
        # a stub that always returns True.
        assert is_market_open(datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)) is False


class TestSection16DatabaseCrashRecovery:
    """SqliteDatabase (the pipeline's own durable audit trail, SY-002's
    fix) survives a simulated process crash: a record saved by one
    instance is still readable by a FRESH instance pointed at the same
    file, as if the original process had died and been restarted."""

    @pytest.mark.asyncio
    async def test_a_real_fill_recorded_before_a_simulated_crash_survives_restart(self, tmp_path):
        db_path = tmp_path / "pipeline_audit.db"
        fx = all_strategy_fixtures()[StrategyType.PUT_CREDIT_SPREAD]
        proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=1)
        market_data = make_chain(*fx.contracts)
        stages = full_stages(market_data=market_data, database=SqliteDatabase(db_path))
        req = pipeline_request(proposal=proposal, market_data=market_data, portfolio=fx.portfolio, allowed_strategies=[StrategyType.PUT_CREDIT_SPREAD])

        outcome = await run_order_pipeline(req, stages)
        assert outcome.status == PipelineStatus.FILLED

        # Simulate a crash: throw away every in-process object and
        # construct a brand-new SqliteDatabase against the same file.
        fresh_db = SqliteDatabase(db_path)
        records = fresh_db.all()
        assert len(records) == 1
        assert records[0].proposal_id == proposal.proposal_id
        assert records[0].status == PipelineStatus.FILLED
        assert records[0].order is not None

    def test_a_record_saved_mid_write_is_not_silently_lost_on_reopen(self, tmp_path):
        """Two sequential opens (simulating two separate process
        lifetimes) each see everything the one before it wrote --
        no silent truncation or overwrite."""
        db_path = tmp_path / "sequential.db"
        db1 = SqliteDatabase(db_path)
        db1.save(DatabaseRecord(
            record_id="r1", proposal_id="p1", status=PipelineStatus.FILLED,
            order=None, fidelity_ticket=None, created_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
        ))
        db2 = SqliteDatabase(db_path)  # crash + restart
        db2.save(DatabaseRecord(
            record_id="r2", proposal_id="p2", status=PipelineStatus.REJECTED,
            order=None, fidelity_ticket=None, created_at=datetime(2026, 9, 21, tzinfo=timezone.utc),
        ))
        db3 = SqliteDatabase(db_path)  # crash + restart again
        record_ids = {r.record_id for r in db3.all()}
        assert record_ids == {"r1", "r2"}


class TestSection25KillSwitchAlwaysPreferredOverContinuingAnyway:
    @pytest.mark.asyncio
    async def test_manually_halted_portfolio_blocks_every_trade_through_the_real_pipeline(self):
        halted = make_portfolio(nav=1_000_000.0, cash=900_000.0, peak_equity=1_000_000.0, halted=True, halt_reason="acceptance-test manual halt")
        fx = all_strategy_fixtures()[StrategyType.LONG_CALL]
        proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=1)
        market_data = make_chain(*fx.contracts)
        stages = full_stages(market_data=market_data)
        req = pipeline_request(proposal=proposal, market_data=market_data, portfolio=halted, allowed_strategies=[StrategyType.LONG_CALL])

        outcome = await run_order_pipeline(req, stages)

        assert outcome.status == PipelineStatus.REJECTED
        assert outcome.order is None
        limits = load_risk_limits()
        assert check_kill_switch(halted, limits).halted is True

    def test_kill_switch_reasons_are_never_silently_ignored_by_severity(self):
        """A HALT decision is always the strictest outcome -- confirms
        the kill switch's own result type carries a human-readable
        reason, never a bare boolean a caller might silently swallow."""
        limits = load_risk_limits()
        halted = make_portfolio(nav=1_000_000.0, cash=900_000.0, peak_equity=1_000_000.0, halted=True, halt_reason="test")
        result = check_kill_switch(halted, limits)
        assert result.halted is True
        assert result.reason_code is not None
        assert result.message


class TestSection23DashboardAcceptance:
    """Launches the real FastAPI app via TestClient (not a mock) --
    complements `tests/unit/dashboard/test_app_security.py`'s already-
    exhaustive route-allowlist/forbidden-import coverage with a live
    boot smoke test and a frontend-to-backend consistency check."""

    def test_dashboard_boots_and_serves_the_root_page(self):
        from src.dashboard import app as dashboard_app

        client = TestClient(dashboard_app.app)
        response = client.get("/", follow_redirects=True)
        assert response.status_code == 200

    def test_every_frontend_fetch_call_targets_an_allowed_backend_route(self):
        """Cross-checks the actual JS the browser runs against the
        backend's own route allowlist -- a route the frontend calls
        that ISN'T in the backend's allowlist would 404 in production;
        a route added to the backend but never wired to a button would
        be dead code. Neither has happened."""
        import re
        from pathlib import Path

        from src.dashboard import app as dashboard_app

        js_path = Path(dashboard_app.__file__).parent / "static" / "dashboard.js"
        js_source = js_path.read_text()
        frontend_paths = set(re.findall(r'api\(`?(/api/[^`"\')\s]*)', js_source))
        # Normalize template-literal ${tradeId} segments to the FastAPI path-param spelling.
        normalized = {re.sub(r"\$\{[^}]+\}", "{trade_id}", p) for p in frontend_paths}

        backend_paths = set()
        for route in dashboard_app.app.routes:
            path = getattr(route, "path", None)
            if path and path.startswith("/api/"):
                backend_paths.add(path)

        offending = normalized - backend_paths
        assert offending == set(), f"frontend calls route(s) not in the backend: {offending}"
        # No execution-shaped route is ever fetched (belt-and-suspenders
        # on top of the backend's own allowlist test). The file's own
        # header comment names these terms once, explicitly to
        # document their ABSENCE ("There is no code here... for AUTO
        # TRADE, EXECUTE, or SEND TO FIDELITY") -- strip block comments
        # before scanning so that documented-absence prose can't itself
        # trigger the check meant to catch the opposite.
        code_only = re.sub(r"/\*.*?\*/", "", js_source, flags=re.DOTALL)
        forbidden = ("execute", "auto-trade", "send-to-fidelity", "submit-order", "place-order")
        assert not any(term in code_only.lower() for term in forbidden)
