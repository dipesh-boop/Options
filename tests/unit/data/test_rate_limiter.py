"""Tests for `src.data.rate_limiter`."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.data.rate_limiter import RateLimitPriority, RateLimitState, degraded_priorities, may_proceed

NOW = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)


def _state(allowed, used, available, reset_raw=None):
    headers = {"X-Ratelimit-Allowed": str(allowed), "X-Ratelimit-Used": str(used), "X-Ratelimit-Available": str(available)}
    if reset_raw is not None:
        headers["X-Ratelimit-Expiry"] = str(reset_raw)
    return RateLimitState.from_headers(headers, observed_at=NOW)


class TestFromHeaders:
    def test_parses_valid_headers(self):
        s = _state(120, 1, 119)
        assert s is not None and s.allowed == 120 and s.used == 1 and s.available == 119

    def test_missing_headers_returns_none(self):
        assert RateLimitState.from_headers({}, observed_at=NOW) is None

    def test_partial_headers_returns_none(self):
        assert RateLimitState.from_headers({"X-Ratelimit-Allowed": "120"}, observed_at=NOW) is None

    def test_non_numeric_headers_returns_none(self):
        headers = {"X-Ratelimit-Allowed": "abc", "X-Ratelimit-Used": "1", "X-Ratelimit-Available": "119"}
        assert RateLimitState.from_headers(headers, observed_at=NOW) is None

    def test_valid_expiry_parses_reset_at(self):
        s = _state(120, 1, 119, reset_raw=1_700_000_000)
        assert s.reset_at is not None

    def test_malformed_expiry_leaves_reset_at_none(self):
        s = _state(120, 1, 119, reset_raw="not-a-timestamp")
        assert s.reset_at is None


class TestUtilizationPct:
    def test_normal_case(self):
        s = _state(100, 25, 75)
        assert s.utilization_pct == 0.25

    def test_zero_allowed_treated_as_full_utilization(self):
        s = _state(0, 0, 0)
        assert s.utilization_pct == 1.0


class TestMayProceed:
    def test_none_state_always_allows(self):
        assert may_proceed(RateLimitPriority.P5_BACKGROUND_RESEARCH, None) is True

    def test_zero_available_blocks_every_priority_including_p0(self):
        s = _state(120, 120, 0)
        for p in RateLimitPriority:
            assert may_proceed(p, s) is False, f"{p} should be blocked when available=0"

    def test_p0_survives_up_to_full_utilization_short_of_exhaustion(self):
        # 119/120 used, 1 remaining -- P0 must still proceed (Part 9:
        # open-position risk monitoring is the last thing ever suspended).
        s = _state(120, 119, 1)
        assert may_proceed(RateLimitPriority.P0_POSITION_RISK, s) is True

    def test_lower_priorities_throttled_before_p0(self):
        # 90% utilization: P5 (ceiling 0.70) and P4 (ceiling 0.80) should
        # be throttled while P0/P1/P2 (higher ceilings) still proceed.
        s = _state(100, 90, 10)
        assert may_proceed(RateLimitPriority.P5_BACKGROUND_RESEARCH, s) is False
        assert may_proceed(RateLimitPriority.P4_OPPORTUNITY_SCANNING, s) is False
        assert may_proceed(RateLimitPriority.P0_POSITION_RISK, s) is True
        assert may_proceed(RateLimitPriority.P1_POSITION_LIFECYCLE, s) is True

    def test_fresh_state_allows_every_priority(self):
        s = _state(120, 0, 120)
        assert all(may_proceed(p, s) for p in RateLimitPriority)


class TestDegradedPriorities:
    def test_none_state_returns_empty(self):
        assert degraded_priorities(None) == ()

    def test_healthy_state_returns_empty(self):
        assert degraded_priorities(_state(120, 0, 120)) == ()

    def test_degraded_state_lists_only_throttled_priorities(self):
        s = _state(100, 90, 10)
        degraded = degraded_priorities(s)
        assert RateLimitPriority.P5_BACKGROUND_RESEARCH in degraded
        assert RateLimitPriority.P0_POSITION_RISK not in degraded

    def test_exhausted_state_lists_every_priority(self):
        s = _state(120, 120, 0)
        assert set(degraded_priorities(s)) == set(RateLimitPriority)


class TestPriorityOrdering:
    def test_p0_is_lowest_value_highest_priority(self):
        assert RateLimitPriority.P0_POSITION_RISK < RateLimitPriority.P5_BACKGROUND_RESEARCH

    def test_exact_six_priorities_in_spec_order(self):
        assert list(RateLimitPriority) == [
            RateLimitPriority.P0_POSITION_RISK,
            RateLimitPriority.P1_POSITION_LIFECYCLE,
            RateLimitPriority.P2_PORTFOLIO_VALUATION,
            RateLimitPriority.P3_PENDING_TICKET_REPRICING,
            RateLimitPriority.P4_OPPORTUNITY_SCANNING,
            RateLimitPriority.P5_BACKGROUND_RESEARCH,
        ]
