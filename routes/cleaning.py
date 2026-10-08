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
