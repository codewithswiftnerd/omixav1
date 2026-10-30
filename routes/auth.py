"""
Accounts API (additive; does not touch upload/clean/export/profile or Pro billing).

POST /api/auth/signup           {name, email, password}  -> {token, user}
POST /api/auth/login            {email, password}        -> {token, user}
POST /api/auth/google           {credential}             -> {token, user}   (needs GOOGLE_CLIENT_ID)
POST /api/auth/forgot-password  {email}                  -> {message}
POST /api/auth/reset-password   {token, password}        -> {message}
GET  /api/auth/me               Authorization: Bearer <token> -> {user}

Aliases: /api/signup /api/register /api/login /api/forgot-password
"""

import json
import os
import re
import urllib.parse
import urllib.request

from flask import Blueprint, request, jsonify, current_app
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from werkzeug.security import generate_password_hash, check_password_hash

from core import user_store

auth_bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SESSION_MAX_AGE = 60 * 60 * 24 * 30   # 30 days
RESET_MAX_AGE = 60 * 60               # 1 hour


def _secret():
    return os.environ.get("OMIXA_SECRET_KEY") or current_app.config.get("SECRET_KEY") or "dev-only-change-me"


def _ser(salt):
    return URLSafeTimedSerializer(_secret(), salt=salt)


def issue_token(user):
    return _ser("session").dumps({"uid": user["id"]})


def current_user():
    """Return the logged-in user dict or None. Reusable by other routes (e.g. Pro checks)."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    try:
        data = _ser("session").loads(header[7:], max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    return user_store.get_by_id(data["uid"])


def _body():
    return request.get_json(silent=True) or {}


def _auth_ok(user, status=200):
    return jsonify({"token": issue_token(user), "user": user_store.public_user(user)}), status


@auth_bp.route("/auth/signup", methods=["POST"])
@auth_bp.route("/signup", methods=["POST"])
@auth_bp.route("/register", methods=["POST"])
def signup():
    d = _body()
    name = (d.get("name") or "").strip()
    email = (d.get("email") or "").strip().lower()
    password = d.get("password") or ""

    if not EMAIL_RE.match(email):
        return jsonify({"error": "Enter a valid email address"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    if user_store.get_by_email(email):
        return jsonify({"error": "An account with this email already exists. Try signing in."}), 409

    user = user_store.create_user(email, name or email.split("@")[0], generate_password_hash(password))
    return _auth_ok(user, 201)


@auth_bp.route("/auth/login", methods=["POST"])
@auth_bp.route("/login", methods=["POST"])
def login():
    d = _body()
    email = (d.get("email") or "").strip().lower()
    password = d.get("password") or ""
    user = user_store.get_by_email(email)

    if not user or not user.get("password_hash") or not check_password_hash(user["password_hash"], password):
        # Same message for both cases so emails can't be probed
        return jsonify({"error": "Incorrect email or password"}), 401
    return _auth_ok(user)


@auth_bp.route("/auth/google", methods=["POST"])
def google_login():
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    if not client_id:
        return jsonify({"error": "Google sign-in is not configured on the server"}), 503

    credential = _body().get("credential") or _body().get("id_token")
    if not credential:
        return jsonify({"error": "Missing Google credential"}), 400

    try:
        url = "https://oauth2.googleapis.com/tokeninfo?" + urllib.parse.urlencode({"id_token": credential})
        with urllib.request.urlopen(url, timeout=10) as resp:
            info = json.loads(resp.read().decode())
    except Exception:
        return jsonify({"error": "Google sign-in failed. Please try again."}), 401

    if info.get("aud") != client_id or info.get("email_verified") not in (True, "true"):
        return jsonify({"error": "Google sign-in failed. Please try again."}), 401

    email = info["email"].lower()
    user = user_store.get_by_email(email)
    if not user:
        user = user_store.create_user(email, info.get("name") or email.split("@")[0], google_id=info.get("sub"))
    elif not user.get("google_id"):
        user_store.link_google(user["id"], info.get("sub"))
        user = user_store.get_by_id(user["id"])
    return _auth_ok(user)


@auth_bp.route("/auth/forgot-password", methods=["POST"])
@auth_bp.route("/forgot-password", methods=["POST"])
def forgot_password():
    email = (_body().get("email") or "").strip().lower()
    user = user_store.get_by_email(email)
    if user:
        token = _ser("reset").dumps({"uid": user["id"]})
        base = os.environ.get("FRONTEND_URL", "http://localhost:3000")
        link = f"{base}/reset-password?token={token}"
        # TODO: send `link` by email (SMTP / Resend / SendGrid). Logged for now.
        current_app.logger.warning("Password reset link for %s: %s", email, link)
    # Always the same reply so emails can't be probed
    return jsonify({"message": "If that email has an account, a reset link has been sent."}), 200


@auth_bp.route("/auth/reset-password", methods=["POST"])
def reset_password():
    d = _body()
    password = d.get("password") or ""
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400
    try:
        data = _ser("reset").loads(d.get("token") or "", max_age=RESET_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return jsonify({"error": "This reset link is invalid or has expired"}), 400
    user_store.set_password(data["uid"], generate_password_hash(password))
    return jsonify({"message": "Password updated. You can sign in now."}), 200


@auth_bp.route("/auth/me", methods=["GET"])
def me():
    user = current_user()
    if not user:
        return jsonify({"error": "Not signed in"}), 401
    return jsonify({"user": user_store.public_user(user)}), 200
