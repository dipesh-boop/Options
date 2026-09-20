"""REJECTED TRADE REVIEW section of `/weekly-review` (Step 16): "Analyze
rejected opportunities. Determine what would have happened if taken. Do
NOT automatically conclude that a rejected winning trade should have
been accepted. Evaluate rejected trades statistically over meaningful
samples."

Pricing "what would have happened" reuses `src.backtest.execution`/
`src.backtest.assignment` exactly as `src.backtest.engine` does — a
hypothetical trade and a backtest trade answer the identical question
("what would this fill and this settlement actually have been worth"),
so this module is a thin adapter from `TradeProposal` to
`src.backtest.simulator.BacktestLeg`, never a second execution model.

The small-sample guard mirrors `src.research.overfitting_guards`' own
idiom (a named threshold, a warning rather than a silent conclusion) —
"a single rejected winner proves nothing" is enforced by making the
statistics object say so, not by a comment asking the reader to
remember it.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.backtest.assignment import realized_settlement_pnl, settle_position
from src.backtest.commissions import CommissionSchedule
from src.backtest.execution import execute_entry, execute_exit
from src.backtest.simulator import BacktestLeg, HistoricalOptionQuote
from src.brokers.paper import PaperBrokerConfig
from src.data.option_chain import OptionRight as DataOptionRight
from src.llm.schemas import LegSide, OptionRight as ProposalOptionRight, TradeProposal

_CONTRACT_MULTIPLIER = 100
# Deliberately permissive limit prices, guaranteeing a fill at whatever
# the market offers rather than gating on the proposal's own original
# target -- the same idiom `src.backtest.engine._close_position` uses
# for management closes ("get out at whatever the market offers"). This
# platform's three approved strategies always *open* on a net credit and
# *close* on a net debit (`src.brokers.paper.price_satisfies_limit`'s
# two branches), so "accept anything" means a very low limit for entry
# (any credit clears it) and a very high one for exit (any debit does).
_GUARANTEED_ENTRY_LIMIT = -10_000.0
_GUARANTEED_EXIT_LIMIT = 10_000.0

MIN_SAMPLE_SIZE_FOR_CONCLUSIONS = 20


def _to_backtest_legs(proposal: TradeProposal) -> list[BacktestLeg]:
    return [
        BacktestLeg(
            right=DataOptionRight.PUT if leg.right == ProposalOptionRight.PUT else DataOptionRight.CALL,
            strike=leg.strike,
            side="sell" if leg.side == LegSide.SELL else "buy",
        )
        for leg in proposal.legs
    ]


@dataclass(frozen=True)
class HypotheticalOutcome:
    proposal_id: str
    ticker: str
    strategy: str
    rejected_stage: str
    hypothetical_pnl: float
    exit_reason: str  # "closed" | "expiration_otm" | "assignment"


def hypothetical_outcome_from_exit_quotes(
    proposal: TradeProposal,
    entry_quotes: list[HistoricalOptionQuote],
    exit_quotes: list[HistoricalOptionQuote],
    *,
    rejected_stage: str,
    fill_config: PaperBrokerConfig,
    commission_schedule: CommissionSchedule,
) -> HypotheticalOutcome:
    """What this proposal would have realized had it been entered and
    then closed via a real market order against `exit_quotes` — e.g. a
    later day's quotes, at a profit target or DTE-management point a
    human reviewer picks. Raises `NoFillError` if either leg's
    hypothetical liquidity couldn't have supported the trade at all."""
    legs = _to_backtest_legs(proposal)
    entry = execute_entry(
        legs=legs, expiration=proposal.expiration, quotes=entry_quotes, requested_contracts=proposal.contracts_requested,
        limit_price=_GUARANTEED_ENTRY_LIMIT, fill_config=fill_config, commission_schedule=commission_schedule,
    )
    exit_result = execute_exit(
        legs=legs, expiration=proposal.expiration, quotes=exit_quotes, contracts=entry.filled_contracts,
        limit_price=_GUARANTEED_EXIT_LIMIT, fill_config=fill_config, commission_schedule=commission_schedule,
    )
    entry_total = entry.realistic_price * _CONTRACT_MULTIPLIER * entry.filled_contracts
    exit_total = exit_result.realistic_price * _CONTRACT_MULTIPLIER * entry.filled_contracts
    pnl = entry_total + exit_total - entry.commission - exit_result.commission
    return HypotheticalOutcome(
        proposal_id=proposal.proposal_id, ticker=proposal.ticker, strategy=proposal.strategy.value,
        rejected_stage=rejected_stage, hypothetical_pnl=pnl, exit_reason="closed",
    )


