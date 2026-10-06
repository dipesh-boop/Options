"""PAPER_TRADING_V1.5.12 regression test (item G of the task spec):
proves, at the BEHAVIOR level against the real production entry point
`scripts/run_validation_cycle.py`, that the validation-cycle runner no
longer supplies the opportunity scan a `proposal_id_prefix` that
already duplicates the scan date.

Root cause (observed on the official 2026-10-06 validation cycle):
`run_validation_cycle.py` called the opportunity scan with
`proposal_id_prefix=f"validation-scan-{now.date().isoformat()}"`, but
`src.workflows.candidate_generation._next_id()` INDEPENDENTLY appends
`now.date().isoformat()` to every proposal_id it builds (see that
function's own SY-001 comment). The date was therefore encoded twice,
pushing a realistic multi-leg PUT_CREDIT_SPREAD proposal_id (ticker +
two strikes) past `TradeProposal.proposal_id`'s `max_length=64` and
failing every such candidate with a Pydantic `ValidationError` before
it ever reached Quant/Risk.

This test intercepts the REAL `src.portfolio.orchestrator
.scan_and_rank_opportunities` call the production runner makes --
never a source-text/string match against the script file -- and
asserts the `proposal_id_prefix` value it actually receives at runtime
does not already carry the date `_next_id()` is about to append. A
literal string comparison against today's exact prefix text would
re-couple this test to incidental naming; checking the actual
no-double-date contract is the behavior that matters.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

import src.portfolio.orchestrator as orchestrator_module
from tests.acceptance.test_review_only_daily_cycle import (
    environment,  # noqa: F401 -- reused as a pytest fixture
    scripts,  # noqa: F401 -- reused as a pytest fixture
)


@pytest.mark.asyncio
class TestProductionRunnerDoesNotDuplicateScanDateInProposalIdPrefix:
    async def test_runner_supplies_a_prefix_that_next_id_can_safely_date_stamp(self, scripts):
        cycle, _confirm = scripts
        captured: dict = {}
        real_scan = orchestrator_module.scan_and_rank_opportunities

        def _capturing_scan(*args, **kwargs):
            captured["proposal_id_prefix"] = kwargs.get("proposal_id_prefix")
            captured["now"] = kwargs.get("now")
            return real_scan(*args, **kwargs)

        with patch.object(orchestrator_module, "scan_and_rank_opportunities", _capturing_scan):
            ok = await cycle.run_validation_cycle()

        assert ok is True
        assert "proposal_id_prefix" in captured, (
            "scan_and_rank_opportunities was never called -- this test cannot "
            "prove anything about the prefix it would have received"
        )
        prefix = captured["proposal_id_prefix"]
        today_iso = captured["now"].date().isoformat()
        assert not prefix.endswith(today_iso), (
            "proposal_id_prefix must not already carry the scan date -- "
            "_next_id() independently appends `now.date().isoformat()` to "
            "every proposal_id it builds, so a caller-supplied prefix that "
            "already ends with the date would double-encode it, exactly "
            "like the V1.5.11 October 6 PUT_CREDIT_SPREAD incident "
            f"(captured prefix: {prefix!r})"
        )
