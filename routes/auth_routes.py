from flask import Blueprint, jsonify, request

from accounts import auth
from accounts.entitlements import effective
from accounts.store import get_store

auth_bp = Blueprint("auth", __name__)


def public_user(user: dict) -> dict:
    """Only what the browser may see. No payment identifiers."""
    eff = effective(user)
    return {
        "uid": user["uid"], "email": user.get("email"), "name": user.get("name"),
        "plan": eff["plan"], "status": eff["status"], "is_pro": eff["is_pro"],
        "subscription_start": eff["start"], "subscription_expires": eff["expires"],
        "renews": eff["renews"], "payment_failed": eff["payment_failed"],
        "created_at": user.get("created_at"),
    }


@auth_bp.post("/session")
def create_session():
    body = request.get_json(silent=True) or {}
    token = body.get("idToken")
    if not isinstance(token, str) or not token or len(token) > 4096:
        return jsonify({"error": "Missing sign-in token."}), 400
    user, err = auth.sign_in(token)
    if err:
        return err
    return jsonify({"user": public_user(user)}), 200


@auth_bp.post("/logout")
def logout():
    auth.sign_out()
    return jsonify({"status": "signed_out"}), 200


@auth_bp.get("/me")
def me():
    if not auth.current_uid():
        return jsonify({"user": None, "accounts_available": get_store() is not None}), 200
    user, err = auth.current_user_record()
    if err:
        return err
    return jsonify({"user": public_user(user), "accounts_available": True}), 200
