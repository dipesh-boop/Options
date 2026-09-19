"""Deterministic quantitative engine: Black-Scholes pricing, Greeks,
implied volatility, probability, expected value, Monte Carlo stress
testing, position sizing, and correlation analysis.

Nothing in this package imports from src.llm — Python Quant sits
strictly beneath the Multi-Agent Layer (ARCHITECTURE.md §5-§6). An LLM
must consume these calculations; it must never replace them with its own
arithmetic, and nothing in this package trusts an LLM-supplied number.

Every function here is pure: no I/O, no global state, and no randomness
except where a function is explicitly a Monte Carlo simulation (and even
then, reproducible when a seed is provided).
"""
