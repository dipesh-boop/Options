"""The 90-day forward paper-trading validation protocol (Step 19).

Purpose, stated explicitly and enforced structurally throughout this
package: this protocol does NOT exist to prove the ~12-15% research
target (`ARCHITECTURE.md` Section 1) is achievable. It exists to
objectively determine, from a real 90-calendar-day forward run against
this platform's own paper-trading machinery, whether the strategy shows
positive risk-adjusted expectancy, controlled drawdowns, disciplined
rule-following, realistic execution, acceptable tail risk, and
robustness across market regimes — and whether continued research is
justified. `src/validation/gates.py`'s 90-day gate never reads
`config/validation.yaml`'s `research_targets` section for its
classification; see that module's own docstring.

No module in this package makes an LLM call, computes a number an LLM
is trusted to override, or contains a live-trading code path — this
protocol runs entirely on top of the existing PaperBroker / Risk Engine
/ Quant Engine / backtest machinery already built and tested in Steps
1-18, exactly as `progress.md`'s Step 19 entry documents.
"""
