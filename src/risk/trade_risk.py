"""Single-trade deterministic risk: independently recomputed economics,
liquidity gating, collateral checks, and position sizing.

Every dollar figure the Risk Engine acts on for the trade under review is
computed here, in Python, from `src.quant` — never trusted verbatim from
an LLM-authored `TradeProposal` (which carries no price/economics fields
at all) and never trusted verbatim from the `QuantitativeAnalysis`
supplied as input either: `cross_check_quantitative_analysis` requires
this module's own independent recomputation to agree with it before
`src.risk.engine` will use it. A mismatch is treated as untrusted data,
not resolved in either input's favor.
"""
from __future__ import annotations

import math
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.data.option_chain import OptionChain, OptionContract, OptionRight as DataOptionRight
from src.data.provider import StaleDataError
from src.llm.schemas import LegSide, OptionLeg, StrategyType, TradeProposal
from src.quant.black_scholes import Leg as QuantLeg
from src.quant.black_scholes import OptionRight as QuantOptionRight
from src.quant.black_scholes import Side as QuantSide
from src.quant.expected_value import (
    StrategyEconomics,
    bear_put_spread_economics,
    bull_call_spread_economics,
    call_credit_spread_economics,
    covered_call_economics,
    csp_economics,
    long_call_economics,
    long_put_economics,
    long_straddle_economics,
    long_strangle_economics,
    protective_collar_economics,
    protective_put_economics,
    put_credit_spread_economics,
)
from src.quant.greeks import Greeks, net_greeks
from src.quant.position_sizing import PositionSizeResult, cap_requested_contracts, fixed_fractional_size
from src.risk.limits import RiskLimitsConfig
from src.risk.portfolio_risk import Portfolio


class TradeRiskError(ValueError):
    """Base class for every reason trade-level economics/liquidity could
    not be established. `src.risk.engine` catches this family and maps
    it to a specific `ReasonCode` — it never propagates as an unhandled
    exception, and never results in an APPROVE."""


class ContractNotFoundError(TradeRiskError):
    pass


class StaleContractError(TradeRiskError):
    pass


class LiquidityError(TradeRiskError):
    pass


class MissingCollateralError(TradeRiskError):
    pass


class UndefinedEconomicsError(TradeRiskError):
    pass


class QuantMismatchError(TradeRiskError):
    pass


