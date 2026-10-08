import logging
import time

from flask import Blueprint, request, jsonify

from utils.file_handler import find_source_file, job_dir_path, job_owner_hash, maybe_sweep, is_valid_job_id
from utils.session import owns, current_session_hash
from utils.observability import get_request_id
from utils.security import remember_entitlement
from config import Config
import jobs.service as job_service
import storage as object_storage
from processing.pipeline import DatasetTooLargeError, run_pipeline
from cleaning.rules import RULE_DISPATCH
import db as metadata_db
from accounts import auth
from accounts.entitlements import effective
from accounts.store import get_store
from proquality.analysis import session_record
from proquality.profiles import ProfileError, normalize_profile
from cleaning.engine import FeatureNotAvailable, RuleConfigError, authorize_profile, parse_profile

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
    maybe_sweep()

    queue_mode = Config.PROCESSING_MODE == "queue"
    if not is_valid_job_id(job_id):
        return jsonify({"error": "Unknown or expired job_id, or no uploaded file found"}), 404
    if queue_mode:
        # Source lives in object storage; the shared DB row is the authority on existence.
        if not metadata_db.get_job(job_id):
            return jsonify({"error": "Unknown or expired job_id, or no uploaded file found"}), 404
    elif not job_dir_path(job_id) or not find_source_file(job_id):
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

    # Pro: signed-in subscribers get deep analysis, Quality Profiles and saved history.
    # Everything is decided server-side from the subscription record, never from the request.
    is_pro, user, profile = False, None, None
    uid = auth.current_uid()
    store = get_store() if uid else None
    if uid and store is not None:
        try:
            user = store.get_user(uid)
            is_pro = bool(user and effective(user)["is_pro"])
        except Exception:
            logger.exception("Could not read the subscription record; processing as Free")
            is_pro = False
    profile_id = body.get("profile_id")
    if profile_id is not None:
        if not isinstance(profile_id, str):
            return jsonify({"error": "'profile_id' must be text"}), 400
        if not is_pro:
            return jsonify({"error": "Quality Profiles are available with Omixa Pro.", "upgrade_required": True}), 402
        profile = store.get_profile(uid, profile_id)
        if not profile:
            return jsonify({"error": "Quality Profile not found"}), 404

    # Declarative per-column cleaning (cleaning/engine). Validated and authorized HERE, against the
    # entitlement derived above from the subscription record. The request body is never consulted
    # for plan information, and the pipeline re-checks with the same frozen flag.
    cleaning_profile = body.get("cleaning_profile")
    if cleaning_profile is not None:
        try:
            parsed = parse_profile(cleaning_profile)
            authorize_profile(parsed, is_pro=is_pro)
        except FeatureNotAvailable as exc:
            return jsonify(exc.to_response()), 402
        except RuleConfigError as exc:
            return jsonify(exc.to_dict()), 400

    if uid:
        remember_entitlement(uid, is_pro)

    if queue_mode:
        return _enqueue(job_id, rules, resolutions, has_header, is_pro, uid, profile, cleaning_profile)

    started = time.monotonic()
    try:
        summary = run_pipeline(job_id, rules=rules, resolutions=resolutions, has_header=has_header,
                               pro=is_pro, profile=profile, cleaning_profile=cleaning_profile)
    except (RuleConfigError, FeatureNotAvailable) as exc:
        # e.g. the profile names a column that is not in the file; nothing was changed or exported
        payload = exc.to_response() if isinstance(exc, FeatureNotAvailable) else exc.to_dict()
        return jsonify({"status": "failed", **payload}), 402 if isinstance(exc, FeatureNotAvailable) else 422
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

    session_id = None
    if is_pro and summary.get("pro"):
        try:
            name = metadata_db.get_job_filename(job_id) or "dataset"
            record = session_record(
                dataset_name=name, ext=(name.rsplit(".", 1)[-1].lower() if "." in name else ""),
                size_bytes=metadata_db.get_job_size(job_id) or 0, has_header=has_header,
                rules_applied=summary.get("rules_applied") or [], processing_ms=processing_ms, pro_block=summary["pro"])
            session_id = store.add_session(uid, record)
        except Exception:
            logger.exception("Could not save the processing session for job %s", job_id)
    if session_id:
        summary["pro"]["session_id"] = session_id

    return jsonify({
        "job_id": job_id,
        "status": "completed",
        "summary": summary,
    }), 200


def _enqueue(job_id, rules, resolutions, has_header, is_pro, uid, profile, cleaning_profile=None):
    """Queue mode: the API only validates, applies admission control and creates the job. A
    dedicated worker (worker.py) does the cleaning. Everything the worker needs (including the
    server-decided is_pro flag and the Quality Profile) is frozen into the job record here, so
    nothing the client sends later can change what runs."""
    session_hash = current_session_hash()
    try:
        admission = job_service.admit(uid, session_hash, is_pro)
    except job_service.AdmissionError as exc:
        resp = jsonify({"error": exc.message, "status": "rejected"})
        if exc.retry_after:
            resp.headers["Retry-After"] = str(exc.retry_after)
        return resp, exc.status
    except Exception:
        logger.exception("admission control failed")
        return jsonify({"error": "Omixa is temporarily unavailable. Please try again shortly."}), 503

    spec = {"kind": "clean", "rules": rules, "resolutions": resolutions, "has_header": has_header,
            "pro": is_pro, "uid": uid, "profile": profile, "cleaning_profile": cleaning_profile}
    try:
        queued = job_service.submit(job_id, spec, is_pro, get_request_id())
    except Exception:
        logger.exception("could not enqueue job %s", job_id)
        return jsonify({"error": "Omixa is temporarily unavailable. Please try again shortly."}), 503
    if not queued:
        # Already queued/running (double click) or unknown: report the real state, don't duplicate.
        job = metadata_db.get_job(job_id)
        if job and job["status"] in ("queued", "running"):
            return jsonify({"job_id": job_id, "status": job["status"]}), 202
        return jsonify({"error": "This job cannot be processed right now."}), 409

    body = {"job_id": job_id, "status": "queued"}
    if admission["high_traffic"]:
        body["message"] = job_service.HIGH_TRAFFIC_MESSAGE
        body["high_traffic"] = True
    return jsonify(body), 202
