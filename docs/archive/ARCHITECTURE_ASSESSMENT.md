# Omixa — Architecture Assessment (pre-change)

Scope: audit of `omixa-backend-fixed.zip` before any scalability work.
No application code has been changed yet.

Honest framing: nothing here makes Omixa "support 1M concurrent users".
This document lists what blocks horizontal scaling today and the order in
which to fix it. Capacity claims come only after load tests (see §8).

---

## 1. Current architecture (as found)

```
Browser ─► Railway (1 service, N replicas possible but BROKEN, see §2)
            gunicorn: 3 sync workers × 2 threads, --preload, timeout 120s
              Flask app
               ├─ signed-cookie session (sid + uid)         [stateless ✔]
               ├─ Firebase Admin: verify ID token once       [managed ✔]
               ├─ Firestore: users, profiles, sessions, payments  [managed ✔]
               ├─ SQLite (data/omixa.db): jobs, login_attempts, error_log  [local ✘]
               ├─ temp/<job_id>/{source.*, owner, original_name, cleaned.*}  [local FS ✘]
               ├─ in-memory per-IP rate limiter              [per-process ✘]
               └─ pandas pipeline run INSIDE the request     [sync ✘]
Paystack ─► /api/billing/webhook (HMAC-SHA512 verified)
```

Job flow today: `POST /api/upload` (saves to local disk) →
`POST /api/process/<id>` (pandas runs in the request, up to 120 s) →
`GET /api/download/<id>` (reads file, deletes job dir).
Upload, process and download must all land on the **same instance**.

## 2. What blocks horizontal scaling (ranked)

| # | Blocker | Where | Why it breaks at >1 instance / high load |
|---|---|---|---|
| 1 | Job state on local disk | `utils/file_handler.py` | Upload on replica A, process on B → 404. Restart/redeploy wipes in-flight jobs. |
| 2 | Heavy pandas work inside web request | `routes/process.py`, `routes/report.py` | 3×2 = 6 concurrent requests per instance. A few large files starve the whole API; `--timeout 120` kills workers mid-job. |
| 3 | Per-process rate limiter | `utils/security.py` | Effective limit = limit × workers × replicas; resets on restart; `_hits` dict never evicts IP keys (slow memory leak). IP-keyed, so Nigerian carrier-NAT users share buckets. Status polling would trip the 30/min limit. |
| 4 | SQLite for operational metadata | `db.py` | Single-writer, local file; each replica has its own DB so admin stats and job history diverge. WAL on a network volume is unsafe. |
| 5 | Request-path cleanup sweep | `sweep_expired_jobs()` called in every job route | `os.listdir(TEMP_DIR)` + `getmtime` per request: O(jobs) on the hot path, and races across processes. |
| 6 | Memory is not bounded by the row/column checks | `processing/pipeline.py::read_source` | `pd.read_excel` loads the **whole** sheet *before* the `MAX_ROWS` check (CSV is capped via `nrows`, Excel is not). 500k rows × 500 cols = 250M cells is far beyond what a worker can hold. `.xls` has no decompression guard. `df.copy()` for Pro doubles it. Source is read twice (`/report` then `/process`). Exporter reloads the full workbook non-read-only. |
| 7 | Payment grant is not atomic | `accounts/paystack.py::apply_charge_success` | `get_user` → `claim_payment` (txn) → `upsert_user`. If the process dies between claim and upsert, the payment is marked claimed but Pro is never granted, and Paystack's retry returns "duplicate". Read-modify-write of `subscription_expires` is not transactional either. |
| 8 | Firestore read on every authenticated request | `routes/upload.py`, `process.py`, `pro_required` | One user-doc read per request; no cache. Fine at low volume, costly/slow at scale. |
| 9 | Health check does disk write + DB stats query | `app.py::health` | Heavy for an LB probe; mixes liveness and readiness. |
| 10 | No request IDs, text-only logs, errors stored in local SQLite | `app.py`, `db.py` | Can't trace a request across API → worker; errors lost per-instance. |
| 11 | No separation of API and worker processes; no Dockerfile | `Procfile`, `railway.json` | Can't scale them independently. |

## 3. What is already good (keep)

- Signed-cookie session + HMAC-hashed owner id → stateless; works across
  instances as long as `OMIXA_SECRET_KEY` is shared.
- Firebase Auth + Firestore are managed and horizontally scalable.
- Webhook signature verification (HMAC-SHA512, constant-time compare),
  server-side entitlement from the subscription record, `claim_payment` txn.
- Upload validation: extension, magic bytes, xlsx zip-bomb guard, macro
  rejection, UUID-validated job ids, non-owner gets same 404, CSP nonce,
  Origin check, `ProxyFix` with explicit hop count, log-injection escaping.
- Cleaning engine, quality scoring and audit log are pure functions of a
  DataFrame — they can move into a worker **unchanged**.

