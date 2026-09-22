"""Deterministic, provider-agnostic rate-limit budget management (Step
22.4 Part 9).

Pure and testable: nothing here makes an HTTP request itself, and
nothing hardcodes "120/minute" as an eternal assumption — `RateLimitState`
is built fresh from whatever a provider's own response headers report
each time (`from_headers`), never from a baked-in number. A concrete
provider (`src.data.tradier_provider`) is the only thing that actually
calls an API; this module just answers "given the most recently observed
state, may a request at this priority proceed right now."

**Priority precedence is the concrete mechanism behind Part 9's "never
sacrifice risk monitoring merely to scan more symbols."** `RateLimitPriority`
is ordered so that as headroom shrinks, lower-priority work (opportunity
scanning, background research) is throttled first — P0 (open-position
risk monitoring) is the very last thing ever suspended, and only once
the provider itself reports zero remaining capacity.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntEnum


class RateLimitPriority(IntEnum):
    """Lower value = higher priority — Part 9's exact ordering."""

    P0_POSITION_RISK = 0
    P1_POSITION_LIFECYCLE = 1
    P2_PORTFOLIO_VALUATION = 2
    P3_PENDING_TICKET_REPRICING = 3
    P4_OPPORTUNITY_SCANNING = 4
    P5_BACKGROUND_RESEARCH = 5


@dataclass(frozen=True)
class RateLimitState:
    """A snapshot of a provider's own reported rate-limit accounting —
    built exclusively from response headers, never from a hardcoded
    assumption about the account's plan."""

    allowed: int
    used: int
    available: int
    reset_at: datetime | None
    observed_at: datetime

    @property
    def utilization_pct(self) -> float:
        if self.allowed <= 0:
            return 1.0
        return self.used / self.allowed

    @classmethod
    def from_headers(cls, headers: dict, *, observed_at: datetime) -> "RateLimitState | None":
        """Returns `None` — never a fabricated state — when the
        provider didn't send rate-limit headers on this particular
        response at all."""
        allowed_raw = headers.get("X-Ratelimit-Allowed")
        used_raw = headers.get("X-Ratelimit-Used")
        available_raw = headers.get("X-Ratelimit-Available")
        if allowed_raw is None or used_raw is None or available_raw is None:
            return None
        reset_at: datetime | None = None
        reset_raw = headers.get("X-Ratelimit-Expiry")
        if reset_raw is not None:
            try:
                reset_at = datetime.fromtimestamp(int(reset_raw), tz=timezone.utc)
            except (ValueError, OSError, OverflowError):
                reset_at = None
        try:
            return cls(
                allowed=int(allowed_raw), used=int(used_raw), available=int(available_raw),
                reset_at=reset_at, observed_at=observed_at,
            )
        except ValueError:
            return None


# Conservative headroom: this platform never intentionally consumes the
# full reported allowance during normal operation (Part 9's explicit
# requirement). Each priority has its own utilization ceiling —
# breaching it throttles that priority (and everything lower) while
# higher priorities keep operating.
_PRIORITY_UTILIZATION_CEILING: dict[RateLimitPriority, float] = {
    # P0 is throttled by utilization alone, never -- Part 9's "preserve
    # open-position/risk monitoring" means it is only ever stopped by
    # the hard available<=0 floor `may_proceed` checks first, never by
    # this ceiling.
    RateLimitPriority.P0_POSITION_RISK: 1.0,
    RateLimitPriority.P1_POSITION_LIFECYCLE: 0.95,
    RateLimitPriority.P2_PORTFOLIO_VALUATION: 0.90,
    RateLimitPriority.P3_PENDING_TICKET_REPRICING: 0.85,
    RateLimitPriority.P4_OPPORTUNITY_SCANNING: 0.80,
    RateLimitPriority.P5_BACKGROUND_RESEARCH: 0.70,
}


def may_proceed(priority: RateLimitPriority, state: RateLimitState | None) -> bool:
    """The single deterministic gate every rate-limited request must
    pass through. `state=None` (no header-derived state observed yet —
    e.g. the very first request of a session) always allows the
    request: there is nothing to throttle against, and refusing the
    first request outright would make the provider unusable."""
    if state is None:
        return True
    if state.available <= 0:
        return False
    return state.utilization_pct <= _PRIORITY_UTILIZATION_CEILING[priority]


def degraded_priorities(state: RateLimitState | None) -> tuple[RateLimitPriority, ...]:
    """Every priority currently throttled, given the same state
    `may_proceed` would use — for reporting on `ProviderHealth`/alerts,
    never used to make the actual gating decision itself (that's always
    a direct `may_proceed` call at the point of the request)."""
    if state is None:
        return ()
    return tuple(p for p in RateLimitPriority if not may_proceed(p, state))
