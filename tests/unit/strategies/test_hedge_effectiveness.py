"""Tests for src.strategies.hedge_effectiveness -- PROTECTIVE_PUT and
PROTECTIVE_COLLAR evaluated by drawdown/tail-loss/CVaR/volatility
reduction, never by standalone P&L. Every field on HedgeEffectivenessReport
and AggregateHedgeEffectiveness is a comparison figure or an averaged
comparison figure -- this test suite explicitly checks that no
success/failure verdict field exists anywhere in either dataclass."""
from __future__ import annotations

import dataclasses
from datetime import date, datetime, timezone

import pytest

from src.data.option_chain import OptionContract, OptionRight as DataOptionRight
from src.quant.black_scholes import Leg, OptionRight, Side
from src.quant.monte_carlo import Position
from src.risk.limits import get_default_limits
from src.strategies.base import StrategyKind, build_strategy_evaluation
from src.strategies.hedge_effectiveness import (
    HEDGE_STRATEGY_KINDS,
    MIN_SAMPLE_SIZE_FOR_HEDGE_CONCLUSIONS,
    aggregate_hedge_effectiveness,
    build_hedge_effectiveness_report,
    evaluate_hedge_effectiveness,
    render_hedge_effectiveness_report,
)
from src.strategies.protective_collar import evaluate_protective_collar
from src.strategies.protective_put import evaluate_protective_put
from src.strategies.put_credit_spread import evaluate_put_credit_spread

NOW = datetime(2026, 9, 22, tzinfo=timezone.utc)
EXPIRATION = date(2026, 10, 16)
SPOT = 100.0
SIGMA = 0.30
T = 24 / 365
RATE = 0.04


def contract(strike: float, right: DataOptionRight, bid: float, ask: float, iv: float = SIGMA) -> OptionContract:
    return OptionContract(
        underlying="XYZ", option_symbol=f"X{strike}{right.value}", expiration=EXPIRATION, strike=strike, right=right,
        bid=bid, ask=ask, last=(bid + ask) / 2, volume=500, open_interest=1000, iv=iv, underlying_price=SPOT,
        timestamp=NOW, source="test",
    )


_COMMON = dict(ticker="XYZ", expiration=EXPIRATION, spot=SPOT, sigma=SIGMA, t=T, rate=RATE, days_to_expiry=24, num_contracts=1, limits=get_default_limits())


def _protective_put(cost_basis: float = 95.0):
    return evaluate_protective_put(put_contract=contract(90, DataOptionRight.PUT, 1.4, 1.6), cost_basis=cost_basis, **_COMMON)


def _protective_collar(cost_basis: float = 95.0):
    return evaluate_protective_collar(
        call_contract=contract(110, DataOptionRight.CALL, 1.2, 1.4), put_contract=contract(90, DataOptionRight.PUT, 1.4, 1.6),
        cost_basis=cost_basis, **_COMMON,
    )


class TestNoSuccessFailureVerdictField:
    def test_hedge_effectiveness_report_has_no_verdict_field(self):
        from src.strategies.hedge_effectiveness import HedgeEffectivenessReport

        field_names = {f.name for f in dataclasses.fields(HedgeEffectivenessReport)}
        forbidden = {"success", "successful", "unsuccessful", "failed", "verdict", "passed", "outcome"}
        assert not (field_names & forbidden)

    def test_aggregate_hedge_effectiveness_has_no_verdict_field(self):
        from src.strategies.hedge_effectiveness import AggregateHedgeEffectiveness

        field_names = {f.name for f in dataclasses.fields(AggregateHedgeEffectiveness)}
        forbidden = {"success", "successful", "unsuccessful", "failed", "verdict", "passed", "outcome"}
        assert not (field_names & forbidden)


class TestEvaluateHedgeEffectivenessRejectsNonHedgeStrategies:
    def test_rejects_a_profit_seeking_strategy(self):
        pcs = evaluate_put_credit_spread(
            short_put_contract=contract(95, DataOptionRight.PUT, 2.8, 3.0), long_put_contract=contract(90, DataOptionRight.PUT, 1.4, 1.6),
            **_COMMON,
        )
        with pytest.raises(ValueError):
            evaluate_hedge_effectiveness(pcs, spot=SPOT, sigma=SIGMA, t=T, rate=RATE)


