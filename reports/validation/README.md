# reports/validation/

Output directory for the 90-day paper-trading validation protocol
(`src/validation/`, Step 19). Nothing under this directory is source
code — it holds run artifacts a real validation session produces:

- `VALIDATION_MANIFEST.json` — the frozen `StrategyVersionManifest` for
  one run, written by `src.validation.protocol.save_manifest`.
- Daily/weekly/checkpoint/90-day reports a caller renders from
  `src.validation.scorecard.ValidationScorecard` and the other
  `src.validation.*` modules' outputs.
- A `SqliteValidationStore` database file (`src.validation.session`), if
  a run is configured to persist there rather than elsewhere.

This directory is intentionally empty of code in the repository itself
— this README exists only so the directory is tracked by git before any
run has produced output.
