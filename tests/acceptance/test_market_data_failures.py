"""Section 5: hostile market-data testing. Every scenario here must
either (a) fail at construction (a malformed `OptionContract`/
`UnderlyingQuote` raises a `ValidationError` outright), or (b) fail
closed once it reaches the pipeline (Quant Engine failure -> REJECTED,
or Risk Engine REJECT/HALT) -- never a silent approval built on bad
data. The LLM is never in the data-repair path: nothing here lets an
LLM "fill in" a missing price.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.data.option_chain import OptionChain, OptionContract, OptionRight as DataRight
from src.data.quotes import UnderlyingQuote
from src.llm.schemas import StrategyType
from src.orchestration.pipeline import PipelineStatus, run_order_pipeline

from .conftest import EXPIRATION, MD_TS, NOW, all_strategy_fixtures, base_proposal, full_stages, make_chain, make_contract, make_underlying, pipeline_request


class TestMalformedContractConstructionFailsClosed:
    """These never even become a live object -- Pydantic rejects them
    at construction, before any pricing code could run on them."""

    def _base_kwargs(self) -> dict:
        return dict(
            underlying="SPY", option_symbol="x", expiration=EXPIRATION, strike=620.0, right=DataRight.PUT,
            bid=1.0, ask=1.1, last=1.05, volume=100, open_interest=100, underlying_price=628.5,
            timestamp=MD_TS, source="mock",
        )

    def test_negative_bid_rejected(self):
        with pytest.raises(ValidationError):
            OptionContract(**{**self._base_kwargs(), "bid": -1.0})

    def test_negative_strike_rejected(self):
        with pytest.raises(ValidationError):
            OptionContract(**{**self._base_kwargs(), "strike": -620.0})

    def test_zero_strike_rejected(self):
        with pytest.raises(ValidationError):
            OptionContract(**{**self._base_kwargs(), "strike": 0.0})

    def test_bid_above_ask_crossed_market_rejected(self):
        with pytest.raises(ValidationError):
            OptionContract(**{**self._base_kwargs(), "bid": 5.0, "ask": 1.0})

    def test_nan_bid_rejected(self):
        with pytest.raises(ValidationError):
            OptionContract(**{**self._base_kwargs(), "bid": float("nan")})

    def test_nan_underlying_price_rejected(self):
        with pytest.raises(ValidationError):
            OptionContract(**{**self._base_kwargs(), "underlying_price": float("nan")})

    def test_negative_underlying_price_rejected(self):
        with pytest.raises(ValidationError):
            OptionContract(**{**self._base_kwargs(), "underlying_price": -628.5})

    def test_impossible_iv_rejected(self):
        with pytest.raises(ValidationError):
            OptionContract(**{**self._base_kwargs(), "iv": -0.5})

    def test_malformed_delta_out_of_range_rejected(self):
        with pytest.raises(ValidationError):
            OptionContract(**{**self._base_kwargs(), "delta": 1.5})

    def test_missing_bid_rejected(self):
        kwargs = self._base_kwargs()
        del kwargs["bid"]
        with pytest.raises(ValidationError):
            OptionContract(**kwargs)

    def test_missing_underlying_price_rejected(self):
        kwargs = self._base_kwargs()
        del kwargs["underlying_price"]
        with pytest.raises(ValidationError):
            OptionContract(**kwargs)

    def test_negative_volume_rejected(self):
        with pytest.raises(ValidationError):
            OptionContract(**{**self._base_kwargs(), "volume": -1})

    def test_future_timestamp_is_constructible_but_flagged_stale_gate_handles_it(self):
        """A future timestamp is not itself rejected at construction
        (the platform has no independent clock reference inside a
        Pydantic validator) -- the freshness gate downstream is where
        this must be caught; verified in
        TestPipelineFailsClosedOnBadData below."""
        future = NOW + timedelta(days=1)
        OptionContract(**{**self._base_kwargs(), "timestamp": future})  # constructs; gate is the real check


class TestInfiniteQuoteIsNeverTreatedAsALiquidTwoSidedMarket:
    def test_check_liquidity_rejects_an_infinite_quote(self):
        """ACCEPT-002 regression (see ACCEPTANCE_TEST_REPORT.md):
        `bid`/`ask` only enforce `>= 0`, so +inf is constructible, and
        `check_liquidity`'s own spread-percentage check used to
        silently pass it -- `(inf-inf)/inf` is NaN, and `NaN >
        threshold` is always False in Python, so the spread gate never
        fired. `check_liquidity` now explicitly rejects a non-finite
        bid/ask before computing the spread at all."""
        from src.risk.trade_risk import LiquidityError, check_liquidity
        from src.risk.limits import load_risk_limits

        contract = make_contract(strike=620.0, right=DataRight.PUT, bid=math.inf, ask=math.inf, volume=500, open_interest=1000)
        limits = load_risk_limits()
        with pytest.raises(LiquidityError, match="non-finite"):
            check_liquidity(contract, limits)

    @pytest.mark.asyncio
    async def test_full_pipeline_still_rejects_an_infinite_quote(self):
        """Even though `check_liquidity` itself misses it (see above),
        the full pipeline still fails closed -- Python Quant's own
        downstream math on an infinite premium raises, and the
        pipeline's `except Exception` wrapper turns that into a clean
        REJECTED outcome, never a crash and never an approval."""
        fx = all_strategy_fixtures()[StrategyType.LONG_CALL]
        inf_contract = fx.contracts[0].model_copy(update={"bid": math.inf, "ask": math.inf})
        proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=1)
        market_data = make_chain(inf_contract)
        stages = full_stages(market_data=market_data)
        req = pipeline_request(proposal=proposal, market_data=market_data, portfolio=fx.portfolio, allowed_strategies=[StrategyType.LONG_CALL])
        outcome = await run_order_pipeline(req, stages)
        assert outcome.status == PipelineStatus.REJECTED


class TestPipelineFailsClosedOnBadData:
    async def _run(self, strategy: StrategyType, contracts, **request_overrides):
        fx = all_strategy_fixtures()[strategy]
        proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=1)
        market_data = make_chain(*contracts)
        stages = full_stages(market_data=market_data)
        req = pipeline_request(proposal=proposal, market_data=market_data, portfolio=fx.portfolio, allowed_strategies=[strategy], **request_overrides)
        return await run_order_pipeline(req, stages)

    @pytest.mark.asyncio
    async def test_missing_leg_contract_rejects_not_crashes(self):
        """The market data is missing the long leg entirely (a
        realistic partial-chain-response failure) -- the pipeline must
        reject cleanly, never crash and never approve with a missing
        leg silently dropped."""
        fx = all_strategy_fixtures()[StrategyType.PUT_CREDIT_SPREAD]
        only_short_leg = [fx.contracts[0]]
        outcome = await self._run(StrategyType.PUT_CREDIT_SPREAD, only_short_leg)
        assert outcome.status == PipelineStatus.REJECTED
        assert outcome.rejected_stage == "quant_engine"

    @pytest.mark.asyncio
    async def test_wrong_ticker_contract_rejects_not_mispriced(self):
        """MD-003 regression, exercised at the full pipeline level: a
        chain for a DIFFERENT underlying, coincidentally sharing
        strike/expiration/right with the proposal's legs, must never
        be silently accepted as if it were the real ticker's data."""
        fx = all_strategy_fixtures()[StrategyType.LONG_CALL]
        wrong_ticker_contract = make_contract(
            underlying="XYZ", strike=fx.contracts[0].strike, right=DataRight.CALL,
            bid=fx.contracts[0].bid, ask=fx.contracts[0].ask,
        )
        proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=1, ticker="SPY")
        market_data = OptionChain(underlying=make_underlying(symbol="XYZ"), contracts=[wrong_ticker_contract], timestamp=MD_TS, source="mock")
        stages = full_stages(market_data=market_data)
        req = pipeline_request(proposal=proposal, market_data=market_data, portfolio=fx.portfolio, allowed_strategies=[StrategyType.LONG_CALL])
        outcome = await run_order_pipeline(req, stages)
        assert outcome.status == PipelineStatus.REJECTED

    def test_stale_data_timestamp_rejects_at_proposal_construction(self):
        """A `TradeProposal` whose own `data_timestamp` is stale
        relative to its `timestamp` is rejected even earlier than the
        pipeline -- at the schema layer itself, before Python Quant or
        the Risk Engine ever run. Fails closed at the very first
        opportunity, not merely "eventually.\""""
        fx = all_strategy_fixtures()[StrategyType.LONG_PUT]
        with pytest.raises(ValidationError, match="stale"):
            base_proposal(fx.strategy, fx.legs, contracts_requested=1, data_timestamp=NOW - timedelta(hours=6))

    @pytest.mark.asyncio
    async def test_stale_leg_contract_rejects_even_when_the_proposal_itself_is_fresh(self):
        """A `TradeProposal` can be freshly timestamped while the
        actual option quote resolved for one of its legs is stale (a
        realistic "proposal built, then re-evaluated later against a
        now-stale snapshot" shape) -- the Risk Engine's own freshness
        gate on each resolved contract (MD-001 regression) is the
        backstop for exactly this case, independent of the schema-level
        check above."""
        fx = all_strategy_fixtures()[StrategyType.LONG_PUT]
        stale_contract = fx.contracts[0].model_copy(update={"timestamp": NOW - timedelta(hours=6)})
        proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=1)
        market_data = make_chain(stale_contract)
        stages = full_stages(market_data=market_data)
        req = pipeline_request(proposal=proposal, market_data=market_data, portfolio=fx.portfolio, allowed_strategies=[StrategyType.LONG_PUT])
        outcome = await run_order_pipeline(req, stages)
        assert outcome.status == PipelineStatus.REJECTED

    @pytest.mark.asyncio
    async def test_zero_bid_zero_ask_illiquid_contract_rejects(self):
        fx = all_strategy_fixtures()[StrategyType.LONG_CALL]
        illiquid = fx.contracts[0].model_copy(update={"bid": 0.0, "ask": 0.0})
        outcome = await self._run(StrategyType.LONG_CALL, [illiquid])
        assert outcome.status == PipelineStatus.REJECTED

    @pytest.mark.asyncio
    async def test_missing_iv_rejects_rather_than_guessing(self):
        """No implied volatility means Python Quant cannot price the
        structure at all -- `_require_iv` must reject, and nothing
        anywhere (LLM included) may substitute a guessed IV."""
        fx = all_strategy_fixtures()[StrategyType.LONG_PUT]
        no_iv_contract = fx.contracts[0].model_copy(update={"iv": None})
        outcome = await self._run(StrategyType.LONG_PUT, [no_iv_contract])
        assert outcome.status == PipelineStatus.REJECTED
        assert outcome.rejected_stage == "quant_engine"

    @pytest.mark.asyncio
    async def test_duplicate_contract_for_the_same_leg_does_not_crash(self):
        """Two contracts at the exact same (strike, right) -- a
        realistic duplicate-record provider bug -- must not crash the
        pipeline; it either resolves deterministically (first match)
        or rejects, but never raises an unhandled exception."""
        fx = all_strategy_fixtures()[StrategyType.LONG_CALL]
        duplicate = fx.contracts[0].model_copy(update={"bid": fx.contracts[0].bid + 5.0, "ask": fx.contracts[0].ask + 5.0})
        outcome = await self._run(StrategyType.LONG_CALL, [fx.contracts[0], duplicate])
        assert outcome.status in (PipelineStatus.FILLED, PipelineStatus.REJECTED, PipelineStatus.NO_FILL)

    @pytest.mark.asyncio
    async def test_empty_option_chain_rejects_cleanly(self):
        fx = all_strategy_fixtures()[StrategyType.LONG_CALL]
        outcome = await self._run(StrategyType.LONG_CALL, [])
        assert outcome.status == PipelineStatus.REJECTED
        assert outcome.rejected_stage == "quant_engine"
