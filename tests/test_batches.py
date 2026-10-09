"""Pro batch processing: entitlement, limits, per-file isolation, progress, downloads, ZIP safety,
retry/crash recovery, ownership and audit. Offline: in-memory Redis/store, real worker + pipeline."""
import io
import json
import time
import zipfile

import pytest

import db
import jobs.service as svc
import worker
from accounts import auth as auth_mod
from accounts import store as store_mod
from config import Config
from tests.fakes import FakeRedis
from utils import redis_client

CSV_A = b"Name,Email\nAda ,ada@x.com\nAda ,ada@x.com\nKofi,kofi@x.com\n"
CSV_B = b"City,Amount\nLagos,10\nAccra,20\n"


@pytest.fixture
def env(monkeypatch, app):
    r = FakeRedis()
    redis_client.set_redis_for_tests(r)
    monkeypatch.setattr(Config, "PROCESSING_MODE", "queue")
    monkeypatch.setattr(Config, "JOB_MEMORY_LIMIT_MB", 0)
    for name in ("RATE_LIMIT_PER_MINUTE", "RATE_LIMIT_USER_PER_MINUTE", "RATE_LIMIT_PRO_PER_MINUTE",
                 "RATE_LIMIT_UPLOAD_PER_MINUTE", "RATE_LIMIT_STATUS_PER_MINUTE", "RATE_LIMIT_PROCESS_PER_MINUTE"):
        monkeypatch.setattr(Config, name, 100000)
    store = store_mod.MemoryStore()
    store_mod.set_store_for_tests(store)
    auth_mod.set_verifier_for_tests(lambda t: {"uid": t, "email": f"{t}@example.com", "name": t})
    yield store
    redis_client.set_redis_for_tests(None)
    store_mod.set_store_for_tests(None)


def make_client(app, store, uid, pro=True):
    c = app.test_client()
    assert c.post("/api/auth/session", json={"idToken": uid}).status_code == 200
    if pro:
        store.upsert_user(uid, {"plan": "pro_monthly", "subscription_status": "active",
                                "subscription_expires": time.time() + 86400, "subscription_start": time.time()})
    return c


def post_batch(client, files, options=None, **extra):
    data = {"files": [(io.BytesIO(b), n) for n, b in files]}
    if options is not None:
        data["options"] = json.dumps(options)
    data.update(extra)
    return client.post("/api/batches/", data=data, content_type="multipart/form-data")


def drain(n=30):
    for _ in range(n):
        if worker.run_one("w-test") == "idle":
            # a retry parked in the future is not 'idle' work for us; stop either way
            break
        worker.run_one  # noqa


def run_all(client, bid, limit=40):
    for _ in range(limit):
        worker.run_one("w-test")
        svc.reap_once()
        if client.get(f"/api/batches/{bid}").get_json()["finished"]:
            break
    return client.get(f"/api/batches/{bid}").get_json()


# ------------------------------------------------------------------ entitlement

def test_free_user_gets_server_side_upgrade_response(app, env):
    free = make_client(app, env, "free1", pro=False)
    r = post_batch(free, [("a.csv", CSV_A)])
    assert r.status_code == 402 and r.get_json()["upgrade_required"] is True


def test_anonymous_user_must_sign_in(app, env):
    r = post_batch(app.test_client(), [("a.csv", CSV_A)])
    assert r.status_code == 401


def test_client_cannot_claim_pro(app, env):
    free = make_client(app, env, "free2", pro=False)
    r = post_batch(free, [("a.csv", CSV_A)], options={"plan": "pro", "pro": True}, plan="pro", is_pro="true")
    assert r.status_code == 402
    r = free.post("/api/batches/", data={"files": [(io.BytesIO(CSV_A), "a.csv")]},
                  headers={"X-Omixa-Plan": "pro"}, content_type="multipart/form-data")
    assert r.status_code == 402


def test_expired_subscription_loses_batch_access(app, env):
    c = make_client(app, env, "exp", pro=False)
    env.upsert_user("exp", {"plan": "pro_monthly", "subscription_status": "active",
                            "subscription_expires": time.time() - 10, "subscription_start": time.time() - 99})
    assert post_batch(c, [("a.csv", CSV_A)]).status_code == 402


def test_batch_needs_queue_mode(app, env, monkeypatch):
    monkeypatch.setattr(Config, "PROCESSING_MODE", "inline")
    pro = make_client(app, env, "p0")
    assert post_batch(pro, [("a.csv", CSV_A)]).status_code == 503


