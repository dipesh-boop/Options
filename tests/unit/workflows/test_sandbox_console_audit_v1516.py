"""PAPER_TRADING_V1.5.16, Required Fix 2: regression tests for the
sandbox console wording fix in `scripts/run_sandbox_cycle.py
._print_ranked_candidate_audit`.

**The bug this guards against.** Before this fix, that function's own
console heading described `scan.scanned` -- every Quant-evaluated
candidate this cycle, Risk-rejected ones included -- as "candidates
ranked this cycle". That collided with the persisted funnel's own
`candidates_ranked` field (`src.workflows.candidate_funnel
.build_candidate_funnel`), which counts only the smaller, Risk-approved
subset with a valid ranking score -- exactly the October 9 sandbox
cycle's observed discrepancy (21 printed vs. 10 persisted) this release
exists to explain and fix.

**Label/observability fix only.** Nothing in `src.portfolio
.opportunity_scan.scan_and_rank_opportunities` or `src.workflows
.candidate_funnel.build_candidate_funnel` is touched or imported here
in a way that could mask a real behavior change -- these tests call
both, completely unmodified, and prove only that the SANDBOX SCRIPT'S
OWN print wording is corrected, while every underlying count,
filtering, ranking, and selection decision stays byte-identical to
what `tests/unit/portfolio/test_opportunity_scan.py` and
`tests/unit/workflows/test_candidate_funnel.py` already independently
prove.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from src.data.option_chain import OptionChain, OptionContract, OptionRight
from src.data.quotes import UnderlyingQuote
from src.llm.schemas import StrategyType
from src.portfolio.opportunity_scan import scan_and_rank_opportunities
from src.risk.broker_constraints import load_broker_capabilities
from src.risk.limits import get_default_limits
from src.risk.portfolio_risk import Portfolio
from src.risk.reason_codes import RiskDecision
from src.workflows.candidate_funnel import _ACCEPTABLE_RISK_DECISIONS, build_candidate_funnel
from src.workflows.candidate_generation import QuantFilterConfig, UniverseEntry

REPO_ROOT = Path(__file__).resolve().parents[3]
NOW = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)
EXP = date(2026, 11, 9)
LIMITS = get_default_limits()
BROKER_CAPS = load_broker_capabilities("internal_paper")
QUANT_FILTER = QuantFilterConfig(min_dte=20, max_dte=45)


def _load_run_sandbox_cycle_module():
    """Loads the real `scripts/run_sandbox_cycle.py` fresh, exactly like
    `tests/acceptance/test_sandbox_isolation.py`'s own `_load_script_module`
    helper -- so these tests exercise the actual, shipped
    `_print_ranked_candidate_audit` function, never a copy."""
    path = REPO_ROOT / "scripts" / "run_sandbox_cycle.py"
    spec = importlib.util.spec_from_file_location("_v1516_console_audit_cycle", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _chain(ticker: str, strike: float, *, bid: float, ask: float, oi: int = 1000, vol: int = 500) -> OptionChain:
    underlying = UnderlyingQuote(
        symbol=ticker, bid=strike * 1.01 - 0.5, ask=strike * 1.01 + 0.5, last=strike * 1.01,
        volume=1_000_000, timestamp=NOW, source="test",
    )
    contract = OptionContract(
        underlying=ticker, option_symbol=f"{ticker}{EXP.isoformat()}P{int(strike*1000):08d}", expiration=EXP,
        strike=strike, right=OptionRight.PUT, bid=bid, ask=ask, last=(bid + ask) / 2,
        volume=vol, open_interest=oi, delta=-0.20, iv=0.22, underlying_price=strike * 1.01, timestamp=NOW, source="test",
    )
    return OptionChain(underlying=underlying, contracts=[contract], timestamp=NOW, source="test")


def _mixed_decision_scan():
    """Builds ONE scan, against ONE $100,000-NAV portfolio, containing
    both a Risk-APPROVED candidate (a cheap, low-strike ticker whose
    worst-case loss stays well inside `absolute_max_risk_per_trade_pct`
    of NAV) and a Risk-REJECTED candidate (an expensive, high-strike
    ticker whose worst-case loss alone exceeds that per-trade budget)
    -- the exact mixed population `scan.scanned` carries every real
    cycle, never fabricated by hand-setting a `risk_decision` field."""
    universe = [UniverseEntry(ticker="CHEAP", sector="ETF"), UniverseEntry(ticker="EXPENSIVE", sector="ETF")]
    chains = {
        "CHEAP": _chain("CHEAP", 8.0, bid=0.15, ask=0.17),
        "EXPENSIVE": _chain("EXPENSIVE", 1000.0, bid=20.00, ask=20.30),
    }
    portfolio = Portfolio(as_of=NOW, nav=100_000.0, cash=100_000.0, peak_equity=100_000.0)
    return scan_and_rank_opportunities(
        universe, chains, [StrategyType.CASH_SECURED_PUT], QUANT_FILTER, LIMITS, portfolio, "normal",
        BROKER_CAPS, now=NOW,
    )


class TestMixedScanSanityPreconditions:
    """Confirms the fixture scenario actually produces one of each
    decision before relying on it below -- if this ever stops holding
    (e.g. a future limits change), the real failure must surface here,
    not as a confusing failure in the wording tests."""

    def test_scan_contains_one_approved_and_one_rejected_candidate(self):
        scan = _mixed_decision_scan()
        assert len(scan.scanned) == 2
        decisions = {s.candidate.proposal.ticker: s.risk_decision for s in scan.scanned}
        assert decisions["CHEAP"] in _ACCEPTABLE_RISK_DECISIONS
        assert decisions["EXPENSIVE"] == RiskDecision.REJECT


class TestItem1_RiskRejectedCandidatesMayAppearInTheAudit:
    def test_the_rejected_tickers_diagnostic_line_is_printed(self, capsys):
        scan = _mixed_decision_scan()
        module = _load_run_sandbox_cycle_module()
        module._print_ranked_candidate_audit(scan)
        out = capsys.readouterr().out
        assert "EXPENSIVE" in out
        assert "risk_decision=reject" in out


class TestItem2_RiskRejectedCandidatesNotCountedInCandidatesRanked:
    def test_candidates_ranked_excludes_the_rejected_candidate(self):
        scan = _mixed_decision_scan()
        funnel = build_candidate_funnel(
            cycle_id="cyc-v1516", generated_at=NOW, universe_tickers=("CHEAP", "EXPENSIVE"),
            chain_received_tickers=frozenset({"CHEAP", "EXPENSIVE"}), diagnostics_by_ticker={},
            scan_result=scan, candidates_persisted=0,
        )
        # Exactly the V1.5.16 bug: len(scan.scanned) == 2, but the
        # persisted funnel's candidates_ranked must still be 1.
        assert len(scan.scanned) == 2
        assert funnel.candidates_ranked == 1


class TestItem3_RiskRejectedCandidatesCanNeverBecomeScanBest:
    def test_best_is_none_or_is_never_the_rejected_ticker(self):
        scan = _mixed_decision_scan()
        assert scan.best is None or scan.best.candidate.proposal.ticker != "EXPENSIVE"


class TestItem4_RiskRejectedCandidatesCanNeverBePersistedForReview:
    """`scripts/run_sandbox_cycle.py` only ever constructs a
    `ReviewedCandidate` from `scan.best` (see its own source around the
    `elif scan.best is None` / `else` branch) -- never from any other
    entry in `scan.scanned`. Combined with
    `TestItem3_RiskRejectedCandidatesCanNeverBecomeScanBest` (a
    Risk-rejected candidate can never BE `scan.best`), this proves a
    Risk-rejected candidate can never reach persistence, without
    needing to stand up the full sandbox database/identity plumbing
    just to re-observe the same guarantee end-to-end."""

    def test_reviewedcandidate_is_only_ever_constructed_from_scan_best(self):
        text = (REPO_ROOT / "scripts" / "run_sandbox_cycle.py").read_text()
        assert text.count("ReviewedCandidate(") == 1
        construction_site = text.split("ReviewedCandidate(", 1)[1][:400]
        assert "best.candidate.proposal" in construction_site
        # The construction call must be reached only AFTER a prior
        # `elif scan.best is None:` branch has already handled the
        # no-candidate case -- never unconditionally, and never from
        # iterating `scan.scanned` directly.
        before_construction = text.split("ReviewedCandidate(", 1)[0]
        assert "elif scan.best is None:" in before_construction
        after_no_best_branch = before_construction.split("elif scan.best is None:", 1)[1]
        assert "best = scan.best" in after_no_best_branch


class TestItem5_ConsoleHeadingNeverCallsScanScannedCandidatesRanked:
    def test_heading_wording_no_longer_says_ranked(self, capsys):
        scan = _mixed_decision_scan()
        module = _load_run_sandbox_cycle_module()
        module._print_ranked_candidate_audit(scan)
        out = capsys.readouterr().out
        assert "ranked candidate audit" not in out
        assert "candidates ranked this cycle" not in out
        assert "candidate evaluation audit" in out
        assert "scanned/evaluated this cycle" in out
        assert str(len(scan.scanned)) in out

    def test_empty_scan_heading_also_avoids_ranking_wording(self, capsys):
        class _EmptyScan:
            scanned = ()
            best = None
            no_trade_reason = None

        module = _load_run_sandbox_cycle_module()
        module._print_ranked_candidate_audit(_EmptyScan())
        out = capsys.readouterr().out
        assert "ranked" not in out.lower()
        assert "candidate evaluation audit" in out


class TestItem6_ExistingFunnelSemanticsForTheSixNamedFieldsAreUnchanged:
    def test_all_six_named_fields_carry_their_documented_meaning(self):
        scan = _mixed_decision_scan()
        funnel = build_candidate_funnel(
            cycle_id="cyc-v1516b", generated_at=NOW, universe_tickers=("CHEAP", "EXPENSIVE"),
            chain_received_tickers=frozenset({"CHEAP", "EXPENSIVE"}), diagnostics_by_ticker={},
            scan_result=scan, candidates_persisted=0,
        )
        # candidates_generated: every quant-evaluated candidate (both).
        assert funnel.candidates_generated == scan.candidates_generated == 2
        # quant_passed: both candidates priced successfully.
        assert funnel.quant_passed == 2
        # risk_passed: only the approved one.
        assert funnel.risk_passed == 1
        # candidates_ranked: only the approved, scoreable one.
        assert funnel.candidates_ranked == 1
        # candidates_selected: 1 iff scan.best is not None, else 0.
        assert funnel.candidates_selected == (1 if scan.best is not None else 0)
        # candidates_persisted_for_review: passed straight through from
        # the caller's own count, untouched by this fix.
        assert funnel.candidates_persisted_for_review == 0
