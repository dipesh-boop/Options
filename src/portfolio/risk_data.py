"""PAPER_TRADING_V1.5.3, Step 3 (historical-data sourcing extended in
Step 3B, PAPER_TRADING_V1.5.4): wires deterministic sector classification
and (when a trustworthy source is available) historical price data into
the canonical `Portfolio` object *before* it reaches the Risk Engine, so
`src.risk.concentration.check_sector_concentration`/`src.risk.correlation
.check_correlation` see real data instead of the pre-V1.5.3 gap: an
unclassified ticker silently bucketed as `"UNKNOWN"` sector, and a missing
`price_history` entry silently skipping the correlation check.

**Root cause this module fixes.** `Portfolio.sector_by_ticker`/
`price_history` are, and have always been, plain `dict`-typed fields on
`Portfolio` (`src.risk.portfolio_risk`) -- the Risk Engine's own
concentration/correlation checks already read them. The gap was never in
the checks themselves; it was that nothing in the production validation
path (`scripts/run_validation_cycle.py`/`scripts/confirm_candidate.py`)
ever populated them before constructing or loading a `Portfolio` -- both
scripts call `Portfolio(...)` with no `sector_by_ticker`/`price_history`
argument at all, so both fields sat at their empty-dict default for the
life of the account. This module is the missing wiring, not a redesign
of the checks it feeds.

**Wire existing components, not a parallel implementation.** Sector
classification is sourced ENTIRELY from `config/universe.yaml` (via
`src.data.universe.load_universe`/`UniverseEntry.sector`) -- the exact
same, already-operator-maintained, already-deterministic field
`src.workflows.candidate_generation.generate_candidates` already reads
for a brand-new candidate's own `Candidate.sector`. No LLM, no guessing,
no new provider: SPY/QQQ are already declared `sector: ETF` there today.
Historical price data for correlation is sourced from an injected
`src.data.historical.HistoricalDataProvider` (the existing abstract
interface for underlying OHLCV bars). As of Step 3B, production callers
(`scripts/run_validation_cycle.py`/`scripts/confirm_candidate.py`) pass
a real `src.data.tradier_provider.TradierMarketDataProvider` instance --
the same already-approved, already-credentialed production Tradier
connection every other market-data call in this codebase already uses,
now also satisfying `HistoricalDataProvider` via its `get_bars` method.
`resolve_price_history_for_correlation` still accepts (and still fails
closed on) `historical_provider=None`, since that remains the correct
contract for any caller -- a test, a future provider swap -- that
genuinely has no provider to offer.

**Deterministic ETF policy.** A ticker whose `config/universe.yaml`
`sector` value is exactly `"ETF"` (case-insensitive) is treated as a
diversified-ETF sector bucket named `"ETF"`, never mapped onto a
single-name GICS sector -- this is the same convention the operator
already used for SPY/QQQ before this step existed; this module makes it
explicit and load-bearing rather than merely descriptive.

**Fail-closed missing-data policy.** This module never fabricates a
sector or a price series. A ticker this module cannot classify, or
cannot supply sufficient/trustworthy history for, is simply left out of
the dict it returns -- `src.risk.engine`/`src.risk.correlation` are what
turn "missing" into a REJECT, and only when `Portfolio.risk_data_required`
is `True` (see that field's own docstring for the installation/activation
split that keeps the currently active validation cohort's behavior
unchanged unless its own operator config explicitly turns this on).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from src.data.historical import HistoricalBar, HistoricalDataProvider, assert_no_lookahead
from src.risk.portfolio_risk import Portfolio
from src.workflows.candidate_generation import UniverseEntry

_ETF_SECTOR_LABEL = "ETF"


class RiskDataWiringError(RuntimeError):
    """Base class for this module's own errors -- never raised across an
    API boundary that expects a `RiskDecisionResult`; callers in
    `src.risk` catch these (or let them signal "no data available") and
    convert them to a proper fail-closed `REJECT`, exactly like every
    other typed exception this codebase's Risk-adjacent modules raise."""


