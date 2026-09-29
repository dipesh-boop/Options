"""PAPER_TRADING_V1.5.3, Step 3: tests for src.portfolio.risk_data --
the sector/correlation wiring layer. Never touches the real operational
database; every store this file needs is either absent entirely (this
module makes no store calls of its own) or an in-memory/temp-file
fixture the caller supplies."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from src.data.historical import HistoricalBar
from src.portfolio.risk_data import (
    CorrelationDataUnavailableError,
    _date_price_map_from_bars,
    apply_correlation_wiring,
    apply_risk_data_wiring,
    apply_sector_wiring,
    classify_universe_sectors,
    resolve_price_history_for_correlation,
    resolve_sector_by_ticker,
)
from src.risk.portfolio_risk import Portfolio
from src.workflows.candidate_generation import UniverseEntry

NOW = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)
UNIVERSE = (
    UniverseEntry(ticker="SPY", sector="ETF"),
    UniverseEntry(ticker="QQQ", sector="ETF"),
    UniverseEntry(ticker="ACME", sector="Technology"),
)


def _portfolio(**overrides) -> Portfolio:
    base = dict(as_of=NOW, nav=100_000.0, cash=100_000.0, peak_equity=100_000.0)
    base.update(overrides)
    return Portfolio(**base)


class _FakeHistoricalProvider:
    """A minimal `HistoricalDataProvider` stand-in -- never a live
    network call, matching every other fake-provider test double in
    this codebase (e.g. tests/acceptance/conftest.py's chain fixtures)."""

    def __init__(self, bars_by_symbol: dict[str, list[HistoricalBar]], *, raise_for: frozenset[str] = frozenset()):
        self._bars_by_symbol = bars_by_symbol
        self._raise_for = raise_for

    async def get_bars(self, symbol: str, start: date, end: date) -> list[HistoricalBar]:
        if symbol in self._raise_for:
            raise RuntimeError(f"simulated provider failure for {symbol}")
        return self._bars_by_symbol.get(symbol, [])


def _bars(symbol: str, *, n: int, end: date, start_close: float = 100.0, step: float = 0.1) -> list[HistoricalBar]:
    return [
        HistoricalBar(
            symbol=symbol, bar_date=end - timedelta(days=n - 1 - i),
            open=start_close + i * step, high=start_close + i * step + 0.5,
            low=start_close + i * step - 0.5, close=start_close + i * step,
            volume=1000, source="test",
        )
        for i in range(n)
    ]


# ------------------------------------------------------------- sector


class TestClassifyUniverseSectors:
    def test_etf_convention_is_deterministic(self):
        classifications = classify_universe_sectors(UNIVERSE)
        assert classifications["SPY"].is_etf is True
        assert classifications["SPY"].sector == "ETF"
        assert classifications["QQQ"].is_etf is True

    def test_single_name_sector_is_deterministic_and_not_treated_as_etf(self):
        classifications = classify_universe_sectors(UNIVERSE)
        assert classifications["ACME"].is_etf is False
        assert classifications["ACME"].sector == "Technology"

    def test_every_classification_carries_provenance(self):
        classifications = classify_universe_sectors(UNIVERSE)
        for c in classifications.values():
            assert c.source == "config/universe.yaml"


class TestResolveSectorByTicker:
    def test_classifiable_tickers_are_resolved(self):
        sector_by_ticker, classifications, unclassified = resolve_sector_by_ticker(
            frozenset({"SPY", "ACME"}), UNIVERSE
        )
        assert sector_by_ticker == {"SPY": "ETF", "ACME": "Technology"}
        assert set(classifications) == {"SPY", "ACME"}
        assert unclassified == frozenset()

    def test_unlisted_ticker_is_reported_unclassified_never_fabricated(self):
        sector_by_ticker, _classifications, unclassified = resolve_sector_by_ticker(frozenset({"NOPE"}), UNIVERSE)
        assert "NOPE" not in sector_by_ticker
        assert unclassified == frozenset({"NOPE"})


class TestApplySectorWiring:
    def test_populates_sector_by_ticker_from_universe(self):
        portfolio = _portfolio()
        wired = apply_sector_wiring(portfolio, UNIVERSE)
        assert wired.sector_by_ticker["SPY"] == "ETF"
        assert wired.sector_by_ticker["QQQ"] == "ETF"

    def test_never_clobbers_a_preexisting_entry_not_covered_by_the_universe(self):
        portfolio = _portfolio(sector_by_ticker={"LEGACY": "SomeOtherSector"})
        wired = apply_sector_wiring(portfolio, UNIVERSE)
        assert wired.sector_by_ticker["LEGACY"] == "SomeOtherSector"
        assert wired.sector_by_ticker["SPY"] == "ETF"

    def test_returns_a_new_object_original_untouched(self):
        portfolio = _portfolio()
        wired = apply_sector_wiring(portfolio, UNIVERSE)
        assert portfolio.sector_by_ticker == {}
        assert wired is not portfolio


# -------------------------------------------------------- correlation


class TestResolvePriceHistoryForCorrelation:
    @pytest.mark.asyncio
    async def test_no_provider_raises_correlation_data_unavailable(self):
        with pytest.raises(CorrelationDataUnavailableError):
            await resolve_price_history_for_correlation(
                frozenset({"SPY", "QQQ"}), historical_provider=None, now=NOW,
                lookback_days=60, min_observations=20,
            )

    @pytest.mark.asyncio
    async def test_sufficient_aligned_bars_are_returned(self):
        end = (NOW.date() - timedelta(days=1))
        provider = _FakeHistoricalProvider({
            "SPY": _bars("SPY", n=25, end=end, start_close=500.0),
            "QQQ": _bars("QQQ", n=25, end=end, start_close=400.0),
        })
        result = await resolve_price_history_for_correlation(
            frozenset({"SPY", "QQQ"}), historical_provider=provider, now=NOW,
            lookback_days=60, min_observations=20,
        )
        assert set(result) == {"SPY", "QQQ"}
        assert len(result["SPY"]) == len(result["QQQ"]) == 25

    @pytest.mark.asyncio
    async def test_insufficient_observations_are_omitted_not_fabricated(self):
        end = NOW.date() - timedelta(days=1)
        provider = _FakeHistoricalProvider({
            "SPY": _bars("SPY", n=25, end=end),
            "QQQ": _bars("QQQ", n=5, end=end),  # below min_observations
        })
        result = await resolve_price_history_for_correlation(
            frozenset({"SPY", "QQQ"}), historical_provider=provider, now=NOW,
            lookback_days=60, min_observations=20,
        )
        # only one ticker survives -- correlation needs at least two, so
        # nothing is returned at all rather than a lone useless series.
        assert result == {}

    @pytest.mark.asyncio
    async def test_future_dated_bar_taints_the_whole_series_for_that_ticker(self):
        end = NOW.date() - timedelta(days=1)
        good = _bars("SPY", n=25, end=end)
        tainted = _bars("QQQ", n=25, end=end)
        tainted[-1] = HistoricalBar(
            symbol="QQQ", bar_date=NOW.date() + timedelta(days=1),  # a genuine lookahead violation
            open=400, high=401, low=399, close=400.5, volume=1000, source="test",
        )
        provider = _FakeHistoricalProvider({"SPY": good, "QQQ": tainted})
        result = await resolve_price_history_for_correlation(
            frozenset({"SPY", "QQQ"}), historical_provider=provider, now=NOW,
            lookback_days=60, min_observations=20,
        )
        assert result == {}  # QQQ dropped entirely -> fewer than 2 usable series

    @pytest.mark.asyncio
    async def test_one_tickers_provider_failure_is_isolated(self):
        end = NOW.date() - timedelta(days=1)
        provider = _FakeHistoricalProvider(
            {"SPY": _bars("SPY", n=25, end=end), "QQQ": _bars("QQQ", n=25, end=end), "ACME": _bars("ACME", n=25, end=end)},
            raise_for=frozenset({"ACME"}),
        )
        result = await resolve_price_history_for_correlation(
            frozenset({"SPY", "QQQ", "ACME"}), historical_provider=provider, now=NOW,
            lookback_days=60, min_observations=20,
        )
        assert "ACME" not in result
        assert set(result) == {"SPY", "QQQ"}

    @pytest.mark.asyncio
    async def test_series_are_aligned_to_the_shortest_length(self):
        end = NOW.date() - timedelta(days=1)
        provider = _FakeHistoricalProvider({
            "SPY": _bars("SPY", n=30, end=end),
            "QQQ": _bars("QQQ", n=22, end=end),
        })
        result = await resolve_price_history_for_correlation(
            frozenset({"SPY", "QQQ"}), historical_provider=provider, now=NOW,
            lookback_days=60, min_observations=20,
        )
        assert len(result["SPY"]) == len(result["QQQ"]) == 22


class TestDatePriceMapFromBars:
    """Step 3B (PAPER_TRADING_V1.5.4): the date-carrying replacement for
    the old `_prices_from_bars` (which discarded dates entirely and
    returned a plain positionally-ordered `list[float]`)."""

    def test_maps_each_bar_date_to_its_close(self):
        bars = [
            HistoricalBar(symbol="SPY", bar_date=date(2026, 1, 2), open=100.0, high=100.0, low=100.0, close=100.0, volume=1, source="test"),
            HistoricalBar(symbol="SPY", bar_date=date(2026, 1, 3), open=101.0, high=101.0, low=101.0, close=101.0, volume=1, source="test"),
        ]
        result = _date_price_map_from_bars(bars)
        assert result == {date(2026, 1, 2): 100.0, date(2026, 1, 3): 101.0}

    def test_out_of_order_input_is_handled_correctly(self):
        bars = [
            HistoricalBar(symbol="SPY", bar_date=date(2026, 1, 3), open=101.0, high=101.0, low=101.0, close=101.0, volume=1, source="test"),
            HistoricalBar(symbol="SPY", bar_date=date(2026, 1, 2), open=100.0, high=100.0, low=100.0, close=100.0, volume=1, source="test"),
        ]
        result = _date_price_map_from_bars(bars)
        assert result == {date(2026, 1, 2): 100.0, date(2026, 1, 3): 101.0}

    def test_duplicate_date_within_one_series_keeps_first_seen_never_merges(self):
        bars = [
            HistoricalBar(symbol="SPY", bar_date=date(2026, 1, 2), open=100.0, high=100.0, low=100.0, close=100.0, volume=1, source="test"),
            HistoricalBar(symbol="SPY", bar_date=date(2026, 1, 2), open=999.0, high=999.0, low=999.0, close=999.0, volume=1, source="test"),
        ]
        result = _date_price_map_from_bars(bars)
        assert result == {date(2026, 1, 2): 100.0}


class TestResolvePriceHistoryDateIntersectionAlignment:
    """Step 3B's critical correction: alignment must be by DATE
    INTERSECTION, never by positional/length-based trimming. These
    scenarios are specifically constructed so the two approaches give
    DIFFERENT answers -- proving the fix, not just re-confirming the
    already-passing contiguous-calendar case."""

    @pytest.mark.asyncio
    async def test_a_mid_series_gap_is_correctly_excluded_not_positionally_misaligned(self):
        """QQQ is missing 2026-01-10 (a single mid-range gap) while SPY
        has a full run. The OLD positional-trim code would have kept
        both series' most-recent N=len(shortest) prices, silently
        pairing SPY's price from one calendar date against QQQ's price
        from a DIFFERENT calendar date once the gap shifted everything
        after it by one slot. The fix must use only genuinely shared
        dates, and never include 2026-01-10 in either series."""
        end = date(2026, 1, 20)
        spy_bars = [
            HistoricalBar(
                symbol="SPY", bar_date=date(2026, 1, 2) + timedelta(days=i),
                open=100.0 + i, high=101.0 + i, low=99.0 + i, close=100.0 + i, volume=1000, source="test",
            )
            for i in range(19)  # 2026-01-02 .. 2026-01-20, every calendar day
        ]
        qqq_bars = [
            HistoricalBar(
                symbol="QQQ", bar_date=date(2026, 1, 2) + timedelta(days=i),
                open=200.0 + i, high=201.0 + i, low=199.0 + i, close=200.0 + i, volume=1000, source="test",
            )
            for i in range(19)
            if date(2026, 1, 2) + timedelta(days=i) != date(2026, 1, 10)  # one mid-range gap
        ]
        provider = _FakeHistoricalProvider({"SPY": spy_bars, "QQQ": qqq_bars})
        now = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        result = await resolve_price_history_for_correlation(
            frozenset({"SPY", "QQQ"}), historical_provider=provider, now=now,
            lookback_days=60, min_observations=5,
        )
        spy_map = _date_price_map_from_bars(spy_bars)
        # The gap date's SPY price must never appear anywhere in the result.
        gap_price = spy_map[date(2026, 1, 10)]
        assert gap_price not in result["SPY"]
        # Every surviving pair must correspond to an actually-shared date.
        qqq_map = _date_price_map_from_bars(qqq_bars)
        common = sorted(set(spy_map) & set(qqq_map))
        assert result["SPY"] == [spy_map[d] for d in common]
        assert result["QQQ"] == [qqq_map[d] for d in common]
        assert len(result["SPY"]) == 18  # 19 calendar days minus the one gap

    @pytest.mark.asyncio
    async def test_fails_closed_when_fewer_than_min_observations_dates_intersect(self):
        """Two tickers each individually have >= min_observations bars,
        but their calendars barely overlap -- the intersection itself
        must be checked against min_observations, not just each raw
        series' own length."""
        provider = _FakeHistoricalProvider({
            "SPY": [
                HistoricalBar(symbol="SPY", bar_date=date(2026, 1, 1) + timedelta(days=i), open=float(i + 1), high=float(i + 1), low=float(i + 1), close=float(i + 1), volume=1, source="test")
                for i in range(25)
            ],
            "QQQ": [
                HistoricalBar(symbol="QQQ", bar_date=date(2026, 1, 20) + timedelta(days=i), open=float(i + 1), high=float(i + 1), low=float(i + 1), close=float(i + 1), volume=1, source="test")
                for i in range(25)  # overlaps SPY on only 2026-01-20..24 (5 dates)
            ],
        })
        now = datetime(2026, 3, 1, tzinfo=timezone.utc)
        result = await resolve_price_history_for_correlation(
            frozenset({"SPY", "QQQ"}), historical_provider=provider, now=now,
            lookback_days=90, min_observations=20,
        )
        assert result == {}  # only 5 shared dates -- below min_observations=20, fails closed

    @pytest.mark.asyncio
    async def test_no_padding_or_fill_ever_introduced(self):
        """A ticker's gap date must never appear in the OTHER ticker's
        aligned series either, with any substituted/padded/filled value
        -- the aligned series length must exactly equal the intersection
        size, never the max or min raw length."""
        end = date(2026, 2, 28)
        full_dates = [date(2026, 2, 1) + timedelta(days=i) for i in range(28)]
        gap_dates = [d for d in full_dates if d != date(2026, 2, 15)]
        provider = _FakeHistoricalProvider({
            "SPY": [HistoricalBar(symbol="SPY", bar_date=d, open=1.0, high=1.0, low=1.0, close=1.0, volume=1, source="test") for d in full_dates],
            "QQQ": [HistoricalBar(symbol="QQQ", bar_date=d, open=2.0, high=2.0, low=2.0, close=2.0, volume=1, source="test") for d in gap_dates],
        })
        now = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        result = await resolve_price_history_for_correlation(
            frozenset({"SPY", "QQQ"}), historical_provider=provider, now=now,
            lookback_days=60, min_observations=5,
        )
        assert len(result["SPY"]) == len(gap_dates) == 27
        assert len(result["QQQ"]) == 27


class TestApplyCorrelationWiring:
    @pytest.mark.asyncio
    async def test_no_provider_leaves_portfolio_unchanged_never_raises(self):
        """Item 14: a provider failure (here, total absence) must never
        propagate as an exception that could crash the daily cycle --
        `apply_correlation_wiring` is best-effort; the fail-closed
        decision belongs to check_correlation, not this function."""
        portfolio = _portfolio()
        wired = await apply_correlation_wiring(
            portfolio, tickers=frozenset({"SPY", "QQQ"}), historical_provider=None, now=NOW,
            lookback_days=60, min_observations=20,
        )
        assert wired.price_history == {}
        assert wired is portfolio

    @pytest.mark.asyncio
    async def test_populates_price_history_when_data_is_available(self):
        end = NOW.date() - timedelta(days=1)
        provider = _FakeHistoricalProvider({"SPY": _bars("SPY", n=25, end=end), "QQQ": _bars("QQQ", n=25, end=end)})
        portfolio = _portfolio()
        wired = await apply_correlation_wiring(
            portfolio, tickers=frozenset({"SPY", "QQQ"}), historical_provider=provider, now=NOW,
            lookback_days=60, min_observations=20,
        )
        assert set(wired.price_history) == {"SPY", "QQQ"}


class TestApplyRiskDataWiring:
    @pytest.mark.asyncio
    async def test_disabled_is_a_complete_no_op(self):
        """Installation vs activation: the active cohort's own config
        defaults this to disabled, and this must leave the portfolio
        byte-for-byte identical -- not just numerically equivalent."""
        portfolio = _portfolio()
        wired = await apply_risk_data_wiring(
            portfolio, universe=UNIVERSE, enabled=False, historical_provider=None, now=NOW,
            lookback_days=60, min_observations=20,
        )
        assert wired is portfolio
        assert wired.risk_data_required is False
        assert wired.sector_by_ticker == {}

    @pytest.mark.asyncio
    async def test_enabled_populates_sectors_and_sets_risk_data_required(self):
        portfolio = _portfolio()
        wired = await apply_risk_data_wiring(
            portfolio, universe=UNIVERSE, enabled=True, historical_provider=None, now=NOW,
            lookback_days=60, min_observations=20,
        )
        assert wired.risk_data_required is True
        assert wired.sector_by_ticker["SPY"] == "ETF"

    @pytest.mark.asyncio
    async def test_enabled_with_no_positions_and_no_provider_is_still_safe(self):
        """Item 11: the empty-portfolio case -- wiring can be installed
        and active with zero existing positions and no historical
        provider at all, and this must not raise."""
        portfolio = _portfolio()
        wired = await apply_risk_data_wiring(
            portfolio, universe=UNIVERSE, enabled=True, historical_provider=None, now=NOW,
            lookback_days=60, min_observations=20,
        )
        assert wired.positions == []
        assert wired.price_history == {}


class TestBackwardCompatibleDeserialization:
    """Item 16: old persisted Portfolio records (from before
    `risk_data_required` existed) remain readable -- the field is
    additive with a safe default, never a required migration."""

    def test_old_shaped_portfolio_json_without_risk_data_required_loads_with_default_false(self):
        old_shape_json = {
            "as_of": NOW.isoformat(),
            "nav": 100_000.0,
            "cash": 100_000.0,
            "peak_equity": 100_000.0,
            "positions": [],
            "underlying_holdings": {},
            "sector_by_ticker": {},
            "price_history": {},
            "halted": False,
            # no "risk_data_required" key at all -- exactly the shape
            # every Portfolio persisted before this step has.
        }
        portfolio = Portfolio.model_validate(old_shape_json)
        assert portfolio.risk_data_required is False
