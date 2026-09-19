"""Synthetic market data provider.

Generates an internally-consistent option chain (priced with Black-Scholes
plus a volatility smile, then perturbed with bid/ask spread and randomized
volume/OI) so the dashboard and analytics can be exercised end-to-end
without a live IBKR/TWS connection. Swap `data_provider=ibkr` in config to
use real data once TWS or IB Gateway is running.
"""
from __future__ import annotations

import hashlib
import math
import random
from datetime import date, timedelta

from app.analytics.greeks import bs_price
from app.data.models import OptionChain, OptionContract, Right, UnderlyingQuote
from app.data.provider import MarketDataProvider

# Deterministic per-symbol base price so repeated calls are stable within a
# process run but still differ sensibly across symbols.
_BASE_PRICES = {
    "AAPL": 225.0,
    "MSFT": 430.0,
    "SPY": 560.0,
    "NVDA": 120.0,
    "TSLA": 250.0,
}
_DEFAULT_BASE_PRICE = 100.0
_BASE_IV = {
    "AAPL": 0.27,
    "MSFT": 0.24,
    "SPY": 0.14,
    "NVDA": 0.45,
    "TSLA": 0.55,
}
_DEFAULT_IV = 0.30


def _seeded_random(symbol: str) -> random.Random:
    seed = int(hashlib.sha256(symbol.encode()).hexdigest(), 16) % (2**32)
    return random.Random(seed)


class MockMarketDataProvider(MarketDataProvider):
    def __init__(self, risk_free_rate: float = 0.05) -> None:
        self._risk_free_rate = risk_free_rate

    async def get_option_chain(self, symbol: str) -> OptionChain:
        symbol = symbol.upper()
        rng = _seeded_random(symbol)
        base_price = _BASE_PRICES.get(symbol, _DEFAULT_BASE_PRICE)
        # Small session-to-session drift so the UI doesn't look frozen.
        spot = round(base_price * (1 + rng.uniform(-0.015, 0.015)), 2)
        base_iv = _BASE_IV.get(symbol, _DEFAULT_IV)

        underlying = UnderlyingQuote(
            symbol=symbol,
            price=spot,
            historical_volatility_30d=round(base_iv * rng.uniform(0.85, 1.05), 4),
        )

        today = date.today()
        expiries = [today + timedelta(days=d) for d in (7, 14, 30, 45, 60)]
        contracts: list[OptionContract] = []

        for expiry in expiries:
            dte = (expiry - today).days
            t = dte / 365.0
            # Strikes in ~2.5%-of-spot increments, centered on spot.
            step = max(round(spot * 0.025, 0), 1.0)
            strikes = [round(spot + i * step, 2) for i in range(-8, 9)]

            for strike in strikes:
                moneyness = strike / spot
                # Simple volatility smile: higher IV away from the money,
                # plus a term-structure decay for longer expiries.
                smile = base_iv * (1 + 0.35 * (moneyness - 1) ** 2)
                term_adj = 1 - 0.05 * min(dte, 60) / 60
                contract_iv = max(smile * term_adj, 0.05)

                for right in (Right.CALL, Right.PUT):
                    theo = bs_price(spot, strike, max(t, 1 / 365), self._risk_free_rate, contract_iv, right)
                    theo = max(theo, 0.01)
                    spread = max(theo * 0.04, 0.02)
                    bid = round(max(theo - spread / 2, 0.0), 2)
                    ask = round(theo + spread / 2, 2)
                    last = round((bid + ask) / 2 + rng.uniform(-spread / 4, spread / 4), 2)

                    # Volume/OI: peak near the money, decay away from it;
                    # occasional spikes simulate "unusual activity".
                    atm_closeness = math.exp(-4 * (moneyness - 1) ** 2)
                    base_oi = int(rng.uniform(50, 3000) * atm_closeness + 10)
                    base_volume = int(base_oi * rng.uniform(0.05, 0.6))
                    if rng.random() < 0.06:  # ~6% of contracts show a volume spike
                        base_volume = int(base_volume * rng.uniform(4, 12) + 500)

                    contracts.append(
                        OptionContract(
                            symbol=symbol,
                            expiry=expiry,
                            strike=strike,
                            right=right,
                            bid=bid,
                            ask=ask,
                            last=max(last, 0.0),
                            volume=base_volume,
                            open_interest=base_oi,
                        )
                    )

        return OptionChain(underlying=underlying, contracts=contracts)
