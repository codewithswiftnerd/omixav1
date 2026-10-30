"""
GET /api/export/<session_id>?format=csv|xlsx|json
Downloads the cleaned dataset.
"""

import io
import json
from flask import Blueprint, request, send_file, jsonify
from core.session_store import store

export_bp = Blueprint('export', __name__)

VALID_FORMATS = {"csv", "xlsx", "json"}


@export_bp.route('/export/<session_id>', methods=['GET'])
def export(session_id: str):
    fmt = request.args.get("format", "csv").lower()

    if fmt not in VALID_FORMATS:
        return jsonify({"error": f"Invalid format. Choose from: {VALID_FORMATS}"}), 422

    session = store.get(session_id)
    if not session:
        return jsonify({"error": "Session not found or expired."}), 404

    if session.cleaned_df is None:
        return jsonify({"error": "Dataset has not been cleaned yet. Call /api/clean first."}), 400

    df = session.cleaned_df
    base_name = session.filename.rsplit('.', 1)[0]

    if fmt == "csv":
        buf = io.BytesIO()
        df.to_csv(buf, index=False)
        buf.seek(0)
        return send_file(
            buf,
            mimetype="text/csv",
            as_attachment=True,
            download_name=f"{base_name}_cleaned.csv",
        )

    elif fmt == "xlsx":
        buf = io.BytesIO()
        with __import__('pandas').ExcelWriter(buf, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name="Cleaned Data")
        buf.seek(0)
        return send_file(
            buf,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=f"{base_name}_cleaned.xlsx",
        )

    elif fmt == "json":
        # Dates are not JSON-serializable by default — convert to ISO strings
        df_copy = df.copy()
        for col in df_copy.select_dtypes(include=["datetime", "datetimetz"]).columns:
            df_copy[col] = df_copy[col].astype(str)

        payload = df_copy.to_dict(orient="records")
        buf = io.BytesIO(json.dumps(payload, indent=2).encode("utf-8"))
        buf.seek(0)
        return send_file(
            buf,
            mimetype="application/json",
            as_attachment=True,
            download_name=f"{base_name}_cleaned.json",
        )
