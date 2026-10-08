import logging

from flask import Blueprint, jsonify

import db as metadata_db
import jobs.service as job_service
from utils import redis_client
from utils.file_handler import is_valid_job_id
from utils.session import owns

jobs_bp = Blueprint("jobs", __name__)
logger = logging.getLogger("omixa.jobs")

_CACHE_SECONDS = 2  # absorbs polling bursts; status changes appear within ~2s


@jobs_bp.get("/<job_id>")
def job_status(job_id):
    """
    Poll a job.  Response: { job_id, status: queued|running|processed|failed|uploaded|..., ... }
      queued    -> may include queue_position and (under load) a high-traffic message
      processed -> includes the same `summary` the synchronous API used to return
      uploaded  -> includes `analysis` after an analysis job finished
      failed    -> includes a safe `error`
    Non-owners get the same 404 as a missing job. Only the already-authorised payload is
    cached (never the owner id), keyed by job id.
    """
    not_found = jsonify({"error": "Unknown or expired job_id"}), 404
    if not is_valid_job_id(job_id):
        return not_found
    try:
        job = metadata_db.get_job(job_id)
    except Exception:
        logger.exception("status lookup failed")
        return jsonify({"error": "Omixa is temporarily unavailable. Please try again shortly."}), 503
    if not job or not owns(job.get("session_hash")):
        return not_found

    r = redis_client.get_redis()
    key = f"omx:js:{job_id}:{job['status']}:{int(job.get('updated_at') or 0)}"
    if r is not None:
        try:
            import json
            cached = r.get(key)
            if cached:
                return jsonify(json.loads(cached)), 200
        except Exception as exc:
            redis_client.note_failure("job_status.cache", exc)
    from routes.cleaning import server_side_is_pro
    payload = job_service.public_status(job, include_pro_analysis=server_side_is_pro())
    if r is not None:
        try:
            import json
            r.set(key, json.dumps(payload, default=str), ex=_CACHE_SECONDS)
        except Exception as exc:
            redis_client.note_failure("job_status.cache", exc)
    return jsonify(payload), 200
