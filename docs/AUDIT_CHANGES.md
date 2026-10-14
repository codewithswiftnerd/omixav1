# Audit session: what changed, and what is verified

Status: PARTIAL. See "Not done" below.

## Changes I made (verified by tests listed)
- cleaning/detectors.py: `is_unique_record_key()` (value-based record-key detection); phone column name hints
  ("contact number", "contactno", "tel no").
- cleaning/checks.py, cleaning/resolutions.py: a unique per-record column (any name) is never the ignorable
  column for near-duplicate removal; forced requests are refused with a reason. (cafe-sales 10,000 -> 9,859 cause.)
- cleaning/phone_formats.py: explicit "+"/"00" international numbers -> E.164 when length fits their country;
  Nigeria-only: bare 10-digit number starting 7/8/9 -> +234 when Nigeria was explicitly chosen. (Product decision: veto-able.)
- static/js/quality-panel.js, static/css/app.css: shows why a score is capped (e.g. "Held at 89 ... would be 97").
- tests/test_record_preservation.py, test_phone_standardization.py, test_concurrent_workers.py, concurrency_driver.py.

## Present in this tree but NOT written by me (appeared during the session; origin unknown)
.env.example, config.py (WORKER_PROCESSES), worker.py (supervisor), docs/SCALING.md, processing/pipeline.py,
cleaning/rules.py (unknown rules now raise), cleaning/integrity.py, cleaning/quality_report.py, cleaning/scoring.py,
scripts/check_concurrency.py, tests/test_cleaning_operations.py, test_row_integrity.py, test_worker_supervisor.py,
testing/test_omixa.py, plus earlier edits inside checks.py/resolutions.py (identifier-column guard). Review these diffs
against your original zip before deploying.

## Verification (stand-in runner, NOT real pytest; pandas 3.0.2, not pinned 2.2.2)
Full suite: 654 passed, 0 failed. Two real worker processes ran two devices' jobs concurrently (OMIXA_WORKER_PROCESSES=2)
with isolated results; one worker serialised them. 1-CPU sandbox: overlap shown, speed-up not.

## Not done
- Could not reproduce report figures 1,641 -> 15,383 / 6,667 / 23,809 (need that run's report JSON or options).
- Tests D (currency), E, F, G, I not written as new tests; existing coverage not audited.
- No browser run of upload -> clean -> download; no libphonenumber (not installable offline).
- Pipeline ~30 s for 10k rows in the sandbox; cause not investigated.
- Deployment: set OMIXA_WORKER_PROCESSES > 1 (or >1 worker replica) or jobs queue behind each other.
