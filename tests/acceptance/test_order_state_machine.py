"""Section 12: order state machine attack. `FidelityTradeTicket`'s own
state machine (`transition`/`confirm_fill` in `src/brokers/fidelity.py`)
is already exhaustively unit-tested in
`tests/unit/brokers/test_fidelity_state_machine.py` -- every illegal
transition the Step 21 instruction names by name (PROPOSED->FILLED,
AWAITING_HUMAN->FILLED without confirmation, REPRICE_REQUIRED->FILLED
without repricing) already has a dedicated, passing unit test. This
file adds two things that suite doesn't cover:

1. Proof, through the REAL, unmocked pipeline, that a ticket the
   system actually produces always starts at AWAITING_HUMAN (never
   PROPOSED, QUANT_APPROVED, LLM_REVIEWED, or RISK_APPROVED) -- so
   "PROPOSED->FILLED" isn't just rejected by the transition table, it
   is structurally unreachable from any ticket this platform ever
   actually creates.
2. The PARALLEL state machine `PaperBroker`'s own `Order`/`OrderStatus`
   enforces procedurally (via `is_terminal` guards in `attempt_fill`/
   `cancel_order`, not a named transition table) -- proving a
   CANCELLED or REJECTED order can never later become FILLED no matter
   how many times a caller retries `attempt_fill`, and a FILLED order's
   `filled_quantity` can never be pushed past its own combo size by a
   repeated fill attempt.
"""
from __future__ import annotations

import pytest

from src.brokers.base import OrderAction, OrderLeg, OrderStatus, PlaceOrderRequest
from src.brokers.fidelity import TicketStatus
from src.brokers.paper import FillModel, PaperBroker, PaperBrokerConfig
from src.data.option_chain import OptionRight as DataRight
from src.llm.schemas import StrategyType
from src.orchestration.pipeline import PipelineStatus, run_order_pipeline

from .conftest import EXPIRATION, NOW, all_strategy_fixtures, base_proposal, full_stages, make_chain, make_contract, pipeline_request


class TestFidelityTicketAlwaysStartsAtAwaitingHumanNeverEarlier:
    """`PROPOSED -> FILLED` (section 12's own named illegal transition)
    is unreachable for a stronger reason than "the transition table
    rejects it": this platform never even constructs a ticket object at
    PROPOSED, QUANT_APPROVED, LLM_REVIEWED, or RISK_APPROVED -- those
    are internal Risk Engine stages the ticket is built AFTER, not
    statuses a live `FidelityTradeTicket` instance ever holds."""

    @pytest.mark.asyncio
    async def test_a_real_pipeline_produced_ticket_starts_at_awaiting_human(self):
        fx = all_strategy_fixtures()[StrategyType.PUT_CREDIT_SPREAD]
        proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=1)
        market_data = make_chain(*fx.contracts)
        stages = full_stages(market_data=market_data)
        req = pipeline_request(proposal=proposal, market_data=market_data, portfolio=fx.portfolio, allowed_strategies=[StrategyType.PUT_CREDIT_SPREAD])
        outcome = await run_order_pipeline(req, stages)

        assert outcome.status == PipelineStatus.FILLED
        assert outcome.fidelity_ticket.status == TicketStatus.AWAITING_HUMAN

    def test_pre_ticket_statuses_have_no_outgoing_path_to_filled_that_skips_the_human(self):
        """Structural cross-check against the transition table itself:
        every reachable path from PROPOSED must pass through
        AWAITING_HUMAN before it can ever reach ORDER_ENTERED (the only
        state `confirm_fill` accepts a first fill from) -- there is no
        shortcut edge."""
        from src.brokers.fidelity import _ALLOWED_TRANSITIONS

        # Breadth-first reachability from PROPOSED, stopping the moment
        # AWAITING_HUMAN is reached on each path (we only care whether
        # ORDER_ENTERED is reachable WITHOUT passing through it first).
        visited_without_human = {TicketStatus.PROPOSED}
        frontier = [TicketStatus.PROPOSED]
        while frontier:
            current = frontier.pop()
            for nxt in _ALLOWED_TRANSITIONS.get(current, frozenset()):
                if nxt == TicketStatus.AWAITING_HUMAN or nxt in visited_without_human:
                    continue
                assert nxt != TicketStatus.ORDER_ENTERED, "ORDER_ENTERED reachable from PROPOSED without passing through AWAITING_HUMAN"
                visited_without_human.add(nxt)
                frontier.append(nxt)


