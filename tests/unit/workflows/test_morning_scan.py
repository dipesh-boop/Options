"""End-to-end tests for `run_morning_scan`/`render_morning_scan_report`:
the approved-trade path, the NO TRADE path, reconciliation/feed/earnings
reporting, and the structural "never places a Fidelity order" guarantee.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from src.brokers.fidelity import TicketStatus
from src.data.earnings import EarningsEvent
from src.llm.schemas import StrategyType
from src.orchestration.pipeline import PipelineOutcome, PipelineStatus
from src.risk.limits import get_default_limits
from src.risk.portfolio_risk import Portfolio
from src.workflows import morning_scan
from src.workflows.candidate_generation import QuantFilterConfig, UniverseEntry
from src.workflows.morning_scan import MorningScanInputs, render_morning_scan_report, run_morning_scan
from src.workflows.reconciliation import ConfirmedFidelityPosition
from tests.unit.workflows.conftest import (
    NOW,
    automated_capabilities,
    da_pass_payload,
    da_reject_payload,
    default_pcs_chain,
    full_stages,
    make_market_regime,
    make_portfolio,
    make_risk_reviewer_note,
    make_snapshot,
    manual_capabilities,
    pm_advance_payload,
    pm_hold_cash_payload,
)

LIMITS = get_default_limits()


def _base_inputs(**overrides) -> MorningScanInputs:
    chain = overrides.pop("chain", default_pcs_chain())
    stages = overrides.pop("stages", full_stages(chain=chain))
    base = dict(
        now=NOW,
        portfolio=make_portfolio(),
        limits=LIMITS,
        confirmed_fidelity_positions=[],
        fetch_results={"XYZ": chain},
        universe=[UniverseEntry("XYZ", "TECH")],
        strategies=[StrategyType.PUT_CREDIT_SPREAD],
        quant_filter=QuantFilterConfig(),
        market_regime=make_market_regime(),
        risk_reviewer_note_factory=make_risk_reviewer_note,
        snapshot_factory=make_snapshot,
        stages=stages,
        automated_broker_capabilities=automated_capabilities(),
        manual_broker_capabilities=manual_capabilities(),
    )
    base.update(overrides)
    return MorningScanInputs(**base)


class TestApprovedTradePath:
    @pytest.mark.asyncio
    async def test_produces_an_awaiting_human_fidelity_ticket(self):
        report = await run_morning_scan(_base_inputs())
        assert report.no_trade_reason is None
        assert report.any_actionable is True
        ticket = report.results[0].outcome.fidelity_ticket
        assert ticket is not None
        assert ticket.status == TicketStatus.AWAITING_HUMAN

    @pytest.mark.asyncio
    async def test_report_carries_quant_and_risk_engine_detail(self):
        report = await run_morning_scan(_base_inputs())
        result = report.results[0]
        assert result.outcome.quantitative_analysis is not None
        assert result.outcome.risk_decision is not None
        assert result.outcome.risk_decision.decision.value == "approve"

    @pytest.mark.asyncio
    async def test_renders_a_ticket_section_not_no_trade(self):
        report = await run_morning_scan(_base_inputs())
        text = render_morning_scan_report(report)
        assert "NO TRADE" not in text
        assert "AWAITING HUMAN EXECUTION" in text
        assert "PROBABILITY OF PROFIT:" in text
        assert "RISK ENGINE STATUS:" in text
        assert "APPROVE" in text


class TestNoTradePaths:
    @pytest.mark.asyncio
    async def test_empty_universe_is_no_trade(self):
        report = await run_morning_scan(_base_inputs(universe=[]))
        assert report.no_trade_reason is not None
        assert "cash is a valid position" in report.no_trade_reason.lower() or "Cash is a valid position" in report.no_trade_reason
        assert report.results == ()

    @pytest.mark.asyncio
    async def test_devils_advocate_reject_is_no_trade(self):
        chain = default_pcs_chain()
        stages = full_stages(chain=chain, da_payload_factory=da_reject_payload)
        report = await run_morning_scan(_base_inputs(chain=chain, stages=stages))
        assert report.no_trade_reason is not None
        assert report.results[0].outcome.fidelity_ticket is None
        assert report.results[0].outcome.status == PipelineStatus.REJECTED

    @pytest.mark.asyncio
    async def test_portfolio_manager_hold_cash_is_no_trade(self):
        chain = default_pcs_chain()
        stages = full_stages(chain=chain, pm_payload_factory=pm_hold_cash_payload)
        report = await run_morning_scan(_base_inputs(chain=chain, stages=stages))
        assert report.no_trade_reason is not None
        assert report.results[0].outcome.status == PipelineStatus.NO_FILL

    @pytest.mark.asyncio
    async def test_no_trade_renders_explicit_no_trade_section(self):
        report = await run_morning_scan(_base_inputs(universe=[]))
        text = render_morning_scan_report(report)
        assert "NO TRADE" in text
        assert report.no_trade_reason in text

    @pytest.mark.asyncio
    async def test_stale_feed_produces_no_candidates_and_no_trade(self):
        chain = default_pcs_chain()
        report = await run_morning_scan(_base_inputs(chain=chain, now=NOW + timedelta(minutes=30)))
        assert report.no_trade_reason is not None
        assert report.freshness.all_fresh is False


class TestFeedAndReconciliationReporting:
    @pytest.mark.asyncio
    async def test_failed_feed_reported_and_excluded_from_screening(self):
        report = await run_morning_scan(_base_inputs(fetch_results={"XYZ": RuntimeError("timeout")}, universe=[UniverseEntry("XYZ", "TECH")]))
        assert report.feed_health.all_healthy is False
        assert report.no_trade_reason is not None

    @pytest.mark.asyncio
    async def test_reconciliation_discrepancy_surfaced_in_report(self):
        confirmed = [ConfirmedFidelityPosition(ticker="AAA", strategy=StrategyType.CASH_SECURED_PUT, expiration=date(2026, 10, 16), strikes=frozenset({50.0}), contracts=1, confirmed_by="dipesh", confirmed_at=NOW)]
        report = await run_morning_scan(_base_inputs(confirmed_fidelity_positions=confirmed, universe=[]))
        assert report.reconciliation.clean is False
        text = render_morning_scan_report(report)
        assert "Reconciliation discrepancies" in text


class TestReconciliationBlocksNewCandidatesRegressionSY006:
    """SY-006: a reconciliation discrepancy used to be only reported,
    never acted on -- the very same scan run that flagged a confirmed
    Fidelity position as untracked internally could still generate (and
    potentially get approved) a fresh, real duplicate trade
    recommendation for that same ticker, since the duplicate-position
    check only ever sees the stale internal `Portfolio.positions`."""

    @pytest.mark.asyncio
    async def test_ticker_with_untracked_fidelity_position_gets_no_new_candidate(self):
        confirmed = [ConfirmedFidelityPosition(
            ticker="XYZ", strategy=StrategyType.PUT_CREDIT_SPREAD, expiration=date(2026, 10, 16),
            strikes=frozenset({95.0, 90.0}), contracts=1, confirmed_by="dipesh", confirmed_at=NOW,
        )]
        report = await run_morning_scan(_base_inputs(confirmed_fidelity_positions=confirmed))
        assert report.reconciliation.clean is False
        assert any(d.kind == "missing_from_internal" and d.ticker == "XYZ" for d in report.reconciliation.discrepancies)
        # the exact SY-006 exploit: this same universe/chain would
        # otherwise produce an approved XYZ candidate (see
        # TestApprovedTradePath) -- it must not, while this ticker's
        # Fidelity position is unreconciled.
        assert report.results == ()
        assert len(report.reconciliation_screened_out) == 1
        assert "XYZ" in report.reconciliation_screened_out[0]

    @pytest.mark.asyncio
    async def test_screened_out_ticker_is_rendered_in_the_report(self):
        confirmed = [ConfirmedFidelityPosition(
            ticker="XYZ", strategy=StrategyType.PUT_CREDIT_SPREAD, expiration=date(2026, 10, 16),
            strikes=frozenset({95.0, 90.0}), contracts=1, confirmed_by="dipesh", confirmed_at=NOW,
        )]
        report = await run_morning_scan(_base_inputs(confirmed_fidelity_positions=confirmed))
        text = render_morning_scan_report(report)
        assert "Screened out pending reconciliation" in text
        assert "XYZ" in text

    @pytest.mark.asyncio
    async def test_a_different_ticker_is_unaffected_by_another_tickers_discrepancy(self):
        """The screen is per-ticker, not a blanket halt on any
        discrepancy anywhere in the portfolio."""
        confirmed = [ConfirmedFidelityPosition(
            ticker="OTHER_TICKER", strategy=StrategyType.CASH_SECURED_PUT, expiration=date(2026, 10, 16),
            strikes=frozenset({50.0}), contracts=1, confirmed_by="dipesh", confirmed_at=NOW,
        )]
        report = await run_morning_scan(_base_inputs(confirmed_fidelity_positions=confirmed))
        assert report.reconciliation.clean is False
        assert report.reconciliation_screened_out == ()
        assert report.no_trade_reason is None  # XYZ's own candidate still proceeds normally


class TestEarningsWindowScreening:
    @pytest.mark.asyncio
    async def test_candidate_within_earnings_window_is_screened_out(self):
        chain = default_pcs_chain()
        earnings = EarningsEvent(symbol="XYZ", earnings_date=date(2026, 10, 15), timing="after_market", confirmed=True, timestamp=NOW, source="mock")
        report = await run_morning_scan(_base_inputs(chain=chain, earnings_by_ticker={"XYZ": earnings}))
        assert report.no_trade_reason is not None
        assert len(report.earnings_screened_out) == 1
        assert "earnings window" in report.earnings_screened_out[0]

    @pytest.mark.asyncio
    async def test_candidate_outside_earnings_window_proceeds(self):
        chain = default_pcs_chain()
        earnings = EarningsEvent(symbol="XYZ", earnings_date=date(2026, 6, 1), timing="after_market", confirmed=True, timestamp=NOW, source="mock")
        report = await run_morning_scan(_base_inputs(chain=chain, earnings_by_ticker={"XYZ": earnings}))
        assert report.no_trade_reason is None
        assert report.earnings_screened_out == ()


class TestPortfolioReporting:
    @pytest.mark.asyncio
    async def test_nav_cash_and_drawdown_reported(self):
        report = await run_morning_scan(_base_inputs())
        assert report.nav == 100_000.0
        assert report.cash == 95_000.0
        assert report.capital_deployed_pct == pytest.approx(0.05)
        assert report.current_drawdown_pct == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_untracked_greeks_reported_honestly_not_fabricated(self):
        report = await run_morning_scan(_base_inputs())
        assert report.portfolio_net_delta is None
        text = render_morning_scan_report(report)
        assert "not tracked" in text

    @pytest.mark.asyncio
    async def test_supplied_greeks_are_rendered(self):
        report = await run_morning_scan(_base_inputs(portfolio_net_delta=-12.5, portfolio_net_theta=8.0, portfolio_net_vega=40.0))
        text = render_morning_scan_report(report)
        assert "-12.5" in text


class TestPortfolioThreadedAcrossCandidatesRegressionSY004:
    """SY-004: every candidate in one scan run used to be evaluated
    against the same pre-scan `Portfolio` snapshot -- the Risk Engine's
    cumulative checks (cash reserve, capital deployed, concentration,
    duplicate position) for candidate N+1 never saw what candidate N
    had already been approved for moments earlier in the same run.
    These tests patch `run_order_pipeline` itself (rather than building
    a full multi-ticker LLM-mocked fixture) so they isolate exactly the
    one thing `run_morning_scan`'s own loop is responsible for: which
    `Portfolio` object it puts on each candidate's `PipelineRequest`."""

    def _two_ticker_inputs(self, **overrides) -> MorningScanInputs:
        chain_a = default_pcs_chain(symbol="AAA")
        chain_b = default_pcs_chain(symbol="BBB")
        stages = full_stages(chain=chain_a)  # never actually reaches the real pipeline; patched away
        base = dict(
            chain=chain_a,
            stages=stages,
            fetch_results={"AAA": chain_a, "BBB": chain_b},
            universe=[UniverseEntry("AAA", "TECH"), UniverseEntry("BBB", "TECH")],
            max_candidates=5,
        )
        base.update(overrides)
        return _base_inputs(**base)

    @pytest.mark.asyncio
    async def test_second_candidates_request_carries_the_first_candidates_updated_portfolio(self, monkeypatch):
        sentinel_portfolio = make_portfolio(nav=999_999.0, cash=1.0, peak_equity=1_000_000.0)
        seen_portfolios: list[Portfolio] = []

        async def fake_run_order_pipeline(request, stages):
            seen_portfolios.append(request.portfolio)
            if len(seen_portfolios) == 1:
                return PipelineOutcome(status=PipelineStatus.FILLED, rejected_stage=None, reason="ok", updated_portfolio=sentinel_portfolio)
            return PipelineOutcome(status=PipelineStatus.NO_FILL, rejected_stage=None, reason="ok")

        monkeypatch.setattr(morning_scan, "run_order_pipeline", fake_run_order_pipeline)
        inputs = self._two_ticker_inputs()
        report = await run_morning_scan(inputs)

        assert len(seen_portfolios) == 2
        assert seen_portfolios[0] is inputs.portfolio  # first candidate: the pre-scan snapshot
        assert seen_portfolios[1] is sentinel_portfolio  # second candidate: the first candidate's own fill result
        assert seen_portfolios[1] is not inputs.portfolio  # the OP-004/SY-004 bug's old (wrong) behavior
        assert report is not None  # sanity: the scan still completes end to end

    @pytest.mark.asyncio
    async def test_a_no_fill_candidate_does_not_reset_the_carried_portfolio(self, monkeypatch):
        """Three candidates: the first fills (produces a cumulative
        portfolio), the second doesn't fill (no updated_portfolio), the
        third must still see the first's cumulative result, not fall
        back to the stale pre-scan snapshot."""
        sentinel_portfolio = make_portfolio(nav=999_999.0, cash=1.0, peak_equity=1_000_000.0)
        seen_portfolios: list[Portfolio] = []

        async def fake_run_order_pipeline(request, stages):
            seen_portfolios.append(request.portfolio)
            if len(seen_portfolios) == 1:
                return PipelineOutcome(status=PipelineStatus.FILLED, rejected_stage=None, reason="ok", updated_portfolio=sentinel_portfolio)
            return PipelineOutcome(status=PipelineStatus.NO_FILL, rejected_stage=None, reason="ok")

        monkeypatch.setattr(morning_scan, "run_order_pipeline", fake_run_order_pipeline)
        chain_c = default_pcs_chain(symbol="CCC")
        inputs = self._two_ticker_inputs(
            fetch_results={"AAA": default_pcs_chain(symbol="AAA"), "BBB": default_pcs_chain(symbol="BBB"), "CCC": chain_c},
            universe=[UniverseEntry("AAA", "TECH"), UniverseEntry("BBB", "TECH"), UniverseEntry("CCC", "TECH")],
        )
        await run_morning_scan(inputs)

        assert len(seen_portfolios) == 3
        assert seen_portfolios[0] is inputs.portfolio
        assert seen_portfolios[1] is sentinel_portfolio
        assert seen_portfolios[2] is sentinel_portfolio  # carried through the no-fill candidate, not reset


