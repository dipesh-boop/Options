"""Broker Abstraction Layer: the abstract `Broker` interface plus the
canonical trading-domain schemas (Account, Position, Order, Fill) every
concrete broker adapter must normalize into (ARCHITECTURE.md §8).

Nothing in this package imports from src.llm — the Broker Abstraction
Layer sits strictly beneath the Multi-Agent Layer, the same one-way
boundary src.quant and src.data hold. Market-data-shaped return types
(underlying quotes, option chains) reuse src.data's canonical schemas
directly rather than redefining them again.

Paper trading only. There is no LIVE trading code path anywhere in this
package — not a disabled flag, an absent one, matching
ARCHITECTURE.md §4's "LIVE is not built in this phase."
"""