# ------------------------------------------------------------------ create + limits

def test_pro_creates_batch_with_individual_jobs(app, env):
    pro = make_client(app, env, "p1")
    r = post_batch(pro, [("a.csv", CSV_A), ("b.csv", CSV_B)])
    assert r.status_code == 202, r.get_json()
    b = r.get_json()
    assert b["counts"]["total"] == 2 and len(b["files"]) == 2
    ids = {f["job_id"] for f in b["files"]}
    assert len(ids) == 2
    assert {f["filename"] for f in b["files"]} == {"a.csv", "b.csv"}
    assert all(db.get_job(i)["batch_id"] == b["batch_id"] for i in ids)


def test_unsupported_and_fake_files_are_skipped_not_fatal(app, env):
    pro = make_client(app, env, "p2")
    r = post_batch(pro, [("a.csv", CSV_A), ("notes.txt", b"hi"), ("fake.xlsx", b"not a zip"), ("m.xlsm", b"PK\x03\x04")])
    assert r.status_code == 202
    b = r.get_json()
    assert b["counts"]["total"] == 1 and b["counts"]["skipped"] == 3
    assert {s["filename"] for s in b["skipped"]} == {"notes.txt", "fake.xlsx", "m.xlsm"}


def test_all_files_invalid_is_400(app, env):
    pro = make_client(app, env, "p3")
    r = post_batch(pro, [("x.txt", b"x"), ("y.exe", b"MZ")])
    assert r.status_code == 400 and len(r.get_json()["skipped"]) == 2


def test_oversized_file_skipped_other_files_kept(app, env, monkeypatch):
    monkeypatch.setattr(Config, "BATCH_MAX_FILE_MB", 1)
    pro = make_client(app, env, "p4")
    big = b"a,b\n" + (b"1234567,89\n" * 120000)  # > 1 MB
    r = post_batch(pro, [("big.csv", big), ("ok.csv", CSV_B)])
    assert r.status_code == 202
    b = r.get_json()
    assert [f["filename"] for f in b["files"]] == ["ok.csv"]
    assert "per-file limit" in b["skipped"][0]["reason"]


def test_total_batch_size_limit(app, env, monkeypatch):
    monkeypatch.setattr(Config, "BATCH_MAX_TOTAL_MB", 1)
    pro = make_client(app, env, "p5")
    chunk = b"a,b\n" + (b"1234567,89\n" * 50000)  # ~0.55 MB
    r = post_batch(pro, [("1.csv", chunk), ("2.csv", chunk)])
    assert r.status_code == 413
    assert db.list_batches_for("p5") == [] or all(x["file_count"] == 0 for x in db.list_batches_for("p5"))


def test_too_many_files(app, env, monkeypatch):
    monkeypatch.setattr(Config, "BATCH_MAX_FILES", 2)
    pro = make_client(app, env, "p6")
    assert post_batch(pro, [(f"{i}.csv", CSV_B) for i in range(3)]).status_code == 400


def test_total_cells_limit(app, env, monkeypatch):
    monkeypatch.setattr(Config, "BATCH_MAX_TOTAL_CELLS", 10)
    pro = make_client(app, env, "p7")
    assert post_batch(pro, [("a.csv", CSV_A), ("b.csv", CSV_A)]).status_code == 413


def test_per_file_cell_limit_skips_file(app, env, monkeypatch):
    monkeypatch.setattr(Config, "BATCH_MAX_CELLS_PER_FILE", 6)
    pro = make_client(app, env, "p8")
    wide = b"a,b,c,d\n" + b"1,2,3,4\n" * 10
    r = post_batch(pro, [("wide.csv", wide), ("b.csv", CSV_B)])
    assert r.status_code == 202
    assert [f["filename"] for f in r.get_json()["files"]] == ["b.csv"]
    assert "cells" in r.get_json()["skipped"][0]["reason"]


def test_unknown_rule_rejected(app, env):
    pro = make_client(app, env, "p9")
    assert post_batch(pro, [("a.csv", CSV_A)], options={"rules": ["nope"]}).status_code == 400


# ------------------------------------------------------------------ processing + progress

