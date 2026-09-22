"""Part 25: Strategy + Management Policy Performance.

**"We are testing STRATEGY + MANAGEMENT POLICY, not merely option
structure"** (Part 25's own words) — every metric here is computed at
BOTH levels: `strategy_level_performance` groups every closed position
by `StrategyKind` alone (e.g. every `PUT_CREDIT_SPREAD`, regardless of
which policy managed it), and `strategy_policy_level_performance`
groups by the *pair* (e.g. `PUT_CREDIT_SPREAD` managed under
`PCS_50PCT_21DTE` specifically, separate from the same structure under
`PCS_HOLD_TO_EXPIRY`) — directly answering Part 1/16's "never assume
one management policy is universally superior."

Sharpe/Sortino/max-drawdown/CVaR reuse `src.backtest.metrics`'s own
implementations against a *synthetic* equity curve built by summing
`ClosedPositionAnalysis.realized_pnl` in exit-date order for the
records in that group — this module never re-derives that math itself.
This is a trade-level curve for the group in isolation, not a
continuously marked whole-portfolio curve; `GroupPerformance` documents
that distinction on the field itself rather than silently overloading
what "Sharpe ratio" means here.

Small-sample results are never presented as proven: `sample_size_warning`
reuses `src.research.overfitting_guards.check_small_sample`'s existing
threshold and wording rather than inventing a second one.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from src.backtest.metrics import historical_cvar, max_drawdown, sharpe_ratio, sortino_ratio
from src.research.overfitting_guards import check_small_sample
from src.strategies.base import StrategyKind
from src.validation.post_trade_analysis import ClosedPositionAnalysis

_ASSIGNMENT_EXIT_REASONS = frozenset({"assigned", "assignment", "called_away"})
_DEFAULT_RISK_FREE_RATE = 0.0


@dataclass(frozen=True)
class GroupPerformance:
    """One performance rollup — either strategy-level
    (`management_policy_name is None`) or strategy+policy-level.
    `sharpe`/`sortino`/`max_drawdown`/`cvar_95` are computed against a
    synthetic, trade-level equity curve for this group alone (see
    module docstring) — not this platform's whole-portfolio curve."""

    group_label: str
    strategy_kind: StrategyKind
    management_policy_name: str | None
    trade_count: int
    win_rate: float
    expectancy: float
    profit_factor: float
    average_win: float
    average_loss: float
    sharpe: float
    sortino: float
    max_drawdown: float
    cvar_95: float
    average_mfe_capture: float | None
    average_mae: float
    average_holding_period_days: float
    assignment_rate: float
    average_slippage: float | None
    average_commission: float
    sample_size_warning: str | None


