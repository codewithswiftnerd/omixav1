# Omixa Backend

Every request is one job, backed by a temp folder scoped to the
browser session that uploaded it (see `utils/session.py`), deleted
after download (or after 30 min if abandoned). No user accounts. A
separate, protected `/admin` dashboard gives an operator aggregate
usage analytics — never a user's dataset contents — backed by a
small local sqlite database (`db.py`) for that metadata only.

## Structure

```
omixa-backend/
├── app.py              entry point: sessions, CORS, security headers, error handlers
├── config.py           env-driven settings: secrets, storage paths, limits
├── db.py                sqlite metadata store for /admin (jobs, errors, login attempts)
├── routes/
│   ├── upload.py        POST /api/upload         -> job_id
│   ├── process.py       POST /api/process/<id>    -> runs cleaning
│   ├── report.py        GET  /api/report/<id>     -> read-only quality report
│   ├── download.py      GET  /api/download/<id>   -> cleaned file
│   └── admin.py          /admin/*                  -> operator dashboard (session-gated)
├── processing/
│   └── pipeline.py      read -> clean -> export orchestration
├── cleaning/
│   ├── rules.py          auto-fix cleaning rules (see below)
│   ├── quality_report.py read-only diagnostics report (detect-only findings)
│   └── detectors.py      shared heuristics (email/phone/date/etc. detection)
├── export/
│   └── exporter.py      writes cleaned df back to csv/xlsx
├── templates/admin/      login + dashboard pages (server-rendered)
├── static/js/admin.js    dashboard data fetching/rendering
└── utils/
    ├── file_handler.py  job folders: save/find/delete temp files, upload content validation
    ├── session.py        anonymous per-browser sessions, job ownership, admin auth, CSRF
    └── security.py       rate limiting
```

## Flow

```
POST /api/upload            -> { job_id }                (session-owned)
GET  /api/report/<job_id>   -> { report }                 (owner only)
POST /api/process/<job_id>  -> { status, summary }        (owner only)
GET  /api/download/<job_id> -> cleaned file, then temp data is deleted (owner only)
```

"Owner only": every job is tagged with a hash of the session that
created it, and every other session gets an identical 404 — not a
distinguishable 403 — whether the `job_id` is real, expired, or just
belongs to someone else. See `utils/session.py` and
`routes/process.py`'s `owns()` note.

## Run locally

```
pip install -r requirements.txt
cp .env.example .env
# edit .env: set FLASK_ENV=development, OMIXA_SECRET_KEY to anything
# for local use, and OMIXA_ADMIN_USERNAME / OMIXA_ADMIN_PASSWORD_HASH
# if you want to try the /admin dashboard locally.
python app.py
```

Server starts on `http://localhost:5000`. `GET /api/health` for a
quick check it's alive (checks temp storage is writable and the
metadata db is reachable).

## Deploying (Railway, Render, etc.)

A `Procfile` is included:

```
web: gunicorn app:app --bind 0.0.0.0:$PORT --workers ${WEB_CONCURRENCY:-3} --threads 2 --timeout 120 --preload --access-logfile - --error-logfile -
```

Point the host at this repo, let it run `pip install -r
requirements.txt`, and it'll pick up the Procfile automatically on
Render/Railway/Heroku-style platforms. `app.py` exposes a module-level
`app` object for gunicorn to import directly (`app:app`), separate
from the `python app.py` dev entry point.

Before sharing a deployed link with anyone: set `OMIXA_SECRET_KEY`
(required — the app refuses to start in production without it, see
`config.py`), and set `OMIXA_ADMIN_USERNAME` /
`OMIXA_ADMIN_PASSWORD_HASH` if you want the `/admin` dashboard
reachable (see "Security" below for both). `Config.MAX_CONTENT_LENGTH`
(`OMIXA_MAX_UPLOAD_MB` env var) caps uploads at 25MB by default, bump
that if you expect larger files.

Storage note for multi-instance deploys: job temp files
(`OMIXA_TEMP_DIR`) and the metadata db (`OMIXA_DB_PATH`) are both
local disk. Multiple gunicorn *workers* on one instance share that
disk fine (the default `--workers 3` above is safe). Running more
than one *instance* behind a load balancer is not — each instance
would have its own temp files and its own metadata db, so a job
created on instance A wouldn't be visible from instance B. Single
instance, multiple workers is the supported shape for now; moving
`utils/file_handler.py`'s storage functions to an object store (S3,
etc.) and `db.py` to a shared database would remove that limit
without touching the cleaning engine or any route, since both are
already the sole seam routes go through for storage.

