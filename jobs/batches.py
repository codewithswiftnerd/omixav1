"""Pro batch processing: a batch is a thin coordinator over ordinary jobs.

Each file is a normal row in `jobs` (own job id, status, error, cleaning summary/audit in
result_json, output object) that runs through the existing queue -> worker -> pipeline path.
The batch only (a) releases its waiting files to the queue a few at a time
(BATCH_MAX_CONCURRENT_FILES), (b) enforces the batch timeout, and (c) finalizes + audits itself
once every file is terminal. Aggregate progress is always computed from the real job rows.
"""
import csv
import io
import json
import logging
import os
import re
import shutil
import tempfile
import time
import zipfile
from typing import Optional

import db as metadata_db
from config import Config
from storage import get_storage, cleaned_key, StorageError

logger = logging.getLogger("omixa.batches")

# job status -> what the batch UI/API shows
FILE_STATUS = {
    "uploaded": "queued",       # accepted, waiting for a free slot in this batch
    "queued": "queued",
    "running": "processing",
    "processed": "completed",
    "downloaded": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "expired": "expired",       # retention passed, output deleted
}
PENDING = {"queued", "processing"}


def file_status(job: dict) -> str:
    return FILE_STATUS.get(job.get("status"), "failed")


def counts(jobs: list[dict], skipped: list) -> dict:
    c = {"queued": 0, "processing": 0, "completed": 0, "failed": 0, "cancelled": 0, "expired": 0}
    for j in jobs:
        c[file_status(j)] += 1
    c["skipped"] = len(skipped)
    c["total"] = len(jobs)
    return c


def aggregate_status(c: dict) -> str:
    if c["queued"] or c["processing"]:
        return "processing" if (c["processing"] or c["completed"] or c["failed"]) else "queued"
    if c["total"] and c["completed"] == c["total"]:
        return "completed"
    if c["completed"]:
        return "completed_with_errors"
    if c["cancelled"] and not c["failed"]:
        return "cancelled"
    return "failed"


def _spec_for(opts: dict, batch_id: str) -> dict:
    spec = dict(opts.get("spec") or {})
    spec["batch_id"] = batch_id
    return spec


def advance(batch_id: str) -> Optional[str]:
    """Idempotent, safe to call from every worker and the reaper. Returns the final status when
    this call finalized the batch, else None."""
    import jobs.service as service

    batch = metadata_db.get_batch(batch_id)
    if not batch or batch["finalized"]:
        return None
    # No lock: releasing a file is a compare-and-set in enqueue_job, so two workers advancing the
    # same batch cannot enqueue a file twice; at worst the concurrency cap is briefly exceeded by one.
    opts = json.loads(batch.get("options_json") or "{}")
    jobs = metadata_db.batch_jobs(batch_id)

    if time.time() - (batch.get("updated_at") or batch["created_at"]) > Config.BATCH_TIMEOUT_SECONDS:
        n = metadata_db.cancel_batch_pending(batch_id)
        if n:
            metadata_db.audit(batch["owner_uid"], "batch.timeout", batch_id, meta={"cancelled_files": n})
            jobs = metadata_db.batch_jobs(batch_id)
    else:
        active = sum(1 for j in jobs if j["status"] in ("queued", "running"))
        cap = max(1, Config.BATCH_MAX_CONCURRENT_FILES)
        for j in jobs:
            if active >= cap:
                break
            if j["status"] == "uploaded":
                if service.submit(j["job_id"], _spec_for(opts, batch_id), True, batch.get("request_id")):
                    active += 1
        jobs = metadata_db.batch_jobs(batch_id)

    if any(j["status"] in ("uploaded", "queued", "running") for j in jobs):
        return None
    skipped = json.loads(batch.get("skipped_json") or "[]")
    c = counts(jobs, skipped)
    status = aggregate_status(c)
    if metadata_db.finalize_batch(batch_id, status):
        done_at = time.time()
        metadata_db.audit(batch["owner_uid"], "batch.completed", batch_id, meta={
            "status": status, "files": c["total"], "completed": c["completed"], "failed": c["failed"],
            "cancelled": c["cancelled"], "skipped": c["skipped"],
            "created_at": batch["created_at"], "completed_at": done_at,
            "wall_seconds": round(done_at - batch["created_at"], 1),
            "processing_ms_total": sum(int(j.get("processing_ms") or 0) for j in jobs),
        })
        return status
    return None


# ----------------------------------------------------------------------------- views

def _rules(job: dict) -> list:
    try:
        return json.loads(job.get("rules_applied") or "[]")
    except ValueError:
        return []


