"""Tests for stages 10-13: universe scan, liquidity filter, quant
filter, and deterministic TradeProposal generation."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from src.llm.schemas import StrategyType
from src.risk.limits import get_default_limits
from src.risk.portfolio_risk import Portfolio, UnderlyingHolding
from src.workflows.candidate_generation import (
    CANDIDATE_GENERATION_ELIGIBLE_STRATEGIES,
    QuantFilterConfig,
    UniverseEntry,
    candidate_eligible_strategies,
    generate_candidates,
    passes_liquidity_filter,
)
from tests.unit.workflows.conftest import EXPIRATION, NOW, default_pcs_chain, make_call, make_chain, make_put

LIMITS = get_default_limits()


def _empty_portfolio() -> Portfolio:
    return Portfolio(as_of=NOW, nav=100_000.0, cash=100_000.0, peak_equity=100_000.0)


class TestPassesLiquidityFilter:
    def test_liquid_contract_passes(self):
        c = make_put(95.0, -0.20, bid=1.95, ask=2.05, oi=1000, vol=500)
        assert passes_liquidity_filter(c, LIMITS) is True

    def test_low_open_interest_fails(self):
        c = make_put(95.0, -0.20, bid=1.95, ask=2.05, oi=1, vol=500)
        assert passes_liquidity_filter(c, LIMITS) is False

    def test_low_volume_fails(self):
        c = make_put(95.0, -0.20, bid=1.95, ask=2.05, oi=1000, vol=0)
        assert passes_liquidity_filter(c, LIMITS) is False

    def test_wide_spread_fails(self):
        c = make_put(95.0, -0.20, bid=1.0, ask=3.0, oi=1000, vol=500)
        assert passes_liquidity_filter(c, LIMITS) is False

    def test_zero_mid_fails_without_dividing_by_zero(self):
        c = make_put(95.0, -0.20, bid=0.0, ask=0.0, oi=1000, vol=500)
        assert passes_liquidity_filter(c, LIMITS) is False


class TestQuantFilterConfigValidation:
    def test_valid_config_constructs(self):
        QuantFilterConfig()

    def test_delta_low_must_be_below_high(self):
        with pytest.raises(ValueError):
            QuantFilterConfig(short_delta_low=0.30, short_delta_high=0.15)

    def test_negative_spread_width_rejected(self):
        with pytest.raises(ValueError):
            QuantFilterConfig(pcs_spread_width=-1.0)

    def test_min_dte_must_be_below_max_dte(self):
        with pytest.raises(ValueError):
            QuantFilterConfig(min_dte=50, max_dte=10)

    def test_profit_target_out_of_range_rejected(self):
        with pytest.raises(ValueError):
            QuantFilterConfig(profit_target=1.5)


class TestCashSecuredPutGeneration:
    def test_generates_a_candidate_in_target_delta_range(self):
        chain = default_pcs_chain()
        candidates = generate_candidates(
            UniverseEntry("XYZ", "TECH"), chain, [StrategyType.CASH_SECURED_PUT], QuantFilterConfig(), LIMITS, _empty_portfolio(), "normal", now=NOW,
        )
        assert len(candidates) == 1
        assert candidates[0].proposal.strategy == StrategyType.CASH_SECURED_PUT
        assert candidates[0].proposal.legs[0].strike == 95.0
        assert candidates[0].entry_delta == -0.20

    def test_no_candidate_when_no_put_in_delta_range(self):
        chain = make_chain([make_put(80.0, -0.05, bid=0.20, ask=0.24)])
        candidates = generate_candidates(
            UniverseEntry("XYZ", "TECH"), chain, [StrategyType.CASH_SECURED_PUT], QuantFilterConfig(), LIMITS, _empty_portfolio(), "normal", now=NOW,
        )
        assert candidates == []

    def test_no_candidate_when_liquidity_fails(self):
        chain = make_chain([make_put(95.0, -0.20, bid=1.0, ask=3.0)])  # wide spread
        candidates = generate_candidates(
            UniverseEntry("XYZ", "TECH"), chain, [StrategyType.CASH_SECURED_PUT], QuantFilterConfig(), LIMITS, _empty_portfolio(), "normal", now=NOW,
        )
        assert candidates == []

    def test_picks_closest_to_target_mid_delta_across_expirations(self):
        near_exp = EXPIRATION
        far_exp = date(2026, 10, 30)  # 40 DTE, still inside the default 20-45 window
        chain = make_chain([
            make_put(93.0, -0.15, bid=1.4, ask=1.5, expiration=near_exp),  # far from mid (0.225)
            make_put(95.0, -0.225, bid=1.9, ask=2.0, expiration=far_exp),  # exactly mid
        ])
        candidates = generate_candidates(
            UniverseEntry("XYZ", "TECH"), chain, [StrategyType.CASH_SECURED_PUT], QuantFilterConfig(), LIMITS, _empty_portfolio(), "normal", now=NOW,
        )
        assert len(candidates) == 1
        assert candidates[0].proposal.expiration == far_exp

    def test_stale_chain_produces_no_candidates(self):
        from datetime import timedelta
        chain = default_pcs_chain()
        candidates = generate_candidates(
            UniverseEntry("XYZ", "TECH"), chain, [StrategyType.CASH_SECURED_PUT], QuantFilterConfig(), LIMITS, _empty_portfolio(), "normal",
            now=NOW + timedelta(minutes=30),
        )
        assert candidates == []

    def test_expiration_outside_dte_window_excluded(self):
        chain = make_chain([make_put(95.0, -0.20, bid=1.95, ask=2.05, expiration=date(2026, 9, 25))])  # 5 DTE, below min_dte=20
        candidates = generate_candidates(
            UniverseEntry("XYZ", "TECH"), chain, [StrategyType.CASH_SECURED_PUT], QuantFilterConfig(), LIMITS, _empty_portfolio(), "normal", now=NOW,
        )
        assert candidates == []


class TestCoveredCallGeneration:
    def test_requires_at_least_100_shares_held(self):
        chain = make_chain([make_call(105.0, 0.22, bid=1.9, ask=2.1)])
        candidates = generate_candidates(
            UniverseEntry("XYZ", "TECH"), chain, [StrategyType.COVERED_CALL], QuantFilterConfig(), LIMITS, _empty_portfolio(), "normal", now=NOW,
        )
        assert candidates == []

    def test_generates_when_shares_are_held(self):
        chain = make_chain([make_call(105.0, 0.22, bid=1.9, ask=2.1)])
        portfolio = Portfolio(as_of=NOW, nav=100_000.0, cash=50_000.0, peak_equity=100_000.0, underlying_holdings={"XYZ": UnderlyingHolding(shares=100, cost_basis=95.0)})
        candidates = generate_candidates(
            UniverseEntry("XYZ", "TECH"), chain, [StrategyType.COVERED_CALL], QuantFilterConfig(), LIMITS, portfolio, "normal", now=NOW,
        )
        assert len(candidates) == 1
        assert candidates[0].proposal.strategy == StrategyType.COVERED_CALL
        assert candidates[0].proposal.legs[0].strike == 105.0

    def test_fewer_than_100_shares_is_not_enough(self):
        chain = make_chain([make_call(105.0, 0.22, bid=1.9, ask=2.1)])
        portfolio = Portfolio(as_of=NOW, nav=100_000.0, cash=50_000.0, peak_equity=100_000.0, underlying_holdings={"XYZ": UnderlyingHolding(shares=50, cost_basis=95.0)})
        candidates = generate_candidates(
            UniverseEntry("XYZ", "TECH"), chain, [StrategyType.COVERED_CALL], QuantFilterConfig(), LIMITS, portfolio, "normal", now=NOW,
        )
        assert candidates == []


class TestPutCreditSpreadGeneration:
    def test_generates_a_two_leg_credit_spread(self):
        chain = default_pcs_chain()
        candidates = generate_candidates(
            UniverseEntry("XYZ", "TECH"), chain, [StrategyType.PUT_CREDIT_SPREAD], QuantFilterConfig(), LIMITS, _empty_portfolio(), "normal", now=NOW,
        )
        assert len(candidates) == 1
        proposal = candidates[0].proposal
        assert len(proposal.legs) == 2
        assert proposal.target_entry == pytest.approx(2.00 - 1.00)  # short mid - long mid

    def test_no_spread_when_long_leg_fails_liquidity(self):
        chain = make_chain([
            make_put(95.0, -0.20, bid=1.95, ask=2.05),
            make_put(90.0, -0.10, bid=0.5, ask=1.5),  # wide spread
        ])
        candidates = generate_candidates(
            UniverseEntry("XYZ", "TECH"), chain, [StrategyType.PUT_CREDIT_SPREAD], QuantFilterConfig(), LIMITS, _empty_portfolio(), "normal", now=NOW,
        )
        assert candidates == []

    def test_no_spread_when_it_would_net_a_debit(self):
        chain = make_chain([
            make_put(95.0, -0.20, bid=0.95, ask=1.05),   # short mid 1.00
            make_put(90.0, -0.10, bid=1.45, ask=1.55),   # long mid 1.50 -- net debit
        ])
        candidates = generate_candidates(
            UniverseEntry("XYZ", "TECH"), chain, [StrategyType.PUT_CREDIT_SPREAD], QuantFilterConfig(), LIMITS, _empty_portfolio(), "normal", now=NOW,
        )
        assert candidates == []

    def test_no_spread_when_no_further_otm_put_available(self):
        chain = make_chain([make_put(95.0, -0.20, bid=1.95, ask=2.05)])  # only one strike
        candidates = generate_candidates(
            UniverseEntry("XYZ", "TECH"), chain, [StrategyType.PUT_CREDIT_SPREAD], QuantFilterConfig(), LIMITS, _empty_portfolio(), "normal", now=NOW,
        )
        assert candidates == []


class TestMultipleStrategiesRequested:
    def test_generates_one_candidate_per_eligible_strategy(self):
        chain = make_chain([
            make_put(95.0, -0.20, bid=1.95, ask=2.05),
            make_put(90.0, -0.10, bid=0.97, ask=1.03),
            make_call(105.0, 0.22, bid=1.9, ask=2.1),
        ])
        portfolio = Portfolio(as_of=NOW, nav=100_000.0, cash=50_000.0, peak_equity=100_000.0, underlying_holdings={"XYZ": UnderlyingHolding(shares=100, cost_basis=95.0)})
        candidates = generate_candidates(
            UniverseEntry("XYZ", "TECH"), chain,
            [StrategyType.CASH_SECURED_PUT, StrategyType.COVERED_CALL, StrategyType.PUT_CREDIT_SPREAD],
            QuantFilterConfig(), LIMITS, portfolio, "normal", now=NOW,
        )
        assert {c.proposal.strategy for c in candidates} == {StrategyType.CASH_SECURED_PUT, StrategyType.COVERED_CALL, StrategyType.PUT_CREDIT_SPREAD}

    def test_proposal_ids_are_unique(self):
        chain = make_chain([
            make_put(95.0, -0.20, bid=1.95, ask=2.05),
            make_put(90.0, -0.10, bid=0.97, ask=1.03),
        ])
        candidates = generate_candidates(
            UniverseEntry("XYZ", "TECH"), chain, [StrategyType.CASH_SECURED_PUT, StrategyType.PUT_CREDIT_SPREAD],
            QuantFilterConfig(), LIMITS, _empty_portfolio(), "normal", now=NOW,
        )
        ids = [c.proposal.proposal_id for c in candidates]
        assert len(ids) == len(set(ids))


class TestProposalIdsAreUniqueAcrossScanRunsRegressionSY001:
    """SY-001: the old `f"{prefix}-{ticker}-{counter}"` scheme reset its
    counter to 0 on every call, so the same ticker's first candidate on
    two different scan runs (e.g. two different days sharing one
    long-lived PaperBroker/idempotency-store instance) collided on the
    identical id -- the second day's genuinely new order would then be
    silently returned as the first day's stale, already-filled result."""

    def _same_shape_chain(self, as_of: datetime):
        # Deliberately the same strike/delta/expiration shape on both
        # "days" -- this is exactly the collision scenario: identical
        # selected contract, called on two different scan dates. Each
        # day's chain is freshly timestamped (as any real day's fetch
        # would be) so the freshness gate doesn't confound the id test.
        return make_chain([make_put(95.0, -0.20, bid=1.95, ask=2.05)], timestamp=as_of)

    def test_same_ticker_two_different_scan_dates_never_collide(self):
        day_two_now = datetime(NOW.year, NOW.month, NOW.day + 1, NOW.hour, tzinfo=timezone.utc)
        day_one = generate_candidates(
            UniverseEntry("XYZ", "TECH"), self._same_shape_chain(NOW), [StrategyType.CASH_SECURED_PUT],
            QuantFilterConfig(), LIMITS, _empty_portfolio(), "normal", now=NOW,
        )
        day_two = generate_candidates(
            UniverseEntry("XYZ", "TECH"), self._same_shape_chain(day_two_now), [StrategyType.CASH_SECURED_PUT],
            QuantFilterConfig(), LIMITS, _empty_portfolio(), "normal", now=day_two_now,
        )
        assert len(day_one) == 1 and len(day_two) == 1
        id_one = day_one[0].proposal.proposal_id
        id_two = day_two[0].proposal.proposal_id
        assert id_one != id_two, "proposal_id collided across two different scan runs for the same ticker/strike"

    def test_proposal_id_encodes_the_scan_date(self):
        candidates = generate_candidates(
            UniverseEntry("XYZ", "TECH"), self._same_shape_chain(NOW), [StrategyType.CASH_SECURED_PUT],
            QuantFilterConfig(), LIMITS, _empty_portfolio(), "normal", now=NOW,
        )
        assert NOW.date().isoformat() in candidates[0].proposal.proposal_id


