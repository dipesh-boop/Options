"""Tests for `src.portfolio.exposure` (Step 22.4 Part 15)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from src.llm.schemas import StrategyType
from src.portfolio.exposure import build_exposure_snapshot
from src.risk.limits import get_default_limits
from src.risk.portfolio_risk import Portfolio, PortfolioPosition, PortfolioPositionLeg, UnderlyingHolding
from src.wheel.models import WheelAccounting, WheelPosition
from src.wheel.state import WheelState

NOW = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)
EXP = date(2026, 10, 23)
LIMITS = get_default_limits()


def _portfolio() -> Portfolio:
    pos1 = PortfolioPosition(
        position_id="p1", ticker="SPY", sector="ETF", strategy=StrategyType.PUT_CREDIT_SPREAD, expiration=EXP,
        legs=[
            PortfolioPositionLeg(right="P", side="sell", strike=450.0, entry_price=6.0),
            PortfolioPositionLeg(right="P", side="buy", strike=440.0, entry_price=3.0),
        ],
        contracts=2, capital_at_risk=1400.0, max_loss=1400.0, opened_at=NOW - timedelta(days=5),
    )
    pos2 = PortfolioPosition(
        position_id="p2", ticker="QQQ", sector="ETF", strategy=StrategyType.COVERED_CALL, expiration=EXP,
        legs=[PortfolioPositionLeg(right="C", side="sell", strike=400.0, entry_price=5.0)],
        contracts=1, capital_at_risk=500.0, max_loss=500.0, opened_at=NOW - timedelta(days=10),
    )
    return Portfolio(
        as_of=NOW, nav=100000.0, cash=70000.0, peak_equity=105000.0, positions=[pos1, pos2],
        underlying_holdings={"QQQ": UnderlyingHolding(shares=100, cost_basis=380.0)},
        sector_by_ticker={"SPY": "ETF", "QQQ": "ETF"},
    )


class TestBuildExposureSnapshot:
    def test_underlying_and_sector_exposure_aggregated(self):
        snap = build_exposure_snapshot(_portfolio(), as_of=NOW, limits=LIMITS)
        assert snap.underlying_exposure_pct == {"SPY": 0.014, "QQQ": 0.005}
        assert snap.sector_exposure_pct == {"ETF": 0.019}

    def test_strategy_and_expiration_concentration(self):
        snap = build_exposure_snapshot(_portfolio(), as_of=NOW, limits=LIMITS)
        assert snap.strategy_exposure_pct[StrategyType.PUT_CREDIT_SPREAD.value] == 0.014
        assert snap.expiration_concentration_pct[EXP] == 0.019

    def test_directional_and_volatility_labels(self):
        snap = build_exposure_snapshot(_portfolio(), as_of=NOW, limits=LIMITS, portfolio_delta=150.0, portfolio_vega=30.0)
        assert snap.directional_exposure == "net_long"
        assert snap.volatility_exposure == "net_long_vol"

    def test_unknown_when_greeks_not_supplied(self):
        snap = build_exposure_snapshot(_portfolio(), as_of=NOW, limits=LIMITS)
        assert snap.directional_exposure == "unknown"
        assert snap.portfolio_delta is None

    def test_short_option_capital_pct_counts_positions_with_a_short_leg(self):
        snap = build_exposure_snapshot(_portfolio(), as_of=NOW, limits=LIMITS)
        assert abs(snap.short_option_capital_pct - 0.019) < 1e-9

    def test_assignment_risk_ids_are_passed_through_not_computed(self):
        snap = build_exposure_snapshot(_portfolio(), as_of=NOW, limits=LIMITS, assignment_risk_position_ids=("p2",))
        assert snap.assignment_risk_position_ids == ("p2",)

    def test_owned_share_exposure_uses_current_price_when_supplied(self):
        snap = build_exposure_snapshot(_portfolio(), as_of=NOW, limits=LIMITS, current_underlying_prices={"QQQ": 405.0})
        assert abs(snap.owned_share_exposure_pct - (100 * 405.0 / 100000.0)) < 1e-9
        assert snap.owned_share_prices_missing == ()

    def test_owned_share_exposure_falls_back_to_cost_basis_when_price_missing(self):
        snap = build_exposure_snapshot(_portfolio(), as_of=NOW, limits=LIMITS)
        assert snap.owned_share_prices_missing == ("QQQ",)
        assert abs(snap.owned_share_exposure_pct - (100 * 380.0 / 100000.0)) < 1e-9

    def test_wheel_cash_commitment_counts_active_wheel(self):
        wheel = WheelPosition(
            wheel_id="w1", ticker="AAPL", state=WheelState.CSP_OPEN, started_at=NOW - timedelta(days=3),
            accounting=WheelAccounting(capital_committed=20000.0, max_capital_committed=20000.0),
        )
        snap = build_exposure_snapshot(_portfolio(), as_of=NOW, limits=LIMITS, wheel_positions=(wheel,))
        assert abs(snap.wheel_cash_commitment_pct - 0.20) < 1e-9

    def test_halted_wheel_still_counted_as_active_capital(self):
        wheel = WheelPosition(
            wheel_id="w1", ticker="AAPL", state=WheelState.WHEEL_HALTED, started_at=NOW - timedelta(days=3),
            accounting=WheelAccounting(capital_committed=20000.0, max_capital_committed=20000.0),
            halt_reason="test",
        )
        snap = build_exposure_snapshot(_portfolio(), as_of=NOW, limits=LIMITS, wheel_positions=(wheel,))
        assert abs(snap.wheel_cash_commitment_pct - 0.20) < 1e-9

    def test_terminal_wheel_state_frees_capital(self):
        wheel = WheelPosition(
            wheel_id="w1", ticker="AAPL", state=WheelState.WHEEL_EXITED, started_at=NOW - timedelta(days=3),
            accounting=WheelAccounting(capital_committed=20000.0, max_capital_committed=20000.0),
            exit_reason="test",
        )
        snap = build_exposure_snapshot(_portfolio(), as_of=NOW, limits=LIMITS, wheel_positions=(wheel,))
        assert snap.wheel_cash_commitment_pct == 0.0

    def test_covered_call_encumbered_shares_from_open_cc_cycle(self):
        from src.wheel.models import CcCycle

        wheel = WheelPosition(
            wheel_id="w1", ticker="QQQ", state=WheelState.CC_OPEN, started_at=NOW - timedelta(days=3),
            cc_cycles=(
                CcCycle(
                    cycle_id="cc1", wheel_id="w1", strike=400.0, expiration=EXP, contracts=1,
                    premium_received_per_share=2.0, opened_at=NOW - timedelta(days=1),
                ),
            ),
        )
        snap = build_exposure_snapshot(_portfolio(), as_of=NOW, limits=LIMITS, wheel_positions=(wheel,))
        assert snap.covered_call_encumbered_shares == {"QQQ": 100}

    def test_empty_portfolio_handled_cleanly(self):
        empty = Portfolio(as_of=NOW, nav=100000.0, cash=100000.0, peak_equity=100000.0)
        snap = build_exposure_snapshot(empty, as_of=NOW, limits=LIMITS)
        assert snap.underlying_exposure_pct == {}
        assert snap.short_option_capital_pct == 0.0
