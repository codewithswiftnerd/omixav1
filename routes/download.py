import os
import mimetypes
import logging
from io import BytesIO
from flask import Blueprint, send_file, jsonify, redirect, request

import storage as object_storage
from config import Config

from utils.file_handler import job_dir_path, job_owner_hash, delete_job, maybe_sweep
from utils.session import owns
import db as metadata_db

download_bp = Blueprint("download", __name__)


@download_bp.get("/<job_id>")
def download_file(job_id):
    """
    Frontend contract:

    Request:  GET /api/download/<job_id>
    Response: the cleaned file as an attachment, then the job's
              temp data is discarded (no database, no persistence).

    Requires the request's session to be the one that uploaded this
    job, same as /api/process and /api/report, see utils/session.py.
    """
    maybe_sweep()

    d = job_dir_path(job_id)
    if not d:
        return jsonify({"error": "Invalid job_id"}), 400

    if object_storage.is_remote():
        return _remote_download(job_id)

    if not owns(job_owner_hash(job_id)):
        # Same "not found" shape for a non-owner as for a real 404.
        return jsonify({
            "error": "No cleaned file ready for this job",
            "detail": "job not found, the job_id is wrong or has expired",
        }), 404

    dir_exists = os.path.isdir(d)
    listing = os.listdir(d) if dir_exists else []

    cleaned = None
    for name in listing:
        if name.startswith("cleaned."):
            cleaned = os.path.join(d, name)
            break

    if not cleaned:
        reason = (
            "job not found, the job_id is wrong or has expired "
            "(job data is deleted after download or after 30 minutes)"
            if not dir_exists
            else "this job hasn't been processed yet, call /api/process first"
        )
        return jsonify({"error": "No cleaned file ready for this job", "detail": reason}), 404

    filename = os.path.basename(cleaned)
    mimetype = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    try:
        # Read the file into memory BEFORE deleting the job folder.
        # Deleting it while send_file still holds the path open used to
        # blow up with a 500 on Windows ("file in use by another
        # process"), os.remove() can't touch a file another
        # process/handle still has open there, unlike POSIX where an
        # unlinked-but-open file keeps working fine. Reading it into a
        # BytesIO first means the bytes are already ours by the time
        # delete_job() runs, so there's nothing left to race.
        with open(cleaned, "rb") as f:
            data = f.read()
    except OSError:
        logging.exception("Could not read cleaned file for job %s", job_id)
        return jsonify({"error": "Could not read the cleaned file for this job"}), 500

    # Discard temp data now that the file is safely in memory.
    metadata_db.record_downloaded(job_id)
    delete_job(job_id)

    return send_file(
        BytesIO(data),
        as_attachment=True,
        download_name=filename,
        mimetype=mimetype,
    )


def _remote_download(job_id):
    """Object-storage mode: the file never passes through the API. The caller gets a
    short-lived presigned URL (private bucket; credentials never leave the server).

      GET /api/download/<id>?format=url  -> 200 {"url": "...", "filename": "...", "expires_in": 300}
      GET /api/download/<id>             -> 302 to the same URL (plain link / navigation)
    """
    nope = jsonify({"error": "No cleaned file ready for this job",
                    "detail": "job not found, the job_id is wrong or has expired"}), 404
    job = metadata_db.get_job(job_id)
    if not job or not owns(job.get("session_hash")):
        return nope
    if job["status"] not in ("processed", "downloaded") or not job.get("cleaned_ext"):
        return jsonify({"error": "No cleaned file ready for this job",
                        "detail": "this job hasn't been processed yet"}), 404
    storage = object_storage.get_storage()
    key = object_storage.cleaned_key(job_id, job["cleaned_ext"])
    if not storage.exists(key):
        return nope
    stem = (job.get("original_filename") or "cleaned").rsplit(".", 1)[0]
    filename = f"cleaned_{stem}.{job['cleaned_ext']}"
    url = storage.presigned_get(key, filename)
    metadata_db.record_downloaded(job_id)
    storage.delete(object_storage.source_key(job_id, job["original_ext"]))  # original no longer needed
    if request.args.get("format") == "url":
        return jsonify({"url": url, "filename": filename, "expires_in": Config.SIGNED_URL_TTL_SECONDS}), 200
    return redirect(url, code=302)