## 4. Decisions worth challenging

- **Postgres vs Firestore.** Firestore is not the scaling problem. The
  relational/transactional data (payments, entitlements, jobs, usage, audit)
  benefits from Postgres; profiles and history can stay in Firestore at
  first behind the existing `store` interface and move later. Recommended:
  Postgres for `jobs`, `payments`, `subscriptions/entitlements`, `usage`,
  `audit`; keep Firebase Auth; migrate profiles/sessions last.
- **Queue.** Redis-backed (RQ or Celery) is the pragmatic first step on
  Railway; SQS/Cloud Tasks if you leave Railway. Durable-queue guarantees
  differ: Redis needs AOF/managed persistence or jobs can be lost.
- **Object storage.** Cloudflare R2 / S3 / Backblaze B2 (S3-compatible),
  private bucket, short-lived presigned URLs, lifecycle rule for expiry.
- **Polling, not websockets, for status** at first (cheap, CDN-friendly,
  cached in Redis).

## 5. Proposed incremental plan (each phase shippable, tests green)

1. **Hygiene + safety nets (no behavior change):** repo cleanup, request
   IDs + JSON logs, cheap `/healthz` vs `/readyz`, fix limiter memory leak,
   cell-count/byte memory guards in `read_source` (reject before load),
   atomic payment grant.
2. **Storage abstraction:** `JobStorage` interface with `LocalStorage` (dev/tests)
   and `S3Storage`; ownership + metadata move out of marker files.
3. **Postgres:** schema + migrations (jobs, payments, entitlements, usage,
   audit), pooled connections, idempotency keys; admin reads from it.
4. **Redis:** distributed rate limiter (per-tier buckets, separate polling
   bucket), job-status cache, config cache, user-entitlement cache (short TTL).
5. **Queue + workers:** `POST /api/process` returns 202 + job id; worker
   entrypoint; retries with backoff, DLQ, timeouts, stuck-job reaper,
   per-user and global concurrency caps, queue-depth backpressure message.
6. **Deploy split:** `web` and `worker` services, Dockerfile, autoscaling
   signals, env-var matrix.
7. **Load tests** (k6/Locust) per stage, results recorded, bottlenecks named.

Frontend impact: `static/js/clean.js` currently expects `/api/process` to
return the finished summary. Phase 5 changes that contract to
202 + polling; the JS must change with it, and I will keep a
synchronous path for small files behind a flag until that is verified.

## 6. Scaling boundaries (qualitative — not measured)

| Layer | Limit today | Limit after plan | Cost driver |
|---|---|---|---|
| App/API | ~6 concurrent requests per instance, all coupled to processing | Stateless; scale on CPU/latency | instance-hours |
| Database | SQLite single writer; Firestore per-doc ~1 write/s | Postgres pooled (needs PgBouncer at high replica counts) | managed DB tier |
| File storage | local disk, lost on deploy | object store, effectively unbounded | GB-months + egress |
| Queue/workers | none | bounded by worker memory (pandas ≈ 5–10× file size in RAM, unmeasured) | worker RAM × time |
| Edge | none | CDN for static + landing | bandwidth |

"1M connected users" is a CDN + stateless-API + cheap-status-polling
problem; "thousands of simultaneous jobs" is a worker-fleet and memory
problem. They need separate budgets.

## 7. Security findings in the uploaded archive (act on these now)

- `.env` contains real-looking secrets (Flask secret key, Firebase service
  account JSON, Paystack secret key).
- `RAILWAY_ENV_VARS.txt` contains the **admin password in plaintext**.
- The zip also bundles `venv/` (Windows), `data/omixa.db` and `temp/` job dirs.
- None of these are tracked in git history (checked), and `.gitignore` is
  correct — they entered the zip only because it was made from the working
  folder. Still: any secret that has left your machine should be rotated.

**Rotate:** admin password (and hash), `OMIXA_SECRET_KEY`, Firebase service
account key, Paystack secret key. Delete `RAILWAY_ENV_VARS.txt`; build zips
with `git archive` instead of zipping the folder.

## 8. Load-testing approach (to be built in Phase 7)

Separate scenarios, each ramped 1k → 10k → 100k virtual users (distributed
generators; one laptop cannot generate 100k): landing/static via CDN, auth,
upload, job creation, status polling, processing throughput, download,
Paystack webhook bursts (including duplicate deliveries). Record p50/p95/p99,
RPS, queue wait, worker throughput, CPU/RAM, DB connections, error rate.
No capacity claim is made until these numbers exist.

## 9. Limits of this audit

- Sandbox had no network and no `pytest`, so the existing test suite was not
  run and redis/boto3/psycopg code can only be exercised against fakes here.
  Real-service behavior must be verified in your staging environment.
- Findings are from reading code; no profiling of memory per file size yet.
