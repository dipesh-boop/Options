"""Position sizing tests: hand-computable cases plus the core safety
property — a requested size can never exceed the deterministically
computed maximum."""
from __future__ import annotations

import pytest

from src.quant.position_sizing import cap_requested_contracts, fixed_fractional_size


class TestFixedFractionalSize:
    def test_hand_computed_case(self):
        # $100,000 account, risking 2% ($2,000) per trade, max loss of
        # $975 per contract (e.g. a CSP: (100-2.50)*100=9750 -> wait,
        # use a round number instead: $500/contract).
        result = fixed_fractional_size(account_equity=100_000, risk_per_trade_pct=0.02, max_loss_per_contract=500)
        # 2% of 100,000 = 2,000; 2,000 / 500 = 4 contracts exactly.
        assert result.contracts == 4
        assert result.risk_allocated == pytest.approx(2000.0)
        assert result.capped_by == "risk_budget"

    def test_floors_to_whole_contracts(self):
        # 2,000 / 750 = 2.667 -> floors to 2.
        result = fixed_fractional_size(account_equity=100_000, risk_per_trade_pct=0.02, max_loss_per_contract=750)
        assert result.contracts == 2

    def test_capital_budget_can_be_the_binding_constraint(self):
        # Risk budget alone would allow 10 contracts (10,000/1,000), but
        # a tight capital cap only allows 3 (15,000/5,000).
        result = fixed_fractional_size(
            account_equity=100_000,
            risk_per_trade_pct=0.10,
            max_loss_per_contract=1_000,
            capital_per_contract=5_000,
            max_capital_pct=0.15,
        )
        assert result.contracts == 3
        assert result.capped_by == "capital_budget"

    def test_risk_budget_binding_when_looser_capital_cap(self):
        result = fixed_fractional_size(
            account_equity=100_000,
            risk_per_trade_pct=0.02,
            max_loss_per_contract=500,
            capital_per_contract=1_000,
            max_capital_pct=0.50,
        )
        # risk: 2,000/500=4; capital: 50,000/1,000=50 -> risk binds.
        assert result.contracts == 4
        assert result.capped_by == "risk_budget"

    @pytest.mark.parametrize(
        "kwargs",
        [
            dict(account_equity=0, risk_per_trade_pct=0.02, max_loss_per_contract=500),
            dict(account_equity=-100, risk_per_trade_pct=0.02, max_loss_per_contract=500),
            dict(account_equity=100_000, risk_per_trade_pct=0, max_loss_per_contract=500),
            dict(account_equity=100_000, risk_per_trade_pct=1.5, max_loss_per_contract=500),
            dict(account_equity=100_000, risk_per_trade_pct=0.02, max_loss_per_contract=0),
        ],
    )
    def test_invalid_inputs_rejected(self, kwargs):
        with pytest.raises(ValueError):
            fixed_fractional_size(**kwargs)


class TestCapRequestedContracts:
    def test_request_within_limit_is_honored_exactly(self):
        result = cap_requested_contracts(
            requested_contracts=3, max_allowed_contracts=10, max_loss_per_contract=500
        )
        assert result.contracts == 3
        assert result.capped_by == "requested"

    def test_request_exceeding_limit_is_capped(self):
        result = cap_requested_contracts(
            requested_contracts=50, max_allowed_contracts=4, max_loss_per_contract=500
        )
        assert result.contracts == 4
        assert result.capped_by == "risk_budget"

    def test_an_llm_can_never_get_more_than_the_deterministic_maximum(self):
        # The core safety property this function exists for: no matter
        # how large the "requested" size, the output never exceeds the
        # independently computed maximum.
        for requested in (0, 1, 4, 5, 1000, 10**6):
            result = cap_requested_contracts(
                requested_contracts=requested, max_allowed_contracts=4, max_loss_per_contract=500
            )
            assert result.contracts <= 4

    def test_negative_requested_contracts_rejected(self):
        with pytest.raises(ValueError):
            cap_requested_contracts(requested_contracts=-1, max_allowed_contracts=4, max_loss_per_contract=500)

    def test_risk_allocated_scales_with_capped_contracts(self):
        result = cap_requested_contracts(
            requested_contracts=100, max_allowed_contracts=4, max_loss_per_contract=500
        )
        assert result.risk_allocated == pytest.approx(4 * 500)