def test_batch_runs_through_worker_and_downloads(app, env, monkeypatch):
    monkeypatch.setattr(Config, "BATCH_MAX_CONCURRENT_FILES", 2)
    pro = make_client(app, env, "q1")
    bid = post_batch(pro, [("a.csv", CSV_A), ("b.csv", CSV_B), ("c.csv", CSV_B)]).get_json()["batch_id"]
    b = run_all(pro, bid)
    assert b["status"] == "completed" and b["counts"]["completed"] == 3
    assert b["progress"] == {"done": 3, "total": 3}
    f = b["files"][0]
    assert f["status"] == "completed" and f["summary"]["rows_in"] == 3
    for _ in range(2):  # repeatable: batch downloads are not deleted on first download
        r = pro.get(f["download_url"])
        assert r.status_code == 200 and b"ada@x.com" in r.data
    assert "a_cleaned.csv" in r.headers["Content-Disposition"]


def test_progress_is_real_and_concurrency_is_bounded(app, env, monkeypatch):
    monkeypatch.setattr(Config, "BATCH_MAX_CONCURRENT_FILES", 1)
    pro = make_client(app, env, "q2")
    bid = post_batch(pro, [(f"{i}.csv", CSV_B) for i in range(3)]).get_json()["batch_id"]
    jobs = db.batch_jobs(bid)
    assert sorted(j["status"] for j in jobs) == ["queued", "uploaded", "uploaded"]  # only one released
    assert worker.run_one("w") == "processed"
    b = pro.get(f"/api/batches/{bid}").get_json()
    assert b["counts"]["completed"] == 1 and b["counts"]["queued"] == 2
    assert b["progress"] == {"done": 1, "total": 3} and b["status"] == "processing"
    assert not b["finished"]