### Deploying to Railway

This repo also ships a `railway.json` (pins the start command
explicitly) and a `.python-version` (pins Python 3.12, matching
what `pandas==2.2.2` has wheels for, without it Railway may build
against a newer Python and fail to install pandas).

1. Push this `omixa-backend/` folder to a GitHub repo (or a
   subfolder of one, if it's a subfolder, set the Railway service's
   **Root Directory** to `omixa-backend`).
2. In Railway: **New Project → Deploy from GitHub repo** → pick the
   repo.
3. Railway auto-detects Python via Nixpacks and uses the
   `railway.json` start command / `Procfile`. No extra environment
   variables are required for this scaffold.
4. Once deployed, Railway assigns a URL under **Settings →
   Networking → Generate Domain**. That's your new app URL.
5. If you're running the frontend from Netlify separately, update
   `static/config.js` to point `OMIXA_API_BASE` at that Railway URL.
   If Flask is serving `static/` directly (the default), there's
   nothing else to change.

Railway's free tier doesn't cold-start/sleep an app the way Render's
free tier does (the "waking up" animation you see on Render after a
period of no traffic), a Railway deploy stays up, though free usage
is capped by monthly credit rather than uptime, so very low traffic
is cheap but the service can still be paused if that credit runs
out.

## Deploying the frontend to Netlify

Netlify only runs static sites and short-lived serverless functions, it can't run this Flask/pandas backend, which keeps job state on disk
across the upload → process → download requests. So the split is:

- **Backend** → Render/Railway (the Procfile above)
- **Frontend** (`static/`) → Netlify

Steps:

1. Deploy the backend to Render/Railway first and note its URL
   (e.g. `https://omixa-backend.onrender.com`).
2. Edit `static/config.js` and set:
   ```js
   window.OMIXA_API_BASE = "https://omixa-backend.onrender.com";
   ```
3. Push to Netlify. `netlify.toml` is already set to publish the
   `static/` folder with no build step, so a git-linked deploy or a
   drag-and-drop of the `static/` folder both work.

CORS on the backend is closed by default to any origin that isn't
explicitly listed once you set `ALLOWED_ORIGINS` (see "Security"
below) — set it to the Netlify site's real origin, and set
`OMIXA_SESSION_SAMESITE=None` on the backend, or the session cookie
(which is how job ownership and login now work, there is no API key
anymore) won't be sent cross-site. See `.env.example`'s "Cross-origin
deploys" section for the full checklist.

## Security

Every request gets a signed, `HttpOnly`, `SameSite` session cookie
(see `utils/session.py`) — that, not a shared secret, is what scopes
a job to the browser that created it and what an admin authenticates
with. Nothing capable of accessing another session's data or acting
as admin is ever present in frontend JS, HTML, or any browser-exposed
config file.

- **Job ownership.** Every uploaded job is tagged with an HMAC hash
  of the session that created it. `/api/process`, `/api/report`, and
  `/api/download` all check it: a request from a different session —
  even with a real, unexpired `job_id` — gets the exact same 404 as a
  `job_id` that doesn't exist, so a leaked or guessed id can't be
  distinguished from "no such job", let alone acted on.
- **Admin auth.** A single operator account, configured via
  `OMIXA_ADMIN_USERNAME` / `OMIXA_ADMIN_PASSWORD_HASH` (see
  `.env.example`), checked server-side with a timing-safe comparison.
  With either unset, `/admin` is unreachable — login fails closed,
  it does not fall open. A successful login rotates the whole
  session (defends against session fixation), and every admin
  state-changing endpoint (login, logout, the manual purge trigger)
  requires a matching CSRF token issued into that same session.
- **CSRF.** A per-session token (`utils/session.py`), verified via a
  hidden form field or an `X-CSRF-Token` header, required on every
  `POST` under `/admin`.
- **Rate limiting.** `RATE_LIMIT_PER_MINUTE` (default `30`) caps
  general `/api/*` traffic per client IP per rolling minute;
  `LOGIN_RATE_LIMIT_PER_MINUTE` (default `8`) is a separate, stricter
  bucket for admin login attempts specifically, so the two can't
  starve each other. Both are simple in-memory counters — cheap, zero
  extra dependencies — but tracked per worker process (so
  `--workers 3` gives roughly triple the effective limit) and don't
  coordinate across multiple instances. Treat this as a baseline, not
  a substitute for platform-level rate limiting at real scale. Set
  either to `0` to disable.
