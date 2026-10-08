"""Distributed-infrastructure behaviour, run against in-memory fakes (no network)."""
import io
import json
import os
import threading
import time
import uuid

import pytest

import app as app_module
import db
import jobs.service as svc
import storage as object_storage
import worker
from config import Config
from jobs.queue import get_queue, DELAYED
from utils import observability, redis_client, security
from tests.fakes import FakeRedis, FakeS3Client
from tests.conftest import make_csv


@pytest.fixture
def fake_redis(monkeypatch):
    r = FakeRedis()
    redis_client.set_redis_for_tests(r)
    yield r
    redis_client.set_redis_for_tests(None)


@pytest.fixture
def queue_mode(monkeypatch, fake_redis):
    monkeypatch.setattr(Config, "PROCESSING_MODE", "queue")
    monkeypatch.setattr(Config, "JOB_MEMORY_LIMIT_MB", 0)  # sandbox-safe; the cap has its own test
    yield


def _upload(client, data=None):
    r = client.post("/api/upload/", data={"file": (make_csv(data), "t.csv")}, content_type="multipart/form-data")
    assert r.status_code == 201, r.get_json()
    return r.get_json()["job_id"]


def _drain(max_loops=10):
    for _ in range(max_loops):
        if worker.run_one("w-test") == "idle":
            break


# ------------------------------------------------------------------ rate limiting

def test_redis_limiter_is_shared_across_instances(fake_redis):
    # two "instances" = two calls through the same Redis; the limit is fleet-wide
    results = [security._check_window("api", "ip:1.2.3.4", 3)[0] for _ in range(5)]
    assert results == [True, True, True, False, False]


def test_limiter_falls_back_to_memory_when_redis_fails(fake_redis):
    fake_redis.down = True
    assert [security._check_window("api", "k", 2)[0] for _ in range(3)] == [True, True, False]


def test_limiter_memory_table_evicts_idle_keys():
    security._hits.clear()
    for i in range(100):
        security._check_memory("api", f"ip{i}", 5)
    for w in security._hits.values():
        w.clear()
    security._last_gc = 0
    security._check_memory("api", "fresh", 5)
    assert len(security._hits) == 1


def test_upload_bucket_is_separate_from_status_bucket(client, monkeypatch, fake_redis):
    monkeypatch.setattr(Config, "RATE_LIMIT_UPLOAD_PER_MINUTE", 2)
    monkeypatch.setattr(Config, "RATE_LIMIT_STATUS_PER_MINUTE", 100)
    codes = [client.post("/api/upload/", data={"file": (make_csv(), "t.csv")},
                         content_type="multipart/form-data").status_code for _ in range(4)]
    assert codes[:2] == [201, 201] and codes[2:] == [429, 429]
    assert client.get("/api/jobs/" + str(uuid.uuid4())).status_code == 404  # not 429


def test_webhook_is_never_rate_limited(client, monkeypatch, fake_redis):
    monkeypatch.setattr(Config, "RATE_LIMIT_PAYMENT_PER_MINUTE", 1)
    codes = {client.post("/api/billing/webhook", data=b"{}").status_code for _ in range(5)}
    assert 429 not in codes


# ------------------------------------------------------------------ observability

def test_request_id_is_generated_and_inbound_id_honoured(client):
    assert len(client.get("/healthz").headers["X-Request-ID"]) >= 8
    assert client.get("/healthz", headers={"X-Request-ID": "trace-12345678"}).headers["X-Request-ID"] == "trace-12345678"
    bad = client.get("/healthz", headers={"X-Request-ID": "x y;"}).headers["X-Request-ID"]
    assert bad != "x y;"


def test_health_endpoints(client):
    assert client.get("/healthz").status_code == 200
    r = client.get("/readyz")
    assert r.status_code == 200 and r.get_json()["status"] == "ready"


def test_metrics_disabled_without_token_and_protected_with_one(client, monkeypatch):
    assert client.get("/api/metrics").status_code == 404
    monkeypatch.setattr(Config, "METRICS_TOKEN", "tok-123")
    assert client.get("/api/metrics").status_code == 404
    r = client.get("/api/metrics", headers={"Authorization": "Bearer tok-123"})
    assert r.status_code == 200 and b"omixa_queue_depth" in r.data


def test_json_log_never_contains_extra_fields_beyond_allowlist():
    import logging
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "hello", (), None)
    rec.password = "hunter2"
    out = json.loads(observability.JsonFormatter().format(rec))
    assert "password" not in out and out["msg"] == "hello"


# ------------------------------------------------------------------ storage