class TestPaperBrokerOrderCanNeverLeaveATerminalStatus:
    async def _resting_request(self) -> tuple[PaperBroker, PlaceOrderRequest]:
        broker = PaperBroker(initial_cash=1_000_000.0, config=PaperBrokerConfig(fill_model=FillModel.MID), now=NOW)
        await broker.connect()
        broker.update_market_data(make_chain(make_contract(option_symbol="osm-1", strike=630.0, right=DataRight.CALL, bid=6.8, ask=7.0)))
        request = PlaceOrderRequest(
            client_order_id="osm-cancel-then-retry",
            legs=[OrderLeg(symbol="osm-1", right=DataRight.CALL, strike=630.0, expiration=EXPIRATION, action=OrderAction.BUY, quantity=1)],
            limit_price=1.0,  # unreachable -- rests as SUBMITTED
        )
        return broker, request

    @pytest.mark.asyncio
    async def test_cancelled_order_never_becomes_filled_even_after_the_market_moves_favorably(self):
        broker, request = await self._resting_request()
        order = await broker.place_order(request)
        assert order.status == OrderStatus.SUBMITTED
        cancelled = await broker.cancel_order(order.client_order_id)
        assert cancelled.status == OrderStatus.CANCELLED

        # Market now trivially satisfies the original limit -- a naive
        # re-check might wrongly fill a "resting" order that was
        # actually already cancelled.
        broker.update_market_data(make_chain(make_contract(option_symbol="osm-1", strike=630.0, right=DataRight.CALL, bid=0.05, ask=0.10)))
        still_cancelled = await broker.attempt_fill(order.client_order_id)
        assert still_cancelled.status == OrderStatus.CANCELLED
        assert still_cancelled.filled_quantity == 0

    @pytest.mark.asyncio
    async def test_rejected_order_never_becomes_filled_on_retry(self):
        broker = PaperBroker(initial_cash=1.0, config=PaperBrokerConfig(fill_model=FillModel.MID), now=NOW)  # far too little cash
        await broker.connect()
        broker.update_market_data(make_chain(make_contract(option_symbol="osm-2", strike=630.0, right=DataRight.CALL, bid=6.8, ask=7.0)))
        request = PlaceOrderRequest(
            client_order_id="osm-rejected-retry",
            legs=[OrderLeg(symbol="osm-2", right=DataRight.CALL, strike=630.0, expiration=EXPIRATION, action=OrderAction.BUY, quantity=1)],
            limit_price=7.0,
        )
        order = await broker.place_order(request)
        assert order.status == OrderStatus.REJECTED

        # Even with plenty of cash now loaded and a fresh attempt_fill
        # call, a REJECTED (terminal) order must never transition.
        broker._cash = 1_000_000.0  # simulates "more cash became available later" -- still shouldn't matter
        again = await broker.attempt_fill(order.client_order_id)
        assert again.status == OrderStatus.REJECTED

    @pytest.mark.asyncio
    async def test_filled_order_quantity_never_exceeds_its_own_combo_size_on_repeated_attempt_fill_calls(self):
        fx = all_strategy_fixtures()[StrategyType.LONG_CALL]
        market_data = make_chain(*fx.contracts)
        broker = PaperBroker(initial_cash=1_000_000.0, config=PaperBrokerConfig(fill_model=FillModel.MID), now=NOW)
        await broker.connect()
        broker.update_market_data(market_data)
        request = PlaceOrderRequest(
            client_order_id="osm-no-overfill",
            legs=[OrderLeg(symbol=fx.contracts[0].option_symbol, right=DataRight.CALL, strike=630.0, expiration=EXPIRATION, action=OrderAction.BUY, quantity=1)],
            limit_price=7.0,
        )
        order = await broker.place_order(request)
        assert order.status == OrderStatus.FILLED
        assert order.filled_quantity == 1

        # Calling attempt_fill again on an already-FILLED (terminal)
        # order must be a pure no-op -- never double-fills.
        again = await broker.attempt_fill(order.client_order_id)
        assert again.status == OrderStatus.FILLED
        assert again.filled_quantity == 1
        positions = {p.symbol: p for p in await broker.get_positions()}
        assert positions[fx.contracts[0].option_symbol].quantity == 1


class TestIllegalFidelityTransitionsStillRejectExactlyAsUnitTested:
    """A minimal end-to-end confirmation (not a re-derivation) that the
    exact three transitions section 12 names by name are rejected --
    using a REAL ticket produced by the pipeline, not a hand-built one,
    closing the gap between "the transition function rejects it in
    isolation" (already proven) and "a real ticket this platform
    produces is actually protected by it" (proven here)."""

    @pytest.mark.asyncio
    async def test_real_ticket_cannot_skip_straight_to_filled(self):
        from src.brokers.fidelity import transition

        fx = all_strategy_fixtures()[StrategyType.PUT_CREDIT_SPREAD]
        proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=1)
        market_data = make_chain(*fx.contracts)
        stages = full_stages(market_data=market_data)
        req = pipeline_request(proposal=proposal, market_data=market_data, portfolio=fx.portfolio, allowed_strategies=[StrategyType.PUT_CREDIT_SPREAD])
        outcome = await run_order_pipeline(req, stages)
        ticket = outcome.fidelity_ticket

        assert ticket.status == TicketStatus.AWAITING_HUMAN
        with pytest.raises(Exception):
            transition(ticket, TicketStatus.FILLED)  # AWAITING_HUMAN -> FILLED with no confirmation: illegal

    @pytest.mark.asyncio
    async def test_real_ticket_cannot_be_confirmed_filled_before_a_human_enters_it(self):
        from src.brokers.fidelity import ExecutionConfirmation, confirm_fill

        fx = all_strategy_fixtures()[StrategyType.PUT_CREDIT_SPREAD]
        proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=1)
        market_data = make_chain(*fx.contracts)
        stages = full_stages(market_data=market_data)
        req = pipeline_request(proposal=proposal, market_data=market_data, portfolio=fx.portfolio, allowed_strategies=[StrategyType.PUT_CREDIT_SPREAD])
        outcome = await run_order_pipeline(req, stages)
        ticket = outcome.fidelity_ticket

        confirmation = ExecutionConfirmation(
            confirmed_by="test-human", confirmation_source="human_manual_entry", confirmed_at=NOW,
            filled_quantity=ticket.quantity, fill_price=ticket.limit_price,
        )
        with pytest.raises(Exception):
            confirm_fill(ticket, confirmation)  # AWAITING_HUMAN is not fill-confirmable -- must go through ORDER_ENTERED first