def _synthetic_equity_curve(records: list[ClosedPositionAnalysis], *, starting_capital: float) -> list[tuple[date, float]]:
    ordered = sorted(records, key=lambda r: r.exit_date)
    if not ordered:
        return []
    equity = starting_capital
    curve = [(ordered[0].entry_date, equity)]
    for r in ordered:
        equity += r.realized_pnl
        curve.append((r.exit_date, equity))
    return curve


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _group_performance(
    records: list[ClosedPositionAnalysis],
    *,
    group_label: str,
    strategy_kind: StrategyKind,
    management_policy_name: str | None,
    starting_capital: float,
    risk_free_annual_rate: float,
) -> GroupPerformance:
    n = len(records)
    if n == 0:
        return GroupPerformance(
            group_label=group_label, strategy_kind=strategy_kind, management_policy_name=management_policy_name,
            trade_count=0, win_rate=0.0, expectancy=0.0, profit_factor=0.0, average_win=0.0, average_loss=0.0,
            sharpe=0.0, sortino=0.0, max_drawdown=0.0, cvar_95=0.0, average_mfe_capture=None, average_mae=0.0,
            average_holding_period_days=0.0, assignment_rate=0.0, average_slippage=None, average_commission=0.0,
            sample_size_warning=check_small_sample(0),
        )

    pnls = [r.realized_pnl for r in records]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p < 0]
    win_rate = len(winners) / n
    expectancy = _mean(pnls)
    gross_profit = sum(winners)
    gross_loss = abs(sum(losers))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    average_win = _mean(winners)
    average_loss = _mean(losers)

    curve = _synthetic_equity_curve(records, starting_capital=starting_capital)
    sharpe = sharpe_ratio(curve, risk_free_annual_rate) if len(curve) > 1 else 0.0
    sortino = sortino_ratio(curve, risk_free_annual_rate) if len(curve) > 1 else 0.0
    mdd = max_drawdown(curve) if len(curve) > 1 else 0.0
    cvar = historical_cvar(curve) if len(curve) > 2 else 0.0

    mfe_captures = [r.exit_efficiency for r in records if r.exit_efficiency is not None]
    slippages = [r.slippage for r in records if r.slippage is not None]
    assignment_rate = sum(1 for r in records if r.exit_reason in _ASSIGNMENT_EXIT_REASONS) / n

    return GroupPerformance(
        group_label=group_label,
        strategy_kind=strategy_kind,
        management_policy_name=management_policy_name,
        trade_count=n,
        win_rate=win_rate,
        expectancy=expectancy,
        profit_factor=profit_factor,
        average_win=average_win,
        average_loss=average_loss,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=mdd,
        cvar_95=cvar,
        average_mfe_capture=(_mean(mfe_captures) if mfe_captures else None),
        average_mae=_mean([r.mae for r in records]),
        average_holding_period_days=_mean([float(r.days_held) for r in records]),
        assignment_rate=assignment_rate,
        average_slippage=(_mean(slippages) if slippages else None),
        average_commission=_mean([r.commissions for r in records]),
        sample_size_warning=check_small_sample(n),
    )


def strategy_level_performance(
    records: list[ClosedPositionAnalysis], strategy_kind: StrategyKind, *, starting_capital: float, risk_free_annual_rate: float = _DEFAULT_RISK_FREE_RATE
) -> GroupPerformance:
    subset = [r for r in records if r.strategy_kind == strategy_kind]
    return _group_performance(
        subset, group_label=strategy_kind.value, strategy_kind=strategy_kind, management_policy_name=None,
        starting_capital=starting_capital, risk_free_annual_rate=risk_free_annual_rate,
    )


def strategy_policy_level_performance(
    records: list[ClosedPositionAnalysis], strategy_kind: StrategyKind, management_policy_name: str,
    *, starting_capital: float, risk_free_annual_rate: float = _DEFAULT_RISK_FREE_RATE,
) -> GroupPerformance:
    subset = [r for r in records if r.strategy_kind == strategy_kind and r.management_policy_name == management_policy_name]
    return _group_performance(
        subset, group_label=f"{strategy_kind.value} + {management_policy_name}", strategy_kind=strategy_kind,
        management_policy_name=management_policy_name, starting_capital=starting_capital, risk_free_annual_rate=risk_free_annual_rate,
    )


def all_strategy_performance(
    records: list[ClosedPositionAnalysis], *, starting_capital: float, risk_free_annual_rate: float = _DEFAULT_RISK_FREE_RATE
) -> list[GroupPerformance]:
    kinds = sorted({r.strategy_kind for r in records}, key=lambda k: k.value)
    return [strategy_level_performance(records, k, starting_capital=starting_capital, risk_free_annual_rate=risk_free_annual_rate) for k in kinds]


def all_strategy_policy_performance(
    records: list[ClosedPositionAnalysis], *, starting_capital: float, risk_free_annual_rate: float = _DEFAULT_RISK_FREE_RATE
) -> list[GroupPerformance]:
    pairs = sorted({(r.strategy_kind, r.management_policy_name) for r in records}, key=lambda p: (p[0].value, p[1]))
    return [
        strategy_policy_level_performance(records, k, p, starting_capital=starting_capital, risk_free_annual_rate=risk_free_annual_rate)
        for k, p in pairs
    ]