class TestProtectivePutHedgeEffectiveness:
    def test_drawdown_avoided_is_positive(self):
        report = evaluate_hedge_effectiveness(_protective_put(), spot=SPOT, sigma=SIGMA, t=T, rate=RATE)
        assert report.drawdown_avoided > 0

    def test_tail_loss_avoided_is_positive(self):
        report = evaluate_hedge_effectiveness(_protective_put(), spot=SPOT, sigma=SIGMA, t=T, rate=RATE)
        assert report.tail_loss_avoided > 0

    def test_cvar_reduction_pct_is_between_0_and_1(self):
        report = evaluate_hedge_effectiveness(_protective_put(), spot=SPOT, sigma=SIGMA, t=T, rate=RATE)
        assert 0.0 < report.cvar_reduction_pct < 1.0

    def test_volatility_reduction_pct_is_positive(self):
        report = evaluate_hedge_effectiveness(_protective_put(), spot=SPOT, sigma=SIGMA, t=T, rate=RATE)
        assert report.volatility_reduction_pct > 0

    def test_upside_sacrificed_is_zero_no_capped_upside(self):
        # A protective put has no short call leg -- nothing caps the upside.
        report = evaluate_hedge_effectiveness(_protective_put(), spot=SPOT, sigma=SIGMA, t=T, rate=RATE)
        assert report.upside_sacrificed == pytest.approx(0.0)

    def test_hedge_cost_is_the_put_premium_in_whole_dollar_terms(self):
        # put mid = (1.4+1.6)/2 = 1.5 -> $150 for 1 contract (100 shares)
        report = evaluate_hedge_effectiveness(_protective_put(), spot=SPOT, sigma=SIGMA, t=T, rate=RATE)
        assert report.hedge_cost == pytest.approx(150.0)

    def test_net_hedge_benefit_is_positive_when_protection_outweighs_cost(self):
        report = evaluate_hedge_effectiveness(_protective_put(), spot=SPOT, sigma=SIGMA, t=T, rate=RATE)
        assert report.net_hedge_benefit == pytest.approx(report.tail_loss_avoided - report.hedge_cost - report.upside_sacrificed)


class TestProtectiveCollarHedgeEffectiveness:
    def test_upside_sacrificed_is_positive_the_call_caps_gains(self):
        report = evaluate_hedge_effectiveness(_protective_collar(), spot=SPOT, sigma=SIGMA, t=T, rate=RATE)
        assert report.upside_sacrificed > 0

    def test_drawdown_avoided_is_at_least_as_large_as_the_protective_put_alone(self):
        # Same put leg sets the floor; the collar's sold call brings in
        # an extra premium credit that nets against losses in every
        # scenario (including the worst ones), so the collar's own
        # drawdown_avoided is put_report's plus that call premium.
        put_report = evaluate_hedge_effectiveness(_protective_put(), spot=SPOT, sigma=SIGMA, t=T, rate=RATE)
        collar_report = evaluate_hedge_effectiveness(_protective_collar(), spot=SPOT, sigma=SIGMA, t=T, rate=RATE)
        call_premium_dollars = (1.2 + 1.4) / 2 * 100  # mid of the 110-strike call, 1 contract
        assert collar_report.drawdown_avoided == pytest.approx(put_report.drawdown_avoided + call_premium_dollars, rel=1e-6)

    def test_hedge_cost_can_be_small_since_the_call_partially_funds_the_put(self):
        put_report = evaluate_hedge_effectiveness(_protective_put(), spot=SPOT, sigma=SIGMA, t=T, rate=RATE)
        collar_report = evaluate_hedge_effectiveness(_protective_collar(), spot=SPOT, sigma=SIGMA, t=T, rate=RATE)
        assert collar_report.hedge_cost < put_report.hedge_cost


class TestPairedComparisonUsesTheSameSimulatedPrices:
    def test_same_seed_gives_deterministic_reproducible_results(self):
        r1 = evaluate_hedge_effectiveness(_protective_put(), spot=SPOT, sigma=SIGMA, t=T, rate=RATE)
        r2 = evaluate_hedge_effectiveness(_protective_put(), spot=SPOT, sigma=SIGMA, t=T, rate=RATE)
        assert r1 == r2

    def test_an_overpriced_far_otm_hedge_with_negative_net_benefit_still_reports_full_detail(self):
        # Deep OTM (little protection value) + wildly overpriced premium
        # -> a virtually guaranteed negative net benefit -- and it must
        # still report every measure in full, never collapse to a bare
        # "unsuccessful" label (there is no such field to collapse to).
        overpriced_put = evaluate_protective_put(put_contract=contract(60, DataOptionRight.PUT, 24.0, 26.0), cost_basis=95.0, **_COMMON)
        report = evaluate_hedge_effectiveness(overpriced_put, spot=SPOT, sigma=SIGMA, t=T, rate=RATE)
        # A far-OTM put that (almost) never pays off in 24 days can make
        # tail outcomes *worse* by its own cost alone -- exactly the
        # honest, unflattering finding this report exists to surface,
        # never hidden behind a pass/fail label.
        assert report.net_hedge_benefit < 0
        assert report.hedge_cost > 0
        assert report.drawdown_avoided >= 0


