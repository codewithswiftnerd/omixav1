import logging
import os
import uuid

from flask import Blueprint, request, jsonify

from config import Config
from utils.file_handler import (
    allowed_file, create_job_dir, delete_job, maybe_sweep, safe_display_name, save_upload,
    set_job_owner, validate_upload_content,
)
from utils.observability import get_request_id
from utils.security import remember_entitlement
from utils.session import current_session_hash
import db as metadata_db
import storage as object_storage
from accounts import auth
from accounts.entitlements import effective, max_upload_bytes
from accounts.store import get_store

upload_bp = Blueprint("upload", __name__)
logger = logging.getLogger("omixa.upload")


def _plan_for_request():
    """(uid, is_pro). Free keeps working if accounts are down."""
    is_pro = False
    uid = auth.current_uid()
    store = get_store() if uid else None
    if uid and store is not None:
        try:
            is_pro = effective(store.get_user(uid))["is_pro"]
        except Exception:
            is_pro = False
        remember_entitlement(uid, is_pro)
    return uid, is_pro


def _too_big(limit, is_pro):
    mb = limit // (1024 * 1024)
    msg = f"This file is over the {mb} MB limit for your plan."
    if not is_pro:
        msg += f" Omixa Pro raises it to {Config.PRO_MAX_UPLOAD_MB} MB."
    return jsonify({"error": msg, "upgrade_required": not is_pro}), 402 if not is_pro else 413


def _stream_size(file) -> int:
    stream = file.stream
    pos = stream.tell()
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(pos)
    return size


@upload_bp.post("/")
def upload_file():
    """
    Frontend contract:

    Request:  multipart/form-data, field name "file"
    Response: { "job_id": "...", "filename": "patients.xlsx", "status": "uploaded" }
              or { "error": "..." }, 400

    Owned by whichever session uploaded it, see utils/session.py.

    Local storage: the file is saved under TEMP_DIR (single instance).
    Object storage (OMIXA_STORAGE_BACKEND=s3): the file is validated, streamed to the private
    bucket and recorded in the shared database, so any API instance / worker can use it.
    """
    maybe_sweep()

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

    owner_hash = current_session_hash()
    display_name = safe_display_name(file.filename, ext)
    uid, is_pro = _plan_for_request()
    limit = max_upload_bytes(is_pro)

    if object_storage.is_remote():
        # Check the plan limit BEFORE spending bandwidth on the bucket.
        size_bytes = _stream_size(file)
        if size_bytes > limit:
            return _too_big(limit, is_pro)
        job_id = str(uuid.uuid4())
        try:
            object_storage.get_storage().put_fileobj(
                object_storage.source_key(job_id, ext), file.stream,
                content_type={"csv": "text/csv"}.get(ext, "application/octet-stream"))
        except object_storage.StorageError:
            logger.exception("Could not store upload for job %s", job_id)
            return jsonify({"error": "Could not save the uploaded file. Please try again."}), 503
        if not metadata_db.record_upload(job_id=job_id, session_hash=owner_hash, ext=ext, filename=display_name,
                                         size_bytes=size_bytes, owner_uid=uid, request_id=get_request_id()):
            object_storage.get_storage().delete_job(job_id)
            return jsonify({"error": "Omixa is temporarily unavailable. Please try again."}), 503
        return jsonify({"job_id": job_id, "filename": display_name, "status": "uploaded"}), 201

    job_id = create_job_dir()
    set_job_owner(job_id, owner_hash)

    try:
        dest = save_upload(file, job_id, ext=ext)
    except Exception:
        # Any failure here (disk full, bad name, ...) must not leave a
        # half-created job folder behind holding user data.
        logger.exception("Could not save upload for job %s", job_id)
        delete_job(job_id)
        return jsonify({"error": "Could not save the uploaded file. Please try again."}), 500

    try:
        size_bytes = os.path.getsize(dest)
    except OSError:
        size_bytes = None

    if size_bytes is not None and size_bytes > limit:
        delete_job(job_id)
        return _too_big(limit, is_pro)

    metadata_db.record_upload(
        job_id=job_id,
        session_hash=owner_hash,
        ext=ext,
        filename=display_name,
        size_bytes=size_bytes,
        owner_uid=uid,
        request_id=get_request_id(),
    )

    return jsonify({
        "job_id": job_id,
        "filename": display_name,
        "status": "uploaded",
    }), 201
