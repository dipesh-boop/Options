"""Part 24: Post-Trade Analysis.

For every CLOSED position under lifecycle management, calculates
realized P&L, return on risk, return on committed capital, days held,
MFE/MAE, exit efficiency, slippage, commissions, benchmark return over
the identical holding period, the management policy used, and the
reason for exit — and, separately, a set of COUNTERFACTUAL exit
outcomes (what if held to expiration? exited at 25/50/75% target?
exited at 21 DTE?) computed WITHOUT altering the historical record.
`ClosedPositionAnalysis.counterfactuals` is a clearly separate,
labeled field that never feeds back into `.realized_pnl` or into any
lifecycle decision — Part 24's explicit "do not use hindsight to alter
recorded decisions."

Counterfactual exit pricing reuses `src.backtest.execution.execute_exit`/
`src.backtest.assignment.settle_position` exactly as
`src.workflows.rejected_trade_review` already does for a REJECTED
proposal. The difference here is the entry leg is already known — a
real, already-filled position — so only the exit half is hypothetical;
this module never re-prices or second-guesses the real entry.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from src.backtest.assignment import realized_settlement_pnl, settle_position
from src.backtest.commissions import CommissionSchedule
from src.backtest.execution import execute_exit
from src.backtest.simulator import BacktestLeg, HistoricalOptionQuote
from src.brokers.paper import PaperBrokerConfig
from src.lifecycle.excursion import exit_efficiency
from src.strategies.base import StrategyKind

_CONTRACT_MULTIPLIER = 100
# Deliberately permissive: guarantees a fill at whatever the market
# offers for a hypothetical exit, the same idiom
# `src.workflows.rejected_trade_review` and `src.backtest.engine`'s own
# management closes use ("get out at whatever the market offers").
_GUARANTEED_EXIT_LIMIT = 10_000.0


@dataclass(frozen=True)
class CounterfactualOutcome:
    """One "what if" exit outcome for an already-closed position.
    Never written back into the position's own recorded state —
    `exit_reason` here is deliberately a plain string ("closed" |
    "expiration_otm" | "assignment"), not a `PositionLifecycleState`,
    since a counterfactual never actually transitioned anything."""

    label: str
    hypothetical_pnl: float
    exit_reason: str


def counterfactual_exit_at_quotes(
    *,
    legs: list[BacktestLeg],
    expiration: date,
    entry_realistic_price: float,
    entry_filled_contracts: int,
    entry_commission: float,
    exit_quotes: list[HistoricalOptionQuote],
    label: str,
    fill_config: PaperBrokerConfig,
    commission_schedule: CommissionSchedule,
) -> CounterfactualOutcome:
    """What this position would have realized had it instead been
    closed against `exit_quotes` (a different day's quotes, e.g. the
    day a 25%/50%/75% profit target or a 21 DTE checkpoint would have
    been hit) rather than however it was actually closed. The entry
    leg's real, already-known fill (`entry_realistic_price`/
    `entry_filled_contracts`/`entry_commission`) is held fixed — only
    the exit is hypothetical."""
    exit_result = execute_exit(
        legs=legs, expiration=expiration, quotes=exit_quotes, contracts=entry_filled_contracts,
        limit_price=_GUARANTEED_EXIT_LIMIT, fill_config=fill_config, commission_schedule=commission_schedule,
    )
    entry_total = entry_realistic_price * _CONTRACT_MULTIPLIER * entry_filled_contracts
    exit_total = exit_result.realistic_price * _CONTRACT_MULTIPLIER * entry_filled_contracts
    pnl = entry_total + exit_total - entry_commission - exit_result.commission
    return CounterfactualOutcome(label=label, hypothetical_pnl=pnl, exit_reason="closed")


def counterfactual_hold_to_expiration(
    *,
    legs: list[BacktestLeg],
    entry_realistic_price: float,
    entry_filled_contracts: int,
    entry_commission: float,
    settlement_price: float,
    label: str = "held_to_expiration",
) -> CounterfactualOutcome:
    """What this position would have realized had it instead been held
    to expiration, settling at `settlement_price`, rather than however
    it was actually closed."""
    entry_total = entry_realistic_price * _CONTRACT_MULTIPLIER * entry_filled_contracts
    settlements = settle_position(legs, entry_filled_contracts, settlement_price)
    realized_impact = realized_settlement_pnl(settlements, entry_filled_contracts)
    pnl = entry_total + realized_impact - entry_commission
    exit_reason = "assignment" if any(s.assigned_or_exercised for s in settlements) else "expiration_otm"
    return CounterfactualOutcome(label=label, hypothetical_pnl=pnl, exit_reason=exit_reason)


@dataclass(frozen=True)
class ClosedPositionAnalysis:
    """The full Part 24 record for one closed position. Every field
    other than `counterfactuals` is the ACTUAL, already-known outcome —
    nothing here is re-derived or re-priced; the caller supplies each
    figure from wherever it was already computed once (PaperBroker's
    fills, the Risk Engine's own economics, `src.lifecycle.excursion`'s
    MFE/MAE)."""

    trade_id: str
    strategy_kind: StrategyKind
    management_policy_name: str
    entry_date: date
    exit_date: date
    realized_pnl: float
    capital_at_risk: float | None
    capital_committed: float | None
    mfe: float
    mae: float
    commissions: float
    exit_reason: str
    slippage: float | None = None
    benchmark_return_pct: float | None = None
    counterfactuals: tuple[CounterfactualOutcome, ...] = field(default_factory=tuple)

    @property
    def days_held(self) -> int:
        return (self.exit_date - self.entry_date).days

    @property
    def return_on_risk(self) -> float | None:
        if self.capital_at_risk is None or self.capital_at_risk <= 0:
            return None
        return self.realized_pnl / self.capital_at_risk

    @property
    def return_on_committed_capital(self) -> float | None:
        if self.capital_committed is None or self.capital_committed <= 0:
            return None
        return self.realized_pnl / self.capital_committed

    @property
    def exit_efficiency(self) -> float | None:
        return exit_efficiency(self.realized_pnl, self.mfe)


def build_closed_position_analysis(
    *,
    trade_id: str,
    strategy_kind: StrategyKind,
    management_policy_name: str,
    entry_date: date,
    exit_date: date,
    realized_pnl: float,
    capital_at_risk: float | None,
    capital_committed: float | None,
    mfe: float,
    mae: float,
    commissions: float,
    exit_reason: str,
    slippage: float | None = None,
    benchmark_return_pct: float | None = None,
    counterfactuals: list[CounterfactualOutcome] | None = None,
) -> ClosedPositionAnalysis:
    if exit_date < entry_date:
        raise ValueError(f"{trade_id}: exit_date {exit_date!r} cannot be before entry_date {entry_date!r}")
    return ClosedPositionAnalysis(
        trade_id=trade_id,
        strategy_kind=strategy_kind,
        management_policy_name=management_policy_name,
        entry_date=entry_date,
        exit_date=exit_date,
        realized_pnl=realized_pnl,
        capital_at_risk=capital_at_risk,
        capital_committed=capital_committed,
        mfe=mfe,
        mae=mae,
        commissions=commissions,
        exit_reason=exit_reason,
        slippage=slippage,
        benchmark_return_pct=benchmark_return_pct,
        counterfactuals=tuple(counterfactuals or ()),
    )