- **Upload validation.** Beyond the `.csv`/`.xlsx`/`.xls` extension
  whitelist, every upload's actual bytes are checked against what its
  extension claims (`utils/file_handler.validate_upload_content`):
  magic-byte checks for `.xlsx`/`.xls`, a real zip-structure check for
  `.xlsx` (including rejecting macro-enabled `.xlsm` content renamed
  to `.xlsx`), and a binary-content check for `.csv`. Filenames are
  sanitized and never used to build a filesystem path directly (every
  job's file is stored as `source.<ext>`, see `utils/file_handler.py`),
  so path traversal isn't reachable through the upload filename.
- **Security headers & CORS.** Every response gets
  `X-Content-Type-Options`, `X-Frame-Options: DENY`,
  `Referrer-Policy`, `Permissions-Policy`, and a `Content-Security-Policy`
  that only allows the app's own scripts (via a per-request nonce for
  the one inline script block, not `unsafe-inline`) — see `app.py`.
  `Strict-Transport-Security` is sent once `FLASK_ENV=production` (or
  `OMIXA_FORCE_HTTPS=1`). CORS is hand-rolled (no `flask-cors`
  dependency): `ALLOWED_ORIGINS` (default `*`) controls it, and
  credentialed (cookie-bearing) cross-origin responses are only ever
  sent to an explicit origin on that list — a wildcard origin never
  gets `Access-Control-Allow-Credentials`, since browsers refuse that
  combination anyway.
- **CSV/Excel formula-injection defense.** Every exported file
  (cleaned CSV/XLSX) has any cell starting with `=`, `+`, `-`, `@`,
  tab, or carriage return prefixed with a single quote before being
  written, the standard mitigation, since Excel/Sheets/LibreOffice
  all treat those as "this cell is a formula" otherwise (e.g. a
  malicious `=cmd|'/c calc'!A1` value in the original upload would
  otherwise execute when the cleaned file is later opened by anyone
  in a spreadsheet app). Numeric/boolean columns are never touched.
  One side effect: phone numbers stored in international format
  (leading `+`) get the same prefix, since `+` is a formula trigger
  too, Excel hides that leading quote automatically (it's the
  standard "force text" marker), but it will be visible if the CSV
  is read as raw text or re-parsed by another script instead of
  opened in a spreadsheet app.

Known trade-offs, not yet addressed:

- The in-memory rate limiter and job/session model assume a single
  instance (see "Storage note" above) — horizontal scaling needs a
  shared store for both.
- The `Content-Security-Policy`'s `style-src` still allows
  `'unsafe-inline'`, a handful of templates use inline `style="..."`
  attributes; removing it would mean moving those into CSS classes, a
  template cleanup rather than a security-endpoint change.
- `ALLOWED_ORIGINS` still defaults to `*` (see `config.py`); set it
  to your actual frontend origin(s) in production, especially for any
  split frontend/backend deploy (session cookies won't work
  cross-origin against a wildcard at all, see `.env.example`).

## What Omixa does and doesn't touch

Prompted by early testing feedback (see below), a few explicit rules
about scope:

- **Header row: your call, not Omixa's.** Omixa never decides on its
  own whether row 1 is a header. `/api/process` and `/api/report`
  both take an optional `has_header` (default `true`); set it to
  `false` and row 1 is treated as ordinary data, with generic column
  names (`column_1`, `column_2`, ...) instead of real headers. The
  `/clean` workspace exposes this as a checkbox.
- **Multiple sheets survive.** If an uploaded `.xlsx` has more than
  one sheet, only the sheet Omixa actually reads and cleans (the
  first one) is rebuilt, every other sheet is carried through to the
  downloaded file exactly as it was in the source, instead of
  disappearing. `.xls` sources can't do this (openpyxl can't open
  `.xls` at all, see `processing/pipeline.py`'s `_first_sheet_name`),
  so a multi-sheet `.xls` upload still only round-trips its first
  sheet.
- **Formatting on the cleaned sheet itself is not preserved.**
  Omixa only ever changes cell *values*, it never explicitly sets a
  font, size, bold/italic state, or column width. But the sheet it
  writes is rebuilt from the cleaned data rather than edited in
  place, so it comes out in the export library's plain defaults
  regardless of what the original file's fonts/sizes/bold/italic/
  column widths were. Keeping the original per-cell styling on the
  cleaned sheet itself (not just the untouched other sheets) would
  mean editing the source workbook's cells in place instead of
  rebuilding the sheet, a bigger change than what's done here.

