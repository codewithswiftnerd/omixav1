"""
GET /api/profile/<session_id>?stage=original|cleaned
Returns data quality profile for a session's dataset.
"""

from flask import Blueprint, request, jsonify
from core.cleaner import profile_dataset
from core.session_store import store

profile_bp = Blueprint('profile', __name__)


@profile_bp.route('/profile/<session_id>', methods=['GET'])
def profile(session_id: str):
    stage = request.args.get("stage", "original")

    session = store.get(session_id)
    if not session:
        return jsonify({"error": "Session not found or expired."}), 404

    if stage == "cleaned":
        if session.cleaned_df is None:
            return jsonify({"error": "No cleaned data yet. Call /api/clean first."}), 400
        df = session.cleaned_df
    else:
        df = session.original_df

    try:
        result = profile_dataset(df)
    except Exception as e:
        return jsonify({"error": f"Profiling failed: {str(e)}"}), 500

    return jsonify({
        "session_id": session_id,
        "stage": stage,
        "profile": result,
    }), 200