class TestEvaluateHedgeEffectivenessRequiresShares:
    def test_raises_when_position_has_no_underlying_shares(self):
        position = Position(legs=[Leg(right=OptionRight.PUT, strike=90, side=Side.BUY, entry_price=1.5, quantity=1)], underlying_shares=0)
        ev = build_strategy_evaluation(
            kind=StrategyKind.PROTECTIVE_PUT, ticker="XYZ", expiration=EXPIRATION, position=position,
            contracts=[contract(90, DataOptionRight.PUT, 1.4, 1.6)], limits=get_default_limits(), spot=SPOT, sigma=SIGMA, t=T, rate=RATE,
            days_to_expiry=24, num_contracts=1, market_outlook="protection", volatility_outlook="irrelevant",
            required_positions="none (test)", capital_requirement=0.0, buying_power_requirement=150.0,
            entry_rules=("e",), exit_rules=("x",), adjustment_rules=("a",), invalidation_rules=("i",),
        )
        with pytest.raises(ValueError):
            evaluate_hedge_effectiveness(ev, spot=SPOT, sigma=SIGMA, t=T, rate=RATE)


class TestAggregateHedgeEffectiveness:
    def test_insufficient_sample_below_threshold(self):
        reports = [evaluate_hedge_effectiveness(_protective_put(), spot=SPOT, sigma=SIGMA, t=T, rate=RATE)]
        agg = aggregate_hedge_effectiveness(reports, strategy_kind=StrategyKind.PROTECTIVE_PUT, min_sample_size=20)
        assert agg.meaningful_sample is False

    def test_meaningful_sample_at_threshold(self):
        reports = [evaluate_hedge_effectiveness(_protective_put(cost_basis=90.0 + i * 0.01), spot=SPOT, sigma=SIGMA, t=T, rate=RATE) for i in range(20)]
        agg = aggregate_hedge_effectiveness(reports, strategy_kind=StrategyKind.PROTECTIVE_PUT, min_sample_size=20)
        assert agg.meaningful_sample is True
        assert agg.sample_size == 20

    def test_averages_are_computed_across_only_the_matching_strategy(self):
        put_reports = [evaluate_hedge_effectiveness(_protective_put(), spot=SPOT, sigma=SIGMA, t=T, rate=RATE)]
        collar_reports = [evaluate_hedge_effectiveness(_protective_collar(), spot=SPOT, sigma=SIGMA, t=T, rate=RATE)]
        agg = aggregate_hedge_effectiveness(put_reports + collar_reports, strategy_kind=StrategyKind.PROTECTIVE_PUT, min_sample_size=1)
        assert agg.sample_size == 1
        assert agg.avg_hedge_cost == pytest.approx(put_reports[0].hedge_cost)


class TestBuildHedgeEffectivenessReport:
    def test_covers_both_hedge_kinds_even_when_empty(self):
        report = build_hedge_effectiveness_report([])
        assert set(report) == {k.value for k in HEDGE_STRATEGY_KINDS}

    def test_only_tracks_the_two_hedge_kinds(self):
        assert set(HEDGE_STRATEGY_KINDS) == {StrategyKind.PROTECTIVE_PUT, StrategyKind.PROTECTIVE_COLLAR}


class TestRenderHedgeEffectivenessReport:
    def test_renders_insufficient_sample_and_never_a_verdict_word(self):
        report = build_hedge_effectiveness_report([])
        text = render_hedge_effectiveness_report(report)
        assert "INSUFFICIENT SAMPLE" in text
        assert "unsuccessful" not in text.lower() or "never labeled unsuccessful" in text.lower()

    def test_renders_all_seven_measures(self):
        reports = [evaluate_hedge_effectiveness(_protective_put(), spot=SPOT, sigma=SIGMA, t=T, rate=RATE) for _ in range(20)]
        report = build_hedge_effectiveness_report(reports, min_sample_size=20)
        text = render_hedge_effectiveness_report(report)
        for label in (
            "Average hedge cost", "Average drawdown avoided", "Average tail loss avoided", "Average CVaR reduction",
            "Average portfolio volatility reduction", "Average upside sacrificed", "Average net hedge benefit",
        ):
            assert label in text
