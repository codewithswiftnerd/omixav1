"""
POST /api/clean
Runs the full cleaning pipeline on a previously uploaded dataset.
"""

from flask import Blueprint, request, jsonify
from core.cleaner import clean_dataset
from core.session_store import store

clean_bp = Blueprint('clean', __name__)

VALID_DOMAINS = {"healthcare", "finance", "general"}
VALID_MISSING_STRATEGIES = {"auto", "drop_rows", "fill_zero"}
VALID_OUTLIER_ACTIONS = {"flag", "cap", "remove"}


@clean_bp.route('/clean', methods=['POST'])
def clean():
    body = request.get_json(silent=True) or {}

    session_id = body.get("session_id")
    if not session_id:
        return jsonify({"error": "Missing 'session_id'"}), 400

    session = store.get(session_id)
    if not session:
        return jsonify({"error": "Session not found or expired. Please re-upload your file."}), 404

    # Options with defaults
    domain = body.get("domain", "general")
    missing_strategy = body.get("missing_strategy", "auto")
    outlier_action = body.get("outlier_action", "flag")

    if domain not in VALID_DOMAINS:
        return jsonify({"error": f"Invalid domain. Choose from: {VALID_DOMAINS}"}), 422
    if missing_strategy not in VALID_MISSING_STRATEGIES:
        return jsonify({"error": f"Invalid missing_strategy. Choose from: {VALID_MISSING_STRATEGIES}"}), 422
    if outlier_action not in VALID_OUTLIER_ACTIONS:
        return jsonify({"error": f"Invalid outlier_action. Choose from: {VALID_OUTLIER_ACTIONS}"}), 422

    try:
        result = clean_dataset(
            df=session.original_df.copy(),
            domain=domain,
            missing_strategy=missing_strategy,
            outlier_action=outlier_action,
        )
    except Exception as e:
        return jsonify({"error": f"Cleaning failed: {str(e)}"}), 500

    cleaned_df = result["dataframe"]
    report = result["report"]

    # Persist cleaned result
    store.update_cleaned(session_id, cleaned_df, report)

    # Return preview (first 10 rows) + report
    preview = cleaned_df.head(10).fillna("").to_dict(orient="records")

    return jsonify({
        "session_id": session_id,
        "report": report,
        "preview": preview,
        "columns": list(cleaned_df.columns),
        "message": (
            f"Cleaning complete. "
            f"{report['rows_removed']} rows and {report['cols_removed']} columns removed. "
            f"Took {report['duration_ms']}ms."
        )
    }), 200
