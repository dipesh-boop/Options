#!/usr/bin/env python3
"""Operator-run production smoke test for the Tradier market-data
provider (Step 22.4A Part 7).

**Market-data-only. GET-only. Never touches an account, order, or
trading endpoint of any kind.** This script imports exactly one class
from this codebase, `src.data.tradier_provider.TradierMarketDataProvider`
-- the same class `tests/acceptance/test_tradier_market_data_only.py`
and `src.validation.freeze`'s own `tradier_market_data_only` check
independently prove never defines an order-shaped method or references
an accounts/orders endpoint. This script calls only its read-only
`get_underlying_quote`/`get_expirations`/`get_option_chain_for_expiration`
methods -- nothing here can place, preview, cancel, or replace an order,
start the 90-day validation cohort, or write to any database or
persistence store. It is safe to run at any time, including against a
live Tradier account, with no side effect beyond the API calls
themselves and this script's own stdout.

**Never prints the bearer token.** The token is loaded exclusively
through `TradierConfig` (`OPTIONS_AGENT_TRADIER_TOKEN` / `.env`, the
same secure mechanism every other part of this codebase already uses)
and this script never logs the `TradierConfig` object itself or any
raw HTTP header -- only a masked "configured: yes/no" and the already-
redacted exception text `classify_tradier_error`/`TradierAuthenticationError`
themselves guarantee never contains the secret (see
`tests/acceptance/test_tradier_market_data_only.py::TestNoCredentialLeakage`
for the structural proof of that redaction).

Usage:
    python scripts/smoke_tradier_market_data.py [SYMBOL]

`SYMBOL` defaults to SPY. Exits 0 on PASS, 1 on FAIL -- suitable for a
CI/ops health-check step, never a substitute for `make verify-freeze` or
the acceptance test suite.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data.rate_limiter import RateLimitPriority  # noqa: E402
from src.data.tradier_provider import (  # noqa: E402
    TradierAuthenticationError,
    TradierConfig,
    TradierMarketDataProvider,
    TradierMalformedResponseError,
    TradierRateLimitError,
)
from src.data.provider import ProviderError  # noqa: E402


def _line(label: str, value: object) -> None:
    print(f"  {label}: {value}")


async def run_smoke_test(symbol: str) -> bool:
    print(f"Tradier market-data smoke test -- symbol={symbol!r}")

    config = TradierConfig()
    _line("token configured", "yes" if config.token else "no")
    _line("base_url", config.base_url)

    try:
        provider = TradierMarketDataProvider(config)
    except TradierAuthenticationError as exc:
        print(f"FAIL: could not construct provider -- {exc}")
        return False

    try:
        # ---------------------------------------------------- 1. quote
        print("\n[1/3] fetching one underlying quote...")
        quote = await provider.get_underlying_quote(symbol, priority=RateLimitPriority.P5_BACKGROUND_RESEARCH)
        _line("symbol", quote.symbol)
        _line("bid/ask/last", f"{quote.bid} / {quote.ask} / {quote.last}")
        _line("source", quote.source)
        _line("timestamp", quote.timestamp.isoformat())

        # ------------------------------------------------ 2. expirations
        print("\n[2/3] fetching expirations...")
        expirations = await provider.get_expirations(symbol, priority=RateLimitPriority.P5_BACKGROUND_RESEARCH)
        _line("expiration count", len(expirations))
        if not expirations:
            print(f"FAIL: no expirations returned for {symbol!r} -- cannot proceed to chain fetch")
            return False
        nearest = expirations[0]
        _line("nearest expiration", nearest.isoformat())

        # ---------------------------------------------- 3. option chain
        print("\n[3/3] fetching one option chain (nearest expiration only)...")
        contracts = await provider.get_option_chain_for_expiration(
            symbol, nearest, priority=RateLimitPriority.P5_BACKGROUND_RESEARCH
        )
        _line("contract count", len(contracts))
        _line("provider/source", "tradier (TradierMarketDataProvider)")

        rate_limit = provider.rate_limit_state
        print("\nrate-limit state (as last reported by Tradier's own response headers):")
        if rate_limit is None:
            _line("state", "not reported by this session's requests")
        else:
            _line("allowed/used/available", f"{rate_limit.allowed} / {rate_limit.used} / {rate_limit.available}")
            _line("utilization", f"{rate_limit.utilization_pct:.1%}")

    except TradierAuthenticationError as exc:
        print(f"\nFAIL: authentication error -- {exc}")
        return False
    except TradierRateLimitError as exc:
        print(f"\nFAIL: rate-limited -- {exc}")
        return False
    except TradierMalformedResponseError as exc:
        print(f"\nFAIL: malformed response -- {exc}")
        return False
    except ProviderError as exc:
        print(f"\nFAIL: provider error -- {exc}")
        return False
    finally:
        await provider.close()

    print("\nPASS: quote, expirations, and one option chain were all fetched successfully.")
    print("No order, preview, cancel, or account/trading endpoint was ever called.")
    print("No validation cohort was started. No database or store was written to.")
    return True


def main() -> int:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "SPY"
    passed = asyncio.run(run_smoke_test(symbol))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
