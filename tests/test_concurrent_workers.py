"""Concurrent processing across devices (real worker processes, shared DB + storage).

Root cause of 'device B waits for device A': a worker process runs ONE job at a time (by design, each
job runs in an isolated child), so with a single worker process every user's job is serialised behind
the previous one. OMIXA_WORKER_PROCESSES=N starts N independent workers (the database claim is a
compare-and-set, so no job runs twice). This verifies both halves on the supported configuration
(queue mode, local storage, no Redis): 1 worker serialises, 2 workers overlap, and results stay isolated.
Limit: overlap proves concurrency, not speed-up; speed-up needs >1 CPU.
"""
import json, os, subprocess, sys

DRIVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "concurrency_driver.py")


def _run(workers, rows=1500):
    p = subprocess.run([sys.executable, DRIVER, str(workers), str(rows)], capture_output=True, text=True, timeout=420)
    line = [l for l in p.stdout.splitlines() if l.startswith("RESULT ")]
    assert line, f"driver produced no result\nSTDOUT tail: {p.stdout[-800:]}\nSTDERR tail: {p.stderr[-800:]}"
    return json.loads(line[0][len("RESULT "):])


def _window(seen):
    return seen["running"], seen["processed"]


def test_two_worker_processes_run_two_devices_jobs_concurrently_and_isolated():
    r = _run(2)
    assert r["submit_codes"] == {"A": 202, "B": 202}
    (ra, da), (rb, db) = _window(r["seen"]["A"]), _window(r["seen"]["B"])
    assert ra < db and rb < da, f"jobs did not overlap: A={ra, da} B={rb, db}"   # each started before the other finished
    assert r["own_rows_only"] == {"A": True, "B": True}                           # every row back, none from the other job
    assert r["cross_access"] == [404, 404]                                        # B cannot read A's status or file


def test_single_worker_process_serialises_jobs_documenting_the_limitation():
    r = _run(1)
    (ra, da), (rb, db) = _window(r["seen"]["A"]), _window(r["seen"]["B"])
    first_done, second_start = (da, rb) if ra <= rb else (db, ra)
    assert second_start >= first_done - 1.0, f"unexpected overlap with one worker: A={ra, da} B={rb, db}"
    assert r["own_rows_only"] == {"A": True, "B": True}