class TestMaxCandidatesCap:
    @pytest.mark.asyncio
    async def test_candidate_pool_capped(self):
        chain = default_pcs_chain()
        report = await run_morning_scan(_base_inputs(
            chain=chain, max_candidates=0, universe=[UniverseEntry("XYZ", "TECH")],
        ))
        assert report.results == ()
        assert report.no_trade_reason is not None


class TestNeverPlacesAFidelityOrder:
    """Structural proof: nothing in this package can move a
    FidelityTradeTicket past AWAITING_HUMAN, and there is no code path
    that calls a real broker's order-submission API."""

    MORNING_SCAN_SOURCE = Path(morning_scan.__file__).read_text(encoding="utf-8")

    def test_module_never_calls_transition_or_confirm_fill(self):
        assert "confirm_fill(" not in self.MORNING_SCAN_SOURCE
        assert "transition(" not in self.MORNING_SCAN_SOURCE

    def test_module_does_not_import_a_live_broker_client(self):
        pattern = re.compile(r"^\s*(from\s+src\.brokers\.ibkr|import\s+src\.brokers\.ibkr)\b", re.MULTILINE)
        assert pattern.search(self.MORNING_SCAN_SOURCE) is None

    def test_module_does_not_construct_fidelity_trade_ticket_directly(self):
        """The only ticket constructor is
        `FidelityManualProvider.generate_trade_ticket`, called from
        inside `run_order_pipeline` -- this module never builds one
        itself, so it can never choose a status other than whatever that
        already-tested path defaults to."""
        assert "FidelityTradeTicket(" not in self.MORNING_SCAN_SOURCE

    @pytest.mark.asyncio
    async def test_every_produced_ticket_is_awaiting_human(self):
        report = await run_morning_scan(_base_inputs())
        for r in report.results:
            if r.outcome.fidelity_ticket is not None:
                assert r.outcome.fidelity_ticket.status == TicketStatus.AWAITING_HUMAN
