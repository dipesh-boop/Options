"""Deterministic Wheel underlying-eligibility screening (Part 4/5): "the
Wheel should only be considered on securities that the system would be
comfortable owning." Every check here is Python arithmetic over already
-canonical data (`OptionContract`, `UnderlyingQuote`, `Portfolio`) or a
reuse of an existing Risk-adjacent module (`src.risk.concentration`,
`src.risk.correlation`) — nothing here is an LLM call, and nothing here
is itself the Risk Engine: this is a pre-screen that decides whether a
candidate is even worth proposing, exactly the role
`src.workflows.candidate_generation.QuantFilterConfig` already plays for
the platform's other strategies. The real, unconditional veto is still
`src.risk.engine.evaluate_trade_proposal`, run again independently on the
actual CSP/CC `TradeProposal` this screen's output leads to.

**Fail closed on missing data.** A check whose required input is absent
(no earnings data, no IV, no delta) is never treated as "assume the safe
case" — it blocks eligibility with a `DATA_INSUFFICIENT`-shaped reason,
per CLAUDE.md's "if required data cannot be determined: DATA_INSUFFICIENT."
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum

from src.data.option_chain import OptionContract
from src.data.quotes import UnderlyingQuote
from src.quant.black_scholes import OptionRight
from src.quant.greeks import delta as bs_delta
from src.risk.concentration import (
    SectorConcentrationError,
    UnderlyingConcentrationError,
    check_sector_concentration,
    check_underlying_concentration,
)
from src.risk.correlation import CorrelationError, check_correlation
from src.risk.limits import RiskLimitsConfig
from src.risk.portfolio_risk import Portfolio
from src.strategies.volatility_engine import VolatilityComparison

_CONTRACT_MULTIPLIER = 100


class EarningsProximityStatus(str, Enum):
    """Never collapse "no earnings event was returned" into this
    enum's NO_EARNINGS_IN_WINDOW value without the caller having
    positively confirmed that -- an `EarningsCalendarProvider` that
    simply has no data for a symbol also returns nothing, and Part 5
    requires those two situations be told apart, never conflated."""

    NO_EARNINGS_IN_WINDOW = "no_earnings_in_window"
    EARNINGS_IN_WINDOW = "earnings_in_window"
    DATA_UNAVAILABLE = "data_unavailable"
    NOT_APPLICABLE_ETF = "not_applicable_etf"


@dataclass(frozen=True)
class WheelEligibilityConfig:
    """Screening preferences, not risk limits -- nothing here gates the
    real order (the Risk Engine does that, downstream, on the actual
    proposal); this only decides what's worth proposing as a Wheel
    candidate at all. Defaults are the research defaults Part 3/4/5/7
    name explicitly."""

    min_underlying_price: float = 10.0
    min_underlying_volume: int = 100_000
    avoid_earnings: bool = True
    earnings_window_days: int = 7
    csp_min_dte: int = 25
    csp_max_dte: int = 60
    csp_preferred_min_dte: int = 30
    csp_preferred_max_dte: int = 45
    csp_short_delta_low: float = 0.10
    csp_short_delta_high: float = 0.30
    csp_preferred_delta_low: float = 0.15
    csp_preferred_delta_high: float = 0.25

    def __post_init__(self) -> None:
        if self.min_underlying_price <= 0:
            raise ValueError("min_underlying_price must be positive")
        if not (0 < self.csp_short_delta_low < self.csp_short_delta_high <= 1.0):
            raise ValueError("csp_short_delta_low must be < csp_short_delta_high, both in (0, 1]")
        if self.csp_min_dte <= 0 or self.csp_min_dte > self.csp_max_dte:
            raise ValueError("csp_min_dte must be positive and <= csp_max_dte")


@dataclass(frozen=True)
class WheelEligibilityCheck:
    name: str
    passed: bool  # False = this check blocks eligibility
    detail: str


@dataclass(frozen=True)
class WheelEligibilityResult:
    ticker: str
    eligible: bool
    checks: tuple[WheelEligibilityCheck, ...]
    blocking_reasons: tuple[str, ...]
    computed_put_delta: float | None
    iv_context: VolatilityComparison | None


def resolve_put_delta(put_contract: OptionContract, *, spot: float, t: float, risk_free_rate: float) -> float | None:
    """The put's delta, computed by Python Quant if the provider didn't
    supply one directly -- never fabricated, and never left as a
    provider-reported number treated as authoritative without a way to
    verify it (the same "no LLM/provider number is trusted verbatim"
    principle CLAUDE.md states for every other Greek in this codebase).
    Returns None (a `DATA_INSUFFICIENT` condition) only when IV itself is
    unavailable, since delta cannot be computed without it."""
    if put_contract.delta is not None:
        return put_contract.delta
    if put_contract.iv is None or put_contract.iv <= 0 or t <= 0:
        return None
    return bs_delta(spot, put_contract.strike, t, risk_free_rate, put_contract.iv, OptionRight.PUT)


def check_wheel_eligibility(
    *,
    ticker: str,
    sector: str,
    is_etf: bool,
    approved_universe: frozenset[str],
    underlying_quote: UnderlyingQuote,
    put_contract: OptionContract,
    put_contracts_requested: int,
    portfolio: Portfolio,
    limits: RiskLimitsConfig,
    config: WheelEligibilityConfig,
    earnings_status: EarningsProximityStatus,
    now: datetime,
    iv_context: VolatilityComparison | None = None,
) -> WheelEligibilityResult:
    checks: list[WheelEligibilityCheck] = []

    def _add(name: str, passed: bool, detail: str) -> None:
        checks.append(WheelEligibilityCheck(name=name, passed=passed, detail=detail))

    _add(
        "approved_universe",
        ticker in approved_universe,
        f"{ticker} {'is' if ticker in approved_universe else 'is NOT'} in the approved universe",
    )

    mid = underlying_quote.mid
    _add(
        "underlying_price",
        mid >= config.min_underlying_price,
        f"underlying mid ${mid:.2f} vs minimum ${config.min_underlying_price:.2f}",
    )
    _add(
        "underlying_liquidity",
        underlying_quote.volume >= config.min_underlying_volume,
        f"underlying volume {underlying_quote.volume:,} vs minimum {config.min_underlying_volume:,}",
    )

    _add(
        "option_open_interest",
        put_contract.open_interest >= limits.min_open_interest,
        f"put open interest {put_contract.open_interest} vs minimum {limits.min_open_interest}",
    )
    _add(
        "option_volume",
        put_contract.volume >= limits.min_volume,
        f"put volume {put_contract.volume} vs minimum {limits.min_volume}",
    )
    option_mid = put_contract.mid
    if option_mid <= 0:
        _add("option_bid_ask_spread", False, "DATA_INSUFFICIENT: put has no usable bid/ask to compute a spread")
    else:
        spread_pct = (put_contract.ask - put_contract.bid) / option_mid
        _add(
            "option_bid_ask_spread",
            spread_pct <= limits.max_bid_ask_spread_pct,
            f"put bid/ask spread {spread_pct:.2%} vs maximum {limits.max_bid_ask_spread_pct:.2%}",
        )

    dte = (put_contract.expiration - now.date()).days
    _add(
        "dte_in_permitted_range",
        config.csp_min_dte <= dte <= config.csp_max_dte,
        f"{dte} DTE vs permitted [{config.csp_min_dte}, {config.csp_max_dte}] "
        f"(preferred [{config.csp_preferred_min_dte}, {config.csp_preferred_max_dte}])",
    )

    t_years = max(dte, 0) / 365.0
    computed_delta = resolve_put_delta(put_contract, spot=underlying_quote.mid, t=t_years, risk_free_rate=limits.risk_free_rate)
    if computed_delta is None:
        _add("delta_in_permitted_range", False, "DATA_INSUFFICIENT: no IV available to determine the put's delta")
    else:
        abs_delta = abs(computed_delta)
        _add(
            "delta_in_permitted_range",
            config.csp_short_delta_low <= abs_delta <= config.csp_short_delta_high,
            f"|delta|={abs_delta:.3f} vs permitted [{config.csp_short_delta_low:.2f}, {config.csp_short_delta_high:.2f}] "
            f"(preferred [{config.csp_preferred_delta_low:.2f}, {config.csp_preferred_delta_high:.2f}])",
        )

    if is_etf:
        _add("earnings_proximity", True, "ETF Wheel: not subject to corporate earnings (macro/event risk tracked separately, informational only)")
    elif not config.avoid_earnings:
        _add("earnings_proximity", True, "AVOID_EARNINGS is disabled by research configuration")
    elif earnings_status == EarningsProximityStatus.NO_EARNINGS_IN_WINDOW:
        _add("earnings_proximity", True, f"no earnings within {config.earnings_window_days} days of expiration")
    elif earnings_status == EarningsProximityStatus.EARNINGS_IN_WINDOW:
        _add("earnings_proximity", False, f"expiration falls within {config.earnings_window_days} days of an earnings event")
    else:
        _add("earnings_proximity", False, "DATA_INSUFFICIENT: earnings calendar data unavailable for this ticker/expiration")

    capital_required = put_contract.strike * _CONTRACT_MULTIPLIER * put_contracts_requested
    _add(
        "cash_available_if_assigned",
        capital_required <= portfolio.cash,
        f"capital required if assigned ${capital_required:,.2f} vs available cash ${portfolio.cash:,.2f}",
    )
    remaining_cash_pct = (portfolio.cash - capital_required) / portfolio.nav if portfolio.nav > 0 else 0.0
    _add(
        "post_trade_cash_reserve",
        remaining_cash_pct >= limits.min_cash_reserve_pct,
        f"post-assignment cash reserve would be {remaining_cash_pct:.2%} vs minimum {limits.min_cash_reserve_pct:.2%}",
    )

    try:
        check_underlying_concentration(portfolio, ticker, capital_required, limits)
        _add("underlying_concentration", True, f"{ticker} concentration within {limits.max_underlying_exposure_pct:.2%} limit if assigned")
    except UnderlyingConcentrationError as exc:
        _add("underlying_concentration", False, str(exc))

    try:
        check_sector_concentration(portfolio, sector, capital_required, limits)
        _add("sector_concentration", True, f"{sector} concentration within {limits.max_sector_exposure_pct:.2%} limit if assigned")
    except SectorConcentrationError as exc:
        _add("sector_concentration", False, str(exc))

    try:
        check_correlation(portfolio, ticker, limits)
        _add("correlation", True, "no disqualifying correlation with existing holdings (or price history unavailable, a documented gap)")
    except CorrelationError as exc:
        _add("correlation", False, str(exc))

    if iv_context is not None:
        spread_desc = f"{iv_context.iv_rv_spread:+.2%}" if iv_context.iv_rv_spread is not None else "unavailable (no realized volatility)"
        checks.append(
            WheelEligibilityCheck(
                name="iv_context",
                passed=True,
                detail=(
                    f"IV {iv_context.implied_volatility:.2%}, IV-RV spread {spread_desc} -- both a premium "
                    "opportunity (richer credit) and a warning sign (the market is pricing more risk into this "
                    "underlying); informational only, never a standalone reason to start a Wheel"
                ),
            )
        )

    blocking = tuple(c.detail for c in checks if not c.passed)
    return WheelEligibilityResult(
        ticker=ticker,
        eligible=not blocking,
        checks=tuple(checks),
        blocking_reasons=blocking,
        computed_put_delta=computed_delta,
        iv_context=iv_context,
    )