class TestGeneratedProposalsAreValid:
    def test_thesis_and_risk_thesis_are_populated_and_non_generic(self):
        chain = default_pcs_chain()
        candidates = generate_candidates(
            UniverseEntry("XYZ", "TECH"), chain, [StrategyType.CASH_SECURED_PUT], QuantFilterConfig(), LIMITS, _empty_portfolio(), "normal", now=NOW,
        )
        proposal = candidates[0].proposal
        assert "XYZ" in proposal.thesis
        assert proposal.risk_thesis
        assert proposal.invalidation_conditions

    def test_management_dte_clamped_to_total_dte(self):
        # 21-day management DTE, but expiration is only ~26 days out --
        # should still validate cleanly (21 <= 26), not clamp visibly here,
        # but a very-near expiration should clamp instead of raising.
        near_exp = date(2026, 9, 30)  # 10 DTE
        chain = make_chain([make_put(95.0, -0.20, bid=1.95, ask=2.05, expiration=near_exp)])
        candidates = generate_candidates(
            UniverseEntry("XYZ", "TECH"), chain, [StrategyType.CASH_SECURED_PUT],
            QuantFilterConfig(min_dte=5, max_dte=45), LIMITS, _empty_portfolio(), "normal", now=NOW,
        )
        assert len(candidates) == 1
        assert candidates[0].proposal.management_dte <= 10


