"""
Omixa processing worker. Run as its own service:   python worker.py

Does ALL heavy CSV/XLSX work (analysis, cleaning, scoring, export) so API instances stay
stateless and fast. Scale by running more replicas; each processes ONE job at a time, in an
isolated child process with a timeout and memory cap, so size replicas by memory, not by
thread count (see docs/SCALING.md).

Loop:  pop a job id from the queue (or poll the DB) -> claim it in the DB (compare-and-set,
lease) -> download source -> process -> upload result -> mark done. Every few seconds it
also runs the reaper (stuck-lease recovery, lost-message republish, TTL purge).
SIGTERM: finish the current job, claim nothing new, exit (safe for rolling deploys).
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import time
import uuid

import db as metadata_db
from config import Config
from jobs import service
from jobs.queue import get_queue, safe_publish
from utils import observability

logger = logging.getLogger("omixa.worker")
_stop = False


def _on_signal(signum, _frame):
    global _stop
    _stop = True
    logger.info("signal %s received: finishing current job then exiting", signum)


def run_one(worker_id: str) -> str:
    """Try to take and run a single job. Returns 'idle', 'throttled' or the job outcome."""
    if Config.GLOBAL_MAX_RUNNING and metadata_db.count_by_status("running") >= Config.GLOBAL_MAX_RUNNING:
        return "throttled"
    queue = get_queue()
    try:
        job_id = queue.pop()
    except Exception:
        job_id = None  # Redis down: fall through to the DB, which is the source of truth
    if job_id is None:
        due = metadata_db.due_queued(1)
        job_id = due[0]["job_id"] if due else None
    if job_id is None:
        return "idle"
    job = metadata_db.claim_job(job_id, worker_id, Config.JOB_LEASE_SECONDS)
    if job is None:
        # Not claimable: finished/claimed elsewhere (drop), or retry not due yet (park it).
        row = metadata_db.get_job(job_id)
        if row and row["status"] == "queued" and (row.get("available_at") or 0) > time.time():
            safe_publish(job_id, row.get("priority", 1), delay=row["available_at"] - time.time())
        return "idle"
    logger.info("claimed job", extra={"job_id": job_id, "worker_id": worker_id, "attempt": job["attempts"]})
    observability.set_request_id(job.get("request_id") or uuid.uuid4().hex)
    outcome = service.execute_job(job, worker_id)
    logger.info("job finished: %s", outcome, extra={"job_id": job_id, "worker_id": worker_id})
    return outcome


def main() -> None:
    observability.configure_logging(logging.INFO, Config.LOG_FORMAT)
    metadata_db.init_db()
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    worker_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    logger.info("worker starting (%s) queue=%s storage=%s", worker_id, get_queue().name,
                os.environ.get("OMIXA_STORAGE_BACKEND", "local"))
    last_reap = 0.0
    while not _stop:
        try:
            if time.monotonic() - last_reap > 10:
                last_reap = time.monotonic()
                service.reap_once()
            outcome = run_one(worker_id)
        except Exception:
            logger.exception("worker loop error; backing off")
            outcome = "idle"
            time.sleep(2)
        if outcome in ("idle", "throttled"):
            time.sleep(0.5 if outcome == "idle" else 2)
    logger.info("worker stopped")


if __name__ == "__main__":
    main()
