"""
Admin dashboard routes.

Everything under /admin except the login page/post requires a
server-verified admin session (admin_required() below, see
utils/session.attempt_admin_login()) — enforced here, not in the
frontend. Every state-changing POST requires a matching CSRF token
(utils/session.verify_csrf_request()). Only operational metadata is
ever shown (db.py) — never dataset contents.
"""

from __future__ import annotations

import functools
import logging

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from utils.security import check_login_rate_limit
from utils.session import (
    attempt_admin_login, client_ip, csrf_token, hash_value,
    is_admin, logout_admin, verify_csrf_request,
)
from utils.file_handler import sweep_expired_jobs
import db as metadata_db

admin_bp = Blueprint("admin", __name__)
logger = logging.getLogger("omixa.admin")


def admin_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not is_admin():
            if request.path.startswith("/admin/api/"):
                return jsonify({"error": "Admin authentication required"}), 401
            return redirect(url_for("admin.login"))
        return view(*args, **kwargs)
    return wrapped


def csrf_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not verify_csrf_request():
            return jsonify({"error": "Invalid or missing CSRF token"}), 403
        return view(*args, **kwargs)
    return wrapped


# --------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------

@admin_bp.get("/login")
def login():
    if is_admin():
        return redirect(url_for("admin.dashboard"))
    return render_template("admin/login.html", csrf_token=csrf_token(), error=None)


@admin_bp.post("/login")
def login_submit():
    limited = check_login_rate_limit()
    if limited:
        return limited

    if not verify_csrf_request():
        return render_template("admin/login.html", csrf_token=csrf_token(), error="Session expired, please try again."), 400

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    ip_hash = hash_value(client_ip())

    ok = attempt_admin_login(username, password)
    metadata_db.record_login_attempt(ip_hash, ok)

    if not ok:
        logger.warning("Failed admin login attempt")
        return render_template("admin/login.html", csrf_token=csrf_token(), error="Invalid username or password."), 401

    return redirect(url_for("admin.dashboard"))


@admin_bp.post("/logout")
@admin_required
@csrf_required
def logout():
    logout_admin()
    return redirect(url_for("admin.login"))


# --------------------------------------------------------------------
# Dashboard page (server-rendered shell; live figures via /admin/api/*)
# --------------------------------------------------------------------

@admin_bp.get("/")
@admin_required
def dashboard():
    return render_template("admin/dashboard.html", csrf_token=csrf_token())


# --------------------------------------------------------------------
# JSON data for the dashboard
# --------------------------------------------------------------------

@admin_bp.get("/api/users")
@admin_required
def api_users():
    """Account counts only (no emails or names). Needs Firebase configured."""
    from accounts.store import get_store
    store = get_store()
    if store is None:
        return jsonify({"available": False}), 200
    try:
        return jsonify({"available": True, **store.user_stats()}), 200
    except Exception:
        return jsonify({"available": False}), 200


@admin_bp.get("/api/stats")
@admin_required
def api_stats():
    window = request.args.get("window", "all")
    window_seconds = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}.get(window)
    stats = metadata_db.stats_summary(window_seconds=window_seconds)
    return jsonify(stats), 200


@admin_bp.get("/api/jobs")
@admin_required
def api_jobs():
    status = request.args.get("status") or None
    fmt = request.args.get("format") or None
    q = request.args.get("q") or None
    try:
        limit = min(max(int(request.args.get("limit", 25)), 1), 100)
        offset = max(int(request.args.get("offset", 0)), 0)
    except ValueError:
        return jsonify({"error": "'limit' and 'offset' must be integers"}), 400

    jobs, total = metadata_db.list_jobs(status=status, fmt=fmt, q=q, limit=limit, offset=offset)
    # session_hash is an internal fingerprint, not something the UI needs to show.
    for j in jobs:
        j.pop("session_hash", None)
    return jsonify({"jobs": jobs, "total": total, "limit": limit, "offset": offset}), 200


@admin_bp.get("/api/errors")
@admin_required
def api_errors():
    return jsonify({"errors": metadata_db.recent_errors(limit=30)}), 200


@admin_bp.get("/api/logins")
@admin_required
def api_logins():
    return jsonify({"attempts": metadata_db.recent_login_attempts(limit=30)}), 200


@admin_bp.post("/api/jobs/purge")
@admin_required
@csrf_required
def api_purge():
    """Manually triggers the same expired-job sweep that already runs
    opportunistically on API traffic (utils/file_handler.
    sweep_expired_jobs), for an operator who wants it to happen now
    rather than waiting for the next upload/process/download call."""
    sweep_expired_jobs()
    return jsonify({"status": "ok"}), 200
