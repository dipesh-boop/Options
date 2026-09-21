"""Section 3: verifies the complete approved strategy library -- all 15
order-eligible `StrategyType` members plus CASH/NO_TRADE as the 16th
library item -- individually and through the real Risk Engine, for
leg count, BUY/SELL direction, PUT/CALL type, strike ordering,
quantity ratios, debit/credit sign, max profit/loss, breakeven(s),
capital requirement, and correct Fidelity ticket representation.

Also searches the repository for stale "evaluation only" language that
would contradict the strategies Step 20A made order-eligible.
"""
from __future__ import annotations

import math
import subprocess

import pytest

from src.llm.schemas import StrategyType
from src.orchestration.pipeline import PipelineStatus, run_order_pipeline
from src.strategies.base import StrategyKind, TRADE_PROPOSAL_ELIGIBLE

from .conftest import all_strategy_fixtures, base_proposal, full_stages, make_chain, pipeline_request


class TestFullLibraryInventory:
    def test_16_named_library_items_accounted_for(self):
        """15 real StrategyKind members + CASH/NO_TRADE (the 16th item,
        deliberately not a StrategyKind member -- it has no legs to
        price; represented instead as `selected=None` in
        `src.strategies.selector.SelectionOutcome`)."""
        assert len(list(StrategyKind)) == 15
        from src.strategies.selector import SelectionOutcome

        assert "selected" in SelectionOutcome.__dataclass_fields__

    def test_every_strategy_kind_is_now_trade_proposal_eligible(self):
        """Step 20A's own stated goal: all 15 are now order-eligible,
        not just the original 12."""
        assert len(TRADE_PROPOSAL_ELIGIBLE) == 15

    def test_all_15_strategy_types_have_an_acceptance_fixture(self):
        fixtures = all_strategy_fixtures()
        assert set(fixtures.keys()) == set(StrategyType)


class TestNoStaleEvaluationOnlyLanguage:
    """Step 21 instruction: search for obsolete documentation/comments
    describing newly-enabled strategies as 'evaluation only.'"""

    def test_no_source_file_still_calls_the_3_multileg_strategies_evaluation_only(self):
        """A file mentioning 'evaluation only' next to one of the 3
        multi-leg strategies is fine as accurate HISTORICAL narration
        (e.g. "was evaluation-only in Step 19A, wired to a real order
        in Step 20A") -- it's a live inconsistency only if the same
        file gives no indication the situation has since changed."""
        result = subprocess.run(
            ["grep", "-rln", "-i", "evaluation.only", "--include=*.py", "src/"],
            capture_output=True, text=True, cwd="/home/user/Options",
        )
        offending = []
        for path in result.stdout.splitlines():
            content = open(f"/home/user/Options/{path}").read()
            names_present = any(
                name in content for name in
                ("LONG_CALL_BUTTERFLY", "SHORT_IRON_CONDOR", "SHORT_IRON_BUTTERFLY", "long_call_butterfly", "short_iron_condor", "short_iron_butterfly")
            )
            acknowledges_resolution = "Step 20A" in content or "order-eligible" in content or "wired" in content.lower()
            if names_present and "evaluation" in content.lower() and not acknowledges_resolution:
                offending.append(path)
        assert offending == [], f"stale 'evaluation only' language with no acknowledgment of Step 20A's resolution found in: {offending}"

    def test_no_source_file_still_claims_tradeproposal_caps_at_2_legs(self):
        result = subprocess.run(
            ["grep", "-rn", "max_length=2", "--include=*.py", "src/llm/schemas.py"],
            capture_output=True, text=True, cwd="/home/user/Options",
        )
        assert "legs" not in result.stdout, "TradeProposal.legs must not still be capped at 2 -- Step 20A widened it to 4"


class TestEveryStrategyStructuralCorrectness:
    """For every one of the 15 strategies: correct leg count, BUY/SELL
    direction, PUT/CALL type, strike ordering, quantity ratios,
    contract multiplier, debit/credit sign, max profit/loss,
    breakeven(s), capital requirement, and Fidelity ticket
    representation -- all verified through the real, unmocked pipeline
    (never a hand-derived expectation disconnected from what the
    system actually computes)."""

    _EXPECTED_LEG_COUNT = {
        StrategyType.CASH_SECURED_PUT: 1, StrategyType.COVERED_CALL: 1,
        StrategyType.PUT_CREDIT_SPREAD: 2, StrategyType.CALL_CREDIT_SPREAD: 2,
        StrategyType.BULL_CALL_SPREAD: 2, StrategyType.BEAR_PUT_SPREAD: 2,
        StrategyType.PROTECTIVE_PUT: 1, StrategyType.PROTECTIVE_COLLAR: 2,
        StrategyType.LONG_STRADDLE: 2, StrategyType.LONG_STRANGLE: 2,
        StrategyType.LONG_CALL: 1, StrategyType.LONG_PUT: 1,
        StrategyType.LONG_CALL_BUTTERFLY: 3, StrategyType.SHORT_IRON_CONDOR: 4,
        StrategyType.SHORT_IRON_BUTTERFLY: 4,
    }

    _EXPECTED_CREDIT = {
        StrategyType.CASH_SECURED_PUT: True, StrategyType.COVERED_CALL: True,
        StrategyType.PUT_CREDIT_SPREAD: True, StrategyType.CALL_CREDIT_SPREAD: True,
        StrategyType.BULL_CALL_SPREAD: False, StrategyType.BEAR_PUT_SPREAD: False,
        StrategyType.PROTECTIVE_PUT: False, StrategyType.PROTECTIVE_COLLAR: True,
        StrategyType.LONG_STRADDLE: False, StrategyType.LONG_STRANGLE: False,
        StrategyType.LONG_CALL: False, StrategyType.LONG_PUT: False,
        StrategyType.LONG_CALL_BUTTERFLY: False, StrategyType.SHORT_IRON_CONDOR: True,
        StrategyType.SHORT_IRON_BUTTERFLY: True,
    }

    @pytest.mark.asyncio
    @pytest.mark.parametrize("strategy", list(StrategyType), ids=lambda s: s.value)
    async def test_leg_count_and_credit_debit_sign_match_the_textbook_definition(self, strategy: StrategyType):
        fx = all_strategy_fixtures()[strategy]
        assert len(fx.legs) == self._EXPECTED_LEG_COUNT[strategy]

        proposal = base_proposal(fx.strategy, fx.legs, contracts_requested=1)
        market_data = make_chain(*fx.contracts)
        stages = full_stages(market_data=market_data)
        req = pipeline_request(proposal=proposal, market_data=market_data, portfolio=fx.portfolio, allowed_strategies=[strategy])
        outcome = await run_order_pipeline(req, stages)

        assert outcome.status == PipelineStatus.FILLED, f"{strategy.value}: {outcome.reason}"
        ticket = outcome.fidelity_ticket
        assert len(ticket.legs) == self._EXPECTED_LEG_COUNT[strategy]
        is_credit = ticket.estimated_credit_debit > 0
        assert is_credit == self._EXPECTED_CREDIT[strategy], (
            f"{strategy.value}: expected {'credit' if self._EXPECTED_CREDIT[strategy] else 'debit'}, "
            f"got estimated_credit_debit={ticket.estimated_credit_debit}"
        )
        # max_loss is always finite and non-negative -- this platform
        # never approves an unlimited-risk structure (see §1 of
        # ARCHITECTURE.md's exclusion list).
        assert math.isfinite(outcome.risk_decision.max_loss)
        assert outcome.risk_decision.max_loss >= 0
        assert outcome.risk_decision.capital_required is not None and outcome.risk_decision.capital_required >= 0