def file_view(batch_id: str, job: dict) -> dict:
    st = file_status(job)
    out = {
        "job_id": job["job_id"],
        "filename": job.get("original_filename"),
        "size_bytes": job.get("file_size_bytes"),
        "status": st,
    }
    if st == "failed":
        out["error"] = job.get("error_public") or "Could not process this file."
    if st == "completed":
        out["summary"] = {
            "rows_in": job.get("rows_in"), "rows_out": job.get("rows_out"),
            "quality_before": job.get("quality_score_before"), "quality_after": job.get("quality_score_after"),
            "rules_applied": _rules(job), "processing_ms": job.get("processing_ms"),
        }
        out["download_url"] = f"/api/batches/{batch_id}/files/{job['job_id']}/download"
    return out


def batch_view(batch: dict, jobs: list[dict]) -> dict:
    skipped = json.loads(batch.get("skipped_json") or "[]")
    c = counts(jobs, skipped)
    status = batch["status"] if batch["finalized"] else aggregate_status(c)
    return {
        "batch_id": batch["batch_id"],
        "status": status,
        "finished": bool(batch["finalized"]),
        "created_at": batch["created_at"],
        "completed_at": batch.get("completed_at"),
        "total_bytes": batch.get("total_bytes"),
        "counts": c,
        "progress": {"done": c["completed"] + c["failed"] + c["cancelled"] + c["expired"], "total": c["total"]},
        "files": [file_view(batch["batch_id"], j) for j in jobs],
        "skipped": skipped,
        "retention_hours": round(Config.BATCH_RETENTION_SECONDS / 3600, 1),
        "download_all_url": f"/api/batches/{batch['batch_id']}/download" if c["completed"] else None,
    }


# ----------------------------------------------------------------------------- ZIP

_NAME_BAD = re.compile(r"[^A-Za-z0-9._ \-()]")


def safe_member_name(original: Optional[str], ext: str, used: set) -> str:
    """Archive member names are generated here, never taken verbatim from the client: no
    directories, no '..', no control characters, always a plain 'name_cleaned.ext'."""
    stem = os.path.basename((original or "file").replace("\\", "/"))
    stem = stem.rsplit(".", 1)[0] if "." in stem else stem
    stem = _NAME_BAD.sub("_", stem).strip(" .") or "file"
    stem = stem[:80]
    name, n = f"{stem}_cleaned.{ext}", 2
    while name.lower() in used:
        name = f"{stem}_cleaned ({n}).{ext}"
        n += 1
    used.add(name.lower())
    return name


def cleaned_download_name(job: dict) -> str:
    return safe_member_name(job.get("original_filename"), job.get("cleaned_ext") or "csv", set())


def _csv_row(values) -> list:
    from export.exporter import _defuse_cell  # filenames/errors are user-influenced text
    return [_defuse_cell(v) if isinstance(v, str) else v for v in values]


def build_zip(batch: dict, jobs: list[dict]):
    """Returns an open, rewound temp file holding the ZIP. Only COMPLETED files are included;
    batch_report.csv lists every file as COMPLETED / FAILED / SKIPPED with the reason."""
    storage = get_storage()
    spool = tempfile.SpooledTemporaryFile(max_size=32 * 1024 * 1024)
    tmpdir = tempfile.mkdtemp(prefix="omx-batchzip-")
    used = {"batch_report.csv"}
    report = io.StringIO()
    w = csv.writer(report)
    w.writerow(["file", "status", "detail", "rows_in", "rows_out", "quality_before", "quality_after", "output_file"])
    try:
        with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED) as zf:
            for j in jobs:
                st = file_status(j)
                if st == "completed":
                    ext = j.get("cleaned_ext") or "csv"
                    key = cleaned_key(j["job_id"], ext)
                    member = safe_member_name(j.get("original_filename"), ext, used)
                    tmp = os.path.join(tmpdir, f"{j['job_id']}.{ext}")
                    try:
                        storage.get_to_path(key, tmp)
                    except StorageError:
                        w.writerow(_csv_row([j.get("original_filename"), "FAILED", "output no longer available", "", "", "", "", ""]))
                        continue
                    zf.write(tmp, arcname=member)
                    w.writerow(_csv_row([j.get("original_filename"), "COMPLETED", "", j.get("rows_in"), j.get("rows_out"),
                                         j.get("quality_score_before"), j.get("quality_score_after"), member]))
                elif st == "failed":
                    w.writerow(_csv_row([j.get("original_filename"), "FAILED", j.get("error_public") or "Could not process this file.", "", "", "", "", ""]))
                else:
                    w.writerow(_csv_row([j.get("original_filename"), "SKIPPED", f"not processed ({st})", "", "", "", "", ""]))
            for s in json.loads(batch.get("skipped_json") or "[]"):
                w.writerow(_csv_row([s.get("filename"), "SKIPPED", s.get("reason"), "", "", "", "", ""]))
            zf.writestr("batch_report.csv", report.getvalue())
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    spool.seek(0)
    return spool
