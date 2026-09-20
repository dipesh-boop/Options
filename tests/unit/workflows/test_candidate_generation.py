"""Tests for stages 10-13: universe scan, liquidity filter, quant
filter, and deterministic TradeProposal generation."""
from __future__ import annotations

from datetime import date

import pytest

from src.llm.schemas import StrategyType
from src.risk.limits import get_default_limits
from src.risk.portfolio_risk import Portfolio, UnderlyingHolding
from src.workflows.candidate_generation import (
    QuantFilterConfig,
    UniverseEntry,
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
