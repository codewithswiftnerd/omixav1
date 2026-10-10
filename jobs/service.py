"""
Job admission, submission, isolated execution and recovery. Used by the API (admit/submit/
status) and by worker.py (execute/reap). The API never runs pandas in queue mode.
"""

from __future__ import annotations

import json
import logging
import multiprocessing as mp
import os
import shutil
import time
from typing import Callable, Optional

import db as metadata_db
from config import Config
from jobs.queue import get_queue, safe_publish
from storage import cleaned_key, get_storage, is_remote, source_key
from utils import observability, redis_client
from utils.file_handler import cleaned_file_path, job_dir_path
from utils.file_handler import delete_job as delete_local_job

logger = logging.getLogger("omixa.jobs")

PRIORITY_PRO, PRIORITY_FREE = 0, 1
HIGH_TRAFFIC_MESSAGE = "Omixa is currently processing high traffic. Your job has been queued."


class AdmissionError(Exception):
    def __init__(self, status: int, message: str, retry_after: int = 0):
        super().__init__(message)
        self.status, self.message, self.retry_after = status, message, retry_after


class JobFailed(Exception):
    """A job attempt failed. `public_message` is safe to show the user (no paths/data)."""
    def __init__(self, error_type: str, public_message: str, retryable: bool, detail: str = ""):
        super().__init__(detail or public_message)
        self.error_type, self.public_message, self.retryable = error_type, public_message, retryable


class LeaseLost(Exception):
    pass


# ------------------------------------------------------------------ admission

def queue_depth() -> int:
    q = get_queue()
    try:
        d = q.depth()
    except Exception as exc:
        redis_client.note_failure("queue.depth", exc)
        d = None
    return d if d is not None else metadata_db.count_by_status("queued")


def active_limit(is_pro: bool, signed_in: bool) -> int:
    if is_pro:
        return Config.MAX_ACTIVE_JOBS_PRO
    return Config.MAX_ACTIVE_JOBS_USER if signed_in else Config.MAX_ACTIVE_JOBS_ANON


def admit(owner_uid: Optional[str], session_hash: str, is_pro: bool) -> dict:
    """Protects the fleet before a job is created. Raises AdmissionError to refuse.
    Returns {'high_traffic': bool}."""
    limit = active_limit(is_pro, bool(owner_uid))
    if limit and metadata_db.count_active_for(owner_uid, session_hash) >= limit:
        raise AdmissionError(429, f"You already have {limit} file(s) being processed. "
                                  "Please wait for one to finish.", retry_after=10)
    depth = queue_depth()
    if Config.QUEUE_HARD_LIMIT and depth >= Config.QUEUE_HARD_LIMIT:
        observability.inc("omixa_jobs_rejected_total", reason="queue_full")
        raise AdmissionError(503, "Omixa is at capacity right now. Please try again in a minute.",
                             retry_after=30)
    return {"high_traffic": bool(Config.QUEUE_SOFT_LIMIT and depth >= Config.QUEUE_SOFT_LIMIT)}


def submit(job_id: str, spec: dict, is_pro: bool, request_id: Optional[str]) -> bool:
    priority = PRIORITY_PRO if is_pro else PRIORITY_FREE
    ok = metadata_db.enqueue_job(job_id, spec, priority, request_id)
    if ok:
        safe_publish(job_id, priority)
        observability.inc("omixa_jobs_submitted_total", kind=spec.get("kind", "clean"))
    return ok


# --------------------------------------------------------------------- status

def public_status(job: dict, include_pro_analysis: bool = False) -> dict:
    """What a client may see about its own job. No internal paths, leases, specs or owner ids."""
    status = job["status"]
    out = {"job_id": job["job_id"], "status": status, "attempts": job.get("attempts") or 0}
    if status == "queued":
        pos = None
        try:
            pos = get_queue().position(job["job_id"])
        except Exception:
            pass
        if pos:
            out["queue_position"] = pos
    if status == "failed":
        out["error"] = job.get("error_public") or "Could not process this file."
    if status == "processed" and job.get("result_json"):
        try:
            out["summary"] = json.loads(job["result_json"])
        except ValueError:
            pass
    if status == "uploaded" and job.get("report_json"):
        try:
            out["analysis"] = json.loads(job["report_json"])
            if not include_pro_analysis and "engine_recommendations" in out["analysis"]:
                # Pro capability: the cached analysis is plan-independent, the view is not
                out["analysis"]["engine_recommendations"] = {
                    "locked": True, "code": "FEATURE_NOT_AVAILABLE", "rule": "recommendations", "required_plan": "pro"}
        except ValueError:
            pass
    return out


