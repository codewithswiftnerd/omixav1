# Audit session: what changed, and what is verified

## Changes I made
**Record loss (cafe-sales 10,000 -> 9,859).** `detectors.is_unique_record_key()`; `checks.py` / `resolutions.py` never treat a
unique per-record column (any name) as the ignorable column for near-duplicate removal; a forced request is refused with a reason.
**Phone.** `phone_formats.py`: explicit "+"/"00" international numbers -> E.164 when length fits their country; Nigeria only, when
Nigeria was explicitly chosen: a bare 10-digit number starting 7/8/9 -> +234 (veto-able product decision). `detectors.py`: "Contact Number",
"ContactNo", "Tel No" trigger the country picker.
**Currency (BEHAVIOUR CHANGE, follows your spec).** `currencies.mixed_currency_markers()` / `currency_marker()`; `rules.py`: "Clean numbers" no
longer strips symbols from a column that mixes currencies (₦ with $, £, €...). The column is left exactly as uploaded and listed under
`left_unconverted` with reason `mixed_currencies`. `₦`/`NGN`/"Naira" and `$`/`USD` count as one currency. One existing test that deliberately
merged ₦ and $ into bare numbers (`test_currency_symbols_and_thousands_separators_still_convert_when_every_value_is_a_number`) was changed to
single-currency data. Revert the guard in `rules.py` if you want the old behaviour.
**Report.** `integrity.py`: new `issue_summary.unique_cells_changed` (exact, null if the per-cell log was truncated); `cells_normalised` definition
corrected (it counts operation-level changes, so a cell changed twice counts twice). `quality-panel.js` / `app.css`: shows why a score is capped.
**Performance.** Pure-function caches (`lru_cache`, 131,072 entries each) on `validation._try_named`, `currencies.strip_currency`,
`detectors.strip_numeric_noise`: real 10,000-row file 34.5 s -> ~10 s with a byte-identical cleaned CSV. Trade-off: recently seen cell strings stay in
worker memory until evicted.
**Tests added.** test_record_preservation, test_phone_standardization, test_concurrent_workers (+ concurrency_driver.py), test_spec_scenarios.

## Present in this tree but NOT written by me (appeared during the session; origin unknown)
.env.example, config.py (WORKER_PROCESSES), worker.py (supervisor), docs/SCALING.md, processing/pipeline.py, cleaning/rules.py (unknown rules raise),
cleaning/integrity.py, cleaning/quality_report.py, cleaning/scoring.py, scripts/check_concurrency.py, tests/test_cleaning_operations.py,
test_row_integrity.py, test_worker_supervisor.py, testing/test_omixa.py, and earlier edits in checks.py / resolutions.py (identifier-column guard).
Review these diffs against your original zip before deploying.

## Verification (stand-in runner, NOT real pytest; pandas 3.0.2, not the pinned 2.2.2)
Full suite: 686 passed, 0 failed. Real 10,000-row file through the HTTP API (upload -> process -> poll -> download): 10,000 rows, all IDs present,
second device gets 404/404. Two real worker processes ran two devices' jobs concurrently with isolated results; one worker serialised them.

## Not reproduced / not done
- Report figures 15,383 / 6,667 / 23,809: not reproducible on this code with default rules, imputation, all rules, or configured markers
  (max "remaining" 7,818). "1,641 detected" DOES match the default run exactly. Send that run's report JSON or options to trace the rest.
- Tests E/F/G are characterisation tests of existing behaviour, not guards for new code.
- No browser run (API journey only); no libphonenumber (not installable offline); 1-CPU sandbox shows concurrency, not speed-up.
- Deployment: set OMIXA_WORKER_PROCESSES > 1 (or run >1 worker replica) or jobs queue behind each other.
