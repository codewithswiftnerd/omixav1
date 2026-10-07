import logging
import os

from flask import Blueprint, request, jsonify

from utils.file_handler import (
    allowed_file, create_job_dir, delete_job, safe_display_name, save_upload,
    set_job_owner, sweep_expired_jobs, validate_upload_content,
)
from utils.session import current_session_hash
import db as metadata_db
from accounts import auth
from accounts.entitlements import effective, max_upload_bytes
from accounts.store import get_store

upload_bp = Blueprint("upload", __name__)
logger = logging.getLogger("omixa.upload")


@upload_bp.post("/")
def upload_file():
    """
    Frontend contract:

    Request:  multipart/form-data, field name "file"
    Response: { "job_id": "...", "filename": "patients.xlsx", "status": "uploaded" }
              or { "error": "..." }, 400

    Owned by whichever session uploaded it — see utils/session.py.
    """
    sweep_expired_jobs()  # no scheduler; sweep opportunistically on traffic

    if "file" not in request.files:
        return jsonify({"error": "No file part in request"}), 400

    file = request.files["file"]

    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400

    if not allowed_file(file.filename):
        return jsonify({"error": "Unsupported file type. Use CSV or Excel."}), 400

    ext = file.filename.rsplit(".", 1)[1].lower()
    content_error = validate_upload_content(file, ext)
    if content_error:
        return jsonify({"error": content_error}), 400

    job_id = create_job_dir()
    owner_hash = current_session_hash()
    set_job_owner(job_id, owner_hash)

    try:
        dest = save_upload(file, job_id, ext=ext)
    except Exception:
        # Any failure here (disk full, bad name, ...) must not leave a
        # half-created job folder behind holding user data.
        logger.exception("Could not save upload for job %s", job_id)
        delete_job(job_id)
        return jsonify({"error": "Could not save the uploaded file. Please try again."}), 500

    display_name = safe_display_name(file.filename, ext)
    try:
        size_bytes = os.path.getsize(dest)
    except OSError:
        size_bytes = None

    # Plan limit (clear message, never silent). Free keeps working if accounts are down.
    is_pro = False
    uid = auth.current_uid()
    store = get_store() if uid else None
    if uid and store is not None:
        try:
            is_pro = effective(store.get_user(uid))["is_pro"]
        except Exception:
            is_pro = False
    limit = max_upload_bytes(is_pro)
    if size_bytes is not None and size_bytes > limit:
        delete_job(job_id)
        mb = limit // (1024 * 1024)
        msg = f"This file is over the {mb} MB limit for your plan."
        if not is_pro:
            msg += " Omixa Pro raises it to 25 MB."
        return jsonify({"error": msg, "upgrade_required": not is_pro}), 402 if not is_pro else 413

    metadata_db.record_upload(
        job_id=job_id,
        session_hash=owner_hash,
        ext=ext,
        filename=display_name,
        size_bytes=size_bytes,
    )

    return jsonify({
        "job_id": job_id,
        "filename": display_name,
        "status": "uploaded",
    }), 201
