"""
Column validation endpoints (read-only: nothing here edits, deletes or converts a value).

POST /api/validate/<job_id>
    body (all optional): {
      "has_header": true,
      "column_types": {"age": "numeric", "phone": "phone"},        # user-confirmed types
      "rules": {"age": {"min": 0, "soft_max": 110, "allow_negative": false},
                "status": {"allowed_values": ["active", "inactive"]},
                "phone": {"country": "NG"},
                "signup_date": {"date_convention": "day_first", "min_date": "2020-01-01"}},
      "defaults": {"reference_date": "2026-10-10"}
    }
    -> {"job_id", "validation": {columns: [...], totals: {...}, classification_rules: {...}}}

POST /api/validate/<job_id>/rows
    body: same config plus {"column": "age", "category": "invalid", "code": null, "offset": 0, "limit": 100}
    -> the affected rows with their ORIGINAL values and the reason each was classified that way.

The same module (cleaning/validation.py) backs the validation block of /api/report, so the report and
this endpoint can never disagree about a count.
"""
import logging

from flask import Blueprint, jsonify, request

from config import Config
from processing.pipeline import DatasetTooLargeError, read_source
from utils.file_handler import find_source_file, job_owner_hash, is_valid_job_id, maybe_sweep
from utils.session import owns
from cleaning import validation

validation_bp = Blueprint("validation", __name__)

# In queue mode pandas must not run in the API process for big files; this endpoint stays inline only
# for files small enough that doing so is cheap. Bigger files get the validation block of the (queued)
# analysis job instead.
INLINE_MAX_CELLS = 500_000
_NOT_FOUND = {"error": "Unknown or expired job_id, or no uploaded file found"}


def _load(job_id, body):
    maybe_sweep()
    if not is_valid_job_id(job_id):
        return None, (jsonify(_NOT_FOUND), 404)
    source_path = find_source_file(job_id)
    if not source_path or not owns(job_owner_hash(job_id)):
        return None, (jsonify(_NOT_FOUND), 404)
    has_header = body.get("has_header", True)
    if not isinstance(has_header, bool):
        return None, (jsonify({"error": "'has_header' must be true or false"}), 400)
    cap = INLINE_MAX_CELLS if Config.PROCESSING_MODE == "queue" else int(getattr(Config, "MAX_CELLS", 0) or 0)
    try:
        return read_source(source_path, has_header=has_header, max_cells=cap), None
    except DatasetTooLargeError as exc:
        msg = str(exc) if Config.PROCESSING_MODE != "queue" else (
            "This file is too large for interactive validation here; its validation summary is included in the "
            "analysis report.")
        return None, (jsonify({"error": msg}), 422)
    except Exception:
        logging.exception("Could not read source file for job %s", job_id)
        return None, (jsonify({"error": "Could not read this file, it may be corrupted, empty, or in an unexpected format."}), 422)


def _config_from(body):
    for key in ("column_types", "rules", "defaults"):
        if key in body and not isinstance(body[key], dict):
            return None, f"'{key}' must be an object"
    bad_types = [t for t in (body.get("column_types") or {}).values() if t not in validation.TYPES]
    if bad_types:
        return None, "Unknown column type. Allowed: " + ", ".join(validation.TYPES)
    return {k: body.get(k) for k in ("column_types", "rules", "defaults")}, None


@validation_bp.post("/<job_id>")
def validate_job(job_id):
    body = request.get_json(silent=True) or {}
    cfg, err = _config_from(body)
    if err:
        return jsonify({"error": err}), 400
    df, failure = _load(job_id, body)
    if failure:
        return failure
    return jsonify({"job_id": job_id, "validation": validation.validate_dataframe(df, cfg)}), 200


@validation_bp.post("/<job_id>/rows")
def validate_rows(job_id):
    body = request.get_json(silent=True) or {}
    cfg, err = _config_from(body)
    if err:
        return jsonify({"error": err}), 400
    column, category = body.get("column"), body.get("category")
    if not isinstance(column, str) or category not in validation.CATEGORIES:
        return jsonify({"error": "'column' (string) and 'category' (one of "
                                 + ", ".join(validation.CATEGORIES) + ") are required"}), 400
    df, failure = _load(job_id, body)
    if failure:
        return failure
    if column not in df.columns:
        return jsonify({"error": f"Column '{column}' is not in this file"}), 404
    try:
        offset, limit = int(body.get("offset", 0)), int(body.get("limit", 100))
    except (TypeError, ValueError):
        return jsonify({"error": "'offset' and 'limit' must be integers"}), 400
    header_offset = 2 if body.get("has_header", True) else 1
    return jsonify(validation.rows_for(df, column, category, cfg, code=body.get("code"), offset=offset,
                                       limit=limit, header_offset=header_offset)), 200