## Cleaning rules

`cleaning/rules.py` runs an ordered pipeline of auto-fix rules
(`cleaning.rules.DEFAULT_RULES`), each safe enough to apply without
asking first:

1. **column_names**, tidy headers into consistent `snake_case`
2. **formatting**, trim/collapse whitespace
3. **missing_token_normalization**, treat `"N/A"`, `"null"`, `"-"`, blanks, etc. as real missing values
4. **numeric_text_cleaning**, strip `$`, `,`, `%` from numbers stored as text and convert dtype (whole-column-safe only)
5. **boolean_standardization**, `Yes/No`, `True/False`, `Y/N` → real booleans (never touches `1`/`0`)
6. **categorical_standardization**, merges case/whitespace-only variants (`Male`/`MALE`/`male`)
7. **email_cleaning**, trims + lowercases email-shaped columns
8. **phone_cleaning**, collapses accidental repeated punctuation only, in phone-shaped columns
9. **date_standardization**, normalizes dates to `YYYY-MM-DD`, but only when day-first vs month-first parsing agree (unambiguous)
10. **missing_values**, median (or mode, for booleans) for numbers, `"Unknown"` for text
11. **duplicates**, drops exact duplicate rows, keeping the first

Anything too ambiguous to fix safely (mismatched date formats,
inconsistent categories that aren't just case variants, outliers,
constant columns, malformed emails/phones, likely near-duplicate
records, mixed-type columns) is never silently changed, it's
surfaced instead via `cleaning/quality_report.py`, both before
cleaning (`GET /api/report/<job_id>`) and after
(`summary.quality_report` from `POST /api/process/<job_id>`), so the
user can decide what to do about it themselves.

Both the fixer and the detector share the same "is this an email
column? a phone column? a date column?" heuristics from
`cleaning/detectors.py`, so the report never promises a fix the rules
don't actually perform.

## Admin dashboard

`/admin` (protected, see "Security" above) is a real-time operator
view backed by `db.py`'s sqlite metadata: total jobs/sessions,
uploads/processed/downloaded/failed counts, CSV/XLS/XLSX breakdown,
processing success rate and average processing time, average quality
score before/after, most common detected issues, most-used cleaning
rules, a searchable/filterable job list, recent system errors, and
recent admin login attempts. It never stores or displays the actual
contents of anyone's uploaded or cleaned dataset — only counts, row/
column numbers, quality scores, rule names, and issue codes.

## Testing

```
pip install -r requirements-dev.txt
pytest
```

`pytest.ini` puts the project root on `sys.path` (`pythonpath = .`)
and the root `conftest.py` sets the environment variables `config.py`
needs before any test module imports it, so this runs cleanly on a
fresh clone with no `.env` file. Coverage:

- `testing/test_omixa.py` — cleaning engine correctness (rules,
  quality scoring, resolutions, job lifecycle)
- `testing/test_omixa_v2.py` — cleaning engine, additional cases
- `testing/test_omixa_security.py` — formula-injection defense, rate
  limiting
- `tests/test_sessions_and_jobs.py` — session cookie issuance, job
  ownership/isolation across sessions, the upload→report→process→
  download happy path
- `tests/test_uploads.py` — extension whitelist, content-sniffing
  (fake CSV/XLSX, macro-enabled workbooks, non-workbook zips), upload
  size limit
- `tests/test_admin.py` — login (wrong password/username/missing
  CSRF), CSRF-protected state-changing endpoints, admin login rate
  limiting, that an admin session doesn't implicitly grant job
  access, dashboard stats/filtering
- `tests/test_security_headers.py` — security headers, CSP nonce,
  CORS origin handling, health check, rate limit headers

---

## Quality model (v2)

OMIXA answers: *what is wrong, how serious, why, what is safe to fix, what needs a human, and did the data improve?*

**Scoring** (`cleaning/scoring.py`) is impact-based, not finding-count based. Each finding's
`impact = affected share × rule criticality × field factor` (field factor comes from the column's semantic
importance). Severity (`critical/high/medium/low`) is derived from impact, bounded by what the rule type allows
(cosmetic issues never exceed `low`), and the legacy `critical/warning/info` field is derived from it. Dimension scores
(completeness, validity, uniqueness, consistency, accuracy, timeliness) are weighted losses; dimensions that do not apply
(e.g. timeliness with no "last updated" column) are reported as `null`, not 100. An unresolved critical/high issue caps the
overall score (`score_capped_by`, `score_before_cap` show this).

