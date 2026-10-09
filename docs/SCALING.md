# Omixa scaling guide

**Omixa is NOT proven at 1,000,000 concurrent users.** Nothing below has been load-tested on real
infrastructure. The numbers are hypotheses until `loadtests/` has been run on staging.

## Measured (sandbox, 1 vCPU, default rules, CSV, single process)
| File | Time | Peak RAM |
|---|---|---|
| 20k rows x 10 cols | 18 s | 84 MB |
| 100k x 10 | 86 s | 123 MB |
| 50k x 40 (2M cells) | 180 s | 148 MB |

Takeaways: (1) **CPU time, not RAM, is the limit**: ~11k cells/s on this box (your hardware will
differ, re-measure). (2) Old inline mode would hit the 120 s gunicorn timeout at roughly 100k+
rows. (3) Profile: ~2/3 of time is `generate_report` (run before and after) and the audit
`changed_cells_mask`; hot spots are per-cell Python regexes via `Series.map`
(`strip_numeric_noise`, `strip_currency`, `check_fixable_formats`). Vectorising these is the
biggest throughput win available and was deliberately NOT done here (accuracy risk, needs its own
test pass). Peak RAM at these sizes is small, but worker memory still must cover the 3M-cell cap
(`OMIXA_MAX_CELLS`): size workers from a measured wide-file run.

## Boundaries by layer
| Layer | Scales how | Limit / watch | Cost driver |
|---|---|---|---|
| App/API | add instances behind LB (stateless) | Firestore reads per request, Redis ops, CPU for upload validation | instance-hours |
| Edge | CDN caches landing/static | origin only sees API + uploads | bandwidth |
| Database | Postgres: pool + PgBouncer, read replica for admin | connections (`DB_POOL_MAX` x replicas), job-row write rate | managed DB tier |
| Redis | one managed instance first | ops/s: rate limit (2/req), status cache; AOF on | managed Redis |
| Object storage | effectively unbounded | request rate, egress, lifecycle rule | GB-month + egress |
| Queue/workers | add replicas | jobs/min = replicas x (60 / seconds per job) | worker CPU-hours (dominant) |
| Firebase | quota-based | Auth verifications, Firestore reads/writes | per-operation billing |

Workers: concurrency = replicas (one job each). Jobs/min per worker ~ 60/avg_seconds. Example:
avg 30 s/job and 2,000 jobs/min peak needs ~1,000 workers: that is the real cost curve, so
vectorising the engine (above) is worth more than any infra tweak. "1M connected users" is
mostly CDN + stateless API + cached polling; "thousands of simultaneous jobs" is the worker bill.

## Autoscaling signals
API: CPU 60%, p95 latency, RPS/instance. Workers: **queue depth / backlog age** (KEDA on
`omixa_queue_depth`), then CPU/memory. See `deploy/k8s/omixa.yaml`. On Railway: set replicas
manually per service (web: `gunicorn`, worker: `python worker.py` via `railway.worker.json`).

## Required infrastructure
Postgres 15+ (managed), Redis 6.2+ managed with persistence, S3-compatible bucket (private, SSE,
lifecycle expire 1 day as backstop, CORS allowing GET from your origin, no credentials),
Firebase project, CDN, platform metrics/log drain, Paystack keys.

## Env vars
Existing: `OMIXA_SECRET_KEY`, `OMIXA_ADMIN_USERNAME`, `OMIXA_ADMIN_PASSWORD_HASH`,
`FIREBASE_SERVICE_ACCOUNT_JSON`/`FIREBASE_PROJECT_ID`, `PAYSTACK_*`, `TRUSTED_PROXY_HOPS`, CORS
settings. New (all in `.env.example`): `OMIXA_PROCESSING_MODE`, `DATABASE_URL`, `REDIS_URL`,
`OMIXA_STORAGE_BACKEND`, `OMIXA_S3_*`, `OMIXA_MAX_CELLS`, `OMIXA_JOB_*`, `OMIXA_QUEUE_*`,
`OMIXA_MAX_ACTIVE_JOBS_*`, `OMIXA_GLOBAL_MAX_RUNNING`, `RATE_LIMIT_*_PER_MINUTE`,
`OMIXA_LOG_FORMAT`, `OMIXA_METRICS_TOKEN`. **Web and worker both need the same secret key,
Firebase, database, Redis and bucket settings.**

## Rollout order (safe, reversible)
1. Deploy code with defaults (no behaviour change). 2. Add Postgres (`DATABASE_URL`) and Redis.
3. Add bucket (`OMIXA_STORAGE_BACKEND=s3`) - still inline. 4. Start worker service, then flip
`OMIXA_PROCESSING_MODE=queue` (rollback = flip back). 5. Run `loadtests/` stage by stage.
In-flight inline/SQLite jobs are not migrated (30-minute TTL): deploy off-peak.

## Security implications
Presigned URLs are the only way to reach files (bearer links valid 5 min; keep TTL short).
Worker has bucket + DB credentials: treat as sensitive. `/api/metrics` is off without a token.
Rate limits key on uid or proxy-corrected IP; set `TRUSTED_PROXY_HOPS` correctly or all users
share one bucket. Job ownership is enforced from the DB row for every status/download/process
call. Logs never include bodies/cookies/files; request IDs only. CSP/CSRF/CORS/cookie settings
are unchanged.

## Cost considerations
Biggest: worker CPU-hours (grow with jobs x seconds/job), then egress, managed Postgres/Redis
tiers, Firebase operations at volume. Free-tier users are throttled by per-user job caps and queue
priority (Pro first).

## Benchmarks

`python benchmarks/bench_cleaning.py` (add `--quick` for 1k/10k rows) times the default rule set on 1k-100k rows, a wide (120-column) and a text-heavy dataset. On the development sandbox the engine ran at roughly 70k cells/second with linear scaling (100k rows x 7 columns in about 9 s). Treat figures as relative, not as guarantees.