# ------------------------------------------------------------- isolated runner

def _child_main(conn, target, args, mem_mb):
    try:
        if mem_mb:
            try:
                import resource
                limit = int(mem_mb) * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
            except Exception:
                pass
        from processing.pipeline import DatasetTooLargeError
        from cleaning.engine import FeatureNotAvailable, RuleConfigError
        try:
            conn.send(("ok", target(*args)))
        except RuleConfigError as exc:
            conn.send(("config", str(exc)))
        except FeatureNotAvailable as exc:
            conn.send(("config", str(exc)))
        except DatasetTooLargeError as exc:
            conn.send(("too_large", str(exc)))
        except MemoryError:
            conn.send(("memory", ""))
        except FileNotFoundError:
            conn.send(("missing", ""))
        except Exception as exc:
            logger.error("job child failed: %s", type(exc).__name__)
            conn.send(("error", type(exc).__name__, str(exc)))
    finally:
        conn.close()


def run_isolated(target: Callable, args: tuple, timeout: float, mem_mb: int = 0,
                 on_tick: Optional[Callable[[], bool]] = None, tick_every: float = 10.0):
    """Runs target(*args) in a child process with a hard timeout and (Linux) an address-space
    cap. A runaway or memory-hungry file kills only the child; the worker survives and the
    job fails or retries cleanly. `on_tick` runs periodically (lease heartbeat); returning
    False means the lease was lost and the child is killed."""
    ctx = mp.get_context("fork" if hasattr(os, "fork") else "spawn")
    parent, child = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_child_main, args=(child, target, args, mem_mb), daemon=True)
    proc.start()
    child.close()
    start = last_tick = time.monotonic()
    try:
        while True:
            if parent.poll(0.5):
                msg = parent.recv()
                break
            now = time.monotonic()
            if not proc.is_alive() and not parent.poll(0.2):
                raise JobFailed("WorkerCrashed", "This file is too large or complex to process within Omixa's limits.",
                                retryable=False, detail=f"child exit {proc.exitcode}")
            if now - start > timeout:
                raise JobFailed("Timeout", "Processing took too long. Try a smaller file.", retryable=False)
            if on_tick and now - last_tick >= tick_every:
                last_tick = now
                if not on_tick():
                    raise LeaseLost()
    except EOFError:
        raise JobFailed("WorkerCrashed", "This file is too large or complex to process within Omixa's limits.",
                        retryable=False, detail="child closed pipe")
    finally:
        if proc.is_alive():
            proc.kill()
        proc.join(5)
        parent.close()

    kind = msg[0]
    if kind == "ok":
        return msg[1]
    if kind == "too_large":
        raise JobFailed("DatasetTooLargeError", msg[1], retryable=False)
    if kind == "config":
        raise JobFailed("CleaningConfigError", msg[1], retryable=False)
    if kind == "memory":
        raise JobFailed("MemoryError", "This file is too large or complex to process within Omixa's limits.",
                        retryable=False)
    if kind == "missing":
        raise JobFailed("FileNotFoundError", "Source file not found for this job, it may have expired.",
                        retryable=False)
    raise JobFailed(msg[1], "Could not process this file, it may be corrupted, empty, or in an unexpected format.",
                    retryable=msg[1] in ("OSError", "ConnectionError", "TimeoutError"), detail=msg[2])


# ---------------------------------------------------------------- job bodies
# (module-level functions so they can run in a forked child)

def _clean_body(job_id: str, spec: dict):
    from processing.pipeline import run_pipeline
    return run_pipeline(job_id, rules=spec.get("rules"), resolutions=spec.get("resolutions"),
                        has_header=spec.get("has_header", True), pro=bool(spec.get("pro")),
                        profile=spec.get("profile"), cleaning_profile=spec.get("cleaning_profile"),
                        max_cells=int(spec.get("max_cells") or 0), leave_as_is=spec.get("leave_as_is"))


