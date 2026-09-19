"""Builds an IV-surface-ready dataset (expiry x strike -> IV/greeks) from a chain."""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel

from app.analytics.greeks import Greeks, compute_contract_greeks
from app.data.models import OptionChain, Right


class SurfacePoint(BaseModel):
    expiry: date
    days_to_expiry: int
    strike: float
    right: Right
    moneyness: float  # strike / spot
    implied_vol: float
    delta: float
    gamma: float
    theta: float
    vega: float
    volume: int
    open_interest: int


def build_iv_surface(chain: OptionChain, risk_free_rate: float, as_of: date | None = None) -> list[SurfacePoint]:
    as_of = as_of or date.today()
    spot = chain.underlying.price
    points: list[SurfacePoint] = []

    for contract in chain.contracts:
        greeks: Greeks | None = compute_contract_greeks(contract, spot, risk_free_rate, as_of)
        if greeks is None:
            continue
        points.append(
            SurfacePoint(
                expiry=contract.expiry,
                days_to_expiry=(contract.expiry - as_of).days,
                strike=contract.strike,
                right=contract.right,
                moneyness=round(contract.strike / spot, 4),
                implied_vol=greeks.implied_vol,
                delta=greeks.delta,
                gamma=greeks.gamma,
                theta=greeks.theta,
                vega=greeks.vega,
                volume=contract.volume,
                open_interest=contract.open_interest,
            )
        )

    points.sort(key=lambda p: (p.expiry, p.right.value, p.strike))
    return points
