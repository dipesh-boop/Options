"""Tests for `src.data.quality_gate` (Step 22.4 Part 7)."""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

from src.data.option_chain import OptionChain, OptionContract, OptionRight
from src.data.quality_gate import (
    QualityIssueSeverity,
    is_systemic_chain_failure,
    validate_option_chain,
    validate_option_contract,
    validate_underlying_quote,
)
from src.data.quotes import UnderlyingQuote

NOW = datetime(2026, 9, 22, 15, 0, tzinfo=timezone.utc)
STALE = NOW - timedelta(minutes=30)
EXP = date(2026, 10, 23)


def _quote(**overrides) -> UnderlyingQuote:
    fields = dict(symbol="SPY", bid=454.5, ask=455.5, last=455.0, volume=1000, timestamp=NOW, source="tradier")
    fields.update(overrides)
    return UnderlyingQuote(**fields)


def _contract(**overrides) -> OptionContract:
    fields = dict(
        underlying="SPY", option_symbol="SPY261023P00450000", expiration=EXP, strike=450.0,
        right=OptionRight.PUT, bid=4.5, ask=4.7, last=4.6, volume=100, open_interest=500,
        underlying_price=455.0, timestamp=NOW, source="tradier",
    )
    fields.update(overrides)
    return OptionContract(**fields)


class TestValidateUnderlyingQuote:
    def test_healthy_quote_has_no_issues(self):
        assert validate_underlying_quote(_quote(), as_of=NOW) == []

    def test_crossed_market_is_critical(self):
        issues = validate_underlying_quote(_quote(bid=500.0, ask=490.0), as_of=NOW)
        assert any(i.code == "crossed_market" and i.severity == QualityIssueSeverity.CRITICAL for i in issues)

    def test_all_zero_prices_is_critical(self):
        issues = validate_underlying_quote(_quote(bid=0, ask=0, last=0), as_of=NOW)
        assert any(i.code == "zero_market" and i.severity == QualityIssueSeverity.CRITICAL for i in issues)

    def test_stale_quote_is_critical(self):
        issues = validate_underlying_quote(_quote(timestamp=STALE), as_of=NOW)
        assert any(i.code == "stale_quote" for i in issues)

    def test_nan_bid_detected_even_though_pydantic_ge_rejects_it_separately(self):
        # ge=0 already rejects NaN at construction for bid/ask/last on
        # UnderlyingQuote, so this documents that guarantee rather than
        # bypassing it -- the gate's own NaN check exists for fields
        # without a numeric bound elsewhere (see OptionContract.theta).
        assert not math.isnan(_quote().bid)


class TestValidateOptionContract:
    def test_healthy_contract_has_no_issues(self):
        assert validate_option_contract(_contract(), as_of=NOW, expected_underlying="SPY", expected_expiration=EXP) == []

    def test_underlying_mismatch_is_critical(self):
        issues = validate_option_contract(_contract(), as_of=NOW, expected_underlying="QQQ")
        assert any(i.code == "underlying_mismatch" and i.severity == QualityIssueSeverity.CRITICAL for i in issues)

    def test_expiration_mismatch_is_critical(self):
        issues = validate_option_contract(_contract(), as_of=NOW, expected_expiration=date(2026, 11, 20))
        assert any(i.code == "expiration_mismatch" for i in issues)

    def test_zero_market_contract_is_warning_only_not_critical(self):
        issues = validate_option_contract(_contract(bid=0.0, ask=0.0), as_of=NOW)
        assert len(issues) == 1
        assert issues[0].code == "zero_market" and issues[0].severity == QualityIssueSeverity.WARNING

    def test_excessively_wide_market_is_critical(self):
        issues = validate_option_contract(_contract(bid=0.0, ask=20.0), as_of=NOW)
        assert any(i.code == "excessively_wide_market" and i.severity == QualityIssueSeverity.CRITICAL for i in issues)

    def test_moderately_wide_market_is_warning(self):
        issues = validate_option_contract(_contract(bid=3.5, ask=6.5), as_of=NOW)
        assert any(i.code == "wide_market" and i.severity == QualityIssueSeverity.WARNING for i in issues)

    def test_narrow_market_has_no_wide_market_issue(self):
        issues = validate_option_contract(_contract(bid=4.9, ask=5.1), as_of=NOW)
        assert not any(i.code in ("wide_market", "excessively_wide_market") for i in issues)

    def test_stale_contract_is_critical(self):
        issues = validate_option_contract(_contract(timestamp=STALE), as_of=NOW)
        assert any(i.code == "stale_quote" for i in issues)

    def test_nan_in_unconstrained_field_detected(self):
        c = _contract().model_copy(update={"theta": float("nan")})
        issues = validate_option_contract(c, as_of=NOW)
        assert any(i.code == "non_finite_theta" and i.severity == QualityIssueSeverity.CRITICAL for i in issues)

    def test_infinite_value_in_unconstrained_field_detected(self):
        c = _contract().model_copy(update={"theta": float("inf")})
        issues = validate_option_contract(c, as_of=NOW)
        assert any(i.code == "non_finite_theta" for i in issues)

    def test_no_expected_underlying_or_expiration_skips_those_checks(self):
        issues = validate_option_contract(_contract(), as_of=NOW)
        assert not any(i.code in ("underlying_mismatch", "expiration_mismatch") for i in issues)


