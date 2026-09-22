"""Step 22.2 Part 23's security proofs: no live brokerage execution was
added, no Fidelity auto-execution, no Alpaca trading API, no LLM Risk
override, no hidden naked-option path, and the Wheel is never exempted
from the platform's ordinary portfolio limits. Every check here is a
structural, executable proof over `src/wheel/`'s actual source and
runtime behavior -- never a comment asserting an intention."""
from __future__ import annotations

import inspect
import re
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from src.wheel import accounting, eligibility, fidelity_events, lifecycle, paper_events, persistence, review_context, risk, state
from src.wheel.models import WheelPosition

REPO_ROOT = Path(__file__).resolve().parents[2]
WHEEL_SRC_DIR = REPO_ROOT / "src" / "wheel"
WHEEL_MODULES = [
    accounting, eligibility, fidelity_events, lifecycle, paper_events, persistence, review_context, risk, state,
]


def _wheel_source_files() -> list[Path]:
    return sorted(p for p in WHEEL_SRC_DIR.glob("*.py") if p.name != "__pycache__")


class TestNoLiveBrokerageExecutionAnywhereInWheel:
    """Part 27's "no live or automatic brokerage execution was added"
    requirement, proven the same way `tests/acceptance
    /test_fidelity_manual_only.py` already proves it for the rest of
    this codebase: a repo-wide regex grep for the exact import shapes
    that would constitute one."""

    _FORBIDDEN_IMPORT_PATTERN = re.compile(
        r"^\s*(from|import)\s+(alpaca\.trading|ib_insync|ibapi)\b", re.MULTILINE
    )
    # place_order is deliberately excluded: PaperBroker.place_order is
    # this platform's own internal simulator (src.brokers.paper), called
    # legitimately throughout src.wheel.paper_events -- the live-shaped
    # names below are ones no module in this codebase defines for any
    # real broker at all, so their mere presence would itself be the
    # red flag.
    _FORBIDDEN_METHOD_NAME_PATTERN = re.compile(
        r"\b(submit_order|send_order|execute_trade|submit_trade|place_trade)\s*\(", re.IGNORECASE
    )

    def test_no_live_trading_client_import_anywhere_in_wheel_package(self):
        for path in _wheel_source_files():
            text = path.read_text(encoding="utf-8")
            assert not self._FORBIDDEN_IMPORT_PATTERN.search(text), f"{path} imports a live trading client"

    def test_no_order_submission_method_name_called_anywhere_in_wheel_package(self):
        for path in _wheel_source_files():
            text = path.read_text(encoding="utf-8")
            for match in self._FORBIDDEN_METHOD_NAME_PATTERN.finditer(text):
                pytest.fail(f"{path} calls forbidden live-execution-shaped method: {match.group(0)!r}")

    def test_no_network_client_imports(self):
        forbidden = re.compile(r"^\s*(from|import)\s+(requests|httpx|aiohttp|urllib\.request|socket)\b", re.MULTILINE)
        for path in _wheel_source_files():
            text = path.read_text(encoding="utf-8")
            assert not forbidden.search(text), f"{path} imports a network client -- src.wheel has no network client of any kind"


class TestNoFidelityAutoExecution:
    def test_fidelity_events_never_calls_confirm_fill_or_transition_itself(self):
        """Only a human-supplied ExecutionConfirmation (via
        src.brokers.fidelity.confirm_fill, called by whatever platform
        component the human's dashboard/CLI action already routes
        through) can move a ticket to FILLED -- src.wheel.fidelity_events
        never calls confirm_fill or transition() itself; it only reads
        an already-confirmed ticket's fields."""
        source = inspect.getsource(fidelity_events)
        assert "confirm_fill(" not in source
        assert re.search(r"\btransition\(", source) is None

    def test_fidelity_events_module_never_constructs_a_filled_ticket_directly(self):
        source = inspect.getsource(fidelity_events)
        assert "TicketStatus.FILLED" not in source

    def test_assignment_recording_functions_take_no_price_or_order_parameters(self):
        """Part 20: assignment is a reconciliation event, never an
        order -- proven at the signature level: neither assignment
        recorder accepts a limit price, an order type, or anything
        order-shaped."""
        sig_csp = inspect.signature(fidelity_events.record_wheel_csp_assignment)
        sig_cc = inspect.signature(fidelity_events.record_wheel_cc_assignment)
        for sig in (sig_csp, sig_cc):
            assert set(sig.parameters) == {"wheel", "now"}


