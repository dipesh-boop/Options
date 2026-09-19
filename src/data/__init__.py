"""Normalized market data layer: canonical schemas every broker/data
provider must convert its raw response into, plus freshness enforcement.

Nothing in this package imports from src.llm — the Market Data Layer
sits strictly beneath the Multi-Agent Layer (ARCHITECTURE.md §3, §9),
the same one-way boundary src.quant holds. Raw broker/provider
responses (dicts, SDK objects) never leave a concrete provider's
implementation: every public method returns one of the canonical,
strict Pydantic types defined here, and `provider.ensure_canonical`
gives any downstream consumer (a future Strategy Screener, and
eventually the LLM-facing context builders in src.llm.context) a
runtime check that only a genuine canonical instance — never a raw dict
— ever crosses that boundary.
"""
