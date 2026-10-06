"""Parts 23-24: portfolio-aware new-opportunity scanning for the
control loop.

Deliberately lighter than `src.workflows.morning_scan.run_morning_scan`
(the heavier, LLM-reviewed daily workflow that also runs Devil's
Advocate and the Portfolio Manager on every surviving candidate) --
every control-loop cycle calls THIS module instead, reusing exactly the
same deterministic, no-LLM building blocks `run_morning_scan` itself is
built from: `src.workflows.candidate_generation.generate_candidates`
(Quant-only screening, no LLM), `src.orchestration.pipeline
.default_quant_stage` (the same Quant pricing every proposal in this
codebase gets, never a second implementation), and
`src.risk.engine.evaluate_trade_proposal` (the one deterministic Risk
Engine, unmodified -- still the final veto). This module never calls
Devil's Advocate or the Portfolio Manager; that heavier daily review
remains `run_morning_scan`'s own job, invoked separately and
deliberately, not on every control-loop cycle.

**Portfolio-aware, per Part 24**: every candidate's post-trade
underlying/sector exposure is computed against the CURRENT `Portfolio`
(`src.risk.portfolio_risk.underlying_exposure_pct`/`sector_exposure_pct`,
reused, never re-derived), and ranking is by risk-adjusted return
(expected value per dollar of capital required) among only the
candidates the Risk Engine actually approved or resized -- Risk remains
the final veto, this module only orders what already survived it.
CASH/NO_TRADE is the explicit, first-class outcome (`best=None`) when
nothing clears `no_trade_hurdle`, exactly matching
`src.strategies.selector.select_best_or_no_trade`'s own doctrine that
"no trade" is a real competitor, never an afterthought.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from src.data.option_chain import OptionChain
from src.llm.schemas import MarketRegimeLabel, StrategyType
from src.orchestration.pipeline import default_quant_stage
from src.risk.broker_constraints import BrokerCapabilities
from src.risk.engine import evaluate_trade_proposal
from src.risk.limits import RiskLimitsConfig
from src.risk.portfolio_risk import Portfolio, sector_exposure_pct, underlying_exposure_pct
from src.risk.reason_codes import RiskDecision
from src.risk.trade_risk import QuantitativeAnalysis
from src.workflows.candidate_generation import Candidate, QuantFilterConfig, UniverseEntry, generate_candidates
from src.workflows.funnel_diagnostics import FunnelDiagnostics

_ACCEPTABLE_RISK_DECISIONS = (RiskDecision.APPROVE, RiskDecision.RESIZE)


@dataclass(frozen=True)
class ScannedCandidate:
    candidate: Candidate
    quantitative_analysis: QuantitativeAnalysis | None
    risk_decision: RiskDecision
    risk_reason: str
    post_trade_underlying_exposure_pct: float
    post_trade_sector_exposure_pct: float
    risk_adjusted_return: float | None  # expected_value / capital_required; None when capital_required <= 0 or unpriced
    # PAPER_TRADING_V1.5.5, Step 4: the Risk Engine's own structured
    # `ReasonCode`s for this decision (`RiskDecisionResult.reason_codes`,
    # already computed by `evaluate_trade_proposal` below -- reused
    # verbatim, never a second rejection-reason vocabulary). Empty when
    # quant/risk evaluation raised before a `RiskDecisionResult` was ever
    # produced. Purely observational -- nothing reads this field to
    # decide `risk_decision`/ranking/selection; it exists only so
    # `src.workflows.candidate_funnel` can aggregate rejection reasons
    # without inventing new codes.
    risk_reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class OpportunityScanResult:
    scanned: tuple[ScannedCandidate, ...]
    best: ScannedCandidate | None  # None = CASH/NO_TRADE, a valid first-class outcome
    no_trade_reason: str | None

    @property
    def candidates_generated(self) -> int:
        return len(self.scanned)

    @property
    def candidates_rejected(self) -> int:
        return sum(1 for c in self.scanned if c.risk_decision not in _ACCEPTABLE_RISK_DECISIONS)


def _risk_adjusted_return(qa: QuantitativeAnalysis) -> float | None:
    if qa.capital_required <= 0:
        return None
    return qa.expected_value / qa.capital_required


def scan_and_rank_opportunities(
    universe: list[UniverseEntry],
    chains_by_ticker: dict[str, OptionChain],
    strategies: list[StrategyType],
    quant_filter: QuantFilterConfig,
    limits: RiskLimitsConfig,
    portfolio: Portfolio,
    market_regime: MarketRegimeLabel,
    broker_capabilities: BrokerCapabilities | None,
    *,
    now: datetime,
    proposal_id_prefix: str = "control-loop-scan",
    no_trade_hurdle: float = 0.0,
    diagnostics_by_ticker: dict[str, FunnelDiagnostics] | None = None,
    on_generation_exception: Callable[[str, str, Exception], None] | None = None,
) -> OpportunityScanResult:
    """One control-loop cycle's opportunity-scan phase (Part 23),
    followed by portfolio-aware ranking (Part 24). A ticker missing from
    `chains_by_ticker` (no quality-gated market data this cycle, Part 7)
    is simply skipped -- never treated as "no opportunity," never
    fabricated. A ticker whose candidate generation or quant/risk
    evaluation raises is isolated to that one candidate/ticker (Part 7's
    isolation doctrine), recorded as a rejected candidate rather than
    aborting the whole scan.

    `diagnostics_by_ticker` (PAPER_TRADING_V1.5.5, Step 4) is entirely
    optional and purely additive -- `None` (every call site before this
    step) leaves every line of this function's own decision logic
    untouched. When supplied, this function does exactly two new
    things, both read-only: (1) it passes
    `diagnostics_by_ticker.get(entry.ticker)` through to
    `generate_candidates` unchanged (which itself only ever records,
    never decides, on a supplied `FunnelDiagnostics` -- see that
    function's own docstring), and (2) `scanned`'s own already-computed
    `risk_decision`/`quantitative_analysis` values are additionally
    copied into the new `ScannedCandidate.risk_reason_codes` field
    below. Neither addition changes `scanned`, `survivors`, `ranked`, or
    `best` in any way -- see
    `tests/unit/workflows/test_candidate_funnel_equivalence.py`.

    `on_generation_exception` (PAPER_TRADING_V1.5.13) is entirely
    optional and purely additive, exactly like `diagnostics_by_ticker`
    above -- `None` (every official call site) leaves this function's
    own decision logic completely untouched. When supplied, it is
    forwarded into `generate_candidates` for each ticker (with
    `entry.ticker` bound in), called with `(ticker, strategy_tag, exc)`
    at the exact same point that call's own `diagnostics
    .record_generation_exception` fires -- read-only, never consulted
    by this function's own control flow. See
    `generate_candidates.on_generation_exception`'s own docstring."""
    scanned: list[ScannedCandidate] = []

    for entry in universe:
        chain = chains_by_ticker.get(entry.ticker)
        if chain is None:
            continue
        ticker_diagnostics = diagnostics_by_ticker.get(entry.ticker) if diagnostics_by_ticker is not None else None
        ticker_on_generation_exception = (
            (lambda strategy_tag, exc, _ticker=entry.ticker: on_generation_exception(_ticker, strategy_tag, exc))
            if on_generation_exception is not None else None
        )
        try:
            candidates = generate_candidates(
                entry, chain, strategies, quant_filter, limits, portfolio, market_regime,
                now=now, proposal_id_prefix=proposal_id_prefix, diagnostics=ticker_diagnostics,
                on_generation_exception=ticker_on_generation_exception,
            )
        except Exception:  # noqa: BLE001 -- one ticker's screening failure isolates, never aborts the scan
            continue

        for candidate in candidates:
            try:
                qa = default_quant_stage(candidate.proposal, chain, portfolio, limits, now=now)
                decision = evaluate_trade_proposal(
                    candidate.proposal, portfolio, qa, chain, broker_capabilities, limits=limits, now=now,
                )
            except Exception as exc:  # noqa: BLE001 -- isolate this one candidate, never crash the cycle
                scanned.append(
                    ScannedCandidate(
                        candidate=candidate, quantitative_analysis=None, risk_decision=RiskDecision.REJECT,
                        risk_reason=f"quant/risk evaluation raised: {exc!r}",
                        post_trade_underlying_exposure_pct=underlying_exposure_pct(portfolio, entry.ticker),
                        post_trade_sector_exposure_pct=sector_exposure_pct(portfolio, entry.sector),
                        risk_adjusted_return=None,
                    )
                )
                continue

            additional_capital = decision.capital_required if decision.capital_required is not None else qa.capital_required
            scanned.append(
                ScannedCandidate(
                    candidate=candidate, quantitative_analysis=qa,
                    risk_decision=decision.decision, risk_reason=decision.message,
                    post_trade_underlying_exposure_pct=underlying_exposure_pct(portfolio, entry.ticker, additional_capital),
                    post_trade_sector_exposure_pct=sector_exposure_pct(portfolio, entry.sector, additional_capital),
                    risk_adjusted_return=_risk_adjusted_return(qa),
                    risk_reason_codes=tuple(code.value for code in decision.reason_codes),
                )
            )

    survivors = [
        s for s in scanned
        if s.risk_decision in _ACCEPTABLE_RISK_DECISIONS and s.risk_adjusted_return is not None
    ]
    if not survivors:
        return OpportunityScanResult(
            scanned=tuple(scanned), best=None,
            no_trade_reason="no candidate was Risk-approved (or resized) this cycle",
        )

    ranked = sorted(survivors, key=lambda s: s.risk_adjusted_return, reverse=True)
    best = ranked[0]
    if best.risk_adjusted_return <= no_trade_hurdle:
        return OpportunityScanResult(
            scanned=tuple(scanned), best=None,
            no_trade_reason=(
                f"best risk-adjusted return {best.risk_adjusted_return:.4f} did not clear the "
                f"NO_TRADE hurdle ({no_trade_hurdle:.4f}) -- cash is a valid position"
            ),
        )

    return OpportunityScanResult(scanned=tuple(scanned), best=best, no_trade_reason=None)