def test_s3_storage_uses_private_encrypted_objects_and_signed_urls(monkeypatch):
    monkeypatch.setattr(Config, "S3_SSE", "AES256")
    client = FakeS3Client()
    st = object_storage.S3Storage(client=client, bucket="b")
    job = str(uuid.uuid4())
    key = object_storage.source_key(job, "csv")
    st.put_fileobj(key, io.BytesIO(b"a,b\n1,2\n"), "text/csv")
    assert client.calls[0][2]["ServerSideEncryption"] == "AES256"
    assert "ACL" not in client.calls[0][2]  # never public
    url = st.presigned_get(key, 'evil"name.csv', ttl=60)
    assert url.endswith("exp=60") and st.exists(key)
    st.delete_job(job)
    assert not st.exists(key)


def test_storage_keys_reject_traversal():
    for bad in ("../x/source.csv", "abc/source.csv", f"{uuid.uuid4()}/../../etc.csv", f"{uuid.uuid4()}/source.exe"):
        with pytest.raises(object_storage.StorageError):
            object_storage.check_key(bad)


# ------------------------------------------------------------------ job lifecycle

def _new_job(owner="h"):
    jid = str(uuid.uuid4())
    assert db.record_upload(jid, owner, "csv", "t.csv", 10)
    return jid


def test_only_one_worker_can_claim_a_job():
    jid = _new_job(); db.enqueue_job(jid, {}, 1)
    wins = []
    def go(n): wins.append(db.claim_job(jid, f"w{n}", 60) is not None)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert wins.count(True) == 1


def test_double_enqueue_is_ignored():
    jid = _new_job()
    assert db.enqueue_job(jid, {}, 1) and not db.enqueue_job(jid, {}, 1)