class TestNoAlpacaTradingApi:
    def test_no_alpaca_import_anywhere_in_wheel_package(self):
        forbidden = re.compile(r"^\s*(from|import)\s+alpaca\b", re.MULTILINE)
        for path in _wheel_source_files():
            text = path.read_text(encoding="utf-8")
            assert not forbidden.search(text), f"{path} imports alpaca -- src.wheel has no market-data dependency of its own"


class TestNoLlmRiskOverride:
    """CLAUDE.md: "No LLM ever computes an authoritative price, Greek,
    probability, or risk figure" and "No LLM output... can bypass" the
    Risk Engine. Proven for src.wheel the same way
    tests/unit/risk/test_architecture_boundary.py already proves it for
    src.risk: none of the deterministic modules import src.llm.client or
    src.llm.router (the only two modules in this codebase capable of an
    actual model call)."""

    _DETERMINISTIC_MODULES = [accounting, eligibility, lifecycle, paper_events, persistence, risk, state]

    def test_no_deterministic_wheel_module_imports_the_llm_client_or_router(self):
        for module in self._DETERMINISTIC_MODULES:
            source = inspect.getsource(module)
            assert "src.llm.client" not in source, f"{module.__name__} imports the LLM client"
            assert "src.llm.router" not in source, f"{module.__name__} imports the LLM router"

    def test_review_context_module_only_supplies_data_never_calls_a_model(self):
        """review_context.py is the one module that legitimately touches
        `src.llm` (it builds context *for* those orchestrators) -- it
        must still never import the client/router that would let it
        actually call a model."""
        source = inspect.getsource(review_context)
        assert "src.llm.client" not in source
        assert "src.llm.router" not in source

    def test_lifecycle_state_transitions_never_take_an_llm_verdict_as_input(self):
        """Every state-changing function in lifecycle.py takes only
        Python-native/Pydantic types -- never a DevilsAdvocateReview or
        PortfolioDecision object that an LLM's verdict could be smuggled
        in through."""
        for name in ("open_csp", "csp_assigned", "open_cc", "shares_called_away", "halt_wheel", "exit_wheel", "reject_candidate"):
            sig = inspect.signature(getattr(lifecycle, name))
            for param in sig.parameters.values():
                annotation = str(param.annotation)
                assert "DevilsAdvocateReview" not in annotation
                assert "PortfolioDecision" not in annotation


