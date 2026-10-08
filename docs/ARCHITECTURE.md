# Omixa architecture (after the scalability work)

## 1. Before
One Flask/gunicorn process group cleaned files **inside the request**, kept every job on **local
disk**, recorded state in a **local SQLite file**, and rate-limited **per process**. Two replicas
could not serve one job; one large file could stall the API.

## 2. After
```
Client -> CDN/edge (static, landing; cache by URL)  -> Load balancer
  -> N stateless API instances (gunicorn, I/O only)       [scale on CPU / latency / RPS]
        | Firebase Auth (verify)   Firestore (users, profiles, sessions, payments)
        | Redis  : fleet-wide rate limits, queue wake-ups, status/entitlement caches (NOT truth)
        | Postgres: job lifecycle, leases, audit_log, usage_daily, metadata (TRUTH for jobs)
        | Object storage (private S3-compatible): uploads + results, presigned downloads
  -> Queue (Redis ZSET delivery; Postgres is truth)
  -> M worker instances (worker.py): 1 job at a time, in an isolated child process
        timeout + memory cap, heartbeat lease, retries w/ backoff, dead-letter, reaper
```
Job flow (queue mode): upload -> validate -> bucket -> `jobs` row -> `POST /api/process` returns
**202** + job id -> worker claims (compare-and-set) -> cleans -> result to bucket -> row
`processed` -> client polls `GET /api/jobs/<id>` -> `GET /api/download/<id>?format=url` returns a
5-minute signed URL. Analysis (`/api/report`) is a queue job too, so pandas never runs in the API.

Modes (all default to the old behaviour): `OMIXA_PROCESSING_MODE=inline|queue`,
`OMIXA_STORAGE_BACKEND=local|s3`, `DATABASE_URL` empty=SQLite, `REDIS_URL` empty=in-process.

## 3. Failure behaviour
| Failure | Result |
|---|---|
| API instance dies | LB removes it (`/readyz`); sessions are signed cookies, jobs are in DB/bucket: nothing lost |
| Worker dies mid-job | lease expires -> reaper re-queues with backoff; after `JOB_MAX_ATTEMPTS` -> `failed`+dead_letter, audit entry |
| Zombie worker finishes late | its writes are compare-and-set on the lease owner: rejected |
| Redis down | rate limiter falls back to per-process; queue delivery falls back to DB polling; caches skipped. Slower, not down |
| Redis loses queue | reaper republishes `queued` rows older than `QUEUED_STALE_SECONDS` |
| Postgres down | `/readyz` fails (API pulled from rotation); in-flight workers retry; Free inline mode unaffected on SQLite |
| Bucket error | upload -> clean 503; worker attempt -> retry with backoff |
| Duplicate Paystack webhook | one Firestore transaction claims the reference **and** writes the entitlement; loser returns "duplicate" |
| Crash mid-grant | nothing committed, Paystack's retry grants it (old code could lose the entitlement) |
| Traffic spike | per-principal job caps -> 429; soft queue limit -> "high traffic" message; hard limit -> 503 + Retry-After |

## 4. Not done (be aware)
* Users/subscriptions/payments stay in **Firestore** (managed, transactional). A Postgres copy
  was not added: dual-writing without a migration/backfill plan is riskier than leaving it.
* Per-request Firestore user read remains (entitlement cache only feeds rate-limit tiers).
* `/api/report` and `/api/process` read the source separately (two downloads in queue mode).
* The cleaning engine itself is unchanged (see SCALING.md, throughput).