def _analyze_body(job_id: str, spec: dict):
    from cleaning.quality_report import generate_report
    from cleaning.recommendations import generate_recommendations
    from processing.pipeline import read_source
    from utils.file_handler import find_source_file
    path = find_source_file(job_id)
    if not path:
        raise FileNotFoundError("source")
    has_header = spec.get("has_header", True)
    from cleaning.engine.recommend import recommend_dataset
    df = read_source(path, has_header=has_header)
    report = generate_report(df, project=True)
    # computed for everyone (it is sampled and cheap) so the cached analysis is plan-independent;
    # the API decides who may SEE it (Pro), see routes/report.py
    from cleaning.validation import validate_dataframe
    return {"has_header": has_header, "report": report, "recommendations": generate_recommendations(report),
            "engine_recommendations": recommend_dataset(df), "validation": validate_dataframe(df)}


def _content_type(ext: str) -> str:
    return {"csv": "text/csv", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "xls": "application/vnd.ms-excel"}.get(ext, "application/octet-stream")


def _save_pro_session(job: dict, spec: dict, summary: dict, processing_ms: int) -> None:
    """Same record the inline path writes (routes/process.py), for Pro jobs run by a worker."""
    if not (spec.get("pro") and spec.get("uid") and summary.get("pro")):
        return
    try:
        from accounts.store import get_store
        from proquality.analysis import session_record
        store = get_store()
        if store is None:
            return
        name = job.get("original_filename") or "dataset"
        record = session_record(
            dataset_name=name, ext=(name.rsplit(".", 1)[-1].lower() if "." in name else ""),
            size_bytes=job.get("file_size_bytes") or 0, has_header=spec.get("has_header", True),
            rules_applied=summary.get("rules_applied") or [], processing_ms=processing_ms,
            pro_block=summary["pro"])
        summary["pro"]["session_id"] = store.add_session(spec["uid"], record)
    except Exception:
        logger.exception("Could not save the processing session for job %s", job["job_id"])


def execute_job(job: dict, worker_id: str) -> str:
    """Runs one CLAIMED job to a terminal-or-retry state. Returns the outcome string.
    Batch files additionally let their batch release the next waiting file / finalize."""
    outcome = _execute_job(job, worker_id)
    if job.get("batch_id"):
        try:
            from jobs import batches
            batches.advance(job["batch_id"])
        except Exception:
            logger.exception("batch advance failed for %s", job.get("batch_id"))
    return outcome


def _execute_job(job: dict, worker_id: str) -> str:
    """Runs one CLAIMED job to a terminal-or-retry state. Returns the outcome string."""
    job_id, ext = job["job_id"], job["original_ext"]
    spec = json.loads(job.get("spec_json") or "{}")
    kind = spec.get("kind", "clean")
    storage = get_storage()
    scratch = job_dir_path(job_id)
    os.makedirs(scratch, exist_ok=True)
    started = time.monotonic()

    def tick() -> bool:
        return metadata_db.heartbeat(job_id, worker_id, Config.JOB_LEASE_SECONDS)

    try:
        if not storage.exists(source_key(job_id, ext)):
            raise JobFailed("SourceMissing", "Source file not found for this job, it may have expired.",
                            retryable=False)
        storage.get_to_path(source_key(job_id, ext), os.path.join(scratch, f"source.{ext}"))
        body = _analyze_body if kind == "analyze" else _clean_body
        result = run_isolated(body, (job_id, spec), Config.JOB_TIMEOUT_SECONDS,
                              Config.JOB_MEMORY_LIMIT_MB, on_tick=tick,
                              tick_every=max(1.0, Config.JOB_LEASE_SECONDS / 3))
        processing_ms = round((time.monotonic() - started) * 1000)

        if kind == "analyze":
            ok = metadata_db.complete_analysis(job_id, worker_id, json.dumps(result, default=str))
            outcome = "analyzed" if ok else "lost"
        else:
            out_ext = "xlsx" if ext == "xls" else ext
            storage.put_path(cleaned_key(job_id, out_ext), cleaned_file_path(job_id, out_ext), _content_type(out_ext))
            _save_pro_session(job, spec, result, processing_ms)
            cs = result.get("cleaning_summary") or {}
            ok = metadata_db.record_processed(
                job_id, rows_in=result.get("rows_in", 0), rows_out=result.get("rows_out", 0),
                columns_in=cs.get("counts", {}).get("columns_processed", 0),
                quality_before=result.get("quality_report_before"), quality_after=result.get("quality_report"),
                rules_applied=result.get("rules_applied") or [], processing_ms=processing_ms,
                worker_id=worker_id, result_json=json.dumps(result, default=str), cleaned_ext=out_ext)
            outcome = "processed" if ok else "lost"
            if ok:
                metadata_db.usage_add(spec.get("uid") or job["session_hash"][:16],
                                      result.get("rows_out", 0), job.get("file_size_bytes") or 0)
        observability.inc("omixa_jobs_finished_total", kind=kind, outcome=outcome)
        observability.inc("omixa_job_processing_ms_total", processing_ms, kind=kind)
        return outcome
    except LeaseLost:
        observability.inc("omixa_jobs_finished_total", kind=kind, outcome="lease_lost")
        logger.warning("lease lost for job %s; another worker owns it now", job_id)
        return "lost"
    except JobFailed as exc:
        return _record_failure(job_id, worker_id, kind, exc.error_type, str(exc), exc.retryable, exc.public_message)
    except Exception as exc:  # storage/db hiccups etc.: transient until proven otherwise
        logger.exception("job %s attempt failed", job_id)
        return _record_failure(job_id, worker_id, kind, type(exc).__name__, str(exc), True,
                               "Could not process this file. Please try again.")
    finally:
        if is_remote():
            shutil.rmtree(scratch, ignore_errors=True)