class TestNoHiddenNakedOptionPath:
    def test_open_cc_always_checks_shares_owned_before_constructing_a_cycle(self):
        """Static proof, not just a passing unit test: the
        UncoveredCallError check happens before any CcCycle is
        constructed in open_cc's own source (so there is no code path,
        including a hypothetical future edit that reorders unrelated
        lines, where a cycle could be built first and checked after)."""
        source = inspect.getsource(lifecycle.open_cc)
        check_pos = source.index("UncoveredCallError")
        cycle_pos = source.index("CcCycle(")
        assert check_pos < cycle_pos

    def test_open_cc_raises_for_zero_shares(self):
        w = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="SPY", now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        with pytest.raises(lifecycle.UncoveredCallError):
            lifecycle.open_cc(
                w, strike=100.0, expiration=date(2026, 2, 1), contracts=1, premium_per_share=1.0, commission=0.65,
                proposal_id=None, position_id=None, now=datetime(2026, 1, 1, tzinfo=timezone.utc),
                below_acquisition_basis=False, below_economic_basis=False, max_loss_if_called_away=None,
            )

    def test_open_cc_raises_for_partially_insufficient_shares(self):
        w = lifecycle.open_wheel_candidate(wheel_id="w1", ticker="SPY", now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        w = w.model_copy(update={"accounting": w.accounting.model_copy(update={"shares_owned": 150})})  # enough for 1 contract, not 2
        w = w.model_copy(update={"state": state.WheelState.CC_ELIGIBLE})
        with pytest.raises(lifecycle.UncoveredCallError):
            lifecycle.open_cc(
                w, strike=100.0, expiration=date(2026, 2, 1), contracts=2, premium_per_share=1.0, commission=0.65,
                proposal_id=None, position_id=None, now=datetime(2026, 1, 1, tzinfo=timezone.utc),
                below_acquisition_basis=False, below_economic_basis=False, max_loss_if_called_away=None,
            )

    def test_paperbroker_itself_also_independently_refuses_an_uncovered_call(self):
        """Defense in depth, re-verified here: PaperBroker's own
        collateral check (src.brokers.paper) refuses a naked call
        exactly as it would for any other strategy -- the Wheel gets no
        special bypass at the broker layer either."""
        import asyncio

        from src.brokers.base import OrderAction, OrderLeg, OrderType, PlaceOrderRequest
        from src.brokers.paper import PaperBroker
        from src.data.option_chain import OptionRight

        broker = PaperBroker(initial_cash=1000.0, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        request = PlaceOrderRequest(
            client_order_id="naked-1",
            legs=[OrderLeg(symbol="SPY260101C00600000", right=OptionRight.CALL, strike=600.0, expiration=date(2026, 1, 1), action=OrderAction.SELL, quantity=1)],
            order_type=OrderType.LIMIT, limit_price=1.0,
        )
        order = asyncio.run(broker.place_order(request))
        assert order.status.value == "rejected"


class TestWheelNeverExemptFromPortfolioLimits:
    def test_wheel_strategy_kind_not_in_brokers_yaml_as_a_separate_capability(self):
        """Part 16/Part 3: a Wheel's orders are ordinary CASH_SECURED_PUT/
        COVERED_CALL TradeProposals -- there must be no separate 'WHEEL'
        entry anywhere in config/brokers.yaml's allowed_strategies lists
        that could carry a different (looser) capability than those two
        already-audited strategies."""
        brokers_yaml = (REPO_ROOT / "config" / "brokers.yaml").read_text(encoding="utf-8")
        assert "WHEEL" not in brokers_yaml

    def test_wheel_kind_never_becomes_a_direct_tradeproposal_strategy_type(self):
        from src.llm.schemas import StrategyType
        from src.strategies.base import TRADE_PROPOSAL_ELIGIBLE, StrategyKind

        assert StrategyKind.WHEEL not in TRADE_PROPOSAL_ELIGIBLE
        assert not hasattr(StrategyType, "WHEEL")

    def test_risk_limits_yaml_never_mentions_wheel(self):
        risk_limits_yaml = (REPO_ROOT / "config" / "risk_limits.yaml").read_text(encoding="utf-8")
        assert "wheel" not in risk_limits_yaml.lower()

    def test_risk_engine_module_never_mentions_wheel(self):
        """The unmodified src.risk.engine has no Wheel-specific branch at
        all -- proof that Wheel orders pass through the exact same
        evaluate_trade_proposal code path as everything else, not a
        parallel or relaxed one."""
        engine_source = (REPO_ROOT / "src" / "risk" / "engine.py").read_text(encoding="utf-8")
        assert "wheel" not in engine_source.lower()

    def test_aggregate_exposure_feeds_the_real_concentration_functions_not_a_local_copy(self):
        source = inspect.getsource(risk)
        assert "from src.risk.portfolio_risk import" in source
        assert "underlying_exposure_pct" in source
        assert "sector_exposure_pct" in source


class TestPydanticModelsRejectUnknownFields:
    """extra='forbid' on every wheel model -- a stray or forged field
    (e.g. an attempt to inject a fabricated risk override) fails
    validation outright rather than being silently accepted."""

    def test_wheel_position_rejects_extra_fields(self):
        with pytest.raises(Exception):
            WheelPosition(
                wheel_id="w1", ticker="SPY", state=state.WheelState.WHEEL_CANDIDATE,
                started_at=datetime(2026, 1, 1, tzinfo=timezone.utc), not_a_real_field=True,
            )
