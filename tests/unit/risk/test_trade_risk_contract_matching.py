"""MD-003 regression: contract resolution must match on the underlying
ticker, not just (expiration, strike, right). Without this, a chain
that correctly labels its own `underlying.symbol` (so it passes the
Risk Engine's separate chain-level ticker check) but contains one
contract mislabeled with a *different* `underlying` string would
otherwise silently resolve to that wrong-ticker contract whenever its
(expiration, strike, right) happens to coincide -- a realistic
possibility for common round strikes and standard monthly expirations
across large-caps."""
from __future__ import annotations

import pytest

from src.data.option_chain import OptionChain
from src.risk.trade_risk import ContractNotFoundError, resolve_leg_contracts
from tests.unit.risk.conftest import EXPIRATION, MD_TS, NOW, make_contract, make_underlying, pcs_proposal


def _chain_with_contracts(contracts, symbol: str = "SPY") -> OptionChain:
    return OptionChain(underlying=make_underlying(symbol=symbol), contracts=contracts, timestamp=MD_TS, source="mock")


class TestResolveLegContractsChecksUnderlying:
    def test_correctly_labeled_contracts_resolve_normally(self):
        proposal = pcs_proposal()
        contracts = [
            make_contract(underlying="SPY", strike=620.0),
            make_contract(underlying="SPY", strike=615.0, option_symbol="SPY261016P00615000"),
        ]
        resolved = resolve_leg_contracts(proposal, _chain_with_contracts(contracts), as_of=NOW, max_age_minutes=15)
        assert len(resolved) == 2
        assert {c.underlying for c in resolved} == {"SPY"}

    def test_wrong_underlying_contract_at_a_coincidentally_matching_strike_is_never_matched(self):
        """The exact MD-003 exploit: a contract at the *proposal's own*
        (expiration, strike, right) shape, but stamped with a different
        ticker -- must be rejected as not found, never silently
        resolved as if it were the proposal's own contract."""
        proposal = pcs_proposal()
        contracts = [
            make_contract(underlying="QQQ", strike=620.0, option_symbol="QQQ261016P00620000"),  # wrong ticker, same strike/expiration/right
            make_contract(underlying="SPY", strike=615.0, option_symbol="SPY261016P00615000"),
        ]
        with pytest.raises(ContractNotFoundError):
            resolve_leg_contracts(proposal, _chain_with_contracts(contracts), as_of=NOW, max_age_minutes=15)

    def test_wrong_underlying_never_silently_substituted_even_when_the_correct_one_is_absent(self):
        proposal = pcs_proposal()
        contracts = [make_contract(underlying="QQQ", strike=620.0), make_contract(underlying="QQQ", strike=615.0)]
        with pytest.raises(ContractNotFoundError, match="underlying='SPY'"):
            resolve_leg_contracts(proposal, _chain_with_contracts(contracts), as_of=NOW, max_age_minutes=15)