def hypothetical_outcome_from_settlement(
    proposal: TradeProposal,
    entry_quotes: list[HistoricalOptionQuote],
    settlement_price: float,
    *,
    rejected_stage: str,
    fill_config: PaperBrokerConfig,
    commission_schedule: CommissionSchedule,
) -> HypotheticalOutcome:
    """What this proposal would have realized had it been entered and
    then held to expiration, settling at `settlement_price`. A rejected
    proposal is always a *new* trade under consideration, never a
    covered position with pre-existing shares, so this always uses the
    intrinsic-value default of `realized_settlement_pnl` (no cost-basis
    override) -- see that function's docstring for why raw
    `cash_impact` (the full strike notional) would overstate this by the
    value of the stock position it ignores."""
    legs = _to_backtest_legs(proposal)
    entry = execute_entry(
        legs=legs, expiration=proposal.expiration, quotes=entry_quotes, requested_contracts=proposal.contracts_requested,
        limit_price=_GUARANTEED_ENTRY_LIMIT, fill_config=fill_config, commission_schedule=commission_schedule,
    )
    entry_total = entry.realistic_price * _CONTRACT_MULTIPLIER * entry.filled_contracts
    settlements = settle_position(legs, entry.filled_contracts, settlement_price)
    realized_impact = realized_settlement_pnl(settlements, entry.filled_contracts)
    pnl = entry_total + realized_impact - entry.commission
    exit_reason = "assignment" if any(s.assigned_or_exercised for s in settlements) else "expiration_otm"
    return HypotheticalOutcome(
        proposal_id=proposal.proposal_id, ticker=proposal.ticker, strategy=proposal.strategy.value,
        rejected_stage=rejected_stage, hypothetical_pnl=pnl, exit_reason=exit_reason,
    )


@dataclass(frozen=True)
class RejectedTradeStatistics:
    sample_size: int
    hit_rate: float
    average_hypothetical_pnl: float
    median_hypothetical_pnl: float
    total_hypothetical_pnl: float
    meaningful_sample: bool
    warning: str | None


def summarize_rejected_outcomes(
    outcomes: list[HypotheticalOutcome], *, min_sample_size: int = MIN_SAMPLE_SIZE_FOR_CONCLUSIONS
) -> RejectedTradeStatistics:
    if not outcomes:
        return RejectedTradeStatistics(
            sample_size=0, hit_rate=0.0, average_hypothetical_pnl=0.0, median_hypothetical_pnl=0.0,
            total_hypothetical_pnl=0.0, meaningful_sample=False, warning="no rejected-trade outcomes supplied this period",
        )
    pnls = sorted(o.hypothetical_pnl for o in outcomes)
    n = len(pnls)
    hit_rate = sum(1 for p in pnls if p > 0) / n
    average = sum(pnls) / n
    mid = n // 2
    median = pnls[mid] if n % 2 == 1 else (pnls[mid - 1] + pnls[mid]) / 2.0
    meaningful = n >= min_sample_size
    warning = None
    if not meaningful:
        warning = (
            f"only {n} rejected-trade outcome(s) sampled this period, below the {min_sample_size}-sample "
            "threshold for a statistically meaningful conclusion — do not treat any single rejected trade's "
            "outcome (winning or losing) as proof the rejection was right or wrong"
        )
    return RejectedTradeStatistics(
        sample_size=n, hit_rate=hit_rate, average_hypothetical_pnl=average, median_hypothetical_pnl=median,
        total_hypothetical_pnl=sum(pnls), meaningful_sample=meaningful, warning=warning,
    )
