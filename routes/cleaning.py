"""
Cleaning-engine catalogue and dry-run profile validation. Neither endpoint touches a dataset, so
neither needs a worker: they are cheap, stateless and safe to serve from any API instance.

The plan is derived from the subscription record (accounts.entitlements.effective), never from the
request. `available` in the catalogue is presentation; /api/process remains the authority.
"""

import logging

from flask import Blueprint, jsonify, request

from accounts import auth
from accounts.entitlements import effective
from accounts.store import get_store
from cleaning.engine import FeatureNotAvailable, RuleConfigError, authorize_profile, parse_profile, catalog
from cleaning.engine import access
from cleaning.mapping import MAPPERS, build_plan, suggest_for_dataframe, validate_custom_mappings
from routes.validation import _load as _load_job_frame

cleaning_bp = Blueprint("cleaning", __name__)
logger = logging.getLogger("omixa.cleaning")


def server_side_is_pro() -> bool:
    uid = auth.current_uid()
    store = get_store() if uid else None
    if not (uid and store is not None):
        return False
    try:
        user = store.get_user(uid)
        return bool(user and effective(user)["is_pro"])
    except Exception:
        logger.exception("Could not read the subscription record; treating as Free")
        return False


@cleaning_bp.get("/rules")
def list_rules():
    """Everything the UI needs to build a rule picker: each rule's parameters and which plan it needs."""
    is_pro = server_side_is_pro()
    return jsonify({
        "plan": access.plan_for(is_pro),
        "rules": catalog(is_pro),
        "features": {f: {"required_plan": "pro", "available": is_pro} for f in sorted(access.PRO_FEATURES)},
        "profile_schema": {
            "name": "optional text",
            "columns": {"<column name in your file>": {"rules": [{"type": "<rule type>", "...": "rule parameters"}]}},
            "decisions": [{"column": "...", "original": "flagged value as uploaded", "action": "accept|reject",
                           "value": "replacement (optional when the rule suggested one)"}],
        },
    }), 200


@cleaning_bp.post("/profile/validate")
def validate_profile():
    """Checks a cleaning profile's structure and the caller's right to use it. Column names are
    checked against the real file when the job runs (the API never opens datasets)."""
    body = request.get_json(silent=True)
    profile = body.get("cleaning_profile") if isinstance(body, dict) else None
    try:
        parsed = parse_profile(profile)
        authorize_profile(parsed, is_pro=server_side_is_pro())
    except FeatureNotAvailable as exc:
        return jsonify(exc.to_response()), 402
    except RuleConfigError as exc:
        return jsonify(exc.to_dict()), 400
    order = {c: [r.type for r in sorted((r for r in rs if r.enabled), key=lambda r: (r.rule.phase, r.order))]
             for c, rs in parsed.columns.items()}
    return jsonify({"valid": True, "name": parsed.name, "execution_order": order,
                    "required_capabilities": sorted(parsed.required_capabilities())}), 200


@cleaning_bp.post("/mapping/<job_id>")
def mapping_preview(job_id):
    """READ-ONLY preview of value standardisation for an uploaded file. Nothing is written or changed.

    No `column`  -> suggestions for every column that really looks like countries / yes-no flags / currency
                    labels / inconsistent categories (a column is never assumed from its name alone).
    With `column` + `category` -> the exact preview for that column in the chosen `output_format`, including
                    the user's own `mappings` ({"SMB": "Small Business"}), so the UI can show what WILL happen.
    The job must belong to the caller; the plan is derived server-side (Pro, like the rule itself).
    """
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "The request body must be an object."}), 400
    try:
        access.authorize({"standardize_values"}, is_pro=server_side_is_pro())
    except FeatureNotAvailable as exc:
        return jsonify(exc.to_response()), 402
    df, failure = _load_job_frame(job_id, body)
    if failure:
        return failure
    categories = {c: m.describe() for c, m in MAPPERS.items()}
    column = body.get("column")
    if column is None:
        return jsonify({"job_id": job_id, "categories": categories, "suggestions": suggest_for_dataframe(df)}), 200
    category = body.get("category")
    if not isinstance(column, str) or column not in df.columns:
        return jsonify({"error": "That column is not in this file."}), 404
    if not isinstance(category, str) or category not in MAPPERS:
        return jsonify({"error": "'category' must be one of: " + ", ".join(MAPPERS) + "."}), 400
    fmt, limit = body.get("output_format"), body.get("limit", 100)
    if fmt is not None and not isinstance(fmt, str):
        return jsonify({"error": "'output_format' must be text."}), 400
    if isinstance(limit, bool) or not isinstance(limit, int):
        return jsonify({"error": "'limit' must be a whole number."}), 400
    limit = min(max(limit, 1), 200)
    mappings, canonical = body.get("mappings") or {}, body.get("canonical") or []
    if not isinstance(mappings, dict) or not isinstance(canonical, list) or \
            not all(isinstance(c, str) for c in canonical) or not isinstance(body.get("confirm_numeric_flags", False), bool):
        return jsonify({"error": "'mappings' must be an object, 'canonical' a list of text and 'confirm_numeric_flags' true or false."}), 400
    if len(mappings) > 200 or len(canonical) > 200:
        return jsonify({"error": "At most 200 mappings or canonical labels."}), 400
    try:
        validate_custom_mappings(mappings)
        plan = build_plan(df[column], column, category, output_format=fmt, mappings=mappings,
                          params={"canonical": canonical, "confirm_numeric_flags": body.get("confirm_numeric_flags", False)},
                          track_rows=False)
    except ValueError as exc:
        return jsonify({"code": "INVALID_MAPPING", "error": str(exc)}), 400
    return jsonify({"job_id": job_id, "preview": plan.to_dict(limit=limit)}), 200
