# Omixa architecture (current)

This is the one current architecture document. Earlier assessments live in `docs/archive/` and are
kept for history only. `docs/SCALING.md` holds measurements; `docs/CLEANING_ENGINE.md` the rule engine.

Omixa is a data-quality and spreadsheet-cleaning product. Safe, explainable, reversible changes;
ambiguous data is reported, not guessed.

## 1. Request flow
```
Browser (templates + static JS)
  -> Flask API (gunicorn, I/O only)
       validate upload (extension, magic bytes, macros, zip-bomb guard, size, cells)
       job admission (per-principal active-job caps, queue soft/hard limits)
  -> Queue (Redis sorted set for wake-ups; PostgreSQL `jobs` table is the source of truth)
  -> Worker (worker.py): claim job (compare-and-set) -> lease + heartbeat -> isolated child process
       (timeout + memory cap) -> cleaning pipeline -> audit + quality reports -> result
  -> Object storage (private S3-compatible bucket): source + cleaned file; presigned downloads
  -> PostgreSQL: job lifecycle, leases, batches, audit_log, usage; Firestore: users, plans, profiles, history
```
Modes: `OMIXA_PROCESSING_MODE=inline|queue`, `OMIXA_STORAGE_BACKEND=local|s3`, `DATABASE_URL` empty = SQLite,
`REDIS_URL` empty = in-process. Inline/local/SQLite is for development and a deliberate single node.

**Production** (`FLASK_ENV=production`, queue mode) refuses to start without `OMIXA_STORAGE_BACKEND=s3` and
`DATABASE_URL`, because a file uploaded through one API instance must be processable by any worker.
(`OMIXA_ALLOW_SINGLE_NODE=1` overrides this for a one-machine deployment.) Required variables:
`OMIXA_SECRET_KEY`, `OMIXA_PROCESSING_MODE=queue`, `OMIXA_STORAGE_BACKEND=s3`, `OMIXA_S3_BUCKET`,
`OMIXA_S3_ENDPOINT_URL`/`OMIXA_S3_REGION`, `OMIXA_S3_ACCESS_KEY_ID`, `OMIXA_S3_SECRET_ACCESS_KEY`,
`DATABASE_URL`, `REDIS_URL`, Firebase and Paystack settings (see README).

## 2. Accounts and plans
* Free cleaning needs no account (anonymous signed-cookie session owns its jobs).
* Pro needs an account: batch processing, Quality Profiles, deeper analysis, PDF reports, saved history, larger files.
* Plan decisions are made on the server from the subscription record (`accounts/entitlements.py`).
  Nothing in a request (fields, headers, JS, localStorage) can grant Pro.

## 3. Pro batch processing
```
Pro user -> POST /api/batches/ (multipart, up to N files)
  -> Pro check (server) -> admission -> per file: same validation as a single upload
     (extension, magic bytes, macros, name sanitization, size, cells); invalid files are SKIPPED with a reason
  -> files stored under generated internal ids; one `jobs` row per file (own id/status/error/audit)
  -> `batches` row; the batch releases files to the existing queue a few at a time
     (OMIXA_BATCH_MAX_CONCURRENT_FILES); the request returns 202 immediately
  -> existing worker/pipeline processes each file; each finished job calls batches.advance()
  -> status = computed from the real job rows (queued / processing / completed / failed / cancelled)
  -> finalize once every file is terminal: batch status + one `batch.completed` audit entry
  -> download one file, or all COMPLETED files as a ZIP (+ batch_report.csv: COMPLETED / FAILED / SKIPPED)
```
* Aggregate status: `queued`, `processing`, `completed`, `completed_with_errors`, `failed`, `cancelled`.
* One failed file never fails the batch. `POST /api/batches/<id>/retry` re-runs failed files; `/cancel` stops waiting files.
* Audit: each file keeps its own result/audit (`GET /api/batches/<id>/files/<job>`). The batch records
  id, owner, created/completed time, file counts and total processing time in `audit_log`.
* Ownership: batches are bound to the owning account; others get 404. A file can only be reached through its own batch.
* ZIP safety: member names are generated (`name_cleaned.ext`, no directories, `..` or control characters, de-duplicated);
  report cells are formula-defused.
* Limits (env, all server-side): max files, total MB, per-file MB, per-file cells, total cells, concurrent files
  per batch, unfinished batches per user, batch timeout, retention. Total cells use a fast pre-scan
  (CSV/XLSX; `.xls` cannot be pre-scanned, its per-file cap is still enforced during processing).
* Batch outputs are kept for `OMIXA_BATCH_RETENTION_SECONDS` (default 24 h) and downloads do not delete them;
  single-file jobs are removed after `OMIXA_JOB_TTL_SECONDS` (default 30 min).
* Batch files cannot be re-processed or downloaded through the single-file endpoints (409).
* Needs queue mode; otherwise the API answers 503.
* Batch applies the default safe rules (optionally "Standardize column names"). Ambiguities are reported in
  each file's audit, not auto-resolved; per-file approvals need the single-file flow.

## 4. Cleaning, audit, reporting
`processing/pipeline.py`: read -> profile/quality (before) -> rules (`cleaning/rules.py`, ordered) -> quality (after)
-> audit/summary -> export. Column-name standardization is opt-in (not in `DEFAULT_RULES`); imputation is opt-in;
identifier-like columns are protected from numeric coercion; ambiguous dates are left for the user.
Countries: one canonical source, `cleaning/phone_formats.COUNTRIES` (+ `COUNTRY_ALIASES`), used by the country rule,
phone engine and resolutions. The frontend never carries its own list.

## 5. Excel fidelity
See `docs/EXCEL_FIDELITY.md` (what is and is not preserved).

## 6. Failure behaviour
| Failure | Result |
|---|---|
| API instance dies | Load balancer removes it (`/readyz`); jobs live in DB/bucket |
| Worker dies mid-job | Lease expires -> reaper re-queues with backoff; after `JOB_MAX_ATTEMPTS` -> failed + dead-letter; batch keeps going |
| Zombie worker finishes late | Writes are compare-and-set on lease owner: rejected |
| Redis down | Rate limits per process; queue falls back to DB polling |
| Bucket error | Upload -> 503; worker attempt retried with backoff |
| Batch stuck | Reaper calls `advance()` on unfinished batches; `OMIXA_BATCH_TIMEOUT_SECONDS` cancels waiting files |
| Traffic spike | Per-principal caps -> 429; soft queue limit -> notice; hard limit -> 503 + Retry-After |

## 7. Data handling (what the product may say)
Files are stored temporarily and deleted automatically (single files: within the TTL; batches: retention period).
Metadata kept: file name, size, row counts, quality scores, rules run. Pro history: figures and findings only,
never rows or example values. No claim of "never stored" or "deleted right after download" is made anywhere.
