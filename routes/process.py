import logging
import time

from flask import Blueprint, request, jsonify

from utils.file_handler import find_source_file, job_dir_path, job_owner_hash, sweep_expired_jobs
from utils.session import owns
from processing.pipeline import DatasetTooLargeError, run_pipeline
from cleaning.rules import RULE_DISPATCH
import db as metadata_db

process_bp = Blueprint("process", __name__)
logger = logging.getLogger("omixa.process")


@process_bp.post("/<job_id>")
def process_file(job_id):
    """
    Frontend contract:

    Request:  POST /api/process/<job_id>
              optional JSON body: {
                "rules": ["missing_values", "duplicates", "formatting"],
                "resolutions": [
                  {"column": "signup_date", "issue": "ambiguous_date_format", "choice": "day_first"}
                ],
                "has_header": true
              }
    Response: { "job_id": "...", "status": "completed", "summary": {...} }
              or { "status": "failed", "error": "..." }, 422

    Requires the request's session to own this job (utils/session.py);
    a mismatched session gets the same 404 as a nonexistent job_id.

    "resolutions" is optional and additive. See cleaning/resolutions.py.

    "has_header" (optional, default true) tells Omixa whether the
    first row of the file is a header row. Omixa never assumes this
    on its own, see processing/pipeline.py's read_source().

    "rules" follows three distinct behaviors (see cleaning.rules.apply_rules):
      - omitted / null -> run the default rule set
      - []              -> run NO cleaning rules (user unchecked everything)
      - [...]           -> run only the rules explicitly named
    """
    sweep_expired_jobs()

    if not job_dir_path(job_id) or not find_source_file(job_id):
        return jsonify({"error": "Unknown or expired job_id, or no uploaded file found"}), 404

    if not owns(job_owner_hash(job_id)):
        # Deliberately identical to the "doesn't exist" response above:
        # this must not confirm to a non-owner that a given job_id is
        # real, only that it isn't usable by them.
        return jsonify({"error": "Unknown or expired job_id, or no uploaded file found"}), 404

    body = request.get_json(silent=True) or {}
    rules = body.get("rules")
    resolutions = body.get("resolutions")
    has_header = body.get("has_header", True)

    if not isinstance(has_header, bool):
        return jsonify({"error": "'has_header' must be true or false"}), 400

    if rules is not None:
        if not isinstance(rules, list) or not all(isinstance(r, str) for r in rules):
            return jsonify({"error": "'rules' must be a list of rule name strings"}), 400
        unknown = [r for r in rules if r not in RULE_DISPATCH]
        if unknown:
            return jsonify({"error": f"Unknown rule(s): {', '.join(unknown)}"}), 400

    if resolutions is not None and not (
        isinstance(resolutions, list) and all(isinstance(r, dict) for r in resolutions)
    ):
        return jsonify({"error": "'resolutions' must be a list of objects"}), 400

    started = time.monotonic()
    try:
        summary = run_pipeline(job_id, rules=rules, resolutions=resolutions, has_header=has_header)
    except DatasetTooLargeError as exc:
        metadata_db.record_failed(job_id, "process", "DatasetTooLargeError", str(exc))
        return jsonify({"status": "failed", "error": str(exc)}), 422
    except FileNotFoundError:
        metadata_db.record_failed(job_id, "process", "FileNotFoundError", "source file missing")
        return jsonify({"status": "failed", "error": "Source file not found for this job, it may have expired."}), 404
    except Exception as exc:
        # Never surface the raw exception (str(e) can contain internal
        # file paths, e.g. FileNotFoundError's message), log the real
        # cause server-side and return a safe, generic message.
        logging.exception("Processing failed for job %s", job_id)
        metadata_db.record_failed(job_id, "process", type(exc).__name__, str(exc))
        return jsonify({
            "status": "failed",
            "error": "Could not process this file, it may be corrupted, empty, or in an unexpected format.",
        }), 422

    processing_ms = round((time.monotonic() - started) * 1000)
    cleaning_summary = summary.get("cleaning_summary") or {}
    metadata_db.record_processed(
        job_id,
        rows_in=summary.get("rows_in", 0),
        rows_out=summary.get("rows_out", 0),
        columns_in=cleaning_summary.get("counts", {}).get("columns_processed", 0),
        quality_before=summary.get("quality_report_before"),
        quality_after=summary.get("quality_report"),
        rules_applied=summary.get("rules_applied") or [],
        processing_ms=processing_ms,
    )

    return jsonify({
        "job_id": job_id,
        "status": "completed",
        "summary": summary,
    }), 200
