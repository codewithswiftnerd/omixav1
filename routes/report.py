import logging
from flask import Blueprint, jsonify, request

from utils.file_handler import find_source_file, job_owner_hash, sweep_expired_jobs
from utils.session import owns
from processing.pipeline import DatasetTooLargeError, read_source
from cleaning.quality_report import generate_report
from cleaning.recommendations import generate_recommendations

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
    sweep_expired_jobs()

    has_header = request.args.get("has_header", "true").strip().lower() != "false"

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
    }), 200
