"""Step 22.5 (PAPER_TRADING_V1.4.4): the Review-Only human-confirmation
boundary for new-position PaperBroker execution during the 90-day
validation.

Nothing in this package imports `src.llm.*` or calls
`src.orchestration.pipeline.run_order_pipeline` -- see `src.review
.confirmation`'s module docstring for the full reasoning. This package is
new and deliberately separate from `src.portfolio`, so nothing already
frozen in that package needs touching to add it.
"""