class CorrelationDataUnavailableError(RiskDataWiringError):
    """Raised by `resolve_price_history_for_correlation` only when the
    caller passes no `historical_provider` at all -- see that function's
    docstring. Every OTHER failure mode (insufficient observations,
    malformed bars, a lookahead violation, one ticker's fetch failing)
    is handled by simply omitting that ticker from the returned dict,
    never by raising -- the fail-closed decision belongs to
    `src.risk.correlation.check_correlation`, which can see the full
    portfolio context (whether correlation evaluation is even required
    this time) that this module deliberately does not have."""


@dataclass(frozen=True)
class SectorClassification:
    """One ticker's sector classification plus enough provenance to
    answer "was this real, and where did it come from" -- never just a
    bare string, per the task's own observability requirement."""

    ticker: str
    sector: str
    is_etf: bool
    source: str  # e.g. "config/universe.yaml"


def classify_universe_sectors(universe: tuple[UniverseEntry, ...]) -> dict[str, SectorClassification]:
    """Deterministic, no-LLM sector classification for every ticker
    `config/universe.yaml` lists. `is_etf` is derived from the existing
    `sector: ETF` convention operators already use for a diversified
    ETF (SPY/QQQ today) -- never guessed, never inferred from the
    ticker symbol itself."""
    result: dict[str, SectorClassification] = {}
    for entry in universe:
        sector = entry.sector.strip()
        is_etf = sector.upper() == _ETF_SECTOR_LABEL
        result[entry.ticker] = SectorClassification(
            ticker=entry.ticker,
            sector=_ETF_SECTOR_LABEL if is_etf else sector,
            is_etf=is_etf,
            source="config/universe.yaml",
        )
    return result


def resolve_sector_by_ticker(
    tickers: frozenset[str], universe: tuple[UniverseEntry, ...]
) -> tuple[dict[str, str], dict[str, SectorClassification], frozenset[str]]:
    """Resolves every ticker in `tickers` against `config/universe.yaml`'s
    classifications. Returns `(sector_by_ticker, classifications,
    unclassified)`: `sector_by_ticker` is exactly the shape
    `Portfolio.sector_by_ticker` expects (only classifiable tickers
    present -- an unclassified ticker is never given a fabricated
    entry); `classifications` carries full provenance for every ticker
    that WAS classified (observability); `unclassified` is every
    requested ticker this module could not classify at all, for the
    caller to decide what "cannot be established reliably" means for
    its own candidate (fail closed, per this module's docstring)."""
    universe_classifications = classify_universe_sectors(universe)
    sector_by_ticker: dict[str, str] = {}
    classifications: dict[str, SectorClassification] = {}
    unclassified: set[str] = set()
    for ticker in tickers:
        classification = universe_classifications.get(ticker)
        if classification is None:
            unclassified.add(ticker)
            continue
        sector_by_ticker[ticker] = classification.sector
        classifications[ticker] = classification
    return sector_by_ticker, classifications, frozenset(unclassified)


def apply_sector_wiring(portfolio: Portfolio, universe: tuple[UniverseEntry, ...]) -> Portfolio:
    """Returns a NEW `Portfolio` (frozen model, never mutated in place)
    whose `sector_by_ticker` covers every ticker `config/universe.yaml`
    can classify, merged with whatever `sector_by_ticker` entries the
    portfolio already carried (a future ticker resolved some other way
    is never clobbered). Deterministic, local, no I/O, no provider
    call -- safe to call on every cycle regardless of market hours or
    provider health."""
    tickers = frozenset({p.ticker for p in portfolio.positions}) | frozenset(e.ticker for e in universe)
    resolved, _classifications, _unclassified = resolve_sector_by_ticker(tickers, universe)
    merged = {**portfolio.sector_by_ticker, **resolved}
    return portfolio.model_copy(update={"sector_by_ticker": merged})


# ---------------------------------------------------------- correlation


