"""Part 4/5/24: MFE/MAE excursion tracking and exit efficiency."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.lifecycle.excursion import exit_efficiency, initial_excursion, update_excursion

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


class TestInitialExcursion:
    def test_mfe_and_mae_both_start_at_first_observation(self):
        s = initial_excursion(-10.0, T0)
        assert s.mfe == -10.0
        assert s.mae == -10.0
        assert s.mfe_at == T0 and s.mae_at == T0


class TestUpdateExcursion:
    def test_mfe_rises_and_mae_falls_over_observations(self):
        s = initial_excursion(-10.0, T0)
        s = update_excursion(s, 50.0, T0 + timedelta(days=1))
        s = update_excursion(s, -20.0, T0 + timedelta(days=2))
        assert s.mfe == 50.0
        assert s.mae == -20.0

    def test_mfe_does_not_move_on_a_worse_observation(self):
        s = initial_excursion(50.0, T0)
        s = update_excursion(s, 10.0, T0 + timedelta(days=1))
        assert s.mfe == 50.0
        assert s.mfe_at == T0

    def test_mae_does_not_move_on_a_better_observation(self):
        s = initial_excursion(-50.0, T0)
        s = update_excursion(s, 10.0, T0 + timedelta(days=1))
        assert s.mae == -50.0
        assert s.mae_at == T0

    def test_last_observation_always_updates(self):
        s = initial_excursion(0.0, T0)
        s = update_excursion(s, 5.0, T0 + timedelta(days=1))
        assert s.last_unrealized_pnl == 5.0
        assert s.last_updated_at == T0 + timedelta(days=1)

    def test_out_of_order_update_raises(self):
        s = initial_excursion(0.0, T0 + timedelta(days=2))
        with pytest.raises(ValueError):
            update_excursion(s, 10.0, T0)

    def test_state_is_frozen_never_mutated(self):
        s = initial_excursion(0.0, T0)
        s2 = update_excursion(s, 10.0, T0 + timedelta(days=1))
        assert s.mfe == 0.0  # original untouched
        assert s2.mfe == 10.0


class TestExitEfficiency:
    def test_positive_mfe_gives_a_ratio(self):
        assert exit_efficiency(30.0, 50.0) == 0.6

    def test_full_capture_gives_one(self):
        assert exit_efficiency(50.0, 50.0) == 1.0

    def test_zero_mfe_returns_none_not_zero(self):
        assert exit_efficiency(0.0, 0.0) is None

    def test_negative_mfe_returns_none(self):
        """Position was never profitable -- 'fraction of peak captured'
        is not meaningful, never fabricated as 0.0 or 1.0."""
        assert exit_efficiency(-10.0, -5.0) is None
