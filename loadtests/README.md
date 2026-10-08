# Omixa load testing

**Nothing here has been run.** Omixa makes no claim about concurrent-user capacity until you run
these against a staging stack and record the numbers.

## Rules
1. Staging only: real Postgres, Redis, S3-compatible bucket, queue mode, Paystack TEST keys.
2. One scenario at a time, so each bottleneck is attributable.
3. Generate load from outside your network; for 10k+ users use distributed generators.
4. Watch the dashboards while you test (below), not just the client-side numbers.

## Stages (repeat per scenario)
1,000 -> 10,000 -> 100,000 -> higher, each held >= 10 min after ramp. Stop a stage when
p99 > 2 s or errors > 1% and record what saturated.

| Scenario | Tool/tag | What it exercises | Likely first bottleneck |
|---|---|---|---|
| Landing/static | k6-edge / `--tags landing` | CDN + edge | CDN config, TLS, origin bandwidth if not cached |
| API/health | `--tags api` | stateless API | CPU per instance, gunicorn threads |
| Auth | (add Firebase test tokens) | Firebase verify + session | Firebase quotas, Firestore reads per request |
| Upload | `--tags upload` | validation + bucket write | API CPU (validation), bucket request rate, bandwidth |
| Job create | `--tags upload` | DB insert + queue publish | Postgres connections/writes, Redis |
| Status polling | `--tags poll` | DB read + Redis cache | Redis ops/s, Postgres reads (cache hit rate) |
| Processing | queue depth, worker metrics | pandas in workers | worker RAM -> replica count; queue wait time |
| Download | `--tags download` | presign + bucket egress | bucket egress cost/limits |
| Webhook | `--tags webhook` | signature + atomic grant | Firestore txn contention on hot user/ref |

## Measure
p50/p95/p99 latency, requests/s, queue wait time (`omixa_queue_depth`, job created->running),
processing throughput (jobs/min per worker), CPU/RAM per pod, DB connections (`pg_stat_activity`),
error rate (`omixa_http_requests_total{status="5xx"}`), 429 rate, dead-letter count.

## Interpreting
Record for each stage: the number, the saturated resource, the fix, the re-test result. The
"estimated scaling limits" table in docs/SCALING.md is a hypothesis until this table is filled in.
