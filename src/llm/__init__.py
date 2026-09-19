"""LLM orchestration layer: Anthropic API client, configurable model
router, strict Pydantic I/O schemas, prompt assembly, and read-only
context builders for the Multi-Agent Layer (see ARCHITECTURE.md §5).

Nothing in this package places or modifies a broker order. The only
schema an agent may emit that is ever allowed near an order is
`src.llm.schemas.TradeProposal`, and even that is inert intent data
until a not-yet-implemented Python Quant / Python Risk Engine reprices
and gates it (see IMPLEMENTATION_PLAN.md Phase 1 and Phase 5).
"""
