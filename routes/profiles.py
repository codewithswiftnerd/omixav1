from flask import Blueprint, jsonify, request

from accounts import auth
from accounts.entitlements import pro_required
from accounts.store import get_store
from proquality.profiles import DEFAULTS, ProfileError, TYPES, normalize_profile

profiles_bp = Blueprint("profiles", __name__)
MAX_PROFILES = 100


def _uid():
    return auth.current_uid()


@profiles_bp.get("/")
@pro_required
def list_profiles():
    return jsonify({"profiles": get_store().list_profiles(_uid()), "types": list(TYPES), "defaults": DEFAULTS}), 200


@profiles_bp.post("/")
@pro_required
def create_profile():
    store = get_store()
    if store.count_profiles(_uid()) >= MAX_PROFILES:
        return jsonify({"error": f"You can save up to {MAX_PROFILES} profiles."}), 400
    try:
        data = normalize_profile(request.get_json(silent=True))
    except ProfileError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"profile": store.save_profile(_uid(), None, data)}), 201


@profiles_bp.get("/<pid>")
@pro_required
def get_profile(pid):
    p = get_store().get_profile(_uid(), pid)
    return (jsonify({"profile": p}), 200) if p else (jsonify({"error": "Not found"}), 404)


@profiles_bp.put("/<pid>")
@pro_required
def update_profile(pid):
    store = get_store()
    if not store.get_profile(_uid(), pid):
        return jsonify({"error": "Not found"}), 404
    try:
        data = normalize_profile(request.get_json(silent=True))
    except ProfileError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"profile": store.save_profile(_uid(), pid, data)}), 200


@profiles_bp.post("/<pid>/duplicate")
@pro_required
def duplicate_profile(pid):
    store = get_store()
    src = store.get_profile(_uid(), pid)
    if not src:
        return jsonify({"error": "Not found"}), 404
    if store.count_profiles(_uid()) >= MAX_PROFILES:
        return jsonify({"error": f"You can save up to {MAX_PROFILES} profiles."}), 400
    data = normalize_profile({**src, "name": (src["name"] + " (copy)")[:80]})
    return jsonify({"profile": store.save_profile(_uid(), None, data)}), 201


@profiles_bp.delete("/<pid>")
@pro_required
def delete_profile(pid):
    return (jsonify({"status": "deleted"}), 200) if get_store().delete_profile(_uid(), pid) else (jsonify({"error": "Not found"}), 404)