def _record_failure(job_id, worker_id, kind, error_type, detail, retryable, public) -> str:
    res = metadata_db.fail_or_retry(job_id, worker_id, error_type, detail, retryable, public)
    observability.inc("omixa_jobs_finished_total", kind=kind, outcome=res)
    if res == "retry":
        job = metadata_db.get_job(job_id) or {}
        safe_publish(job_id, job.get("priority", 1), delay=max(0, (job.get("available_at") or 0) - time.time()))
    elif res == "dead":
        get_queue().dead_letter(job_id, error_type)
        metadata_db.audit("worker", "job.dead_letter", job_id, meta={"error_type": error_type})
        logger.error("job %s dead-lettered after retries (%s)", job_id, error_type)
    return res


# -------------------------------------------------------------------- reaper

def reap_once() -> dict:
    """Housekeeping that makes the system self-healing. Idempotent and safe to run from every
    worker (each step is a compare-and-set); a Redis lock just avoids redundant work."""
    q = get_queue()
    stats = {"leases_released": 0, "republished": 0, "promoted": 0, "purged": 0}
    if not q.try_lock("omx:q:reaper-lock", 20):
        return stats
    for row in metadata_db.expired_leases():
        res = metadata_db.release_expired_lease(row["job_id"])
        if res == "retry":
            stats["leases_released"] += 1
            job = metadata_db.get_job(row["job_id"]) or {}
            safe_publish(row["job_id"], job.get("priority", 1),
                         delay=max(0, (job.get("available_at") or 0) - time.time()))
        elif res == "dead":
            stats["leases_released"] += 1
            q.dead_letter(row["job_id"], "LeaseExpired")
            metadata_db.audit("reaper", "job.dead_letter", row["job_id"], meta={"error_type": "LeaseExpired"})
    for row in metadata_db.stale_queued(Config.QUEUED_STALE_SECONDS):
        if safe_publish(row["job_id"], row["priority"]):
            stats["republished"] += 1
    try:
        stats["promoted"] = q.promote_due()
    except Exception as exc:
        redis_client.note_failure("queue.promote", exc)
    purge = metadata_db.purge_candidates(Config.JOB_TTL_SECONDS)
    done = []
    for row in purge:
        get_storage().delete_job(row["job_id"])
        delete_local_job(row["job_id"])
        done.append(row["job_id"])
    metadata_db.mark_purged(done)
    stats["purged"] = len(done)
    try:  # batches: release waiting files, enforce the batch timeout, finalize finished batches
        from jobs import batches
        for bid in metadata_db.active_batch_ids():
            batches.advance(bid)
    except Exception:
        logger.exception("batch reaper step failed")
    return stats