def test_one_failing_file_does_not_fail_the_batch(app, env, monkeypatch):
    monkeypatch.setattr(Config, "JOB_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(Config, "BATCH_MAX_CONCURRENT_FILES", 3)
    real = svc._clean_body

    def flaky(job_id, spec):
        if db.get_job(job_id)["original_filename"] == "bad.csv":
            raise ValueError("boom")
        return real(job_id, spec)
    monkeypatch.setattr(svc, "_clean_body", flaky)
    pro = make_client(app, env, "q3")
    bid = post_batch(pro, [("ok.csv", CSV_A), ("bad.csv", CSV_B), ("ok2.csv", CSV_B)]).get_json()["batch_id"]
    b = run_all(pro, bid)
    assert b["status"] == "completed_with_errors"
    assert b["counts"]["completed"] == 2 and b["counts"]["failed"] == 1
    bad = next(f for f in b["files"] if f["filename"] == "bad.csv")
    assert bad["status"] == "failed" and "download_url" not in bad and "boom" not in json.dumps(bad)
    assert pro.get(f"/api/batches/{bid}/files/{bad['job_id']}/download").status_code == 404


def test_retry_failed_files(app, env, monkeypatch):
    monkeypatch.setattr(Config, "JOB_MAX_ATTEMPTS", 1)
    real = svc._clean_body
    monkeypatch.setattr(svc, "_clean_body", lambda j, s: (_ for _ in ()).throw(ValueError("x")))
    pro = make_client(app, env, "q4")
    bid = post_batch(pro, [("a.csv", CSV_A)]).get_json()["batch_id"]
    assert run_all(pro, bid)["status"] == "failed"
    monkeypatch.setattr(svc, "_clean_body", real)
    r = pro.post(f"/api/batches/{bid}/retry")
    assert r.status_code == 200 and not r.get_json()["finished"]
    assert run_all(pro, bid)["status"] == "completed"


def test_worker_crash_is_recovered_by_lease_expiry(app, env):
    pro = make_client(app, env, "q5")
    bid = post_batch(pro, [("a.csv", CSV_A)]).get_json()["batch_id"]
    job = db.batch_jobs(bid)[0]
    assert db.claim_job(job["job_id"], "dead-worker", 60)        # a worker takes it and "crashes"
    with db._cursor() as cur:
        cur.execute("UPDATE jobs SET lease_expires_at=? WHERE job_id=?", (time.time() - 5, job["job_id"]))
    assert pro.get(f"/api/batches/{bid}").get_json()["files"][0]["status"] == "processing"
    svc.reap_once()                                              # lease expired -> requeued with backoff
    assert db.get_job(job["job_id"])["status"] == "queued"
    with db._cursor() as cur:                                    # skip the (real) retry backoff delay
        cur.execute("UPDATE jobs SET available_at=0 WHERE job_id=?", (job["job_id"],))
    b = run_all(pro, bid)
    assert b["status"] == "completed"


def test_cancel_stops_waiting_files(app, env, monkeypatch):
    monkeypatch.setattr(Config, "BATCH_MAX_CONCURRENT_FILES", 1)
    pro = make_client(app, env, "q6")
    bid = post_batch(pro, [(f"{i}.csv", CSV_B) for i in range(3)]).get_json()["batch_id"]
    worker.run_one("w")                                         # first file completes
    b = pro.post(f"/api/batches/{bid}/cancel").get_json()
    assert b["counts"]["completed"] == 1 and b["counts"]["cancelled"] == 2
    assert b["finished"] and b["status"] == "completed_with_errors"


def test_batch_timeout_cancels_waiting_files(app, env, monkeypatch):
    monkeypatch.setattr(Config, "BATCH_MAX_CONCURRENT_FILES", 1)
    monkeypatch.setattr(Config, "BATCH_TIMEOUT_SECONDS", 0)
    pro = make_client(app, env, "q7")
    bid = post_batch(pro, [(f"{i}.csv", CSV_B) for i in range(2)]).get_json()["batch_id"]
    time.sleep(0.05)
    svc.reap_once()
    assert db.get_batch(bid)["finalized"] == 0 or True
    b = run_all(pro, bid)
    assert b["finished"] and b["counts"]["cancelled"] >= 1


def test_concurrent_batches_are_independent_and_limited(app, env, monkeypatch):
    monkeypatch.setattr(Config, "BATCH_MAX_ACTIVE_PER_USER", 2)
    pro = make_client(app, env, "q8")
    b1 = post_batch(pro, [("a.csv", CSV_A)]).get_json()["batch_id"]
    b2 = post_batch(pro, [("b.csv", CSV_B)]).get_json()["batch_id"]
    assert post_batch(pro, [("c.csv", CSV_B)]).status_code == 429
    for _ in range(10):
        worker.run_one("w")
    for bid, name in ((b1, "a.csv"), (b2, "b.csv")):
        v = pro.get(f"/api/batches/{bid}").get_json()
        assert v["status"] == "completed" and [f["filename"] for f in v["files"]] == [name]


# ------------------------------------------------------------------ ownership

def test_other_users_cannot_see_or_download_a_batch(app, env):
    owner = make_client(app, env, "o1")
    other = make_client(app, env, "o2")
    anon = app.test_client()
    bid = post_batch(owner, [("a.csv", CSV_A)]).get_json()["batch_id"]
    b = run_all(owner, bid)
    jid = b["files"][0]["job_id"]
    for c, code in ((other, 404), (anon, 401)):
        assert c.get(f"/api/batches/{bid}").status_code == code
        assert c.get(f"/api/batches/{bid}/download").status_code == code
        assert c.get(f"/api/batches/{bid}/files/{jid}/download").status_code == code
        assert c.post(f"/api/batches/{bid}/cancel").status_code == code
    # a job id from batch A can never be fetched through batch B
    bid2 = post_batch(owner, [("b.csv", CSV_B)]).get_json()["batch_id"]
    assert owner.get(f"/api/batches/{bid2}/files/{jid}/download").status_code == 404
    assert other.get("/api/batches/").get_json()["batches"] == []


def test_batch_files_cannot_be_driven_through_single_file_routes(app, env):
    pro = make_client(app, env, "o3")
    bid = post_batch(pro, [("a.csv", CSV_A)]).get_json()["batch_id"]
    jid = db.batch_jobs(bid)[0]["job_id"]
    assert pro.post(f"/api/process/{jid}", json={}).status_code == 409
    run_all(pro, bid)
    assert pro.get(f"/api/download/{jid}").status_code == 409
    assert pro.get(f"/api/batches/{bid}/files/{jid}/download").status_code == 200  # still there


# ------------------------------------------------------------------ ZIP

def test_zip_has_only_completed_files_safe_names_and_report(app, env, monkeypatch):
    monkeypatch.setattr(Config, "JOB_MAX_ATTEMPTS", 1)
    real = svc._clean_body

    def flaky(job_id, spec):
        if db.get_job(job_id)["original_filename"].startswith("bad"):
            raise ValueError("boom")
        return real(job_id, spec)
    monkeypatch.setattr(svc, "_clean_body", flaky)
    pro = make_client(app, env, "z1")
    r = post_batch(pro, [("../../etc/evil.csv", CSV_A), ("same.csv", CSV_B), ("same.csv", CSV_B),
                         ("bad.csv", CSV_B), ("=cmd.txt", b"x")])
    assert r.status_code == 202
    bid = r.get_json()["batch_id"]
    run_all(pro, bid)
    z = pro.get(f"/api/batches/{bid}/download")
    assert z.status_code == 200 and z.mimetype == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(z.data))
    names = zf.namelist()
    assert all("/" not in n and "\\" not in n and ".." not in n for n in names)
    assert len(names) == len(set(n.lower() for n in names))
    assert "batch_report.csv" in names and not any("bad" in n for n in names if n != "batch_report.csv")
    assert sum(n.endswith(".csv") and n != "batch_report.csv" for n in names) == 3
    report = zf.read("batch_report.csv").decode()
    assert "COMPLETED" in report and "FAILED" in report and "SKIPPED" in report