class TestCandidateEligibleStrategies:
    """Step 22.5 (PAPER_TRADING_V1.4.4): moved here from
    tests/unit/data/test_universe.py -- narrowing a configured strategy
    list to the ones this module can actually generate a candidate for
    requires `StrategyType`, which `src.data` must never import (see
    `tests/unit/data/test_architecture_boundary.py`)."""

    def test_candidate_eligible_strategies_filters_to_the_three_implemented(self):
        all_15 = tuple(StrategyType)
        eligible = candidate_eligible_strategies(all_15)
        assert set(eligible) == set(CANDIDATE_GENERATION_ELIGIBLE_STRATEGIES)
        assert StrategyType.LONG_CALL_BUTTERFLY not in eligible

    def test_candidate_eligible_strategies_preserves_configured_order(self):
        ordered = (StrategyType.PUT_CREDIT_SPREAD, StrategyType.CASH_SECURED_PUT)
        assert candidate_eligible_strategies(ordered) == ordered


class TestTimestampDomainIntegrityRegressionV157:
    """PAPER_TRADING_V1.5.7: deterministic reproduction of the exact
    2026-10-02 production defect, and proof of its fix -- NO network
    access, NO sleep(), NO wall-clock dependency.

    Root cause: `scripts/run_validation_cycle.py` captured its cycle
    `now` BEFORE fetching market data; `TradierMarketDataProvider
    .get_option_chain_for_dte_window` then stamped `chain.timestamp`
    with a LATER `datetime.now(timezone.utc)`, during the fetch. A
    `TradeProposal` built with `timestamp=<pre-fetch now>` and
    `data_timestamp=chain.timestamp` (later) therefore always violated
    `TradeProposal`'s own (correct, never-weakened) integrity check:
    `data_timestamp cannot be after the proposal timestamp`.

    `T` below stands in for the pre-fetch cycle `now`; `T + delta`
    stands in for the chain's own (later, genuinely fresher) timestamp.
    """

    T = NOW
    T_PLUS_DELTA = NOW + timedelta(seconds=5)

    def _chain_at(self, timestamp):
        return make_chain([make_put(95.0, -0.20, bid=1.95, ask=2.05)], timestamp=timestamp)

    def test_reproduces_the_2026_10_02_incident_using_the_broken_pre_fetch_ordering(self):
        """Calling generate_candidates with `now` EARLIER than
        `chain.timestamp` (the exact pre-V1.5.7 ordering bug) must no
        longer raise or silently vanish -- it must be caught internally
        and recorded as a generation_exception, never a crash, never a
        false construction_success."""
        from src.workflows.funnel_diagnostics import FunnelDiagnostics

        chain = self._chain_at(self.T_PLUS_DELTA)
        diag = FunnelDiagnostics(ticker="XYZ")
        candidates = generate_candidates(
            UniverseEntry("XYZ", "TECH"), chain, [StrategyType.CASH_SECURED_PUT], QuantFilterConfig(),
            LIMITS, _empty_portfolio(), "normal", now=self.T, diagnostics=diag,
        )
        # the real chain timestamp is untouched -- never falsified
        # backwards to satisfy the check
        assert chain.timestamp == self.T_PLUS_DELTA
        # no Candidate was produced for this mis-ordered call
        assert candidates == []
        # but the failure IS now visible, correctly categorized, and
        # never confused with a legitimate construction rejection
        assert ("CASH_SECURED_PUT", "generation_exception", "ValidationError") in diag.strategy_events
        assert not any(evt[1] == "construction_success" for evt in diag.strategy_events)

    def test_correct_post_fetch_evaluation_timestamp_produces_a_valid_candidate(self):
        """The actual V1.5.7 fix: calling generate_candidates with `now`
        AT OR AFTER `chain.timestamp` (the corrected ordering --
        `evaluation_as_of` captured after the fetch completes) produces
        a real Candidate, with a genuinely valid TradeProposal, no
        construction failure, and no future-data validator weakened or
        bypassed to get there."""
        from src.workflows.funnel_diagnostics import FunnelDiagnostics

        chain = self._chain_at(self.T_PLUS_DELTA)
        evaluation_as_of = self.T_PLUS_DELTA + timedelta(seconds=1)
        diag = FunnelDiagnostics(ticker="XYZ")
        candidates = generate_candidates(
            UniverseEntry("XYZ", "TECH"), chain, [StrategyType.CASH_SECURED_PUT], QuantFilterConfig(),
            LIMITS, _empty_portfolio(), "normal", now=evaluation_as_of, diagnostics=diag,
        )
        assert len(candidates) == 1
        proposal = candidates[0].proposal
        # the required invariant, satisfied honestly -- never by
        # tolerance, never by falsifying either timestamp
        assert proposal.timestamp >= proposal.data_timestamp
        assert proposal.data_timestamp == self.T_PLUS_DELTA  # the real chain timestamp, untouched
        assert proposal.timestamp == evaluation_as_of  # a genuine evaluation time, not a fabricated value
        assert ("CASH_SECURED_PUT", "construction_success", None) in diag.strategy_events
        assert not any(evt[1] == "generation_exception" for evt in diag.strategy_events)

    def test_exactly_equal_timestamps_are_valid_not_a_boundary_failure(self):
        chain = self._chain_at(self.T_PLUS_DELTA)
        candidates = generate_candidates(
            UniverseEntry("XYZ", "TECH"), chain, [StrategyType.CASH_SECURED_PUT], QuantFilterConfig(),
            LIMITS, _empty_portfolio(), "normal", now=self.T_PLUS_DELTA,
        )
        assert len(candidates) == 1
        assert candidates[0].proposal.timestamp == candidates[0].proposal.data_timestamp

    def test_future_data_integrity_validator_itself_is_unmodified_and_still_rejects_bad_input(self):
        """Direct proof this fix never weakens, removes, or adds
        tolerance to TradeProposal's own integrity check -- constructing
        one directly with data_timestamp after timestamp must still
        raise, exactly as before this step."""
        import pytest as _pytest
        from pydantic import ValidationError

        from src.llm.schemas import Conviction, LegSide, OptionLeg, OptionRight, StrategyType as _ST, TradeDirection, TradeProposal

        with _pytest.raises(ValidationError, match="data_timestamp cannot be after the proposal timestamp"):
            TradeProposal(
                proposal_id="direct-1", timestamp=self.T, ticker="XYZ", strategy=_ST.CASH_SECURED_PUT,
                market_regime="normal", expiration=EXPIRATION,
                legs=[OptionLeg(right=OptionRight.PUT, strike=95.0, side=LegSide.SELL)],
                direction=TradeDirection.NEUTRAL, contracts_requested=1, target_entry=1.0, profit_target=0.5,
                management_dte=21, thesis="t", risk_thesis="rt", confidence=Conviction.MEDIUM,
                data_sources=["test"], data_timestamp=self.T_PLUS_DELTA, invalidation_conditions=["x"],
            )


