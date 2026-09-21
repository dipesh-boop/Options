"""The deterministic portfolio Risk Engine: `evaluate_trade_proposal`.

THIS MODULE HAS ABSOLUTE VETO AUTHORITY. No LLM may override its
decision, and no LLM call exists anywhere in `evaluate_trade_proposal`'s
call graph — every module it imports (`src.risk.*`, `src.quant.*`,
`src.data.*`, `src.brokers.fidelity`) is itself free of any dependency on
`src.llm.client` or `src.llm.router` (the only two modules in this
codebase capable of an actual model call). The one `src.llm` import this
package allows anywhere is `src.llm.schemas` — plain Pydantic data types
(`TradeProposal`, `StrategyType`, ...) that flow into this module as
*data*, never as a call. See
`tests/unit/risk/test_architecture_boundary.py` for the proof.

Every numeric policy value here comes from `config/risk_limits.yaml` via
`src.risk.limits` — nothing is hard-coded (see
`tests/unit/risk/test_no_hardcoded_limits.py`).

FAIL CLOSED is the organizing principle: `evaluate_trade_proposal` never
returns `RiskDecision.APPROVE` because a check merely didn't run. Every
exception raised by a step below is caught at that step and mapped to a
specific `ReasonCode`; anything not anticipated falls through to the
outer handler and becomes `ReasonCode.REJECT_UNKNOWN_RISK`. Position
sizing (`src.risk.trade_risk.size_trade`) can only ever reduce
`TradeProposal.contracts_requested`, never increase it.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from src.brokers.fidelity import ApprovedOrder, FidelityLegAction, FidelityManualProvider, FidelityOrderLeg, FidelityTradeTicket
from src.data.option_chain import OptionChain, OptionRight as DataOptionRight
from src.data.provider import StaleDataError
from src.llm.schemas import LegSide, StrategyType, TradeAction, TradeProposal, ensure_trade_proposal
from src.quant.black_scholes import Leg as QuantLeg
from src.quant.black_scholes import OptionRight as QuantOptionRight
from src.quant.black_scholes import Side as QuantSide
from src.quant.monte_carlo import Position as QuantPosition
from src.risk.broker_constraints import BrokerCapabilities
from src.risk.concentration import (
    SectorConcentrationError,
    UnderlyingConcentrationError,
    check_sector_concentration,
    check_underlying_concentration,
)
from src.risk.correlation import CorrelationError, check_correlation
from src.risk.drawdown import DrawdownZone, current_drawdown_pct, drawdown_zone, sizing_multiplier_for_zone
from src.risk.kill_switch import check_kill_switch
from src.risk.limits import RiskLimitsConfig, get_default_limits
from src.risk.portfolio_risk import Portfolio, find_duplicate_position
from src.risk.reason_codes import ReasonCode, RiskDecision
from src.risk.stress import StressTestError, worst_case_stress_loss
from src.risk.trade_risk import (
    ContractNotFoundError,
    LiquidityError,
    MissingCollateralError,
    QuantitativeAnalysis,
    QuantMismatchError,
    StaleContractError,
    TradeRiskError,
    UndefinedEconomicsError,
    check_all_legs_liquid,
    check_collateral,
    compute_trade_economics,
    cross_check_quantitative_analysis,
    resolve_leg_contracts,
    size_trade,
)


@dataclass(frozen=True)
class RiskDecisionResult:
    decision: RiskDecision
    reason_codes: list[ReasonCode]
    approved_contracts: int | None
    message: str
    max_loss: float | None = None
    max_profit: float | None = None
    capital_required: float | None = None
    worst_case_stress_loss_pct_of_nav: float | None = None
    approved_order: ApprovedOrder | None = None
    fidelity_ticket: FidelityTradeTicket | None = None


def _reject(code: ReasonCode, message: str) -> RiskDecisionResult:
    return RiskDecisionResult(decision=RiskDecision.REJECT, reason_codes=[code], approved_contracts=None, message=message)


def _halt(code: ReasonCode, message: str) -> RiskDecisionResult:
    return RiskDecisionResult(decision=RiskDecision.HALT, reason_codes=[code], approved_contracts=None, message=message)


_DATA_RIGHT = {"C": DataOptionRight.CALL, "P": DataOptionRight.PUT}
_QUANT_RIGHT = {"C": QuantOptionRight.CALL, "P": QuantOptionRight.PUT}
_STRATEGY_DISPLAY = {
    StrategyType.CASH_SECURED_PUT: "CASH SECURED PUT",
    StrategyType.COVERED_CALL: "COVERED CALL",
    StrategyType.PUT_CREDIT_SPREAD: "PUT CREDIT SPREAD",
    StrategyType.CALL_CREDIT_SPREAD: "CALL CREDIT SPREAD",
    StrategyType.BULL_CALL_SPREAD: "BULL CALL SPREAD",
    StrategyType.BEAR_PUT_SPREAD: "BEAR PUT SPREAD",
    StrategyType.PROTECTIVE_PUT: "PROTECTIVE PUT",
    StrategyType.PROTECTIVE_COLLAR: "PROTECTIVE COLLAR",
    StrategyType.LONG_STRADDLE: "LONG STRADDLE",
    StrategyType.LONG_STRANGLE: "LONG STRANGLE",
    StrategyType.LONG_CALL: "LONG CALL",
    StrategyType.LONG_PUT: "LONG PUT",
    StrategyType.LONG_CALL_BUTTERFLY: "LONG CALL BUTTERFLY",
    StrategyType.SHORT_IRON_CONDOR: "SHORT IRON CONDOR",
    StrategyType.SHORT_IRON_BUTTERFLY: "SHORT IRON BUTTERFLY",
}


def evaluate_trade_proposal(
    proposal_obj: object,
    portfolio: Portfolio,
    quantitative_analysis: QuantitativeAnalysis,
    market_data: OptionChain,
    broker_capabilities: BrokerCapabilities | None,
    *,
    limits: RiskLimitsConfig | None = None,
    now: datetime,
) -> RiskDecisionResult:
    """The single deterministic choke point every trade must pass
    through. Never raises: every failure mode, anticipated or not, is
    converted to a `RiskDecisionResult` with `RiskDecision.REJECT` (or
    `HALT`) before it reaches the caller.

    `now` is **required** and must be the real wall clock (MD-001 fix):
    an earlier optional `now: datetime | None = None` silently fell back
    to `proposal.timestamp` — the proposal's own self-reported time,
    never independently verified — whenever a caller used the natural
    default-parameter calling convention. Since `market_data.timestamp`
    and `proposal.timestamp` are normally stamped moments apart at
    proposal-construction time, that fallback made the freshness gate
    report "fresh" regardless of how much real time had actually
    elapsed since the data was fetched. Making `now` mandatory closes
    the gap without introducing a second, time-dependent code path (a
    silent `datetime.now()` fallback would make this function's
    behavior depend on the wall clock at import time even in tests that
    never intended that) — every caller must now say explicitly what
    moment it considers "now"."""
    limits = limits or get_default_limits()
    try:
        return _evaluate(proposal_obj, portfolio, quantitative_analysis, market_data, broker_capabilities, limits, now)
    except Exception as exc:  # noqa: BLE001 - the deliberate fail-closed backstop
        return _reject(ReasonCode.REJECT_UNKNOWN_RISK, f"risk could not be positively established: {exc!r}")


def _evaluate(
    proposal_obj: object,
    portfolio: Portfolio,
    quantitative_analysis: QuantitativeAnalysis,
    market_data: OptionChain,
    broker_capabilities: BrokerCapabilities | None,
    limits: RiskLimitsConfig,
    now: datetime,
) -> RiskDecisionResult:
    # 0. Boundary guard: only a genuine, already-validated TradeProposal
    # instance may proceed — a dict, a subclass, or any other type is
    # rejected outright, exactly like every other boundary in this
    # codebase (src.llm.schemas.ensure_trade_proposal,
    # src.data.provider.ensure_canonical).
    try:
        proposal = ensure_trade_proposal(proposal_obj)
    except TypeError as exc:
        return _reject(ReasonCode.REJECT_MALFORMED_PROPOSAL, str(exc))

    if quantitative_analysis.proposal_id != proposal.proposal_id:
        return _reject(
            ReasonCode.REJECT_QUANT_MISMATCH,
            f"QuantitativeAnalysis.proposal_id={quantitative_analysis.proposal_id!r} does not match "
            f"TradeProposal.proposal_id={proposal.proposal_id!r}",
        )

    # 1. Kill switch first: a halted portfolio accepts no new risk,
    # regardless of how the trade under review looks.
    kill = check_kill_switch(portfolio, limits)
    if kill.halted:
        return _halt(kill.reason_code, kill.message or "portfolio halted")

    # 1.5. TS-004 fix: CLOSE/ROLL is not yet an implemented pipeline
    # capability -- `_build_approved_order` (and every downstream Order
    # Validator / Fidelity-ticket path) only knows how to construct an
    # opening order. Before this check existed, a CLOSE/ROLL proposal
    # that cleared every other risk check still received
    # decision=APPROVE with approved_order=None, and
    # validate_and_build_order_request's first, unguarded
    # `approved_order.quantity` access raised a plain AttributeError --
    # not this module's own OrderValidationError -- crashing the entire
    # pipeline call instead of producing any clean result. Rejecting
    # explicitly here, before any of that machinery runs, means a
    # CLOSE/ROLL proposal always gets a normal RiskDecisionResult like
    # any other proposal, never a crash.
    if proposal.action != TradeAction.OPEN:
        return _reject(
            ReasonCode.REJECT_UNSUPPORTED_ACTION,
            f"action={proposal.action.value!r} is not yet a supported pipeline capability; "
            "only OPEN proposals can be evaluated for an approved order",
        )

    as_of = now

    # 2. Market data identity + freshness.
    if market_data.underlying.symbol != proposal.ticker:
        return _reject(
            ReasonCode.REJECT_MALFORMED_PROPOSAL,
            f"market_data is for {market_data.underlying.symbol!r}, proposal is for {proposal.ticker!r}",
        )
    try:
        market_data.underlying.require_fresh(as_of, max_age=timedelta(minutes=limits.max_market_data_age_minutes))
    except StaleDataError as exc:
        return _reject(ReasonCode.REJECT_STALE_DATA, str(exc))

    # 3. Broker capability — never inferred.
    if broker_capabilities is None:
        return _reject(ReasonCode.REJECT_ACCOUNT_CAPABILITY, "no broker capability explicitly configured")
    if not broker_capabilities.options_enabled:
        return _reject(ReasonCode.REJECT_ACCOUNT_CAPABILITY, f"{broker_capabilities.broker_name}: options are not enabled")
    if proposal.strategy not in broker_capabilities.allowed_strategies:
        return _reject(
            ReasonCode.REJECT_ACCOUNT_CAPABILITY,
            f"{broker_capabilities.broker_name} is not configured to allow {proposal.strategy.value}",
        )
    if portfolio.account_alias is not None and portfolio.account_alias != broker_capabilities.account_alias:
        return _reject(
            ReasonCode.REJECT_BROKER_ACCOUNT_MISMATCH,
            f"portfolio account_alias={portfolio.account_alias!r} does not match broker_capabilities "
            f"account_alias={broker_capabilities.account_alias!r}",
        )

    # 4. Resolve every leg's live contract, requiring freshness.
    try:
        contracts = resolve_leg_contracts(proposal, market_data, as_of=as_of, max_age_minutes=limits.max_market_data_age_minutes)
    except ContractNotFoundError as exc:
        return _reject(ReasonCode.REJECT_INVALID_CONTRACT, str(exc))
    except StaleContractError as exc:
        return _reject(ReasonCode.REJECT_STALE_DATA, str(exc))

    # 5. Liquidity.
    try:
        check_all_legs_liquid(contracts, limits)
    except LiquidityError as exc:
        return _reject(ReasonCode.REJECT_LIQUIDITY, str(exc))

    # 6. Independently recompute economics — first at the requested
    # size (to cross-check the supplied QuantitativeAnalysis), then
    # per-contract (for sizing).
    try:
        requested_economics = compute_trade_economics(
            proposal, contracts, portfolio, limits, num_contracts=proposal.contracts_requested
        )
        per_contract_economics = compute_trade_economics(proposal, contracts, portfolio, limits, num_contracts=1)
    except MissingCollateralError as exc:
        return _reject(ReasonCode.REJECT_MISSING_COLLATERAL, str(exc))
    except UndefinedEconomicsError as exc:
        return _reject(ReasonCode.REJECT_UNDEFINED_MAX_LOSS, str(exc))

    # 7. Cross-check the supplied QuantitativeAnalysis against this
    # module's own independent recomputation.
    try:
        cross_check_quantitative_analysis(
            quantitative_analysis, requested_economics, limits, num_contracts=proposal.contracts_requested
        )
    except QuantMismatchError as exc:
        return _reject(ReasonCode.REJECT_QUANT_MISMATCH, str(exc))

    # 8. Drawdown zone -> sizing multiplier (tightens, never loosens).
    dd_pct = current_drawdown_pct(portfolio)
    zone = drawdown_zone(dd_pct, limits)
    multiplier = sizing_multiplier_for_zone(zone, limits)

    # 9. Position sizing: never more than requested.
    try:
        sized = size_trade(proposal.contracts_requested, per_contract_economics, portfolio, limits, risk_multiplier=multiplier)
    except UndefinedEconomicsError as exc:
        return _reject(ReasonCode.REJECT_UNDEFINED_MAX_LOSS, str(exc))

    if sized.contracts <= 0:
        if zone == DrawdownZone.RISK_REDUCTION:
            return _reject(
                ReasonCode.REJECT_DRAWDOWN_RISK_REDUCTION,
                f"portfolio drawdown {dd_pct:.2%} is in the risk-reduction zone and no contracts "
                "could be sized within the reduced risk budget",
            )
        return _reject(ReasonCode.REJECT_MAX_TRADE_RISK, "no contracts could be sized within the configured risk budget")

    try:
        final_economics = compute_trade_economics(proposal, contracts, portfolio, limits, num_contracts=sized.contracts)
    except (MissingCollateralError, UndefinedEconomicsError) as exc:  # pragma: no cover - already validated above
        return _reject(ReasonCode.REJECT_UNDEFINED_MAX_LOSS, str(exc))

    # 10. Collateral, specific to the sized quantity.
    try:
        check_collateral(proposal, portfolio, sized.contracts)
    except MissingCollateralError as exc:
        return _reject(ReasonCode.REJECT_MISSING_COLLATERAL, str(exc))

    # 11. Cash / buying power.
    if final_economics.capital_required > portfolio.cash:
        return _reject(
            ReasonCode.REJECT_BUYING_POWER,
            f"capital required (${final_economics.capital_required:,.2f}) exceeds available cash "
            f"(${portfolio.cash:,.2f})",
        )
    remaining_cash = portfolio.cash - final_economics.capital_required
    if remaining_cash / portfolio.nav < limits.min_cash_reserve_pct:
        return _reject(
            ReasonCode.REJECT_INSUFFICIENT_CASH,
            f"post-trade cash reserve would be {remaining_cash / portfolio.nav:.2%}, below the "
            f"{limits.min_cash_reserve_pct:.2%} minimum",
        )
    deployed_after_pct = (portfolio.nav - remaining_cash) / portfolio.nav
    if deployed_after_pct > limits.absolute_max_capital_deployed_pct:
        return _reject(
            ReasonCode.REJECT_BUYING_POWER,
            f"post-trade capital deployed would be {deployed_after_pct:.2%}, exceeding the absolute "
            f"maximum of {limits.absolute_max_capital_deployed_pct:.2%}",
        )

    # 12. Duplicate position.
    duplicate = find_duplicate_position(
        portfolio,
        ticker=proposal.ticker,
        strategy=proposal.strategy,
        expiration=proposal.expiration,
        strikes=frozenset(leg.strike for leg in proposal.legs),
    )
    if duplicate is not None:
        return _reject(
            ReasonCode.REJECT_DUPLICATE_POSITION,
            f"an identical open position already exists: {duplicate.position_id}",
        )

    # 13. Concentration.
    try:
        check_underlying_concentration(portfolio, proposal.ticker, final_economics.capital_required, limits)
        sector = portfolio.sector_by_ticker.get(proposal.ticker, "UNKNOWN")
        check_sector_concentration(portfolio, sector, final_economics.capital_required, limits)
    except UnderlyingConcentrationError as exc:
        return _reject(ReasonCode.REJECT_UNDERLYING_CONCENTRATION, str(exc))
    except SectorConcentrationError as exc:
        return _reject(ReasonCode.REJECT_SECTOR_CONCENTRATION, str(exc))

    # 14. Correlation.
    try:
        check_correlation(portfolio, proposal.ticker, limits)
    except CorrelationError as exc:
        return _reject(ReasonCode.REJECT_CORRELATION, str(exc))

    # 15. Stress testing: the new position's own worst case across the
    # required shock grid, added to existing positions' already-known
    # max_loss (a conservative static figure — see
    # src.risk.stress.worst_case_stress_loss's docstring).
    try:
        new_quant_position = _build_quant_position(proposal, contracts, sized.contracts, portfolio)
        spot = next(c.underlying_price for c in contracts if c.underlying_price)
        sigma = next(c.iv for c in contracts if c.iv)
        days_to_expiry = max((proposal.expiration - proposal.timestamp.date()).days, 1)
        t = days_to_expiry / 365.0
        new_worst_case = worst_case_stress_loss(new_quant_position, spot, sigma, t, limits.risk_free_rate)
    except (StopIteration, StressTestError) as exc:
        return _reject(ReasonCode.REJECT_STRESS_TEST_FAILURE, f"could not stress-test the proposed position: {exc}")

    existing_max_loss_total = sum(p.max_loss for p in portfolio.positions)
    combined_worst_case_pct = (existing_max_loss_total + new_worst_case) / portfolio.nav
    if combined_worst_case_pct > limits.max_stress_loss_pct_of_nav:
        return _reject(
            ReasonCode.REJECT_STRESS_TEST_FAILURE,
            f"worst-case stressed portfolio loss would be {combined_worst_case_pct:.2%} of NAV, "
            f"exceeding the {limits.max_stress_loss_pct_of_nav:.2%} limit",
        )

    # Decision.
    decision = RiskDecision.APPROVE if sized.capped_by == "requested" else RiskDecision.RESIZE
    reason_codes = [ReasonCode.APPROVED if decision == RiskDecision.APPROVE else ReasonCode.RESIZED_POSITION_RISK]

    # ApprovedOrder is broker-agnostic data (Step 12's Order Validator /
    # PaperBroker path needs one for an AUTOMATED broker exactly as much
    # as Fidelity's manual path does) — built for any approved OPEN
    # action, regardless of execution_mode. Only the human-readable
    # Fidelity ticket is MANUAL-specific.
    approved_order: ApprovedOrder | None = None
    fidelity_ticket: FidelityTradeTicket | None = None
    if proposal.action == TradeAction.OPEN:
        approved_order = _build_approved_order(
            proposal, contracts, sized.contracts, final_economics, broker_capabilities, as_of
        )
        if broker_capabilities.execution_mode == "MANUAL":
            fidelity_ticket = FidelityManualProvider().generate_trade_ticket(approved_order)

    return RiskDecisionResult(
        decision=decision,
        reason_codes=reason_codes,
        approved_contracts=sized.contracts,
        message=(
            f"{decision.value}: {sized.contracts} contract(s) "
            f"(requested {proposal.contracts_requested}, capped_by={sized.capped_by})"
        ),
        max_loss=final_economics.max_loss,
        max_profit=final_economics.max_profit,
        capital_required=final_economics.capital_required,
        worst_case_stress_loss_pct_of_nav=combined_worst_case_pct,
        approved_order=approved_order,
        fidelity_ticket=fidelity_ticket,
    )


def _build_quant_position(proposal: TradeProposal, contracts, num_contracts: int, portfolio: Portfolio) -> QuantPosition:
    legs = [
        QuantLeg(
            right=_QUANT_RIGHT[leg.right.value],
            strike=leg.strike,
            side=QuantSide.BUY if leg.side == LegSide.BUY else QuantSide.SELL,
            entry_price=contract.mid,
            quantity=num_contracts * leg.quantity_ratio,
        )
        for leg, contract in zip(proposal.legs, contracts)
    ]
    underlying_shares = 0
    underlying_cost_basis = 0.0
    if proposal.strategy in (StrategyType.COVERED_CALL, StrategyType.PROTECTIVE_PUT, StrategyType.PROTECTIVE_COLLAR):
        holding = portfolio.underlying_holdings.get(proposal.ticker)
        if holding is not None:
            underlying_shares = holding.shares
            underlying_cost_basis = holding.cost_basis
    return QuantPosition(legs=legs, underlying_shares=underlying_shares, underlying_cost_basis=underlying_cost_basis)


def _build_approved_order(
    proposal: TradeProposal,
    contracts,
    num_contracts: int,
    economics,
    broker_capabilities: BrokerCapabilities,
    as_of: datetime,
) -> ApprovedOrder:
    legs = [
        FidelityOrderLeg(
            action=FidelityLegAction.SELL_TO_OPEN if leg.side == LegSide.SELL else FidelityLegAction.BUY_TO_OPEN,
            put_call=_DATA_RIGHT[leg.right.value],
            strike=leg.strike,
            expiration=proposal.expiration,
            contracts=num_contracts * leg.quantity_ratio,
        )
        for leg in proposal.legs
    ]
    # Step 19A: generalized per-leg net bid/ask, replacing the original
    # single-short-plus-single-long lookup (which raised StopIteration
    # for a strategy with no short leg at all, e.g. long straddle/
    # strangle). Each leg contributes to the combo's credit-received
    # range: a short leg's own (bid, ask) directly; a long leg's own
    # (bid, ask) negated (a debit paid reduces the combo's net credit).
    # This exactly reproduces the original two-leg credit-spread formula
    # (verified: short.bid - long.ask / short.ask - long.bid) and
    # correctly extends to a net-debit result (a negative "credit") for
    # every Tier-1 debit strategy and to two-long-leg strategies.
    #
    # Step 20A: each leg is also weighted by its own `quantity_ratio` --
    # a flat 1 for every leg of every strategy before Step 20A (so this
    # is a no-op for all of them), but 2 for LONG_CALL_BUTTERFLY's
    # middle (short) leg, correctly pricing its 1:-2:1 net debit instead
    # of understating it as a 1:-1:1 combo would.
    net_bid = sum(
        (c.bid if leg.side == LegSide.SELL else -c.ask) * leg.quantity_ratio for leg, c in zip(proposal.legs, contracts)
    )
    net_ask = sum(
        (c.ask if leg.side == LegSide.SELL else -c.bid) * leg.quantity_ratio for leg, c in zip(proposal.legs, contracts)
    )
    net_mid = (net_bid + net_ask) / 2.0
    is_debit = net_mid < 0
    limit_price = abs(net_mid)

    if is_debit:
        # The worst (highest) debit a human would still accept paying —
        # required to be >= limit_price by ApprovedOrder's own validator
        # for a net-debit order (`estimated_credit_debit < 0`).
        minimum_acceptable_price = round(limit_price * 1.1, 2)
        # dollar exit target: close once the position is worth more than
        # was paid for it by profit_target's fraction (an approximation
        # for long-premium strategies, mirroring the credit-side formula
        # below rather than a second, strategy-specific derivation).
        profit_target_price = round(limit_price * (1.0 + proposal.profit_target), 2)
    else:
        minimum_acceptable_price = round(limit_price * 0.9, 2)
        # dollar exit target implied by TradeProposal.profit_target (a
        # fraction of the credit received to capture before closing).
        profit_target_price = round(limit_price * (1.0 - proposal.profit_target), 2)

    return ApprovedOrder(
        risk_approval_id=str(uuid.uuid4()),
        account_alias=broker_capabilities.account_alias,
        ticker=proposal.ticker,
        strategy=_STRATEGY_DISPLAY[proposal.strategy],
        underlying_price=next(c.underlying_price for c in contracts if c.underlying_price),
        expiration=proposal.expiration,
        legs=legs,
        quantity=num_contracts,
        limit_price=limit_price,
        minimum_acceptable_price=minimum_acceptable_price if minimum_acceptable_price > 0 else 0.01,
        estimated_credit_debit=net_mid,
        net_bid=net_bid,
        net_ask=net_ask,
        max_profit=economics.max_profit,
        max_loss=economics.max_loss,
        breakeven=economics.breakeven,
        breakeven_upper=economics.breakeven_upper,
        capital_at_risk=max(economics.capital_required, economics.max_loss),
        return_on_capital=economics.return_on_capital,
        profit_target=profit_target_price if profit_target_price > 0 else 0.01,
        loss_management_rule=f"Close or roll if loss approaches 2x the credit received (risk_approval generated {as_of.isoformat()}).",
        DTE_management_rule=f"Review/close/roll according to strategy rules at {proposal.management_dte} DTE.",
        management_dte=proposal.management_dte,
        timestamp=proposal.timestamp,
        market_data_timestamp=min(max(c.timestamp for c in contracts), proposal.timestamp),
    )