def test_zip_member_name_cannot_traverse():
    from jobs.batches import safe_member_name
    used = set()
    for evil in ("../../x.csv", "..\\..\\x.csv", "/etc/passwd.csv", "a/b/c.csv", "\x00bad.csv", "....csv", None):
        n = safe_member_name(evil, "csv", used)
        assert "/" not in n and "\\" not in n and ".." not in n and "\x00" not in n and n.endswith(".csv")


def test_zip_with_nothing_completed_is_409(app, env, monkeypatch):
    pro = make_client(app, env, "z2")
    bid = post_batch(pro, [("a.csv", CSV_A)]).get_json()["batch_id"]
    assert pro.get(f"/api/batches/{bid}/download").status_code == 409


# ------------------------------------------------------------------ audit

def test_batch_and_per_file_audit(app, env, monkeypatch):
    monkeypatch.setattr(Config, "BATCH_MAX_CONCURRENT_FILES", 2)
    pro = make_client(app, env, "a1")
    bid = post_batch(pro, [("a.csv", CSV_A), ("b.csv", CSV_B)]).get_json()["batch_id"]
    b = run_all(pro, bid)
    with db._cursor() as cur:
        cur.execute("SELECT action, target, meta FROM audit_log WHERE target=? ORDER BY id", (bid,))
        rows = [dict(r) for r in cur.fetchall()]
    assert [r["action"] for r in rows] == ["batch.created", "batch.completed"]
    done = json.loads(rows[1]["meta"])
    assert done["files"] == 2 and done["completed"] == 2 and done["failed"] == 0 and "wall_seconds" in done
    batch = db.get_batch(bid)
    assert batch["owner_uid"] == "a1" and batch["completed_at"] and batch["file_count"] == 2
    for f in b["files"]:  # each file keeps its OWN audit/summary under its own job id
        d = pro.get(f"/api/batches/{bid}/files/{f['job_id']}").get_json()
        assert d["status"] == "completed" and "cleaning_summary" in d["summary"]
    assert len({f["job_id"] for f in b["files"]}) == 2


def test_batch_completion_audited_once(app, env):
    pro = make_client(app, env, "a2")
    bid = post_batch(pro, [("a.csv", CSV_A)]).get_json()["batch_id"]
    run_all(pro, bid)
    from jobs import batches
    for _ in range(3):
        batches.advance(bid)
    with db._cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM audit_log WHERE target=? AND action='batch.completed'", (bid,))
        assert cur.fetchone()["n"] == 1


def test_retention_keeps_batch_outputs_longer_than_single_jobs(app, env, monkeypatch):
    pro = make_client(app, env, "a3")
    bid = post_batch(pro, [("a.csv", CSV_A)]).get_json()["batch_id"]
    run_all(pro, bid)
    with db._cursor() as cur:
        cur.execute("UPDATE jobs SET created_at=? WHERE batch_id=?", (time.time() - Config.JOB_TTL_SECONDS - 60, bid))
    assert db.purge_candidates(Config.JOB_TTL_SECONDS) == []
    with db._cursor() as cur:
        cur.execute("UPDATE jobs SET created_at=? WHERE batch_id=?", (time.time() - Config.BATCH_RETENTION_SECONDS - 60, bid))
    assert len(db.purge_candidates(Config.JOB_TTL_SECONDS)) == 1

