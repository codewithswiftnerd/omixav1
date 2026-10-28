from flask import Blueprint, jsonify, request

from accounts import auth, emails
from accounts.entitlements import effective
from accounts.store import get_store
from config import Config
from utils import mailer
from utils.security import check_limit, client_key

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


@auth_bp.post("/password-reset")
def password_reset():
    """Branded reset email via our SMTP. Firebase still creates the reset link (secure, expiring, single-use).

    Every well-formed request gets the same 200 answer whether or not the address has an account, and the work
    happens in the background, so neither the response nor its timing reveals which addresses are registered.
    503 {"fallback": true} means this server cannot send email at all (a server-wide fact, not about the
    address): the sign-in page then asks Firebase to send its own default reset email instead.
    """
    body = request.get_json(silent=True) or {}
    email = body.get("email")
    if not isinstance(email, str) or not mailer.valid_address(email.strip()):
        return jsonify({"error": "Enter a valid email address."}), 400
    email = email.strip()
    if not mailer.is_configured():
        return jsonify({"error": "Email is unavailable right now.", "fallback": True}), 503

    for ns, key, limit, window in (
        ("reset_ip", client_key(), Config.RATE_LIMIT_RESET_EMAIL_PER_MINUTE, 60),
        ("reset_addr", emails.address_key(email), Config.RATE_LIMIT_RESET_EMAIL_PER_ADDRESS_HOUR, 3600),
    ):
        allowed, retry = check_limit(ns, key, limit, window)
        if not allowed:
            resp = jsonify({"error": "Too many reset requests. Please wait a while and try again."})
            resp.headers["Retry-After"] = str(retry)
            return resp, 429

    if not emails.request_password_reset(email):
        return jsonify({"error": "Email is busy right now. Please try again in a moment.", "fallback": True}), 503
    return jsonify({"status": "accepted"}), 200
