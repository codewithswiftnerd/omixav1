"""
POST /api/upload
Accepts a file, parses it, profiles it, and returns a session_id.
"""

from flask import Blueprint, request, jsonify
from core.parser import parse_file
from core.cleaner import profile_dataset
from core.session_store import store

upload_bp = Blueprint('upload', __name__)


@upload_bp.route('/upload', methods=['POST'])
def upload():
    if 'file' not in request.files:
        return jsonify({"error": "No file provided. Send multipart/form-data with key 'file'"}), 400

    file = request.files['file']
    if not file.filename:
        return jsonify({"error": "Empty filename"}), 400

    try:
        df, ext = parse_file(file)
    except ValueError as e:
        return jsonify({"error": str(e)}), 422
    except Exception as e:
        return jsonify({"error": f"Failed to parse file: {str(e)}"}), 500

    # Profile before cleaning
    profile = profile_dataset(df)

    # Store session
    session_id = store.create(filename=file.filename, df=df)

    return jsonify({
        "session_id": session_id,
        "filename": file.filename,
        "file_type": ext,
        "profile": profile,
        "message": f"File uploaded successfully. {profile['rows']} rows, {profile['cols']} columns detected."
    }), 200
