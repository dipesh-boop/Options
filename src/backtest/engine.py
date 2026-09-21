"""The backtest engine: `run_backtest` drives the day-by-day simulation
loop, managing every open position through profit-target/DTE checks,
expiration/assignment, and new entries, while keeping two fully
independent cash/equity tracks — realistic and theoretical — so
`src.backtest.metrics` can report "theoretical midpoint return" and
"realistic execution return" as genuinely separate series, never one
derived from the other after the fact.

This module never generates a trade signal itself — `EntrySignal`s are
supplied by the caller (a test, or eventually a Strategy Screener this
codebase doesn't have yet). What this module owns is executing and
managing those signals *realistically*.

**Bias prevention**, one mechanism per named bias rather than one generic
disclaimer:

- **Look-ahead bias / data leakage.** Every single quote lookup this
  loop performs — for open positions and for new entries alike — is
  piped through `assert_no_lookahead_options(quote_lookup(...), as_of)`
  before use; `build_backtest_result`'s optional SPY comparison is
  likewise piped through `assert_no_lookahead` (`benchmark.py`). Neither
  is a convention a caller could forget: both raise the moment a quote or
  bar dated after `as_of` would otherwise be used.
- **Survivorship bias.** Only "where possible" per the spec, because it
  is a *data-provider* property, not something this engine can enforce
  on its own: `HistoricalOptionChainProvider`/`HistoricalDataProvider`
  are vendor-agnostic abstractions (no real historical vendor is wired up
  yet — the same open question `ARCHITECTURE.md §12` already flags). A
  provider that silently drops delisted/failed tickers from its history
  would bias any backtest run against it; that risk is documented here
  rather than papered over, since fixing it requires a real vendor
  decision this module doesn't make.
- **Future earnings knowledge.** This engine consumes `EntrySignal`s, it
  does not generate them — an earnings-window exclusion is a *signal
  generation* concern (a future Strategy Screener's job, exactly like
  `EntrySignal`'s own docstring already disclaims), not an execution
  concern. The reusable primitive already exists for that screener to use
  (`src.data.earnings.is_within_earnings_window`, built in an earlier
  step) — this module simply has no earnings data flowing through it to
  leak in the first place.
- **Future volatility knowledge.** `HistoricalOptionQuote.iv` for day N
  is whatever the caller's historical record says for day N — it is a
  field on the same quote object `assert_no_lookahead_options` already
  gates, so a day-N+k IV value can no more reach a day-N decision than a
  day-N+k price can.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable

from typing import Literal

from src.backtest.assignment import realized_settlement_pnl, settle_position
from src.backtest.benchmark import BenchmarkComparison, compare_to_benchmarks
from src.backtest.commissions import CommissionSchedule
from src.backtest.execution import execute_entry, execute_exit, flip_legs
from src.backtest.expiration import is_expiring_today, management_dte_reached, profit_target_reached
from src.backtest.metrics import PerformanceMetrics, compute_metrics
from src.backtest.simulator import (
    BacktestPosition,
    EntrySignal,
    HistoricalOptionQuote,
    PortfolioState,
    TradeRecord,
    assert_no_lookahead_options,
)
from src.backtest.slippage import NoFillError, mark_to_market
from src.brokers.paper import PaperBrokerConfig, _is_credit_pairing
from src.data.option_chain import OptionRight
from src.data.historical import HistoricalBar

_CONTRACT_MULTIPLIER = 100

QuoteLookup = Callable[[str, date], list[HistoricalOptionQuote]]


def _estimate_capital_at_risk(
    legs, contracts: int, underlying_cost_basis: float, entry_credit_total: float = 0.0
) -> float:
    """A simplified, strategy-agnostic collateral estimate — the same
    reasoning `src.brokers.paper.PaperBroker._required_collateral` uses
    (and, for a same-right short/long pair, the exact same
    `_is_credit_pairing` rule, imported directly rather than
    re-implemented, so the two can never silently disagree about which
    strike order is a credit spread and which is a debit spread).

    `entry_credit_total` is `realistic_entry_credit_total` (dollars,
    whole position, signed — negative for a debit) — Step 14B added
    this parameter because a debit spread or a pure long position
    (no short legs at all: long call/put, long straddle/strangle, a
    protective put's own long-put leg) previously fell through to
    either the credit-spread-width formula (wrong for a debit vertical:
    it isn't at risk beyond the debit already paid) or a short-strikes
    sum that is empty — and therefore *zero* — for any position with no
    short legs, silently under-reporting capital at risk for every
    long-premium strategy Step 19A added. Every branch below now uses
    the debit actually paid instead of either mistake."""
    shorts = [leg for leg in legs if leg.side == "sell"]
    longs = [leg for leg in legs if leg.side == "buy"]
    debit_paid = max(-entry_credit_total, 0.0)

    if len(shorts) == 1 and len(longs) == 1 and shorts[0].right != longs[0].right and underlying_cost_basis > 0:
        # Protective collar: one short call (covered by the held
        # shares) + one long put. Capital at risk is the covering
        # shares' cost basis, same as covered call/protective put below
        # — the short call carries no naked risk, and the put's own
        # small debit is already reflected in cash.
        return underlying_cost_basis * _CONTRACT_MULTIPLIER * contracts + debit_paid

    if len(shorts) == 1 and len(longs) == 1 and shorts[0].right == longs[0].right:
        if _is_credit_pairing(shorts[0], longs[0]):
            return abs(shorts[0].strike - longs[0].strike) * _CONTRACT_MULTIPLIER * contracts
        # Bull call spread / bear put spread: a debit vertical's maximum
        # loss is already the premium paid — no additional collateral.
        return debit_paid

    if len(shorts) == 1 and not longs:
        if underlying_cost_basis > 0:
            return underlying_cost_basis * _CONTRACT_MULTIPLIER * contracts  # covered call
        return shorts[0].strike * _CONTRACT_MULTIPLIER * contracts  # cash-secured put

    if not shorts:
        # Pure long/debit shape: long call, long put, long straddle,
        # long strangle, or a protective put bought fresh (shares' cost
        # basis, when set, dominates the same way it does above).
        if underlying_cost_basis > 0:
            return underlying_cost_basis * _CONTRACT_MULTIPLIER * contracts + debit_paid
        return debit_paid

    # Step 20A: LONG_CALL_BUTTERFLY -- one short leg (the middle strike,
    # at 2x quantity) plus two long wings of equal quantity, same right,
    # short strike strictly between the two long strikes. A fully
    # defined-risk debit structure: max loss is already the debit paid,
    # exactly like the debit-vertical branch above -- no additional
    # collateral (the sum-of-short-strikes fallback below would
    # otherwise wildly overstate risk on the short middle leg alone).
    if (
        len(shorts) == 1 and len(longs) == 2
        and longs[0].right == longs[1].right == shorts[0].right
        and longs[0].quantity_ratio == longs[1].quantity_ratio
        and shorts[0].quantity_ratio == 2 * longs[0].quantity_ratio
        and min(longs[0].strike, longs[1].strike) < shorts[0].strike < max(longs[0].strike, longs[1].strike)
    ):
        return debit_paid

    # Step 20A: SHORT_IRON_CONDOR / SHORT_IRON_BUTTERFLY -- two short
    # legs (put + call) each paired with a long wing of the same right,
    # equal quantities throughout. Standard defined-risk margin is the
    # WIDER of the two wing widths, never their sum -- only one side can
    # ever finish in-the-money at expiration, so the narrower side's
    # width never adds to the wider side's own worst case. (The
    # sum-of-short-strikes fallback below would badly overstate this,
    # and a naive sum-of-both-widths would still overstate it by the
    # narrower width.)
    if len(shorts) == 2 and len(longs) == 2:
        short_put = next((leg for leg in shorts if leg.right == OptionRight.PUT), None)
        short_call = next((leg for leg in shorts if leg.right == OptionRight.CALL), None)
        long_put = next((leg for leg in longs if leg.right == OptionRight.PUT), None)
        long_call = next((leg for leg in longs if leg.right == OptionRight.CALL), None)
        quantities = {leg.quantity_ratio for leg in (*shorts, *longs)}
        if short_put is not None and short_call is not None and long_put is not None and long_call is not None and len(quantities) == 1:
            put_width = short_put.strike - long_put.strike
            call_width = long_call.strike - short_call.strike
            if put_width > 0 and call_width > 0:
                return max(put_width, call_width) * _CONTRACT_MULTIPLIER * contracts

    # Not one of this platform's supported shapes above -- fall back to
    # the sum of short strikes as a conservative (never an under-estimate
    # for a credit strategy) stand-in.
    return sum(leg.strike for leg in shorts) * _CONTRACT_MULTIPLIER * contracts


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: float
    fill_config: PaperBrokerConfig
    commission_schedule: CommissionSchedule
    risk_free_annual_rate: float = 0.04


def _match_quotes_for_legs(
    quotes: list[HistoricalOptionQuote], position_legs, expiration: date, underlying: str
) -> list[HistoricalOptionQuote] | None:
    """MD-003 fix: matches on `(underlying, expiration, strike, right)`,
    not just `(expiration, strike, right)` -- see
    `src.risk.trade_risk._find_contract`'s docstring for why omitting
    the underlying check is a real (if narrow) mispricing risk, not
    just theoretical: a caller-side quote-lookup bug that returns
    another ticker's quotes would otherwise silently match by
    coincidental strike/expiration/right and price this leg from a
    completely different underlying's market."""
    matched: list[HistoricalOptionQuote] = []
    for leg in position_legs:
        found = next(
            (q for q in quotes if q.underlying == underlying and q.strike == leg.strike and q.right == leg.right and q.expiration == expiration),
            None,
        )
        if found is None:
            return None
        matched.append(found)
    return matched


def _close_position(
    position: BacktestPosition,
    as_of: date,
    close_reason: str,
    quotes_today: list[HistoricalOptionQuote],
    config: BacktestConfig,
) -> tuple[TradeRecord, float, float]:
    """Closes via a real market order (profit target / DTE management) —
    not expiration settlement, which `_settle_expired_position` handles
    separately since it isn't a market fill at all. Returns the trade
    record plus the (realistic, theoretical) *cash deltas this closing
    event alone contributes* — entry cash already moved when the
    position opened, so only the exit debit and exit commission apply
    here; entry commission is not subtracted a second time."""
    matched = _match_quotes_for_legs(quotes_today, position.legs, position.expiration, position.ticker)
    if matched is None:
        raise ValueError(f"no quotes available to close {position.position_id} on {as_of.isoformat()}")

    exit_result = execute_exit(
        legs=list(position.legs),
        expiration=position.expiration,
        quotes=matched,
        contracts=position.contracts,
        limit_price=10_000.0,  # management closes are not price-limited: get out at whatever the market offers
        fill_config=config.fill_config,
        commission_schedule=config.commission_schedule,
    )
    # execute_exit's net_price is signed (negative = a debit paid to
    # close a credit position) — already the correct sign for a cash
    # delta, no separate negation needed.
    realistic_exit_total = exit_result.realistic_price * _CONTRACT_MULTIPLIER * position.contracts
    theoretical_exit_total = exit_result.theoretical_price * _CONTRACT_MULTIPLIER * position.contracts
    total_commission = position.entry_commission + exit_result.commission

    realistic_pnl = position.realistic_entry_credit_total + realistic_exit_total - total_commission
    theoretical_pnl = position.theoretical_entry_credit_total + theoretical_exit_total

    record = TradeRecord(
        position_id=position.position_id,
        ticker=position.ticker,
        strategy=position.strategy,
        contracts=position.contracts,
        opened_at=position.opened_at,
        closed_at=as_of,
        close_reason=close_reason,
        capital_at_risk=position.capital_at_risk,
        entry_spread_pct=position.entry_spread_pct,
        realistic_entry_credit=position.realistic_entry_credit_total,
        realistic_exit_debit=realistic_exit_total,
        theoretical_entry_credit=position.theoretical_entry_credit_total,
        theoretical_exit_debit=theoretical_exit_total,
        commission_paid=total_commission,
        realistic_pnl=realistic_pnl,
        theoretical_pnl=theoretical_pnl,
    )
    realistic_cash_delta = realistic_exit_total - exit_result.commission
    theoretical_cash_delta = theoretical_exit_total
    return record, realistic_cash_delta, theoretical_cash_delta


def _settle_expired_position(position: BacktestPosition, as_of: date, settlement_price: float) -> tuple[TradeRecord, float]:
    """Expiration settlement's economics are identical on both tracks (a
    fixed strike/intrinsic-value-based formula, no market fill involved)
    — the caller applies the single returned realized-impact figure to
    both `realistic_cash` and `theoretical_cash`.

    This engine has no ongoing share ledger (`realistic_equity`/
    `theoretical_equity` are cash-only, always), so a settlement's
    contribution is computed via `realized_settlement_pnl` — **intrinsic
    value**, not the full strike notional a raw `cash_impact` sum would
    give. See that function's docstring: a covered call's shares (the
    only case with `underlying_shares_held > 0`) are realized against
    their actual cost basis; any other assignment/exercise is treated as
    an immediate mark-to-settlement of the resulting stock position,
    since it is never tracked beyond this instant.
    """
    settlements = settle_position(list(position.legs), position.contracts, settlement_price)
    any_assigned = any(s.assigned_or_exercised for s in settlements)
    close_reason = "assignment" if any_assigned else "expiration_otm"
    realized_impact = realized_settlement_pnl(
        settlements,
        position.contracts,
        underlying_shares_held=position.underlying_shares_held,
        underlying_cost_basis=position.underlying_cost_basis,
    )

    realistic_pnl = position.realistic_entry_credit_total + realized_impact - position.entry_commission
    theoretical_pnl = position.theoretical_entry_credit_total + realized_impact

    record = TradeRecord(
        position_id=position.position_id,
        ticker=position.ticker,
        strategy=position.strategy,
        contracts=position.contracts,
        opened_at=position.opened_at,
        closed_at=as_of,
        close_reason=close_reason,
        capital_at_risk=position.capital_at_risk,
        realistic_entry_credit=position.realistic_entry_credit_total,
        entry_spread_pct=position.entry_spread_pct,
        realistic_exit_debit=realized_impact,
        theoretical_entry_credit=position.theoretical_entry_credit_total,
        theoretical_exit_debit=realized_impact,
        commission_paid=position.entry_commission,
        realistic_pnl=realistic_pnl,
        theoretical_pnl=theoretical_pnl,
    )
    return record, realized_impact


def run_backtest(
    entries: list[EntrySignal],
    quote_lookup: QuoteLookup,
    trading_days: list[date],
    config: BacktestConfig,
) -> PortfolioState:
    if trading_days != sorted(trading_days):
        raise ValueError("trading_days must be sorted ascending")

    state = PortfolioState(realistic_cash=config.initial_cash, theoretical_cash=config.initial_cash)
    entries_by_date: dict[date, list[EntrySignal]] = {}
    for e in entries:
        entries_by_date.setdefault(e.entry_date, []).append(e)

    position_counter = 0

    for as_of in trading_days:
        still_open: list[BacktestPosition] = []
        for position in state.open_positions:
            quotes_today = assert_no_lookahead_options(quote_lookup(position.ticker, as_of), as_of)

            if is_expiring_today(position, as_of):
                if not quotes_today:
                    raise ValueError(f"no settlement data for {position.ticker} expiring {as_of.isoformat()}")
                settlement_price = quotes_today[0].underlying_price
                record, cash_impact = _settle_expired_position(position, as_of, settlement_price)
                # Settlement is a fixed-formula cash flow (strike-based),
                # identical on both tracks — no market fill involved, so
                # no slippage difference between them.
                state.realistic_cash += cash_impact
                state.theoretical_cash += cash_impact
                state.closed_trades.append(record)
                continue

            matched = _match_quotes_for_legs(quotes_today, position.legs, position.expiration, position.ticker)
            should_close = False
            close_reason = ""
            if matched is not None:
                # Value the *closing* order (flipped legs), not the
                # original position, and negate: `mark_to_market`'s
                # net_price is signed like a real order (negative =
                # debit paid), so a short/credit position's buy-to-close
                # value comes back negative here. Under FillModel.MID
                # this happens to equal the unflipped valuation (no
                # buy/sell asymmetry), which is why the bug this replaces
                # went uncaught by the earlier plain-MID smoke test — but
                # MID_WITH_SLIPPAGE/LIQUIDITY_ADJUSTED apply slippage
                # against whichever direction is actually being traded,
                # so pricing the wrong direction understates the real
                # cost to close and triggers profit-target exits too
                # early.
                closing_legs = flip_legs(list(position.legs))
                net_price_to_close = mark_to_market(closing_legs, position.expiration, matched, config.fill_config)
                current_cost_to_close = -net_price_to_close
                if profit_target_reached(position, current_cost_to_close * _CONTRACT_MULTIPLIER * position.contracts, position.realistic_entry_credit_total):
                    should_close, close_reason = True, "profit_target"
                elif management_dte_reached(position, as_of):
                    should_close, close_reason = True, "dte_management"

            if should_close:
                try:
                    record, realistic_cash_delta, theoretical_cash_delta = _close_position(position, as_of, close_reason, quotes_today, config)
                except NoFillError:
                    still_open.append(position)
                    continue
                state.closed_trades.append(record)
                state.realistic_cash += realistic_cash_delta
                state.theoretical_cash += theoretical_cash_delta
                continue

            still_open.append(position)
        state.open_positions = still_open

        for entry in entries_by_date.get(as_of, []):
            quotes_today = assert_no_lookahead_options(quote_lookup(entry.ticker, as_of), as_of)
            matched = _match_quotes_for_legs(quotes_today, entry.legs, entry.expiration, entry.ticker)
            if matched is None:
                continue
            try:
                result = execute_entry(
                    legs=list(entry.legs),
                    expiration=entry.expiration,
                    quotes=matched,
                    requested_contracts=entry.contracts_requested,
                    limit_price=entry.limit_price,
                    fill_config=config.fill_config,
                    commission_schedule=config.commission_schedule,
                )
            except NoFillError:
                continue

            position_counter += 1
            realistic_credit_total = result.realistic_price * _CONTRACT_MULTIPLIER * result.filled_contracts
            theoretical_credit_total = result.theoretical_price * _CONTRACT_MULTIPLIER * result.filled_contracts

            position = BacktestPosition(
                position_id=f"bt-{position_counter}",
                ticker=entry.ticker,
                strategy=entry.strategy,
                legs=entry.legs,
                contracts=result.filled_contracts,
                expiration=entry.expiration,
                opened_at=as_of,
                management_dte=entry.management_dte,
                profit_target_pct=entry.profit_target_pct,
                realistic_entry_credit_total=realistic_credit_total,
                theoretical_entry_credit_total=theoretical_credit_total,
                capital_at_risk=_estimate_capital_at_risk(
                    entry.legs, result.filled_contracts, entry.underlying_cost_basis, realistic_credit_total,
                ),
                entry_spread_pct=result.max_leg_spread_pct,
                entry_commission=result.commission,
                underlying_shares_held=entry.underlying_shares_held,
                underlying_cost_basis=entry.underlying_cost_basis,
            )
            state.open_positions.append(position)
            state.realistic_cash += realistic_credit_total - result.commission
            state.theoretical_cash += theoretical_credit_total

        realistic_equity = state.realistic_cash
        theoretical_equity = state.theoretical_cash
        state.realistic_equity_curve.append((as_of, realistic_equity))
        state.theoretical_equity_curve.append((as_of, theoretical_equity))

    return state


TargetCategory = Literal["exceeds", "meets", "approaches", "falls_below"]

DEFAULT_TARGET_LOW = 0.12
DEFAULT_TARGET_HIGH = 0.15
DEFAULT_APPROACH_MARGIN = 0.03


def evaluate_target(
    realistic_cagr: float,
    *,
    target_low: float = DEFAULT_TARGET_LOW,
    target_high: float = DEFAULT_TARGET_HIGH,
    approach_margin: float = DEFAULT_APPROACH_MARGIN,
) -> TargetCategory:
    """Categorizes **realistic**, never theoretical, CAGR against the
    platform's 12-15% research target. "Do not manipulate assumptions to
    reach it" — this always grades the execution-realistic figure, the
    one number this package guarantees hasn't been flattered by a
    frictionless-fill assumption."""
    if realistic_cagr >= target_high:
        return "exceeds"
    if realistic_cagr >= target_low:
        return "meets"
    if realistic_cagr >= target_low - approach_margin:
        return "approaches"
    return "falls_below"


@dataclass(frozen=True)
class BacktestResult:
    realistic_metrics: PerformanceMetrics
    theoretical_metrics: PerformanceMetrics
    trades: list[TradeRecord]
    slippage_cost_per_year: float
    slippage_pct_of_gross_profit: float
    turnover_per_year: float
    average_bid_ask_spread_pct: float
    benchmark: BenchmarkComparison | None
    target_category: TargetCategory


def _years_covered(equity_curve: list[tuple[date, float]]) -> float:
    if len(equity_curve) < 2:
        return 0.0
    return (equity_curve[-1][0] - equity_curve[0][0]).days / 365.0


def build_backtest_result(
    state: PortfolioState,
    config: BacktestConfig,
    spy_bars: list[HistoricalBar] | None = None,
) -> BacktestResult:
    """Assembles every reporting requirement Step 13 names into one
    object: separately-reported theoretical/realistic metrics
    (`src.backtest.metrics`), execution-quality figures computed
    directly from the trade log's own theoretical-vs-realistic fields
    (never re-derived from equity-curve differences, which would blur
    slippage together with commission and cash-timing effects), a
    benchmark comparison when SPY bars are supplied, and an honest
    target-category verdict computed from the realistic track only."""
    realistic_metrics = compute_metrics(state.realistic_equity_curve, state.closed_trades, config.initial_cash, config.risk_free_annual_rate)
    theoretical_metrics = compute_metrics(state.theoretical_equity_curve, state.closed_trades, config.initial_cash, config.risk_free_annual_rate)

    years = _years_covered(state.realistic_equity_curve)
    trades = state.closed_trades

    total_slippage = sum((t.theoretical_pnl - t.realistic_pnl - t.commission_paid) for t in trades)
    gross_profit_theoretical = sum(t.theoretical_pnl for t in trades if t.theoretical_pnl > 0)
    total_notional = sum(abs(t.realistic_entry_credit) + abs(t.realistic_exit_debit) for t in trades)

    slip_per_year = total_slippage / years if years > 0 else 0.0
    slip_pct_gross = total_slippage / gross_profit_theoretical if gross_profit_theoretical > 0 else 0.0
    turnover = total_notional / (config.initial_cash * years) if years > 0 and config.initial_cash > 0 else 0.0
    avg_spread = sum(t.entry_spread_pct for t in trades) / len(trades) if trades else 0.0

    benchmark = None
    if spy_bars is not None and len(state.realistic_equity_curve) >= 2:
        start_date, start_equity = state.realistic_equity_curve[0]
        end_date, end_equity = state.realistic_equity_curve[-1]
        _, theoretical_start = state.theoretical_equity_curve[0]
        _, theoretical_end = state.theoretical_equity_curve[-1]
        benchmark = compare_to_benchmarks(
            strategy_realistic_return=(end_equity / start_equity - 1.0) if start_equity > 0 else 0.0,
            strategy_theoretical_return=(theoretical_end / theoretical_start - 1.0) if theoretical_start > 0 else 0.0,
            spy_bars=spy_bars,
            risk_free_annual_rate=config.risk_free_annual_rate,
            start=start_date,
            end=end_date,
        )

    return BacktestResult(
        realistic_metrics=realistic_metrics,
        theoretical_metrics=theoretical_metrics,
        trades=trades,
        slippage_cost_per_year=slip_per_year,
        slippage_pct_of_gross_profit=slip_pct_gross,
        turnover_per_year=turnover,
        average_bid_ask_spread_pct=avg_spread,
        benchmark=benchmark,
        target_category=evaluate_target(realistic_metrics.cagr),
    )
