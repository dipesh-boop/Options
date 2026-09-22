"""Tests for `src.portfolio.orchestrator` (Step 22.4A Part 1-4, 8-10)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from src.brokers.fidelity import ApprovedOrder, FidelityLegAction, FidelityManualProvider, FidelityOrderLeg
from src.data.option_chain import OptionChain, OptionContract, OptionRight
from src.data.quotes import UnderlyingQuote
from src.data.rate_limiter import RateLimitPriority, RateLimitState
from src.lifecycle.persistence import InMemoryLifecycleStore
from src.lifecycle.policies_library import policies_for_strategy
from src.llm.schemas import StrategyType
from src.portfolio.actions import ControlLoopAction
from src.portfolio.alerts import ControlLoopAlertType
from src.portfolio.orchestrator import (
    OpportunityScanConfig,
    OuterCycleInputs,
    TicketMonitorConfig,
    run_outer_cycle,
)
from src.portfolio.persistence import InMemoryControlLoopStore
from src.quant.black_scholes import OptionRight as QuantOptionRight
from src.risk.broker_constraints import load_broker_capabilities
from src.risk.limits import get_default_limits
from src.risk.portfolio_risk import Portfolio, PortfolioPosition, PortfolioPositionLeg
from src.strategies.base import StrategyKind
from src.workflows.candidate_generation import QuantFilterConfig, UniverseEntry

NOW = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)
EXP = date(2026, 10, 23)
LIMITS = get_default_limits()
PCS_POLICY = policies_for_strategy(StrategyKind.PUT_CREDIT_SPREAD)[0]

_EXTRAS = {
    "earnings_data_available": True, "days_to_earnings": 9999, "initial_credit": 3.0,
    "profit_capture_denominator": 600.0, "short_leg_is_itm": False, "short_leg_extrinsic_value": 4.6,
    "position_delta_abs": 0.20,
}


def _pcs_position(position_id="p1", ticker="SPY") -> PortfolioPosition:
    return PortfolioPosition(
        position_id=position_id, ticker=ticker, sector="ETF", strategy=StrategyType.PUT_CREDIT_SPREAD,
        expiration=EXP,
        legs=[
            PortfolioPositionLeg(right="P", side="sell", strike=450.0, entry_price=6.0),
            PortfolioPositionLeg(right="P", side="buy", strike=440.0, entry_price=3.0),
        ],
        contracts=2, capital_at_risk=1400.0, max_loss=1400.0, opened_at=NOW - timedelta(days=5),
    )


def _spy_position_chain(*, timestamp=NOW) -> OptionChain:
    underlying = UnderlyingQuote(symbol="SPY", bid=454.5, ask=455.5, last=455.0, volume=1000, timestamp=timestamp, source="tradier")
    contracts = [
        OptionContract(
            underlying="SPY", option_symbol="SPY261023P00450000", expiration=EXP, strike=450.0, right=OptionRight.PUT,
            bid=4.5, ask=4.7, last=4.6, volume=100, open_interest=500, underlying_price=455.0, timestamp=timestamp, source="tradier",
        ),
        OptionContract(
            underlying="SPY", option_symbol="SPY261023P00440000", expiration=EXP, strike=440.0, right=OptionRight.PUT,
            bid=1.8, ask=2.0, last=1.9, volume=100, open_interest=500, underlying_price=455.0, timestamp=timestamp, source="tradier",
        ),
    ]
    return OptionChain(underlying=underlying, contracts=contracts, timestamp=timestamp, source="tradier")


def _scan_chain(ticker="QQQ", underlying_last=380.0, *, timestamp=NOW) -> OptionChain:
    underlying = UnderlyingQuote(symbol=ticker, bid=underlying_last - 0.5, ask=underlying_last + 0.5, last=underlying_last, volume=1_000_000, timestamp=timestamp, source="tradier")
    contracts = [
        OptionContract(
            underlying=ticker, option_symbol=f"{ticker}{EXP.isoformat()}P{int(strike*1000):08d}", expiration=EXP, strike=strike,
            right=OptionRight.PUT, bid=3.0, ask=3.2, last=3.1, volume=vol, open_interest=oi,
            delta=delta, iv=0.18, underlying_price=underlying_last, timestamp=timestamp, source="tradier",
        )
        for strike, delta, oi, vol in [(360.0, -0.20, 1000, 500), (355.0, -0.12, 800, 300), (350.0, -0.08, 600, 200)]
    ]
    return OptionChain(underlying=underlying, contracts=contracts, timestamp=timestamp, source="tradier")


def _pending_ticket():
    approved = ApprovedOrder(
        risk_approval_id="r1", account_alias="acct", ticker="SPY", strategy="put_credit_spread",
        underlying_price=455.0, expiration=EXP,
        legs=[
            FidelityOrderLeg(action=FidelityLegAction.SELL_TO_OPEN, put_call=QuantOptionRight.PUT, strike=450.0, expiration=EXP, contracts=2),
            FidelityOrderLeg(action=FidelityLegAction.BUY_TO_OPEN, put_call=QuantOptionRight.PUT, strike=440.0, expiration=EXP, contracts=2),
        ],
        quantity=2, limit_price=3.0, minimum_acceptable_price=2.8, estimated_credit_debit=3.0, net_bid=2.9,
        net_ask=3.1, max_profit=600.0, max_loss=1400.0, breakeven=447.0, capital_at_risk=1400.0,
        return_on_capital=0.43, profit_target=1.5, loss_management_rule="x", DTE_management_rule="y",
        management_dte=21, timestamp=NOW, market_data_timestamp=NOW,
    )
    return FidelityManualProvider().generate_trade_ticket(approved)


def _portfolio(positions=None, nav=100000.0, cash=80000.0) -> Portfolio:
    return Portfolio(
        as_of=NOW, nav=nav, cash=cash, peak_equity=nav, positions=positions or [],
        sector_by_ticker={"SPY": "ETF", "QQQ": "ETF"},
    )


def _base_inputs(**overrides) -> OuterCycleInputs:
    pos1 = _pcs_position("p1", "SPY")
    fields = dict(
        cycle_id="cycle-1", as_of=NOW, portfolio=_portfolio([pos1]), limits=LIMITS, provider="tradier",
        provider_health_status="healthy", is_trading_day=True, is_market_open=True,
        fetch_results={"SPY": _spy_position_chain()},
        lifecycle_store=InMemoryLifecycleStore(), control_loop_store=InMemoryControlLoopStore(),
        policy_name_for_position={"p1": PCS_POLICY.name}, extra_monitoring_inputs={"p1": dict(_EXTRAS)},
    )
    fields.update(overrides)
    return OuterCycleInputs(**fields)


def _scan_config(**overrides) -> OpportunityScanConfig:
    fields = dict(
        universe=(UniverseEntry(ticker="QQQ", sector="ETF"),),
        chains_by_ticker={"QQQ": _scan_chain()},
        strategies=(StrategyType.CASH_SECURED_PUT,),
        quant_filter=QuantFilterConfig(min_dte=20, max_dte=45),
        market_regime="normal",
        broker_capabilities=load_broker_capabilities("internal_paper"),
    )
    fields.update(overrides)
    return OpportunityScanConfig(**fields)


class TestExistingPositionMonitoringAlwaysRuns:
    def test_control_result_is_produced_from_the_unmodified_control_loop(self):
        result = run_outer_cycle(_base_inputs())
        snap = next(s for s in result.control_result.decision_snapshots if s.position_id == "p1")
        assert snap.recommended_action == ControlLoopAction.HOLD

    def test_never_gated_by_rate_limit_even_when_fully_exhausted(self):
        exhausted = RateLimitState(allowed=100, used=100, available=0, reset_at=None, observed_at=NOW)
        result = run_outer_cycle(_base_inputs(rate_limit_state=exhausted))
        assert result.control_result.decision_snapshots  # existing-position monitoring still ran


class TestOpportunityScanWiring:
    def test_default_no_trade_hurdle_yields_no_snapshot_and_honest_counts(self):
        result = run_outer_cycle(_base_inputs(opportunity_scan=_scan_config()))
        assert result.opportunity_scan_result is not None
        assert result.opportunity_scan_skipped_reason is None
        assert result.control_result.cycle_record.opportunities_scanned == 1
        # CASH/NO_TRADE is a valid outcome -- no fabricated snapshot for "nothing."
        assert result.opportunity_decision_snapshot is None

    def test_forcing_a_best_candidate_creates_a_review_decision_snapshot(self):
        # A very negative hurdle forces `best` to be populated even though
        # real expected value is negative -- this isolates the WIRING
        # (snapshot creation, persistence, alert) from option-pricing
        # realism, which `tests/unit/portfolio/test_opportunity_scan.py`
        # already covers directly.
        cfg = _scan_config(no_trade_hurdle=-1000.0)
        big_portfolio = _portfolio([_pcs_position("p1", "SPY")], nav=5_000_000.0, cash=4_900_000.0)
        inputs = _base_inputs(portfolio=big_portfolio, opportunity_scan=cfg)
        result = run_outer_cycle(inputs)
        assert result.opportunity_scan_result.best is not None
        snap = result.opportunity_decision_snapshot
        assert snap is not None
        assert snap.recommended_action == ControlLoopAction.REVIEW
        assert snap.position_id is None
        assert snap.opportunity_proposal_id == result.opportunity_scan_result.best.candidate.proposal.proposal_id
        # persisted, not just returned in-memory
        persisted = inputs.control_loop_store.decision_snapshots_for_cycle("cycle-1")
        assert any(s.opportunity_proposal_id == snap.opportunity_proposal_id for s in persisted)

    def test_best_candidate_raises_a_new_opportunity_alert(self):
        cfg = _scan_config(no_trade_hurdle=-1000.0)
        big_portfolio = _portfolio([_pcs_position("p1", "SPY")], nav=5_000_000.0, cash=4_900_000.0)
        inputs = _base_inputs(portfolio=big_portfolio, opportunity_scan=cfg)
        result = run_outer_cycle(inputs)
        alert_types = {a.alert_type for a in result.new_alerts}
        assert ControlLoopAlertType.NEW_OPPORTUNITY in alert_types

    def test_no_universe_configured_skips_honestly(self):
        result = run_outer_cycle(_base_inputs())
        assert result.opportunity_scan_result is None
        assert "no opportunity-scan universe" in result.opportunity_scan_skipped_reason
        assert result.control_result.cycle_record.opportunities_scanned == 0
        assert result.control_result.cycle_record.candidates_generated == 0

    def test_explicit_skip_flag_honestly_reported(self):
        result = run_outer_cycle(_base_inputs(opportunity_scan=_scan_config(), skip_opportunity_scan=True))
        assert result.opportunity_scan_result is None
        assert "explicitly skipped" in result.opportunity_scan_skipped_reason

    def test_missing_chain_for_a_universe_ticker_never_creates_a_trade(self):
        # QQQ configured in the universe but no chain supplied for it --
        # never fabricated into a candidate.
        cfg = _scan_config(chains_by_ticker={})
        result = run_outer_cycle(_base_inputs(opportunity_scan=cfg))
        assert result.control_result.cycle_record.opportunities_scanned == 0
        assert result.control_result.cycle_record.candidates_generated == 0
        assert result.opportunity_decision_snapshot is None


class TestTicketMonitorWiring:
    def test_stale_ticket_is_repriced_and_reported(self):
        ticket = _pending_ticket()
        cfg = TicketMonitorConfig(
            pending_tickets=(ticket,),
            current_quotes_by_trade_id={ticket.trade_id: (2.5, 2.7, NOW)},  # below minimum_acceptable_price
        )
        result = run_outer_cycle(_base_inputs(ticket_monitor=cfg))
        assert result.ticket_monitor_result is not None
        assert result.ticket_monitor_result.reprice_required_count == 1

    def test_healthy_ticket_produces_no_reprice(self):
        ticket = _pending_ticket()
        cfg = TicketMonitorConfig(
            pending_tickets=(ticket,),
            current_quotes_by_trade_id={ticket.trade_id: (2.85, 3.05, NOW)},
        )
        result = run_outer_cycle(_base_inputs(ticket_monitor=cfg))
        assert result.ticket_monitor_result.reprice_required_count == 0

    def test_no_pending_tickets_skips_honestly(self):
        result = run_outer_cycle(_base_inputs())
        assert result.ticket_monitor_result is None
        assert "no pending tickets" in result.ticket_monitor_skipped_reason

    def test_explicit_skip_flag_honestly_reported(self):
        ticket = _pending_ticket()
        cfg = TicketMonitorConfig(pending_tickets=(ticket,), current_quotes_by_trade_id={ticket.trade_id: (2.85, 3.05, NOW)})
        result = run_outer_cycle(_base_inputs(ticket_monitor=cfg, skip_ticket_monitor=True))
        assert result.ticket_monitor_result is None
        assert "explicitly skipped" in result.ticket_monitor_skipped_reason


class TestRateLimitPriority:
    """Part 10: existing-position/risk monitoring > pending-ticket safety
    monitoring > new-opportunity scanning."""

    def _inputs_with_both_stages(self, rate_limit_state):
        ticket = _pending_ticket()
        return _base_inputs(
            rate_limit_state=rate_limit_state,
            ticket_monitor=TicketMonitorConfig(
                pending_tickets=(ticket,), current_quotes_by_trade_id={ticket.trade_id: (2.85, 3.05, NOW)},
            ),
            opportunity_scan=_scan_config(),
        )

    def test_opportunity_scanning_throttled_before_ticket_monitoring(self):
        # Utilization between P4's ceiling (0.80) and P3's (0.85): only
        # opportunity scanning is sacrificed.
        state = RateLimitState(allowed=100, used=82, available=18, reset_at=None, observed_at=NOW)
        result = run_outer_cycle(self._inputs_with_both_stages(state))
        assert result.opportunity_scan_result is None
        assert result.ticket_monitor_result is not None
        assert result.control_result.decision_snapshots  # risk monitoring untouched

    def test_both_optional_stages_throttled_before_risk_monitoring_is_ever_touched(self):
        # Utilization between P3's ceiling (0.85) and P2's (0.90): both
        # optional stages are sacrificed, but existing-position/risk
        # monitoring still runs (it has no gate at all in this module).
        state = RateLimitState(allowed=100, used=87, available=13, reset_at=None, observed_at=NOW)
        result = run_outer_cycle(self._inputs_with_both_stages(state))
        assert result.opportunity_scan_result is None
        assert result.ticket_monitor_result is None
        assert result.control_result.decision_snapshots

    def test_no_rate_limit_state_never_throttles_anything(self):
        result = run_outer_cycle(self._inputs_with_both_stages(None))
        assert result.opportunity_scan_result is not None
        assert result.ticket_monitor_result is not None


class TestExposurePersistence:
    def test_exposure_snapshot_is_saved_after_the_cycle(self):
        inputs = _base_inputs()
        run_outer_cycle(inputs)
        saved = inputs.control_loop_store.get_exposure_snapshot("cycle-1")
        assert saved is not None
        assert "SPY" in saved.underlying_exposure_pct


class TestAlertDeduplication:
    def test_repeated_cycles_with_the_same_condition_do_not_duplicate_alerts(self):
        halted_portfolio = _portfolio([_pcs_position("p1", "SPY")])
        halted_portfolio = halted_portfolio.model_copy(update={"halted": True, "halt_reason": "test halt"})
        store = InMemoryControlLoopStore()
        lifecycle_store = InMemoryLifecycleStore()
        first = run_outer_cycle(_base_inputs(portfolio=halted_portfolio, control_loop_store=store, lifecycle_store=lifecycle_store))
        second = run_outer_cycle(
            _base_inputs(cycle_id="cycle-2", portfolio=halted_portfolio, control_loop_store=store, lifecycle_store=lifecycle_store)
        )
        assert any(a.alert_type == ControlLoopAlertType.RISK_HALT for a in first.new_alerts)
        assert not any(a.alert_type == ControlLoopAlertType.RISK_HALT for a in second.new_alerts)
        assert len([a for a in store.all_unresolved_alerts() if a.alert_type == ControlLoopAlertType.RISK_HALT]) == 1


class TestDegradedDataAcceptance:
    """Part 9: SPY healthy, QQQ simulated failure -- QQQ isolated, SPY
    continues, degraded_mode=True, bad/missing QQQ data cannot create an
    actionable new trade, no fabricated quote/fill."""

    def test_qqq_position_failure_isolates_while_spy_continues(self):
        pos1 = _pcs_position("p1", "SPY")
        pos2 = _pcs_position("p2", "QQQ")
        portfolio = _portfolio([pos1, pos2])
        inputs = _base_inputs(
            portfolio=portfolio,
            fetch_results={"SPY": _spy_position_chain(), "QQQ": RuntimeError("simulated timeout")},
            policy_name_for_position={"p1": PCS_POLICY.name, "p2": PCS_POLICY.name},
            extra_monitoring_inputs={"p1": dict(_EXTRAS), "p2": dict(_EXTRAS)},
        )
        result = run_outer_cycle(inputs)
        snaps = {s.position_id: s for s in result.control_result.decision_snapshots}
        assert snaps["p2"].recommended_action == ControlLoopAction.DATA_INSUFFICIENT
        assert snaps["p1"].recommended_action != ControlLoopAction.DATA_INSUFFICIENT
        assert result.control_result.cycle_record.degraded_mode is True

    def test_missing_qqq_chain_in_opportunity_scan_cannot_produce_a_trade(self):
        # QQQ in the universe but no chain data available this cycle --
        # scan_and_rank_opportunities skips it (never fabricates) rather
        # than this stage refusing to run at all.
        cfg = _scan_config(universe=(UniverseEntry(ticker="QQQ", sector="ETF"),), chains_by_ticker={})
        result = run_outer_cycle(_base_inputs(opportunity_scan=cfg))
        assert result.opportunity_scan_result is not None
        assert result.opportunity_scan_result.candidates_generated == 0
        assert result.opportunity_scan_result.best is None
        assert result.control_result.cycle_record.opportunities_scanned == 0
        assert result.opportunity_decision_snapshot is None
