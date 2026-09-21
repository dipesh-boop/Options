from __future__ import annotations

from datetime import date, datetime, timezone

from src.data.option_chain import OptionContract, OptionRight as DataOptionRight
from src.risk.limits import get_default_limits
from src.risk.portfolio_risk import Portfolio, UnderlyingHolding

NOW = datetime(2026, 9, 22, 14, 30, tzinfo=timezone.utc)
EXPIRATION = date(2026, 10, 16)
SPOT = 100.0


def contract(strike: float, right: DataOptionRight, bid: float, ask: float, iv: float = 0.25) -> OptionContract:
    return OptionContract(
        underlying="XYZ", option_symbol=f"XYZ{strike}{right.value}", expiration=EXPIRATION, strike=strike, right=right,
        bid=bid, ask=ask, last=(bid + ask) / 2, volume=500, open_interest=1000, iv=iv, underlying_price=SPOT,
        timestamp=NOW, source="test",
    )


def limits():
    return get_default_limits()


def portfolio(**overrides) -> Portfolio:
    base = dict(as_of=NOW, nav=100_000.0, cash=90_000.0, peak_equity=100_000.0, sector_by_ticker={"XYZ": "Technology"})
    base.update(overrides)
    return Portfolio(**base)


def portfolio_with_shares(shares: int = 100, cost_basis: float = 95.0, **overrides) -> Portfolio:
    overrides.setdefault("underlying_holdings", {"XYZ": UnderlyingHolding(shares=shares, cost_basis=cost_basis)})
    return portfolio(**overrides)