class QuantitativeAnalysis(BaseModel):
    """What an upstream Python Quant pass already computed for this
    proposal (ARCHITECTURE.md §6/§9 data flow: Python Quant runs before
    Python Risk Engine). The Risk Engine treats this as a claim to
    verify, not a fact to trust — see `cross_check_quantitative_analysis`
    below."""

    model_config = ConfigDict(frozen=True)

    proposal_id: str = Field(min_length=1, max_length=64)
    generated_at: datetime
    max_profit: float
    max_loss: float
    breakeven: float
    capital_required: float
    return_on_capital: float
    annualized_roc: float
    probability_of_profit: float
    expected_value: float
    net_delta: float
    net_vega: float
    # Step 19A addition: additive, defaults to None. Set only for the
    # two-sided strategies (long straddle, long strangle) -- `breakeven`
    # above is the LOWER of the two in that case, mirroring
    # `src.quant.expected_value.StrategyEconomics`'s own addition.
    breakeven_upper: float | None = None

    @field_validator(
        "max_loss",
        "breakeven",
        "capital_required",
        "return_on_capital",
        "annualized_roc",
        "probability_of_profit",
        "expected_value",
        "net_delta",
        "net_vega",
    )
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError(f"QuantitativeAnalysis field must be finite, got {v!r}")
        return v

    @field_validator("breakeven_upper")
    @classmethod
    def _breakeven_upper_finite(cls, v: float | None) -> float | None:
        if v is not None and not math.isfinite(v):
            raise ValueError(f"breakeven_upper must be finite, got {v!r}")
        return v

    @field_validator("max_profit")
    @classmethod
    def _max_profit_finite_or_positive_infinity(cls, v: float) -> float:
        """The one field this module allows to be non-finite, and only
        in the +inf direction: a long call / long straddle / long
        strangle / protective put's upside genuinely has no cap (see
        `src.quant.expected_value.long_call_economics` and its
        siblings). -inf and NaN are still rejected outright -- an
        "unbounded loss" or a not-a-number max_profit is never a valid
        claim for any strategy this platform allows (no naked short
        calls, no unfunded naked puts, no unlimited-risk strategies)."""
        if math.isnan(v):
            raise ValueError("max_profit cannot be NaN")
        if v == -math.inf:
            raise ValueError("max_profit cannot be -infinity")
        return v

    @field_validator("generated_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        return v


def _find_contract(
    market_data: OptionChain, *, underlying: str, expiration: date, strike: float, right: DataOptionRight
) -> OptionContract:
    """MD-003 fix: matches on `(underlying, expiration, strike, right)`,
    not just `(expiration, strike, right)`. Without the ticker check, a
    caller that accidentally supplied a chain/quote set for the wrong
    underlying (e.g. a dict keyed by ticker string but never cross-
    validated against `chain.underlying.symbol`) combined with a
    coincidentally-matching strike/expiration/right -- very plausible
    for common round strikes and standard monthly expirations across
    large-caps -- would otherwise silently resolve to a completely
    different underlying's contract with no error at all."""
    for c in market_data.contracts:
        if c.underlying == underlying and c.expiration == expiration and c.strike == strike and c.right == right:
            return c
    raise ContractNotFoundError(
        f"no contract in market_data for underlying={underlying!r} expiration={expiration} "
        f"strike={strike} right={right.value!r}"
    )


def require_fresh_contract(contract: OptionContract, *, as_of: datetime, max_age_minutes: float) -> OptionContract:
    from datetime import timedelta

    try:
        return contract.require_fresh(as_of, max_age=timedelta(minutes=max_age_minutes))
    except StaleDataError as exc:
        raise StaleContractError(str(exc)) from exc


def check_liquidity(contract: OptionContract, limits: RiskLimitsConfig) -> None:
    if contract.open_interest is None or contract.open_interest < limits.min_open_interest:
        raise LiquidityError(
            f"{contract.option_symbol}: open_interest={contract.open_interest} "
            f"below minimum {limits.min_open_interest}"
        )
    if contract.volume is None or contract.volume < limits.min_volume:
        raise LiquidityError(
            f"{contract.option_symbol}: volume={contract.volume} below minimum {limits.min_volume}"
        )
    if contract.bid <= 0 or contract.ask <= 0:
        raise LiquidityError(f"{contract.option_symbol}: no two-sided market (bid={contract.bid}, ask={contract.ask})")
    mid = contract.mid
    spread_pct = (contract.ask - contract.bid) / mid if mid > 0 else math.inf
    if spread_pct > limits.max_bid_ask_spread_pct:
        raise LiquidityError(
            f"{contract.option_symbol}: bid/ask spread {spread_pct:.2%} exceeds maximum "
            f"{limits.max_bid_ask_spread_pct:.2%}"
        )


_DATA_RIGHT = {"C": DataOptionRight.CALL, "P": DataOptionRight.PUT}


def _leg_data_right(leg: OptionLeg) -> DataOptionRight:
    return _DATA_RIGHT[leg.right.value]


def resolve_leg_contracts(
    proposal: TradeProposal, market_data: OptionChain, *, as_of: datetime, max_age_minutes: float
) -> list[OptionContract]:
    """Every leg's live contract, resolved and required-fresh. Raises
    `ContractNotFoundError`/`StaleContractError` (both `TradeRiskError`)
    on any leg that cannot be positively resolved — there is no partial
    result."""
    resolved = []
    for leg in proposal.legs:
        contract = _find_contract(
            market_data, underlying=proposal.ticker, expiration=proposal.expiration, strike=leg.strike, right=_leg_data_right(leg)
        )
        resolved.append(require_fresh_contract(contract, as_of=as_of, max_age_minutes=max_age_minutes))
    return resolved


def check_all_legs_liquid(contracts: list[OptionContract], limits: RiskLimitsConfig) -> None:
    for contract in contracts:
        check_liquidity(contract, limits)


def check_collateral(proposal: TradeProposal, portfolio: Portfolio, contracts: int) -> None:
    """Verifies the specific collateral each strategy requires is
    actually present — distinct from, and in addition to, the general
    cash-reserve/buying-power checks in `src.risk.engine`."""
    if proposal.strategy == StrategyType.CASH_SECURED_PUT:
        short_leg = proposal.legs[0]
        required_cash = short_leg.strike * 100 * contracts
        if portfolio.cash < required_cash:
            raise MissingCollateralError(
                f"cash-secured put requires ${required_cash:,.2f} cash collateral; "
                f"portfolio has ${portfolio.cash:,.2f} cash"
            )
    elif proposal.strategy == StrategyType.COVERED_CALL:
        holding = portfolio.underlying_holdings.get(proposal.ticker)
        required_shares = 100 * contracts
        if holding is None or holding.shares < required_shares:
            held = holding.shares if holding else 0
            raise MissingCollateralError(
                f"covered call requires {required_shares} shares of {proposal.ticker}; "
                f"portfolio holds {held}"
            )
    elif proposal.strategy in (
        StrategyType.PUT_CREDIT_SPREAD,
        StrategyType.CALL_CREDIT_SPREAD,
        StrategyType.BULL_CALL_SPREAD,
        StrategyType.BEAR_PUT_SPREAD,
    ):
        # Defined-risk verticals: the opposite leg itself is the
        # collateral once both legs are held; no separate share/cash
        # requirement beyond the capital_required already enforced as
        # buying power.
        return

    elif proposal.strategy == StrategyType.PROTECTIVE_PUT:
        holding = portfolio.underlying_holdings.get(proposal.ticker)
        required_shares = 100 * contracts
        if holding is None or holding.shares < required_shares:
            held = holding.shares if holding else 0
            raise MissingCollateralError(
                f"protective_put requires {required_shares} shares of {proposal.ticker} already held "
                f"(this strategy protects an existing position, it does not establish one); "
                f"portfolio holds {held}"
            )

    elif proposal.strategy == StrategyType.PROTECTIVE_COLLAR:
        holding = portfolio.underlying_holdings.get(proposal.ticker)
        required_shares = 100 * contracts
        if holding is None or holding.shares < required_shares:
            held = holding.shares if holding else 0
            raise MissingCollateralError(
                f"protective_collar requires {required_shares} shares of {proposal.ticker} already held; "
                f"portfolio holds {held}"
            )

    elif proposal.strategy in (
        StrategyType.LONG_STRADDLE,
        StrategyType.LONG_STRANGLE,
        StrategyType.LONG_CALL,
        StrategyType.LONG_PUT,
    ):
        # Pure long-premium strategies: fully paid for at entry (the
        # debit is the only cash consumed), same reasoning as a long
        # leg within a vertical spread above. No share/cash collateral
        # requirement beyond the buying-power check src.risk.engine
        # already runs against capital_required.
        return

    else:  # pragma: no cover - StrategyType is exhaustive today
        raise MissingCollateralError(f"no collateral rule defined for strategy {proposal.strategy!r}")


def resolve_credit(proposal: TradeProposal, contracts: list[OptionContract]) -> float:
    """The net credit (or, for a debit strategy, net debit — always
    returned as a positive magnitude, the same convention the original
    three strategies established) per share this module prices the
    structure at, from current market mids — the same value
    `compute_trade_economics` uses internally, exposed separately so
    `src.risk.engine` can build a `FidelityTradeTicket`'s limit/net-bid/
    net-ask fields from the exact same, single source of truth rather
    than re-deriving it."""
    if proposal.strategy in (
        StrategyType.CASH_SECURED_PUT, StrategyType.COVERED_CALL,
        StrategyType.PROTECTIVE_PUT, StrategyType.LONG_CALL, StrategyType.LONG_PUT,
    ):
        return contracts[0].mid

    if proposal.strategy == StrategyType.PUT_CREDIT_SPREAD:
        _, short_contract = _matching(proposal.legs, contracts, LegSide.SELL)
        _, long_contract = _matching(proposal.legs, contracts, LegSide.BUY)
        credit = short_contract.mid - long_contract.mid
        if credit <= 0:
            raise UndefinedEconomicsError(
                f"put credit spread nets to a non-positive credit ({credit:.4f}) from current market prices"
            )
        return credit

    if proposal.strategy == StrategyType.CALL_CREDIT_SPREAD:
        _, short_contract = _matching(proposal.legs, contracts, LegSide.SELL)
        _, long_contract = _matching(proposal.legs, contracts, LegSide.BUY)
        credit = short_contract.mid - long_contract.mid
        if credit <= 0:
            raise UndefinedEconomicsError(
                f"call credit spread nets to a non-positive credit ({credit:.4f}) from current market prices"
            )
        return credit

    if proposal.strategy in (StrategyType.BULL_CALL_SPREAD, StrategyType.BEAR_PUT_SPREAD):
        _, short_contract = _matching(proposal.legs, contracts, LegSide.SELL)
        _, long_contract = _matching(proposal.legs, contracts, LegSide.BUY)
        debit = long_contract.mid - short_contract.mid
        if debit <= 0:
            raise UndefinedEconomicsError(
                f"{proposal.strategy.value} nets to a non-positive debit ({debit:.4f}) from current market prices"
            )
        return debit

    if proposal.strategy == StrategyType.PROTECTIVE_COLLAR:
        _, call_contract = _matching_by_right(proposal.legs, contracts, DataOptionRight.CALL)
        _, put_contract = _matching_by_right(proposal.legs, contracts, DataOptionRight.PUT)
        return call_contract.mid - put_contract.mid  # net_credit; may legitimately be negative (a net-debit collar)

    if proposal.strategy in (StrategyType.LONG_STRADDLE, StrategyType.LONG_STRANGLE):
        _, call_contract = _matching_by_right(proposal.legs, contracts, DataOptionRight.CALL)
        _, put_contract = _matching_by_right(proposal.legs, contracts, DataOptionRight.PUT)
        return call_contract.mid + put_contract.mid  # total debit paid

    raise UndefinedEconomicsError(f"no credit formula for strategy {proposal.strategy!r}")  # pragma: no cover


def compute_trade_economics(
    proposal: TradeProposal,
    contracts: list[OptionContract],
    portfolio: Portfolio,
    limits: RiskLimitsConfig,
    *,
    num_contracts: int = 1,
) -> StrategyEconomics:
    """Independently recomputes `StrategyEconomics` for `num_contracts`
    of the proposed structure from the resolved, fresh market contracts —
    never from `proposal` alone (which has no price fields) and never by
    trusting a caller-supplied result."""
    spot = _underlying_spot(proposal, contracts)
    days_to_expiry = (proposal.expiration - proposal.timestamp.date()).days
    t = max(days_to_expiry, 1) / 365.0
    rate = limits.risk_free_rate
    credit = resolve_credit(proposal, contracts)

    if proposal.strategy == StrategyType.CASH_SECURED_PUT:
        sigma = _require_iv(contracts[0])
        return csp_economics(spot, proposal.legs[0].strike, credit, t, rate, sigma, max(days_to_expiry, 1), num_contracts)

    if proposal.strategy == StrategyType.COVERED_CALL:
        sigma = _require_iv(contracts[0])
        holding = portfolio.underlying_holdings.get(proposal.ticker)
        if holding is None:
            raise MissingCollateralError(f"no underlying share holding on record for {proposal.ticker}")
        return covered_call_economics(
            spot, proposal.legs[0].strike, credit, holding.cost_basis, t, rate, sigma, max(days_to_expiry, 1), num_contracts
        )

    if proposal.strategy == StrategyType.PUT_CREDIT_SPREAD:
        short_leg, short_contract = _matching(proposal.legs, contracts, LegSide.SELL)
        long_leg, _ = _matching(proposal.legs, contracts, LegSide.BUY)
        sigma = _require_iv(short_contract)
        return put_credit_spread_economics(
            spot, short_leg.strike, long_leg.strike, credit, t, rate, sigma, max(days_to_expiry, 1), num_contracts
        )

    if proposal.strategy == StrategyType.CALL_CREDIT_SPREAD:
        short_leg, short_contract = _matching(proposal.legs, contracts, LegSide.SELL)
        long_leg, _ = _matching(proposal.legs, contracts, LegSide.BUY)
        sigma = _require_iv(short_contract)
        return call_credit_spread_economics(
            spot, short_leg.strike, long_leg.strike, credit, t, rate, sigma, max(days_to_expiry, 1), num_contracts
        )

    if proposal.strategy == StrategyType.BULL_CALL_SPREAD:
        long_leg, long_contract = _matching(proposal.legs, contracts, LegSide.BUY)
        short_leg, _ = _matching(proposal.legs, contracts, LegSide.SELL)
        sigma = _require_iv(long_contract)
        return bull_call_spread_economics(
            spot, long_leg.strike, short_leg.strike, credit, t, rate, sigma, max(days_to_expiry, 1), num_contracts
        )

    if proposal.strategy == StrategyType.BEAR_PUT_SPREAD:
        long_leg, long_contract = _matching(proposal.legs, contracts, LegSide.BUY)
        short_leg, _ = _matching(proposal.legs, contracts, LegSide.SELL)
        sigma = _require_iv(long_contract)
        return bear_put_spread_economics(
            spot, long_leg.strike, short_leg.strike, credit, t, rate, sigma, max(days_to_expiry, 1), num_contracts
        )

    if proposal.strategy == StrategyType.PROTECTIVE_PUT:
        holding = portfolio.underlying_holdings.get(proposal.ticker)
        if holding is None:
            raise MissingCollateralError(f"no underlying share holding on record for {proposal.ticker}")
        sigma = _require_iv(contracts[0])
        return protective_put_economics(
            spot, proposal.legs[0].strike, credit, holding.cost_basis, t, rate, sigma, max(days_to_expiry, 1), num_contracts
        )

    if proposal.strategy == StrategyType.PROTECTIVE_COLLAR:
        holding = portfolio.underlying_holdings.get(proposal.ticker)
        if holding is None:
            raise MissingCollateralError(f"no underlying share holding on record for {proposal.ticker}")
        call_leg, call_contract = _matching_by_right(proposal.legs, contracts, DataOptionRight.CALL)
        put_leg, _ = _matching_by_right(proposal.legs, contracts, DataOptionRight.PUT)
        sigma = _require_iv(call_contract)
        return protective_collar_economics(
            spot, call_leg.strike, put_leg.strike, credit, holding.cost_basis, t, rate, sigma,
            max(days_to_expiry, 1), num_contracts,
        )

    if proposal.strategy == StrategyType.LONG_CALL:
        sigma = _require_iv(contracts[0])
        return long_call_economics(spot, proposal.legs[0].strike, credit, t, rate, sigma, max(days_to_expiry, 1), num_contracts)

    if proposal.strategy == StrategyType.LONG_PUT:
        sigma = _require_iv(contracts[0])
        return long_put_economics(spot, proposal.legs[0].strike, credit, t, rate, sigma, max(days_to_expiry, 1), num_contracts)

    if proposal.strategy == StrategyType.LONG_STRADDLE:
        call_leg, call_contract = _matching_by_right(proposal.legs, contracts, DataOptionRight.CALL)
        put_leg, put_contract = _matching_by_right(proposal.legs, contracts, DataOptionRight.PUT)
        sigma = _require_iv(call_contract)
        return long_straddle_economics(
            spot, call_leg.strike, call_contract.mid, put_contract.mid, t, rate, sigma, max(days_to_expiry, 1), num_contracts
        )

    if proposal.strategy == StrategyType.LONG_STRANGLE:
        call_leg, call_contract = _matching_by_right(proposal.legs, contracts, DataOptionRight.CALL)
        put_leg, put_contract = _matching_by_right(proposal.legs, contracts, DataOptionRight.PUT)
        sigma = _require_iv(call_contract)
        return long_strangle_economics(
            spot, call_leg.strike, put_leg.strike, call_contract.mid, put_contract.mid,
            t, rate, sigma, max(days_to_expiry, 1), num_contracts,
        )

    raise UndefinedEconomicsError(f"no economics formula for strategy {proposal.strategy!r}")  # pragma: no cover


def compute_trade_greeks(proposal: TradeProposal, contracts: list[OptionContract], portfolio: Portfolio) -> Greeks:
    spot = _underlying_spot(proposal, contracts)
    days_to_expiry = max((proposal.expiration - proposal.timestamp.date()).days, 1)
    t = days_to_expiry / 365.0
    quant_legs = []
    for leg, contract in zip(proposal.legs, contracts):
        sigma = _require_iv(contract)
        quant_legs.append(
            QuantLeg(
                right=QuantOptionRight.CALL if leg.right.value == "C" else QuantOptionRight.PUT,
                strike=leg.strike,
                side=QuantSide.BUY if leg.side == LegSide.BUY else QuantSide.SELL,
                entry_price=contract.mid,
                quantity=1,
            )
        )
    # net_greeks needs a single sigma; use the first (short) leg's IV as
    # the position-level vol input, consistent with compute_trade_economics.
    sigma = _require_iv(contracts[0])
    underlying_shares = 0
    if proposal.strategy in (StrategyType.COVERED_CALL, StrategyType.PROTECTIVE_PUT, StrategyType.PROTECTIVE_COLLAR):
        holding = portfolio.underlying_holdings.get(proposal.ticker)
        underlying_shares = holding.shares if holding else 0
    return net_greeks(quant_legs, spot, t, 0.0, sigma, underlying_shares=underlying_shares)


def cross_check_quantitative_analysis(
    supplied: QuantitativeAnalysis, recomputed: StrategyEconomics, limits: RiskLimitsConfig, *, num_contracts: int
) -> None:
    """Raises `QuantMismatchError` if the caller-supplied
    `QuantitativeAnalysis` disagrees with this module's own independent
    recomputation beyond `limits.quant_cross_check_tolerance_pct`. A
    mismatch means the supplied analysis was computed from different
    inputs (stale market data, a different proposal) than what is being
    evaluated right now — this module never resolves that in either
    side's favor, it rejects."""
    scaled_max_profit = recomputed.max_profit
    scaled_max_loss = recomputed.max_loss
    tol = limits.quant_cross_check_tolerance_pct

    def _within_tolerance(a: float, b: float) -> bool:
        # Both sides infinite (an unbounded-upside strategy correctly
        # reported by both the caller and this module's own
        # recomputation) is an exact agreement, not a mismatch -- a
        # naive (a-b)/b division would produce inf-inf=NaN here, which
        # `abs(...) <= tol` always evaluates False for, wrongly
        # rejecting two numbers that agree perfectly.
        if math.isinf(a) and math.isinf(b) and a == b:
            return True
        if math.isinf(a) or math.isinf(b):
            return False
        if b == 0:
            return abs(a) < 1e-6
        return abs(a - b) / abs(b) <= tol

    if not _within_tolerance(supplied.max_profit, scaled_max_profit):
        raise QuantMismatchError(
            f"supplied max_profit={supplied.max_profit:.2f} disagrees with recomputed "
            f"{scaled_max_profit:.2f} by more than {tol:.1%}"
        )
    if not _within_tolerance(supplied.max_loss, scaled_max_loss):
        raise QuantMismatchError(
            f"supplied max_loss={supplied.max_loss:.2f} disagrees with recomputed "
            f"{scaled_max_loss:.2f} by more than {tol:.1%}"
        )
    if not _within_tolerance(supplied.breakeven, recomputed.breakeven):
        raise QuantMismatchError(
            f"supplied breakeven={supplied.breakeven:.2f} disagrees with recomputed "
            f"{recomputed.breakeven:.2f} by more than {tol:.1%}"
        )
    if recomputed.breakeven_upper is not None:
        if supplied.breakeven_upper is None or not _within_tolerance(supplied.breakeven_upper, recomputed.breakeven_upper):
            raise QuantMismatchError(
                f"supplied breakeven_upper={supplied.breakeven_upper!r} disagrees with recomputed "
                f"{recomputed.breakeven_upper:.2f} by more than {tol:.1%}"
            )


def size_trade(
    requested_contracts: int,
    per_contract_economics: StrategyEconomics,
    portfolio: Portfolio,
    limits: RiskLimitsConfig,
    *,
    risk_multiplier: float = 1.0,
) -> PositionSizeResult:
    """The one place a contract count is ever bounded. Never returns
    more than `requested_contracts` (`cap_requested_contracts`'s own
    guarantee) — `risk_multiplier` (< 1 in a drawdown risk-reduction
    zone, see `src.risk.drawdown`) can only tighten the budget further,
    never loosen it."""
    max_loss_per_contract = per_contract_economics.max_loss
    capital_per_contract = per_contract_economics.capital_required
    if max_loss_per_contract <= 0:
        raise UndefinedEconomicsError("max_loss_per_contract must be positive to size a position")

    by_risk_budget = fixed_fractional_size(
        portfolio.nav,
        limits.target_risk_per_trade_pct * risk_multiplier,
        max_loss_per_contract,
        capital_per_contract=capital_per_contract,
        max_capital_pct=limits.normal_max_capital_deployed_pct,
    )
    absolute_cap = math.floor((portfolio.nav * limits.absolute_max_risk_per_trade_pct) / max_loss_per_contract)
    max_allowed = min(by_risk_budget.contracts, max(absolute_cap, 0))

    return cap_requested_contracts(requested_contracts, max_allowed, max_loss_per_contract, capital_per_contract)


def _require_iv(contract: OptionContract) -> float:
    if contract.iv is None or not math.isfinite(contract.iv) or contract.iv <= 0:
        raise UndefinedEconomicsError(
            f"{contract.option_symbol}: no usable implied volatility to price this structure"
        )
    return contract.iv


def _underlying_spot(proposal: TradeProposal, contracts: list[OptionContract]) -> float:
    for c in contracts:
        if c.underlying_price is not None and math.isfinite(c.underlying_price) and c.underlying_price > 0:
            return c.underlying_price
    raise UndefinedEconomicsError(f"no usable underlying price for {proposal.ticker}")


def _matching(legs: list[OptionLeg], contracts: list[OptionContract], side: LegSide) -> tuple[OptionLeg, OptionContract]:
    for leg, contract in zip(legs, contracts):
        if leg.side == side:
            return leg, contract
    raise UndefinedEconomicsError(f"no leg with side={side!r}")  # pragma: no cover - schema-enforced


def _matching_by_right(
    legs: list[OptionLeg], contracts: list[OptionContract], right: DataOptionRight
) -> tuple[OptionLeg, OptionContract]:
    for leg, contract in zip(legs, contracts):
        if _leg_data_right(leg) == right:
            return leg, contract
    raise UndefinedEconomicsError(f"no leg with right={right!r}")  # pragma: no cover - schema-enforced