**Remediation classes** on every finding: `safe_auto_fix` (deterministic, reversible, both finding and fix high-confidence),
`requires_review` (ambiguous, inferential, imputation, destructive), `do_not_modify` (correctness cannot be established:
digits lost to scientific notation, reused identifiers, possible non-binary gender answers, stale records). Operations are
labelled `normalization | imputation | correction | inference | deletion`. Imputation is never "safe", and is
never applied to identifiers, emails, phones or dates, even on request.

**Adding a rule:** write a check `fn(ctx) -> [raw findings]`, then `register_rule(RuleSpec(...))`
(`cleaning/rule_registry.py`). Scoring, recommendations, scorecard and audit pick it up. See `cleaning/builtin_rules.py`.

**Audit & reversibility** (`cleaning/audit.py`): every step is diffed before/after, so counts are measured, not
self-reported. Each entry has rule id, reason, operation kind, before/after examples, cells/rows affected, confidence and
approval (`automatic | user_selected | user_approved`). `revert(df, log)` restores cells, removed rows, dropped columns and
renamed headers from the in-run ledger (and refuses if the ledger was truncated). The ledger is **not persisted**; jobs are
deleted after download. Persisting it is the natural next step for dataset version history.

**Expected score after safe fixes** is a real simulation (only safe rules are applied to a copy and re-scored), so
completeness is never credited for imputed values; imputed cells also surface as an accuracy finding.

## Security notes

* **Rotate the secrets that were in `RAILWAY_ENV_VARS.txt`.** Removing the file does not un-leak them; anything that was
  ever pushed is compromised. Generate new values with `python scripts/generate_secrets.py` (prints only; writes nothing)
  and set them in your host's variables. In production the app refuses to start with the leaked key (checked by SHA-256
  fingerprint, the key itself is not in the repo) or with a key under 32 chars. Also purge the file from git history.
* `X-Forwarded-For` is only trusted through `OMIXA_TRUSTED_PROXY_HOPS` (default 1 in production, 0 in development).
* State-changing `/api` calls with a foreign `Origin` are rejected unless listed in `ALLOWED_ORIGINS`.
* Row/column/zip-inflation ceilings: `OMIXA_MAX_ROWS`, `OMIXA_MAX_COLUMNS`, `OMIXA_MAX_XLSX_UNCOMPRESSED_MB`.

## Running the tests

`pip install -r requirements-dev.txt && pytest`

## Free and Pro

**Free = "Clean my file."** The whole cleaning engine, quality score, issue detection, before/after
preview and cleaned download. No account needed. Only cap: upload size (`OMIXA_FREE_MAX_UPLOAD_MB`, 10).

**Pro = "Help me establish, maintain and prove that my data is trustworthy."** $5/month or $50/year:
Quality Profiles, profile PASS/WARNING/FAIL checks, column-level scores and dimension analysis,
what-changed report, change log, PDF quality report, saved history. Uploads up to 25 MB.

### Setup
1. Firebase: enable Email/Password and Google sign-in; create a service account. Set `FIREBASE_SERVICE_ACCOUNT_JSON`,
   `FIREBASE_PROJECT_ID` and the public `FIREBASE_WEB_CONFIG_JSON`. Deploy `firestore.rules` and `firestore.indexes.json`.
2. Paystack: create two plans (monthly, yearly) in NGN, set `PAYSTACK_PLAN_MONTHLY/ANNUAL` and `PAYSTACK_SECRET_KEY`.
   Add the webhook URL `https://YOUR-DOMAIN/api/billing/webhook` in the Paystack dashboard.
3. `pip install -r requirements.txt`. See `.env.example` for every variable.

### Firestore
`users/{uid}` (email, name, plan, subscription_status, subscription_start, subscription_expires, paystack_* ids),
`users/{uid}/qualityProfiles/{id}`, `users/{uid}/sessions/{id}` (figures only, never rows),
`payments/{reference}` (server only, makes payment handling idempotent).

### Security model
Pro is decided on the server from the Firestore subscription record on every request. The browser only holds a signed
session cookie. Paystack events are accepted only with a valid HMAC-SHA512 signature, and the redirect is re-verified with
Paystack's API. Firestore rules deny all client writes.

### Currency cleaning
`cleaning/currencies.py` covers 140+ currencies (170+ countries/regions). Add a row to extend it.

### Tests
`python -m unittest tests.test_pro_accounts` (offline: in-memory store and a fake Paystack).
