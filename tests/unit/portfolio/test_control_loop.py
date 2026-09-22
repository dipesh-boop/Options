"""Tests for `src.portfolio.control_loop` (Step 22.4 Part 12)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from src.data.option_chain import OptionChain, OptionContract, OptionRight
from src.data.quotes import UnderlyingQuote
from src.lifecycle.persistence import InMemoryLifecycleStore
from src.lifecycle.policies_library import policies_for_strategy
from src.llm.schemas import StrategyType
from src.portfolio.actions import ControlLoopAction
from src.portfolio.control_loop import ControlCycleInputs, run_control_cycle
from src.portfolio.persistence import InMemoryControlLoopStore
from src.risk.limits import get_default_limits
from src.risk.portfolio_risk import Portfolio, PortfolioPosition, PortfolioPositionLeg
from src.strategies.base import StrategyKind

NOW = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)
EXP = date(2026, 10, 23)
LIMITS = get_default_limits()
PCS_POLICY = policies_for_strategy(StrategyKind.PUT_CREDIT_SPREAD)[0]


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


def _spy_chain(*, timestamp=NOW) -> OptionChain:
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


_EXTRAS = {
    "earnings_data_available": True, "days_to_earnings": 9999, "initial_credit": 3.0,
    "profit_capture_denominator": 600.0, "short_leg_is_itm": False, "short_leg_extrinsic_value": 4.6,
    "position_delta_abs": 0.20,
}


def _base_inputs(**overrides) -> ControlCycleInputs:
    pos1 = _pcs_position(position_id="p1", ticker="SPY")
    portfolio = Portfolio(as_of=NOW, nav=100000.0, cash=80000.0, peak_equity=100000.0, positions=[pos1], sector_by_ticker={"SPY": "ETF"})
    fields = dict(
        cycle_id="cycle-1", as_of=NOW, portfolio=portfolio, limits=LIMITS, provider="tradier",
        provider_health_status="healthy", is_trading_day=True, is_market_open=True,
        fetch_results={"SPY": _spy_chain()},
        lifecycle_store=InMemoryLifecycleStore(), control_loop_store=InMemoryControlLoopStore(),
        policy_name_for_position={"p1": PCS_POLICY.name}, extra_monitoring_inputs={"p1": dict(_EXTRAS)},
    )
    fields.update(overrides)
    return ControlCycleInputs(**fields)


class TestHealthyCycle:
    def test_position_with_full_market_data_and_policy_evaluates_to_hold(self):
        result = run_control_cycle(_base_inputs())
        snap = next(s for s in result.decision_snapshots if s.position_id == "p1")
        assert snap.recommended_action == ControlLoopAction.HOLD

    def test_cycle_record_reflects_successful_symbols(self):
        result = run_control_cycle(_base_inputs())
        assert result.cycle_record.symbols_successful == ("SPY",)
        assert result.cycle_record.symbols_failed == ()
        assert result.cycle_record.degraded_mode is False

    def test_valuation_and_exposure_are_populated(self):
        result = run_control_cycle(_base_inputs())
        assert result.valuation.is_complete is True
        assert "SPY" in result.exposure.underlying_exposure_pct

    def test_lifecycle_store_receives_position_record_and_snapshot(self):
        inputs = _base_inputs()
        run_control_cycle(inputs)
        rec = inputs.lifecycle_store.get_position("p1")
        assert rec is not None and rec.management_policy_name == PCS_POLICY.name
        assert len(inputs.lifecycle_store.snapshots_for_trade("p1")) == 1

    def test_control_loop_store_receives_snapshot_and_cycle_record(self):
        inputs = _base_inputs()
        run_control_cycle(inputs)
        assert len(inputs.control_loop_store.decision_snapshots_for_cycle("cycle-1")) == 1
        assert inputs.control_loop_store.get_cycle_record("cycle-1") is not None


class TestDegradedProvider:
    def test_missing_chain_for_a_position_isolates_to_data_insufficient(self):
        pos1 = _pcs_position(position_id="p1", ticker="SPY")
        pos2 = _pcs_position(position_id="p2", ticker="QQQ")
        portfolio = Portfolio(as_of=NOW, nav=100000.0, cash=80000.0, peak_equity=100000.0, positions=[pos1, pos2], sector_by_ticker={"SPY": "ETF", "QQQ": "ETF"})
        inputs = _base_inputs(portfolio=portfolio, fetch_results={"SPY": _spy_chain(), "QQQ": RuntimeError("timeout")})
        result = run_control_cycle(inputs)
        snaps = {s.position_id: s for s in result.decision_snapshots}
        assert snaps["p2"].recommended_action == ControlLoopAction.DATA_INSUFFICIENT
        assert snaps["p1"].recommended_action != ControlLoopAction.DATA_INSUFFICIENT
        assert result.cycle_record.degraded_mode is True

    def test_stale_chain_rejected_by_quality_gate_isolates_position(self):
        old = NOW - timedelta(minutes=30)
        inputs = _base_inputs(fetch_results={"SPY": _spy_chain(timestamp=old)})
        result = run_control_cycle(inputs)
        snap = next(s for s in result.decision_snapshots if s.position_id == "p1")
        assert snap.recommended_action == ControlLoopAction.DATA_INSUFFICIENT

    def test_one_position_failure_never_aborts_the_whole_cycle(self):
        pos1 = _pcs_position(position_id="p1", ticker="SPY")
        pos2 = _pcs_position(position_id="p2", ticker="QQQ")
        portfolio = Portfolio(as_of=NOW, nav=100000.0, cash=80000.0, peak_equity=100000.0, positions=[pos1, pos2], sector_by_ticker={"SPY": "ETF", "QQQ": "ETF"})
        inputs = _base_inputs(portfolio=portfolio, fetch_results={"SPY": _spy_chain(), "QQQ": RuntimeError("timeout")})
        result = run_control_cycle(inputs)
        assert len(result.decision_snapshots) == 2  # both positions produced a decision, no exception


class TestMarketClosed:
    def test_is_market_open_false_is_recorded_on_every_snapshot(self):
        inputs = _base_inputs(is_market_open=False, is_trading_day=False)
        result = run_control_cycle(inputs)
        snap = next(s for s in result.decision_snapshots if s.position_id == "p1")
        assert snap.is_market_open is False
        assert result.cycle_record.market_open is False


class TestRiskHalt:
    def test_manual_halt_propagates_to_portfolio_halt_action(self):
        pos1 = _pcs_position(position_id="p1", ticker="SPY")
        halted_portfolio = Portfolio(
            as_of=NOW, nav=100000.0, cash=80000.0, peak_equity=100000.0, positions=[pos1],
            sector_by_ticker={"SPY": "ETF"}, halted=True, halt_reason="test halt",
        )
        inputs = _base_inputs(portfolio=halted_portfolio)
        result = run_control_cycle(inputs)
        assert result.kill_switch.halted is True
        assert result.cycle_record.halt_state is True
        snap = next(s for s in result.decision_snapshots if s.position_id == "p1")
        assert snap.recommended_action == ControlLoopAction.PORTFOLIO_HALT
        assert snap.human_action_required is True

    def test_drawdown_halt_propagates(self):
        pos1 = _pcs_position(position_id="p1", ticker="SPY")
        drawdown_portfolio = Portfolio(
            as_of=NOW, nav=80000.0, cash=60000.0, peak_equity=100000.0, positions=[pos1], sector_by_ticker={"SPY": "ETF"},
        )
        inputs = _base_inputs(portfolio=drawdown_portfolio)
        result = run_control_cycle(inputs)
        assert result.kill_switch.halted is True  # 20% drawdown exceeds the 15% halt threshold


class TestPolicyConfiguration:
    def test_unknown_policy_name_isolated_never_crashes(self):
        inputs = _base_inputs(policy_name_for_position={"p1": "NOT_A_REAL_POLICY"})
        result = run_control_cycle(inputs)
        snap = next(s for s in result.decision_snapshots if s.position_id == "p1")
        assert snap.recommended_action == ControlLoopAction.DATA_INSUFFICIENT

    def test_missing_policy_configuration_isolated_and_flags_human_action(self):
        inputs = _base_inputs(policy_name_for_position={})
        result = run_control_cycle(inputs)
        snap = next(s for s in result.decision_snapshots if s.position_id == "p1")
        assert snap.recommended_action == ControlLoopAction.DATA_INSUFFICIENT
        assert snap.human_action_required is True

    def test_second_cycle_reuses_persisted_lifecycle_record_without_repeated_policy_name(self):
        inputs = _base_inputs()
        run_control_cycle(inputs)
        inputs2 = ControlCycleInputs(**{**inputs.__dict__, "cycle_id": "cycle-2", "policy_name_for_position": {}})
        result2 = run_control_cycle(inputs2)
        snap = next(s for s in result2.decision_snapshots if s.position_id == "p1")
        assert snap.recommended_action == ControlLoopAction.HOLD


class TestOpenPositionMonitoring:
    def test_lifecycle_trigger_fires_a_non_hold_action(self):
        # A short leg that has gone deep ITM should trip the lifecycle
        # engine's assignment-risk rule via extras.
        extras = {**_EXTRAS, "short_leg_is_itm": True, "short_leg_extrinsic_value": 0.05}
        inputs = _base_inputs(extra_monitoring_inputs={"p1": extras})
        result = run_control_cycle(inputs)
        snap = next(s for s in result.decision_snapshots if s.position_id == "p1")
        assert snap.recommended_action != ControlLoopAction.HOLD

    def test_lifecycle_triggers_counted_in_cycle_record(self):
        extras = {**_EXTRAS, "short_leg_is_itm": True, "short_leg_extrinsic_value": 0.05}
        inputs = _base_inputs(extra_monitoring_inputs={"p1": extras})
        result = run_control_cycle(inputs)
        assert result.cycle_record.lifecycle_triggers >= 1
