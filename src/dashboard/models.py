"""Dashboard-owned state (Step 18): the mutable, per-session record of
what the human-execution dashboard is showing and tracking.

Nothing in this module computes a price, a Greek, or a risk decision —
every number here was already produced by `src.risk`/`src.quant`/
`src.orchestration`/`src.brokers.fidelity`, this module only tracks
*lifecycle* state (which ticket is at which status, what a human has
since confirmed) and the append-only audit trail of every action taken.
It is deliberately process-local (no persistence) — the same honestly-
flagged Phase-0 gap every other `InMemory*` placeholder in this codebase
carries (see `progress.md`); a real deployment would swap `DashboardState`
for something backed by `src.orchestration.pipeline.SqliteDatabase`-style
durable storage without changing anything downstream of it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from src.brokers.fidelity import FidelityTradeTicket
from src.data.option_chain import OptionChain
from src.llm.devils_advocate import DevilsAdvocateEvaluation
from src.llm.schemas import TradeProposal
from src.risk.engine import RiskDecisionResult
from src.risk.limits import RiskLimitsConfig
from src.risk.portfolio_risk import Portfolio
from src.risk.trade_risk import QuantitativeAnalysis
from src.wheel.models import WheelPosition


class AuditEventType(str, Enum):
    """Exactly the events Step 18 requires recorded: "every refresh,
    approval, rejection, ticket copy, order-entered event, fill, partial
    fill, cancellation." APPROVAL fires whenever a ticket becomes
    actionable at AWAITING_HUMAN (initial load, or after a reprice
    regenerates it) — the point at which the Risk Engine's own APPROVE/
    RESIZE decision reaches a human, since the dashboard has no separate
    "approve" button of its own (the Risk Engine already approved it;
    the dashboard's only decisions are REJECT, or acting on a fill)."""

    REFRESH = "refresh"
    APPROVAL = "approval"
    REJECTION = "rejection"
    TICKET_COPY = "ticket_copy"
    ORDER_ENTERED = "order_entered"
    FILL = "fill"
    PARTIAL_FILL = "partial_fill"
    CANCELLATION = "cancellation"


@dataclass(frozen=True)
class AuditEvent:
    event_id: str
    trade_id: str | None
    event_type: AuditEventType
    at: datetime
    actor: str
    detail: str


class AuditLog:
    """Append-only. No method here ever removes or edits a recorded
    event — the audit trail is the one thing a human-execution dashboard
    must never let any action quietly bypass."""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []

    def record(self, event: AuditEvent) -> None:
        self._events.append(event)

    def all(self) -> list[AuditEvent]:
        return list(self._events)

    def for_trade(self, trade_id: str) -> list[AuditEvent]:
        return [e for e in self._events if e.trade_id == trade_id]


@dataclass(frozen=True)
class OrderEntryRecord:
    """What a human reports after manually entering an order into
    Fidelity Trader+ — the three fields Step 18's ORDER ENTERED section
    requires: the ACTUAL limit price they entered (which may differ
    from the ticket's own target if Fidelity's book moved before they
    finished typing), contracts, and when. Tracked separately from
    `FidelityTradeTicket` (which stays exactly what the Risk Engine
    approved) rather than overwriting the ticket's own `limit_price`."""

    actual_limit_entered: float
    contracts: int
    entered_at: datetime
    entered_by: str


@dataclass
class OpportunityRecord:
    """One candidate's full, mutable dashboard lifecycle: the
    already-computed proposal/quant/Devil's-Advocate/Risk-Engine
    artifacts (never recomputed here directly — only displayed, and, on
    a REFRESH PRICE action, replaced wholesale by a fresh pipeline run),
    plus the ticket's own state-machine status and whatever a human has
    since confirmed about it. `market_data` is `None` when this record
    was built from a `MorningScanReport` (the scan doesn't retain each
    candidate's chain past producing the ticket) — populated honestly,
    never fabricated, the first time a REFRESH PRICE actually runs."""

    trade_id: str
    proposal: TradeProposal
    market_data: OptionChain | None
    quantitative_analysis: QuantitativeAnalysis | None
    devils_advocate_review: DevilsAdvocateEvaluation | None
    risk_decision: RiskDecisionResult | None
    ticket: FidelityTradeTicket
    order_entry: OrderEntryRecord | None = None
    rejection_reason: str | None = None
    cancellation_reason: str | None = None


@dataclass
class DashboardState:
    """The whole in-memory dashboard session: the current portfolio, the
    risk limits it's judged against, every tracked opportunity keyed by
    `trade_id`, and the append-only audit log. Portfolio-level Greeks
    and daily/YTD P&L are optional, caller-supplied inputs — exactly the
    same "not tracked" honesty `src.workflows.morning_scan` already
    established, never fabricated when absent."""

    portfolio: Portfolio
    limits: RiskLimitsConfig
    opportunities: dict[str, OpportunityRecord] = field(default_factory=dict)
    audit_log: AuditLog = field(default_factory=AuditLog)
    portfolio_net_delta: float | None = None
    portfolio_net_theta: float | None = None
    portfolio_net_vega: float | None = None
    daily_pnl: float | None = None
    ytd_return_pct: float | None = None
    # Step 22.2: read-only Wheel visibility (Part 19). Keyed by wheel_id,
    # exactly like `opportunities` is keyed by trade_id -- populated by
    # whatever loads/persists Wheel state (src.wheel.persistence), never
    # computed by the dashboard itself. current_price_by_ticker supplies
    # the live mark used to compute each Wheel's unrealized P&L for
    # display (src.wheel.accounting.summarize_wheel_economics) -- a
    # ticker missing from it simply renders without a live mark, never a
    # fabricated one.
    wheels: dict[str, WheelPosition] = field(default_factory=dict)
    current_price_by_ticker: dict[str, float] = field(default_factory=dict)
