import logging
from flask import Blueprint, jsonify, request

from utils.file_handler import find_source_file, job_owner_hash, maybe_sweep, is_valid_job_id
from utils.session import owns, current_session_hash
from utils.observability import get_request_id
from config import Config
import db as metadata_db
import json
import jobs.service as job_service
from processing.pipeline import DatasetTooLargeError, read_source
from cleaning.quality_report import generate_report
from cleaning.recommendations import generate_recommendations
from cleaning.engine import FeatureNotAvailable
from cleaning.engine.recommend import recommend_dataset
from routes.cleaning import server_side_is_pro

report_bp = Blueprint("report", __name__)


@report_bp.get("/<job_id>")
def get_report(job_id):
    """
    Frontend contract:

    Request:  GET /api/report/<job_id>?has_header=true
    Response: { "job_id": "...", "report": {...}, "recommendations": {...} }
              or { "error": "..." }, 404

    Requires the request's session to be the one that uploaded this
    job, same as /api/process, see utils/session.py.

    Read-only: analyzes the uploaded file as-is. Does not run any
    cleaning rule and does not write anything to the job folder.

    "has_header" (optional query param, default true) is whether the
    first row is a header row, same meaning and default as
    /api/process's "has_header". Passing false here analyzes row 1 as
    ordinary data instead of Omixa assuming it's a header.

    "recommendations" is derived entirely from "report", it groups
    the same findings into rule-backed "safe" fixes vs "ambiguous"
    ones that need manual review, so the frontend can pre-select
    recommended rules before the user hits Clean my data. See
    cleaning/recommendations.py.
    """
    maybe_sweep()

    has_header = request.args.get("has_header", "true").strip().lower() != "false"

    if Config.PROCESSING_MODE == "queue":
        return _queued_report(job_id, has_header)

    source_path = find_source_file(job_id)
    if not source_path:
        return jsonify({"error": "Unknown or expired job_id, or no uploaded file found"}), 404

    if not owns(job_owner_hash(job_id)):
        return jsonify({"error": "Unknown or expired job_id, or no uploaded file found"}), 404

    try:
        df = read_source(source_path, has_header=has_header)
    except DatasetTooLargeError as exc:
        return jsonify({"error": str(exc)}), 422
    except Exception:
        logging.exception("Could not read source file for job %s", job_id)
        return jsonify({"error": "Could not read this file, it may be corrupted, empty, or in an unexpected format."}), 422

    report = generate_report(df, project=True)

    return jsonify({
        "job_id": job_id,
        "report": report,
        "recommendations": generate_recommendations(report),
        "engine_recommendations": _gate_engine_recs(recommend_dataset(df)),
    }), 200


def _gate_engine_recs(recs):
    """Column-type detection + per-column rule recommendations are a Pro capability. The plan
    comes from the server-side subscription record; Free callers get a locked marker, no data."""
    if recs is not None and server_side_is_pro():
        return recs
    body = FeatureNotAvailable("recommendations").to_response()
    return {"locked": True, "code": body["code"], "rule": body["rule"], "required_plan": body["required_plan"]}


def _queued_report(job_id, has_header):
    """Queue mode: analysis is a job like cleaning (pandas never runs in the API). First call
    enqueues it and returns 202; the client polls GET /api/jobs/<id> until it carries
    'analysis'. A finished analysis for the same has_header setting is returned directly."""
    err = {"error": "Unknown or expired job_id, or no uploaded file found"}
    if not is_valid_job_id(job_id):
        return jsonify(err), 404
    job = metadata_db.get_job(job_id)
    if not job or not owns(job.get("session_hash")):
        return jsonify(err), 404

    if job["status"] == "uploaded" and job.get("report_json"):
        try:
            cached = json.loads(job["report_json"])
            if cached.get("has_header") == has_header:
                return jsonify({"job_id": job_id, "report": cached["report"],
                                "recommendations": cached["recommendations"],
                                "engine_recommendations": _gate_engine_recs(cached.get("engine_recommendations"))}), 200
        except ValueError:
            pass
    if job["status"] in ("queued", "running"):
        return jsonify({"job_id": job_id, "status": job["status"]}), 202
    if job["status"] not in ("uploaded", "failed"):
        return jsonify({"error": "This job cannot be analysed right now."}), 409

    is_pro = False
    try:
        admission = job_service.admit(job.get("owner_uid"), current_session_hash(), is_pro)
    except job_service.AdmissionError as exc:
        resp = jsonify({"error": exc.message, "status": "rejected"})
        if exc.retry_after:
            resp.headers["Retry-After"] = str(exc.retry_after)
        return resp, exc.status
    spec = {"kind": "analyze", "has_header": has_header}
    if not job_service.submit(job_id, spec, False, get_request_id()):
        return jsonify({"error": "This job cannot be analysed right now."}), 409
    body = {"job_id": job_id, "status": "queued"}
    if admission["high_traffic"]:
        body["message"] = job_service.HIGH_TRAFFIC_MESSAGE
    return jsonify(body), 202