def _date_price_map_from_bars(bars: list[HistoricalBar]) -> dict[date, float]:
    """Builds a `{bar_date: close}` map for one ticker's fetched bars --
    the carrier for true date-based alignment (see
    `resolve_price_history_for_correlation`'s docstring for why this
    replaced a plain `list[float]`). A `bar_date` that repeats within
    this same series is defensive-programmed against here too (not just
    at the provider layer -- this function must be correct for ANY
    `HistoricalDataProvider`, not only the one concrete Tradier
    implementation that already de-duplicates its own responses): the
    first-seen close for that date is kept, the repeat is dropped,
    never averaged or overwritten -- ambiguous data is data this module
    declines to use, not data it guesses about."""
    result: dict[date, float] = {}
    for bar in sorted(bars, key=lambda b: b.bar_date):
        if bar.bar_date in result:
            continue
        result[bar.bar_date] = bar.close
    return result


async def resolve_price_history_for_correlation(
    tickers: frozenset[str],
    *,
    historical_provider: HistoricalDataProvider | None,
    now: datetime,
    lookback_days: int,
    min_observations: int,
) -> dict[str, list[float]]:
    """Best-effort population of `Portfolio.price_history` for
    `tickers`, sourced from `historical_provider.get_bars` -- the
    existing `src.data.historical.HistoricalDataProvider` abstraction,
    never a second, parallel historical-data fetch mechanism.

    **Why `historical_provider` could be `None`.** No concrete
    `HistoricalDataProvider` was wired into the official validation path
    as of PAPER_TRADING_V1.5.3 -- `src.data.tradier_provider
    .TradierMarketDataProvider` (the ONLY market-data provider official
    validation may use) implemented no historical-bars endpoint at that
    time; see STEP_23_3_FREEZE_REPORT.md's "External Dependency
    Discovered" section. `TradierMarketDataProvider.get_bars` now exists
    (PAPER_TRADING_V1.5.4, Step 3B, an explicitly-approved follow-up) and
    every production call site passes it, but this function still
    accepts `None` and still fails closed the identical way for any
    caller (a test, a future provider swap) that genuinely has none to
    offer -- `CorrelationDataUnavailableError` is the CORRECT fail-closed
    signal for `src.risk.correlation.check_correlation` to act on
    whenever correlation evaluation is actually required (i.e. the
    portfolio already holds a position), and a complete no-op for an
    empty portfolio, which never calls this function at all (see
    `src.risk.correlation.check_correlation`'s own "nothing to
    correlate against" early return).

    **Date-INTERSECTION alignment (Step 3B correction).** Prior to this
    step, alignment was positional: the most recent `N` closes were kept
    per ticker, where `N` was simply the shortest series' LENGTH. That is
    wrong whenever two tickers' fetched bars don't share the same
    trading-calendar coverage for reasons OTHER than "one has fewer
    total observations" -- e.g. one ticker's provider response is
    missing a single mid-range day a listing/halt/data-vendor gap
    dropped, while the other's is complete: positional trimming would
    silently pair each ticker's Nth-from-the-end price against a
    DIFFERENT actual calendar date for the other ticker, correlating two
    misaligned series without any way to detect it. This function now
    computes the actual SET INTERSECTION of `bar_date`s present in every
    ticker's (post-no-lookahead, post-min-observations) series, uses
    ONLY those shared dates -- in ascending date order, identical order
    for every ticker -- and never pads, forward-fills, back-fills, or
    substitutes a value for a date one ticker lacks. Fewer than
    `min_observations` dates surviving the intersection fails closed
    (returns `{}`) exactly like too few raw observations does.

    Fetches `lookback_days` of daily bars per ticker ending strictly
    before `now.date()` (never including `now`'s own not-yet-closed
    session), rejects a bar dated after `now.date()` via
    `assert_no_lookahead` (point-in-time discipline, ARCHITECTURE.md
    §9/§11 -- ANY future-dated bar taints the WHOLE series for that
    ticker, never silently dropped only for that one bar), and omits
    (never fabricates) a ticker whose fetch failed, returned no bars, or
    whose own series (before intersection) has fewer than
    `min_observations` points. A ticker omitted here, or a date absent
    from the final intersection, is exactly the "insufficient/
    unavailable" signal `check_correlation` fails closed on when
    required."""
    if historical_provider is None:
        raise CorrelationDataUnavailableError(
            "no historical-data provider was supplied for correlation evaluation -- missing price "
            "history is never treated as zero correlation"
        )

    as_of_date = now.date()
    start = as_of_date - timedelta(days=lookback_days)
    per_ticker_dates: dict[str, dict[date, float]] = {}
    for ticker in sorted(tickers):
        try:
            bars = await historical_provider.get_bars(ticker, start, as_of_date - timedelta(days=1))
        except Exception:  # noqa: BLE001 -- one ticker's fetch failure is isolated, never aborts the others
            continue
        if not bars:
            continue
        try:
            assert_no_lookahead(bars, as_of_date - timedelta(days=1))
        except ValueError:
            continue  # a future-dated bar taints the whole series for this ticker -- omitted, never trimmed
        date_prices = _date_price_map_from_bars(bars)
        if len(date_prices) < min_observations:
            continue
        per_ticker_dates[ticker] = date_prices

    if len(per_ticker_dates) < 2:
        return {}

    common_dates = set.intersection(*(set(dp.keys()) for dp in per_ticker_dates.values()))
    if len(common_dates) < min_observations:
        return {}

    ordered_dates = sorted(common_dates)
    return {
        ticker: [date_prices[d] for d in ordered_dates]
        for ticker, date_prices in per_ticker_dates.items()
    }