class TestGenerationExceptionObservabilityV157:
    """PAPER_TRADING_V1.5.7: a candidate-generation exception (any
    exception raised during a strategy's own TradeProposal construction,
    not just the timestamp-domain one above) must never silently
    disappear, must never falsely increment construction_successes, and
    must never leak a raw/uncontrolled exception payload."""

    def test_an_exception_for_one_strategy_does_not_block_a_later_strategy_on_the_same_ticker(self):
        """Direct proof of the section-15 "strategy anomaly" fix: before
        this step, an exception raised inside the CASH_SECURED_PUT block
        aborted generate_candidates entirely, so COVERED_CALL/
        PUT_CREDIT_SPREAD's own diagnostic calls never ran at all for
        that ticker. After this fix, CASH_SECURED_PUT's failure is
        isolated to itself -- PUT_CREDIT_SPREAD, requested in the same
        call, still gets its own fair attempt."""
        from src.workflows.funnel_diagnostics import FunnelDiagnostics

        bad_now = NOW  # earlier than the chain timestamp below -- breaks CSP's proposal construction
        # A chain whose timestamp is after `bad_now` -- the exact mismatch shape.
        later_chain = make_chain(
            [make_put(95.0, -0.20, bid=1.95, ask=2.05), make_put(90.0, -0.10, bid=0.97, ask=1.03)],
            timestamp=NOW + timedelta(seconds=5),
        )
        diag = FunnelDiagnostics(ticker="XYZ")
        candidates = generate_candidates(
            UniverseEntry("XYZ", "TECH"), later_chain,
            [StrategyType.CASH_SECURED_PUT, StrategyType.PUT_CREDIT_SPREAD], QuantFilterConfig(),
            LIMITS, _empty_portfolio(), "normal", now=bad_now, diagnostics=diag,
        )
        assert candidates == []  # both strategies build against the same mis-ordered now/chain
        strategies_with_events = {evt[0] for evt in diag.strategy_events}
        # the critical assertion: PUT_CREDIT_SPREAD's events exist at
        # all -- proving generate_candidates did not abort after CSP's
        # exception the way it did before this fix
        assert "PUT_CREDIT_SPREAD" in strategies_with_events
        assert ("CASH_SECURED_PUT", "generation_exception", "ValidationError") in diag.strategy_events
        assert ("PUT_CREDIT_SPREAD", "generation_exception", "ValidationError") in diag.strategy_events

    def test_generation_exception_category_is_bounded_never_the_raw_message(self):
        from src.workflows.funnel_diagnostics import FunnelDiagnostics

        later_chain = make_chain([make_put(95.0, -0.20, bid=1.95, ask=2.05)], timestamp=NOW + timedelta(seconds=5))
        diag = FunnelDiagnostics(ticker="XYZ")
        generate_candidates(
            UniverseEntry("XYZ", "TECH"), later_chain, [StrategyType.CASH_SECURED_PUT], QuantFilterConfig(),
            LIMITS, _empty_portfolio(), "normal", now=NOW, diagnostics=diag,
        )
        event = next(e for e in diag.strategy_events if e[1] == "generation_exception")
        category = event[2]
        # a short, bounded exception-class name -- never a multi-line
        # pydantic error dump, never field values, never a provider
        # payload fragment
        assert category == "ValidationError"
        assert len(category) < 64
        assert "\n" not in category

    def test_generate_candidates_never_raises_for_this_failure_mode_even_without_diagnostics(self):
        """The isolation itself must not depend on the caller having
        opted into diagnostics -- src.workflows.morning_scan calls
        generate_candidates with diagnostics=None and must be protected
        too."""
        later_chain = make_chain([make_put(95.0, -0.20, bid=1.95, ask=2.05)], timestamp=NOW + timedelta(seconds=5))
        candidates = generate_candidates(
            UniverseEntry("XYZ", "TECH"), later_chain, [StrategyType.CASH_SECURED_PUT], QuantFilterConfig(),
            LIMITS, _empty_portfolio(), "normal", now=NOW,  # diagnostics omitted entirely
        )
        assert candidates == []  # no exception propagated
