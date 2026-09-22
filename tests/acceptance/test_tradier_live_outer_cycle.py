"""Step 22.4A Part 8: an OPTIONAL live-data acceptance test for the
outer Portfolio Control Loop orchestrator (`src.portfolio.orchestrator
.run_outer_cycle`).

**Requires a real, explicitly configured Tradier production token
(`OPTIONS_AGENT_TRADIER_TOKEN`); skipped entirely otherwise** -- this is
never run as part of the ordinary offline test suite and never blocks
`make test`/CI when no token is configured.

Uses an entirely simulated/in-memory `Portfolio` (never a real account),
`InMemoryLifecycleStore`, and `InMemoryControlLoopStore` -- creates NO
real brokerage order, uses NO Fidelity execution path, and starts NO
validation cohort (this module never imports `src.validation.session`'s
cohort-start machinery, or `src.brokers.fidelity`'s ticket-submission
path, at all). The only network call anywhere in this test is a
read-only Tradier market-data fetch (quote + one expiration's chain);
nothing here can place, preview, or cancel an order.

Does NOT hard-code that the resulting lifecycle action must be HOLD --
live market conditions can legitimately produce HOLD, EXIT, REVIEW, or
any other deterministic outcome; the acceptance criterion is that the
correct machinery runs safely end-to-end and produces an auditable
result, never a specific number.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from src.data.tradier_provider import TradierConfig, TradierMarketDataProvider
from src.data.option_chain import OptionChain
from src.dashboard.control_loop_projection import load_latest_control_loop_state
from src.dashboard.models import DashboardState
from src.lifecycle.persistence import InMemoryLifecycleStore
from src.lifecycle.policies_library import policies_for_strategy
from src.llm.schemas import StrategyType
from src.portfolio.orchestrator import OuterCycleInputs, run_outer_cycle
from src.portfolio.persistence import InMemoryControlLoopStore
from src.risk.limits import get_default_limits
from src.risk.portfolio_risk import Portfolio, PortfolioPosition, PortfolioPositionLeg
from src.strategies.base import StrategyKind

pytestmark = pytest.mark.skipif(
    not os.environ.get("OPTIONS_AGENT_TRADIER_TOKEN"),
    reason="OPTIONS_AGENT_TRADIER_TOKEN not configured -- this optional live-data acceptance test is skipped",
)

PCS_POLICY = policies_for_strategy(StrategyKind.PUT_CREDIT_SPREAD)[0]


async def _fetch_live_spy_chain_and_quote(provider: TradierMarketDataProvider):
    quote = await provider.get_underlying_quote("SPY")
    expirations = await provider.get_expirations("SPY")
    assert expirations, "Tradier returned no SPY expirations -- cannot proceed with a live acceptance test"
    target_dte = 30
    chosen = min(expirations, key=lambda d: abs((d - datetime.now(timezone.utc).date()).days - target_dte))
    contracts = await provider.get_option_chain_for_expiration("SPY", chosen)
    chain = OptionChain(underlying=quote, contracts=contracts, timestamp=datetime.now(timezone.utc), source="tradier")
    return quote, chosen, chain


def _pick_put_credit_spread_strikes(chain: OptionChain, spot: float) -> tuple[float, float]:
    puts = sorted({c.strike for c in chain.contracts if c.right.value == "P" and c.strike < spot})
    assert len(puts) >= 2, "not enough live OTM put strikes returned to build a simulated PCS position"
    short_strike = min(puts, key=lambda s: abs(s - spot * 0.98))
    remaining = [s for s in puts if s < short_strike]
    long_strike = max(remaining) if remaining else min(puts)
    return short_strike, long_strike


def _mid_for(chain: OptionChain, strike: float) -> float:
    contract = next(c for c in chain.contracts if c.right.value == "P" and c.strike == strike)
    return contract.mid


@pytest.mark.asyncio
class TestLiveOuterCycleAcceptance:
    async def test_full_outer_cycle_runs_safely_against_real_market_data(self):
        provider = TradierMarketDataProvider(TradierConfig())
        try:
            quote, expiration, chain = await _fetch_live_spy_chain_and_quote(provider)
        finally:
            await provider.close()

        spot = quote.last
        short_strike, long_strike = _pick_put_credit_spread_strikes(chain, spot)
        short_entry = _mid_for(chain, short_strike)
        long_entry = _mid_for(chain, long_strike)

        now = datetime.now(timezone.utc)
        position = PortfolioPosition(
            position_id="live-p1", ticker="SPY", sector="ETF", strategy=StrategyType.PUT_CREDIT_SPREAD,
            expiration=expiration,
            legs=[
                PortfolioPositionLeg(right="P", side="sell", strike=short_strike, entry_price=short_entry),
                PortfolioPositionLeg(right="P", side="buy", strike=long_strike, entry_price=long_entry),
            ],
            contracts=1, capital_at_risk=(short_strike - long_strike) * 100, max_loss=(short_strike - long_strike) * 100,
            opened_at=now - timedelta(days=1),
        )
        portfolio = Portfolio(as_of=now, nav=100000.0, cash=90000.0, peak_equity=100000.0, positions=[position], sector_by_ticker={"SPY": "ETF"})

        lifecycle_store = InMemoryLifecycleStore()
        control_loop_store = InMemoryControlLoopStore()

        inputs = OuterCycleInputs(
            cycle_id="live-cycle-1", as_of=now, portfolio=portfolio, limits=get_default_limits(),
            provider="tradier", provider_health_status="healthy", is_trading_day=True, is_market_open=True,
            fetch_results={"SPY": chain}, lifecycle_store=lifecycle_store, control_loop_store=control_loop_store,
            policy_name_for_position={"live-p1": PCS_POLICY.name},
            extra_monitoring_inputs={
                "live-p1": dict(
                    earnings_data_available=False, days_to_earnings=None, initial_credit=short_entry - long_entry,
                    profit_capture_denominator=(short_entry - long_entry) * 100, short_leg_is_itm=short_strike > spot,
                    short_leg_extrinsic_value=short_entry, position_delta_abs=0.20,
                )
            },
        )

        result = run_outer_cycle(inputs)

        # -- existing-position control cycle completes
        assert result.control_result.cycle_record.is_complete

        # -- market data passes the quality gate when healthy (never
        # asserted unconditionally -- a genuinely stale/thin live quote
        # legitimately isolates to DATA_INSUFFICIENT, which is itself a
        # valid, honestly-reported outcome, not a test failure).
        snap = next(s for s in result.control_result.decision_snapshots if s.position_id == "live-p1")
        if "SPY" in result.control_result.cycle_record.symbols_successful:
            assert result.control_result.valuation.is_complete or snap.recommended_action.value == "data_insufficient"

        # -- valuation and exposure exist
        assert result.control_result.valuation is not None
        assert result.control_result.exposure is not None
        assert "SPY" in result.control_result.exposure.underlying_exposure_pct

        # -- a lifecycle/risk decision snapshot exists for the position,
        # with no hard-coded expected action (Part 8's own instruction).
        assert snap.recommended_action is not None

        # -- opportunity/ticket stages are represented honestly (both
        # skipped, since neither was configured this cycle) rather than
        # silently absent from the result.
        assert result.ticket_monitor_result is None
        assert result.ticket_monitor_skipped_reason is not None
        assert result.opportunity_scan_result is None
        assert result.opportunity_scan_skipped_reason is not None

        # -- exposure was actually persisted (Part 5's prerequisite for
        # dashboard projection)
        assert control_loop_store.get_exposure_snapshot("live-cycle-1") is not None

        # -- dashboard projection can consume the result
        dashboard_state = DashboardState(portfolio=portfolio, limits=get_default_limits())
        load_latest_control_loop_state(dashboard_state, control_loop_store=control_loop_store)
        assert dashboard_state.latest_cycle_record is not None
        assert dashboard_state.latest_cycle_record.cycle_id == "live-cycle-1"
        assert dashboard_state.latest_exposure is not None

        # -- no real order, no Fidelity execution, no validation cohort:
        # structurally guaranteed -- this test never imports
        # src.brokers.fidelity's ticket-submission path or
        # src.validation.session's cohort-start machinery at all.
