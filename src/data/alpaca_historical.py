"""Minimal Alpaca historical-bars adapter (Step 22.1).

Per the governing instruction's Part 11: audited first, not assumed.
Nothing in `src.backtest`/`src.validation` currently calls a live
`HistoricalDataProvider` instance — `src.backtest.benchmark
.spy_total_return` and `src.validation.benchmarks
.compare_against_benchmarks` both take `bars: list[HistoricalBar]` as a
plain caller-supplied parameter (only test fixtures populate it today;
see ARCHITECTURE.md §12, which flags a historical options-data vendor
as a still-open question). This module exists so a future caller *can*
supply real Alpaca-sourced bars through that same parameter -- it does
not redesign or wire into the existing (fixture-driven) benchmark/
regime infrastructure, per that Part's own "do not redesign unrelated
historical infrastructure unnecessarily" instruction.

Market-data only, same as `alpaca_provider.py`: only
`alpaca.data.historical.stock.StockHistoricalDataClient.get_stock_bars`
is called; `alpaca.trading` is never imported.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

from src.data.alpaca_provider import AlpacaConfig, classify_alpaca_error
from src.data.historical import HistoricalBar, HistoricalDataProvider
from src.data.provider import ProviderError


class AlpacaHistoricalDataProvider(HistoricalDataProvider):
    def __init__(self, config: AlpacaConfig | None = None, *, stock_client=None) -> None:
        self._config = (config or AlpacaConfig()).validate_feeds()
        if stock_client is None and (not self._config.api_key or not self._config.api_secret):
            raise ProviderError(
                "Alpaca historical data requires OPTIONS_AGENT_ALPACA_API_KEY and "
                "OPTIONS_AGENT_ALPACA_API_SECRET to be set (see .env.example)."
            )
        self._stock_client = stock_client or self._build_real_stock_client()

    def _build_real_stock_client(self):
        from alpaca.data.historical.stock import StockHistoricalDataClient

        return StockHistoricalDataClient(self._config.api_key, self._config.api_secret)

    async def get_bars(self, symbol: str, start: date, end: date) -> list[HistoricalBar]:
        try:
            return await asyncio.to_thread(self._get_bars_sync, symbol, start, end)
        except Exception as exc:  # noqa: BLE001
            raise classify_alpaca_error(exc) from exc

    def _get_bars_sync(self, symbol: str, start: date, end: date) -> list[HistoricalBar]:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        symbol = symbol.upper()
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc),
            end=datetime.combine(end, datetime.min.time(), tzinfo=timezone.utc),
            feed=DataFeed(self._config.stock_feed),
        )
        result = self._stock_client.get_stock_bars(request)
        raw_bars = result[symbol] if hasattr(result, "__getitem__") else result.data.get(symbol, [])

        bars: list[HistoricalBar] = []
        for raw in raw_bars:
            try:
                bars.append(
                    HistoricalBar(
                        symbol=symbol,
                        bar_date=raw.timestamp.date(),
                        open=float(raw.open),
                        high=float(raw.high),
                        low=float(raw.low),
                        close=float(raw.close),
                        volume=int(raw.volume or 0),
                        source=f"alpaca_{self._config.stock_feed}",
                    )
                )
            except ValueError:
                continue  # malformed bar (e.g. high < low) -- skip, never fabricate
        return bars