async def apply_correlation_wiring(
    portfolio: Portfolio,
    *,
    tickers: frozenset[str],
    historical_provider: HistoricalDataProvider | None,
    now: datetime,
    lookback_days: int,
    min_observations: int,
) -> Portfolio:
    """Returns a NEW `Portfolio` with `price_history` populated for
    whatever subset of `tickers` `resolve_price_history_for_correlation`
    could supply trustworthy data for -- silently unchanged (never
    raising) when no provider is configured, matching this function's
    role as best-effort population; `src.risk.correlation
    .check_correlation` is what turns the resulting gap into a REJECT
    when `Portfolio.risk_data_required` is set and correlation
    evaluation is actually required."""
    try:
        resolved = await resolve_price_history_for_correlation(
            tickers, historical_provider=historical_provider, now=now,
            lookback_days=lookback_days, min_observations=min_observations,
        )
    except CorrelationDataUnavailableError:
        return portfolio
    if not resolved:
        return portfolio
    merged = {**portfolio.price_history, **resolved}
    return portfolio.model_copy(update={"price_history": merged})


async def apply_risk_data_wiring(
    portfolio: Portfolio,
    *,
    universe: tuple[UniverseEntry, ...],
    enabled: bool,
    historical_provider: HistoricalDataProvider | None,
    now: datetime,
    lookback_days: int,
    min_observations: int,
) -> Portfolio:
    """The single entry point `scripts/run_validation_cycle.py`/
    `scripts/confirm_candidate.py` call right after loading `Portfolio`
    and before it reaches candidate generation/the Risk Engine.

    **Installation vs. activation** (this step's own explicit
    requirement): when `enabled` is `False` -- the default for the
    currently active cohort's `config/operations.yaml` -- this function
    returns `portfolio` completely UNCHANGED: no `sector_by_ticker`/
    `price_history` population, `risk_data_required` stays `False`.
    Candidate eligibility for that cohort is therefore byte-for-byte
    identical to before this step existed. Only when a FUTURE cohort's
    operator config sets `risk_data_wiring.enabled: true` does this
    function populate sector/correlation data and set
    `Portfolio.risk_data_required = True`, switching the Risk Engine's
    missing-data behavior from "silently skip" to "fail closed" for
    that cohort going forward."""
    if not enabled:
        return portfolio
    tickers = frozenset({p.ticker for p in portfolio.positions}) | frozenset(e.ticker for e in universe)
    wired = apply_sector_wiring(portfolio, universe)
    wired = await apply_correlation_wiring(
        wired, tickers=tickers, historical_provider=historical_provider, now=now,
        lookback_days=lookback_days, min_observations=min_observations,
    )
    return wired.model_copy(update={"risk_data_required": True})
