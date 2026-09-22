"""Durable Wheel data shapes: one `wheel_id` ties together every CSP
cycle, every CC cycle, every state transition, and the running
accounting ledger for one Wheel position. Every model here is
`frozen=True` (same convention `src.brokers.base.Order`/`Position`
already use) — lifecycle functions in `src.wheel.lifecycle` never mutate
a `WheelPosition` in place, they build a new one via `.model_copy(update=...)`,
so a reference held anywhere (an audit log, a test assertion) can never
be silently changed out from under it.

Every dollar figure here is either a literal fill price (something
`src.brokers.paper.PaperBroker`/`src.brokers.fidelity` actually recorded)
or arithmetic on those fill prices performed in `src.wheel.accounting` —
never an LLM-originated or fabricated number, the same invariant
`CLAUDE.md` states for every other dollar figure in this codebase.
"""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.wheel.state import WheelState

_CONFIG = ConfigDict(extra="forbid", frozen=True)


def _tz_aware(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("timestamp fields must be timezone-aware")
    return v


class CspCloseReason(str, Enum):
    EXPIRED_WORTHLESS = "expired_worthless"
    BOUGHT_TO_CLOSE = "bought_to_close"
    ASSIGNED = "assigned"


class CcCloseReason(str, Enum):
    EXPIRED_WORTHLESS = "expired_worthless"
    BOUGHT_TO_CLOSE = "bought_to_close"
    ASSIGNED_CALLED_AWAY = "assigned_called_away"


class WheelEventType(str, Enum):
    """Part 14's required auditable event set, verbatim."""

    CSP_OPENED = "csp_opened"
    CSP_BOUGHT_BACK = "csp_bought_back"
    CSP_EXPIRED = "csp_expired"
    CSP_ASSIGNED = "csp_assigned"
    STOCK_POSITION_CREATED = "stock_position_created"
    CC_OPENED = "cc_opened"
    CC_BOUGHT_BACK = "cc_bought_back"
    CC_EXPIRED = "cc_expired"
    CC_ASSIGNED = "cc_assigned"
    SHARES_CALLED_AWAY = "shares_called_away"
    WHEEL_CLOSED = "wheel_closed"
    WHEEL_HALTED = "wheel_halted"
    WHEEL_REJECTED = "wheel_rejected"
    # Not one of Part 14's named events verbatim, but required by Part 7
    # ("If an acceptable covered call cannot be found: NO_CC_TRADE.
    # Holding the shares without selling a call must be valid") to be
    # auditable like every other decision point -- otherwise a Wheel
    # that deliberately holds shares uncalled for weeks would leave no
    # trace of *why* no covered call was sold in any given review cycle.
    NO_CC_TRADE = "no_cc_trade"


class WheelEvent(BaseModel):
    """One auditable event in a Wheel's life (Part 14). `related_id` is
    whatever identifier ties this event back to its source of truth --
    a `TradeProposal.proposal_id`, a `PaperBroker` `client_order_id`, or
    an `ExpirationSettlement`'s option symbol -- never re-derived, always
    copied verbatim from that source."""

    model_config = _CONFIG

    event_id: str = Field(min_length=1, max_length=64)
    wheel_id: str = Field(min_length=1, max_length=64)
    event_type: WheelEventType
    occurred_at: datetime
    detail: str = Field(min_length=1, max_length=2000)
    related_id: str | None = None
    cash_impact: float = 0.0
    share_impact: int = 0

    _tz = field_validator("occurred_at")(classmethod(lambda cls, v: _tz_aware(v)))


class CspCycle(BaseModel):
    model_config = _CONFIG

    cycle_id: str = Field(min_length=1, max_length=64)
    wheel_id: str = Field(min_length=1, max_length=64)
    strike: float = Field(gt=0)
    expiration: date
    contracts: int = Field(gt=0)
    premium_received_per_share: float = Field(ge=0)
    commissions_paid: float = Field(ge=0, default=0.0)
    proposal_id: str | None = None
    position_id: str | None = None
    opened_at: datetime
    closed_at: datetime | None = None
    close_reason: CspCloseReason | None = None
    close_price_per_share: float | None = Field(default=None, ge=0)
    realized_pnl: float | None = None

    _tz_open = field_validator("opened_at")(classmethod(lambda cls, v: _tz_aware(v)))

    @field_validator("closed_at")
    @classmethod
    def _tz_closed(cls, v: datetime | None) -> datetime | None:
        return _tz_aware(v) if v is not None else None

    @model_validator(mode="after")
    def _closed_fields_consistent(self) -> "CspCycle":
        is_closed = self.closed_at is not None
        has_reason = self.close_reason is not None
        if is_closed != has_reason:
            raise ValueError("closed_at and close_reason must be set together")
        return self

    @property
    def is_open(self) -> bool:
        return self.closed_at is None


class CcCycle(BaseModel):
    model_config = _CONFIG

    cycle_id: str = Field(min_length=1, max_length=64)
    wheel_id: str = Field(min_length=1, max_length=64)
    strike: float = Field(gt=0)
    expiration: date
    contracts: int = Field(gt=0)
    premium_received_per_share: float = Field(ge=0)
    commissions_paid: float = Field(ge=0, default=0.0)
    proposal_id: str | None = None
    position_id: str | None = None
    opened_at: datetime
    closed_at: datetime | None = None
    close_reason: CcCloseReason | None = None
    close_price_per_share: float | None = Field(default=None, ge=0)
    realized_pnl: float | None = None

    # Part 8: deterministic below-basis flags, computed once at proposal
    # time and carried with the cycle for audit -- never silently
    # recomputed differently downstream.
    below_acquisition_basis: bool = False
    below_economic_basis: bool = False
    max_loss_if_called_away: float | None = None

    _tz_open = field_validator("opened_at")(classmethod(lambda cls, v: _tz_aware(v)))

    @field_validator("closed_at")
    @classmethod
    def _tz_closed(cls, v: datetime | None) -> datetime | None:
        return _tz_aware(v) if v is not None else None

    @model_validator(mode="after")
    def _closed_fields_consistent(self) -> "CcCycle":
        is_closed = self.closed_at is not None
        has_reason = self.close_reason is not None
        if is_closed != has_reason:
            raise ValueError("closed_at and close_reason must be set together")
        if self.below_economic_basis and not self.below_acquisition_basis:
            # Economic basis (acquisition basis minus net premium
            # collected) is always <= acquisition basis, so a strike
            # below economic basis is necessarily also below acquisition
            # basis -- a caller reporting the opposite has a bug, and
            # this model refuses to persist that inconsistent state
            # rather than silently storing it (Part 8: "both flags must
            # be visible to Portfolio Manager and Risk Engine").
            raise ValueError("below_economic_basis implies below_acquisition_basis")
        return self

    @property
    def is_open(self) -> bool:
        return self.closed_at is None


class WheelStateTransitionRecord(BaseModel):
    model_config = _CONFIG

    wheel_id: str = Field(min_length=1, max_length=64)
    from_state: WheelState
    to_state: WheelState
    occurred_at: datetime
    reason: str = Field(min_length=1, max_length=500)

    _tz = field_validator("occurred_at")(classmethod(lambda cls, v: _tz_aware(v)))


class WheelAccounting(BaseModel):
    """The durable, price-independent ledger (Part 6/9). Anything that
    depends on the *current* market price (unrealized P&L, current
    market value) is deliberately NOT stored here -- it is computed on
    demand by `src.wheel.accounting.summarize_wheel_economics` from a
    caller-supplied live price, since a stored "unrealized P&L" would go
    stale the instant it was written and nothing would ever refresh it.
    """

    model_config = _CONFIG

    total_csp_premium: float = 0.0
    total_cc_premium: float = 0.0
    total_commissions: float = 0.0
    gross_stock_acquisition_cost: float = 0.0  # strike * 100 * contracts, set at assignment, 0 before
    shares_owned: int = Field(ge=0, default=0)
    # Tax/accounting-style basis (per share): the strike paid at
    # assignment, unadjusted by premium. None until assigned.
    acquisition_basis_per_share: float | None = Field(default=None, gt=0)
    # Strategy economic basis (per share): acquisition basis minus every
    # dollar of net Wheel premium collected per share held. None until
    # assigned. Part 6: "reports must clearly distinguish the two."
    economic_basis_per_share: float | None = None
    realized_stock_pnl: float = 0.0  # realized only once shares are sold (called away or exited)
    realized_option_pnl: float = 0.0  # sum of every closed CSP/CC cycle's own realized_pnl
    capital_committed: float = 0.0  # cash reserved (CSP open) or shares' acquisition cost (holding), right now
    max_capital_committed: float = 0.0  # high-water mark of capital_committed over the Wheel's life
    shares_acquired_at: datetime | None = None  # set on assignment, used for "days holding stock"

    @field_validator("total_csp_premium", "total_cc_premium", "total_commissions", "gross_stock_acquisition_cost", "capital_committed", "max_capital_committed")
    @classmethod
    def _non_negative(cls, v: float) -> float:
        if v < 0:
            raise ValueError(f"expected a non-negative running total, got {v!r}")
        return v


class WheelPosition(BaseModel):
    """The full state of one Wheel. `wheel_id` is the persistent unique
    identifier Part 2 requires -- every `CspCycle`/`CcCycle`/`WheelEvent`/
    `WheelStateTransitionRecord` carries it, so the complete audit trail
    for one Wheel can always be reconstructed from `wheel_id` alone."""

    model_config = _CONFIG

    wheel_id: str = Field(min_length=1, max_length=64)
    ticker: str = Field(pattern=r"^[A-Z]{1,10}$")
    state: WheelState
    started_at: datetime
    completed_at: datetime | None = None

    csp_cycles: tuple[CspCycle, ...] = ()
    cc_cycles: tuple[CcCycle, ...] = ()
    state_history: tuple[WheelStateTransitionRecord, ...] = ()
    events: tuple[WheelEvent, ...] = ()

    accounting: WheelAccounting = Field(default_factory=WheelAccounting)

    # References only -- the actual DevilsAdvocateReview/PortfolioDecision
    # objects live wherever this platform already stores agent outputs
    # (src.llm.audit); this Wheel record keeps only the ids that tie a
    # given CSP/CC proposal to the reviews that were run on it, per Part 21.
    risk_decision_ids: tuple[str, ...] = ()
    devils_advocate_review_ids: tuple[str, ...] = ()
    portfolio_manager_decision_ids: tuple[str, ...] = ()

    halt_reason: str | None = None
    exit_reason: str | None = None
    rejection_reason: str | None = None

    _tz_start = field_validator("started_at")(classmethod(lambda cls, v: _tz_aware(v)))

    @field_validator("completed_at")
    @classmethod
    def _tz_completed(cls, v: datetime | None) -> datetime | None:
        return _tz_aware(v) if v is not None else None

    @property
    def open_csp_cycle(self) -> CspCycle | None:
        return next((c for c in self.csp_cycles if c.is_open), None)

    @property
    def open_cc_cycle(self) -> CcCycle | None:
        return next((c for c in self.cc_cycles if c.is_open), None)

    @property
    def csp_cycle_count(self) -> int:
        return len(self.csp_cycles)

    @property
    def cc_cycle_count(self) -> int:
        return len(self.cc_cycles)