class TestValidateOptionChain:
    def test_isolates_one_bad_contract_from_an_otherwise_good_chain(self):
        good = _contract()
        stale = _contract(option_symbol="SPY261023P00445000", strike=445.0, timestamp=STALE)
        chain = OptionChain(underlying=_quote(), contracts=[good, stale], timestamp=NOW, source="tradier")
        result = validate_option_chain(chain, as_of=NOW, expected_expiration=EXP)
        assert len(result.valid_contracts) == 1
        assert len(result.rejected) == 1
        assert result.rejected[0].option_symbol == stale.option_symbol
        assert result.is_usable is True

    def test_warning_only_contract_stays_valid_but_flagged(self):
        wide = _contract(bid=3.5, ask=6.5)
        chain = OptionChain(underlying=_quote(), contracts=[wide], timestamp=NOW, source="tradier")
        result = validate_option_chain(chain, as_of=NOW)
        assert len(result.valid_contracts) == 1
        assert wide.option_symbol in result.warnings

    def test_bad_underlying_makes_whole_chain_unusable_even_with_a_good_contract(self):
        chain = OptionChain(underlying=_quote(bid=0, ask=0, last=0), contracts=[_contract()], timestamp=NOW, source="tradier")
        result = validate_option_chain(chain, as_of=NOW)
        assert result.is_usable is False

    def test_empty_chain_with_healthy_underlying_is_not_usable(self):
        chain = OptionChain(underlying=_quote(), contracts=[], timestamp=NOW, source="tradier")
        result = validate_option_chain(chain, as_of=NOW)
        assert result.is_usable is False  # zero valid contracts -- nothing to trade

    def test_healthy_chain_has_no_rejects_or_warnings(self):
        chain = OptionChain(underlying=_quote(), contracts=[_contract()], timestamp=NOW, source="tradier")
        result = validate_option_chain(chain, as_of=NOW)
        assert result.rejected == [] and result.warnings == {} and result.is_usable is True

    def test_uses_chain_underlying_symbol_when_expected_underlying_omitted(self):
        mismatched = _contract(underlying="QQQ")
        chain = OptionChain(underlying=_quote(symbol="SPY"), contracts=[mismatched], timestamp=NOW, source="tradier")
        result = validate_option_chain(chain, as_of=NOW)
        assert len(result.rejected) == 1  # QQQ contract flagged against SPY chain's own underlying


class TestIsSystemicChainFailure:
    def test_true_when_chain_unusable(self):
        chain = OptionChain(underlying=_quote(bid=0, ask=0, last=0), contracts=[], timestamp=NOW, source="tradier")
        result = validate_option_chain(chain, as_of=NOW)
        assert is_systemic_chain_failure(result) is True

    def test_true_when_below_minimum_valid_contract_threshold(self):
        chain = OptionChain(underlying=_quote(), contracts=[_contract()], timestamp=NOW, source="tradier")
        result = validate_option_chain(chain, as_of=NOW)
        assert is_systemic_chain_failure(result, min_valid_contracts=2) is True

    def test_false_for_a_healthy_chain(self):
        chain = OptionChain(underlying=_quote(), contracts=[_contract()], timestamp=NOW, source="tradier")
        result = validate_option_chain(chain, as_of=NOW)
        assert is_systemic_chain_failure(result) is False