def test_retry_uses_exponential_backoff_then_dead_letters(monkeypatch, fake_redis):
    monkeypatch.setattr(Config, "JOB_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(Config, "JOB_RETRY_BASE_SECONDS", 10)
    jid = _new_job(); db.enqueue_job(jid, {}, 1)
    db.claim_job(jid, "w", 60)
    assert db.fail_or_retry(jid, "w", "OSError", "x", True) == "retry"
    j = db.get_job(jid); assert j["status"] == "queued" and j["available_at"] > time.time() + 5
    assert db.claim_job(jid, "w", 60) is None  # not due yet
    db._cursor  # noqa
    with db._cursor() as cur:
        cur.execute("UPDATE jobs SET available_at=0 WHERE job_id=?", (jid,))
    db.claim_job(jid, "w", 60)
    assert db.fail_or_retry(jid, "w", "OSError", "x", True) == "dead"
    assert db.get_job(jid)["dead_letter"] == 1
    assert db.backoff_seconds(1) == 10 and db.backoff_seconds(3) == 40 and db.backoff_seconds(30) == 300


def test_zombie_worker_cannot_overwrite_after_lease_loss():
    jid = _new_job(); db.enqueue_job(jid, {}, 1); db.claim_job(jid, "old", 60)
    with db._cursor() as cur:
        cur.execute("UPDATE jobs SET lease_expires_at=1 WHERE job_id=?", (jid,))
    assert db.release_expired_lease(jid) == "retry"
    with db._cursor() as cur:
        cur.execute("UPDATE jobs SET available_at=0 WHERE job_id=?", (jid,))
    db.claim_job(jid, "new", 60)
    assert not db.record_processed(jid, rows_in=1, rows_out=1, columns_in=1, quality_before=None,
                                   quality_after=None, rules_applied=[], processing_ms=1, worker_id="old")
    assert db.fail_or_retry(jid, "old", "X", "x", True) == "lost"
    assert db.get_job(jid)["lease_owner"] == "new"


def test_reaper_recovers_stuck_jobs_and_republishes_lost_messages(fake_redis, monkeypatch):
    stuck = _new_job(); db.enqueue_job(stuck, {}, 1); db.claim_job(stuck, "dead-worker", 60)
    lost = _new_job(); db.enqueue_job(lost, {}, 0)
    with db._cursor() as cur:
        cur.execute("UPDATE jobs SET lease_expires_at=1 WHERE job_id=?", (stuck,))
        cur.execute("UPDATE jobs SET updated_at=1 WHERE job_id=?", (lost,))
    stats = svc.reap_once()
    assert stats["leases_released"] == 1 and stats["republished"] >= 1
    assert db.get_job(stuck)["status"] == "queued"
    assert get_queue().r.zcard("omx:q:pending") >= 1 or get_queue().r.zcard(DELAYED) >= 1


def test_reaper_purges_expired_jobs_and_files(monkeypatch):
    monkeypatch.setattr(Config, "JOB_TTL_SECONDS", 1)
    jid = _new_job()
    d = os.path.join(Config.TEMP_DIR, jid); os.makedirs(d)
    open(os.path.join(d, "source.csv"), "w").write("a\n1\n")
    with db._cursor() as cur:
        cur.execute("UPDATE jobs SET created_at=1 WHERE job_id=?", (jid,))
    assert svc.reap_once()["purged"] == 1
    assert not os.path.exists(os.path.join(d, "source.csv")) and db.get_job(jid)["status"] == "expired"


# ------------------------------------------------------------------ isolation

def _sleepy(n): time.sleep(n); return 1
def _hog():
    return bytearray(900 * 1024 * 1024)
def _ok(): return {"v": 1}


def test_isolated_runner_returns_results_and_kills_on_timeout():
    assert svc.run_isolated(_ok, (), 10) == {"v": 1}
    with pytest.raises(svc.JobFailed) as e:
        svc.run_isolated(_sleepy, (30,), 1.5)
    assert e.value.error_type == "Timeout" and not e.value.retryable


def test_isolated_runner_contains_memory_bombs():
    with pytest.raises(svc.JobFailed) as e:
        svc.run_isolated(_hog, (), 20, mem_mb=400)
    assert e.value.error_type in ("MemoryError", "WorkerCrashed")


# ------------------------------------------------------------------ end to end (queue mode)

def test_queue_mode_api_never_runs_pandas_and_worker_does(client, queue_mode, monkeypatch):
    import routes.process as proc
    monkeypatch.setattr(proc, "run_pipeline", lambda *a, **k: (_ for _ in ()).throw(AssertionError("API ran pandas")))
    jid = _upload(client)
    r = client.post(f"/api/process/{jid}", json={"rules": ["formatting"]})
    assert r.status_code == 202 and r.get_json()["status"] == "queued"
    assert client.get(f"/api/jobs/{jid}").get_json()["status"] == "queued"
    _drain()
    s = client.get(f"/api/jobs/{jid}").get_json()
    assert s["status"] == "processed" and "summary" in s
    d = client.get(f"/api/download/{jid}")
    assert d.status_code == 200 and d.data.startswith(b"Name")


def test_queue_mode_analysis_is_async_too(client, queue_mode):
    jid = _upload(client)
    r = client.get(f"/api/report/{jid}?has_header=true")
    assert r.status_code == 202
    _drain()
    done = client.get(f"/api/report/{jid}?has_header=true")
    assert done.status_code == 200 and "report" in done.get_json()
    s = client.get(f"/api/jobs/{jid}").get_json()
    assert s["status"] == "uploaded" and s["analysis"]["has_header"] is True


def test_other_sessions_cannot_see_or_download_a_queued_job(client, queue_mode):
    jid = _upload(client)
    client.post(f"/api/process/{jid}", json={}); _drain()
    stranger = app_module.app.test_client()
    assert stranger.get(f"/api/jobs/{jid}").status_code == 404
    assert stranger.get(f"/api/download/{jid}").status_code == 404
    assert stranger.post(f"/api/process/{jid}", json={}).status_code == 404


def test_per_user_active_job_limit_and_queue_hard_limit(client, queue_mode, monkeypatch):
    monkeypatch.setattr(Config, "MAX_ACTIVE_JOBS_ANON", 1)
    a, b = _upload(client), _upload(client)
    assert client.post(f"/api/process/{a}", json={}).status_code == 202
    r = client.post(f"/api/process/{b}", json={})
    assert r.status_code == 429 and "Retry-After" in r.headers
    monkeypatch.setattr(Config, "MAX_ACTIVE_JOBS_ANON", 5)
    monkeypatch.setattr(Config, "QUEUE_HARD_LIMIT", 1)
    r = client.post(f"/api/process/{b}", json={})
    assert r.status_code == 503 and "Retry-After" in r.headers


def test_high_traffic_message_when_queue_is_deep(client, queue_mode, monkeypatch):
    monkeypatch.setattr(Config, "QUEUE_SOFT_LIMIT", 1)
    a, b = _upload(client), _upload(client)
    client.post(f"/api/process/{a}", json={})
    r = client.post(f"/api/process/{b}", json={})
    assert r.status_code == 202 and "high traffic" in r.get_json()["message"]


def test_failed_job_reports_safe_error_not_internals(client, queue_mode, monkeypatch):
    jid = _upload(client, b"")  # empty csv -> unprocessable
    client.post(f"/api/process/{jid}", json={})
    for _ in range(12):
        with db._cursor() as cur:
            cur.execute("UPDATE jobs SET available_at=0 WHERE job_id=?", (jid,))
        worker.run_one("w")
    s = client.get(f"/api/jobs/{jid}").get_json()
    assert s["status"] == "failed" and "/" not in s["error"] and "Traceback" not in s["error"]


def test_remote_storage_flow_uses_signed_urls_and_no_local_files(client, queue_mode, monkeypatch):
    fake = FakeS3Client()
    object_storage.set_storage_for_tests(object_storage.S3Storage(client=fake, bucket="b"))
    try:
        jid = _upload(client)
        assert not os.path.exists(os.path.join(Config.TEMP_DIR, jid))      # API kept nothing locally
        assert any("source.csv" in k for _, k in fake.objects)
        client.post(f"/api/process/{jid}", json={}); _drain()
        assert not os.path.exists(os.path.join(Config.TEMP_DIR, jid))      # worker scratch cleaned
        r = client.get(f"/api/download/{jid}?format=url")
        assert r.status_code == 200 and r.get_json()["url"].startswith("https://signed.example/")
        assert client.get(f"/api/download/{jid}").status_code == 302
        assert not any("source.csv" in k for _, k in fake.objects)         # original dropped after download
    finally:
        object_storage.set_storage_for_tests(None)


# ------------------------------------------------------------------ payments

def test_simultaneous_webhook_deliveries_grant_exactly_once():
    from accounts.store import MemoryStore
    store = MemoryStore()
    grants = []
    def compute(user):
        grants.append(1)
        return {"plan": "monthly", "subscription_status": "active", "subscription_expires": time.time() + 100}
    results = []
    def go(): results.append(store.grant_payment("ref1", "u1", compute, {"uid": "u1"}) is not None)
    ts = [threading.Thread(target=go) for _ in range(20)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert results.count(True) == 1 and len(grants) == 1
    assert store.get_payment("ref1")["processed"] and store.get_user("u1")["plan"] == "monthly"


def test_failed_grant_does_not_consume_the_payment_reference():
    from accounts.store import MemoryStore
    store = MemoryStore()
    def boom(user): raise RuntimeError("crash mid-grant")
    with pytest.raises(RuntimeError):
        store.grant_payment("ref2", "u1", boom, {})
    assert not (store.get_payment("ref2") or {}).get("processed")
    assert store.grant_payment("ref2", "u1", lambda u: {"plan": "monthly"}, {}) is not None


# ------------------------------------------------------------------ memory guard

def test_cell_limit_rejects_wide_files_before_loading(monkeypatch, tmp_path):
    import pandas as pd
    from processing.pipeline import read_source, DatasetTooLargeError
    monkeypatch.setattr(Config, "MAX_CELLS", 50)
    df = pd.DataFrame({f"c{i}": range(30) for i in range(5)})
    df.to_csv(tmp_path / "a.csv", index=False); df.to_excel(tmp_path / "a.xlsx", index=False)
    for f in ("a.csv", "a.xlsx"):
        with pytest.raises(DatasetTooLargeError):
            read_source(str(tmp_path / f))


# ------------------------------------------------------------------ cleaning engine through the queue

def test_cleaning_profile_runs_in_the_worker_and_is_plan_checked_at_enqueue(client, queue_mode):
    """Free -> 402 and nothing is enqueued. Pro -> the declarative profile is frozen into the job spec,
    executed by the worker (not the API request), and its report lands in the job result."""
    import time
    from accounts import auth as auth_mod, store as store_mod
    store = store_mod.MemoryStore()
    store_mod.set_store_for_tests(store)
    auth_mod.set_verifier_for_tests(lambda t: {"uid": t, "email": f"{t}@example.com", "name": t})
    try:
        assert client.post("/api/auth/session", json={"idToken": "u9"}).status_code == 200
        data = b"Name,Phone\n  ada  obi ,0803-123-4567\nkofi,12\n"
        profile = {"columns": {"Name": {"rules": [{"type": "trim_whitespace"}, {"type": "normalize_case", "mode": "title"}]},
                               "Phone": {"rules": [{"type": "normalize_phone", "country": "NG"}]}}}
        free_job = _upload(client, data)
        assert client.post(f"/api/process/{free_job}", json={"cleaning_profile": profile}).status_code == 402
        assert db.get_job(free_job)["status"] == "uploaded"          # nothing was enqueued

        store.upsert_user("u9", {"plan": "pro_monthly", "subscription_status": "active",
                                 "subscription_expires": time.time() + 86400, "subscription_start": time.time()})
        job = _upload(client, data)
        r = client.post(f"/api/process/{job}", json={"cleaning_profile": profile})
        assert r.status_code == 202, r.get_json()
        assert json.loads(db.get_job(job)["spec_json"])["cleaning_profile"] == profile
        _drain()
        status = client.get(f"/api/jobs/{job}").get_json()
        assert status["status"] == "processed", status
        eng = status["summary"]["cleaning_engine"]
        assert eng["columns"]["Phone"]["rules"][0]["changed"] == 1 and eng["totals"]["review_pending"] == 1
        out = client.get(f"/api/download/{job}").data.decode()
        assert "Ada Obi" in out and "+2348031234567" in out
    finally:
        store_mod.set_store_for_tests(None)
        auth_mod.set_verifier_for_tests(None)
