"""
Server-side session, admin auth, CSRF and job-ownership helpers.

No API key anymore. Flask's signed session cookie (HttpOnly + Secure
+ SameSite, config.py) gives every visitor an unguessable session id
on their first request — that's what scopes a job to the browser
that created it (see job ownership below). Not an accounts system,
just enough to say "this job belongs to this session".

Admin auth is separate: a single operator account
(OMIXA_ADMIN_USERNAME / OMIXA_ADMIN_PASSWORD_HASH), checked
server-side, session-flagged.
"""

from __future__ import annotations

import hmac
import hashlib
import secrets
import time

from flask import session, request
from werkzeug.security import check_password_hash

from config import Config

# --------------------------------------------------------------------
# Anonymous per-browser session (job ownership)
# --------------------------------------------------------------------


def ensure_session() -> None:
    """Assigns an unguessable session id to first-time visitors, via
    Flask's signed session cookie. Idempotent — a returning visitor
    keeps the same id (and therefore the same jobs) until the cookie
    expires or they clear it."""
    if "sid" not in session:
        session["sid"] = secrets.token_urlsafe(32)
        session.permanent = True


def session_id() -> str | None:
    return session.get("sid")


def hash_value(value: str) -> str:
    """HMAC-SHA256 keyed with SECRET_KEY. Used to store a
    non-reversible, non-forgeable fingerprint of a session id (for job
    ownership checks and admin analytics) instead of the raw id, so
    the metadata database never holds a value that is itself a live
    session credential."""
    return hmac.new(Config.SECRET_KEY.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def current_session_hash() -> str:
    ensure_session()
    return hash_value(session_id())


def owns(owner_hash: str | None) -> bool:
    """True if the current request's session is the one that created
    a job with this owner_hash. A missing/blank owner_hash (e.g. a
    pre-existing job dir from before this feature) never matches, so
    it fails closed rather than open."""
    if not owner_hash:
        return False
    return hmac.compare_digest(owner_hash, current_session_hash())


# --------------------------------------------------------------------
# Admin auth
# --------------------------------------------------------------------


ADMIN_ABSOLUTE_SECONDS = 60 * 60   # an admin sign-in never lasts more than an hour
ADMIN_IDLE_SECONDS = 20 * 60       # ...or 20 minutes without activity


def is_admin() -> bool:
    """Admin flag plus server-checked lifetimes. The session cookie is signed client-side, so without these a
    copied admin cookie would stay valid for the full 12-hour cookie lifetime, even after logout."""
    if not session.get("is_admin"):
        return False
    now = time.time()
    started, seen = session.get("admin_at"), session.get("admin_seen")
    if not isinstance(started, (int, float)) or not isinstance(seen, (int, float)) \
            or now - started > ADMIN_ABSOLUTE_SECONDS or now - seen > ADMIN_IDLE_SECONDS:
        logout_admin()
        return False
    session["admin_seen"] = now
    return True


def attempt_admin_login(username: str, password: str) -> bool:
    """Verifies against the single configured admin account. Runs the
    password hash check even on a username mismatch so a wrong
    username doesn't return measurably faster than a wrong password
    (a naive short-circuit here would leak whether a guessed username
    is the real one via response timing)."""
    configured_user = Config.ADMIN_USERNAME
    configured_hash = Config.ADMIN_PASSWORD_HASH

    if not configured_user or not configured_hash:
        return False  # admin login is unreachable until both are set, not "open"

    username_ok = bool(username) and hmac.compare_digest(username, configured_user)
    password_ok = bool(password) and check_password_hash(configured_hash, password)

    if not (username_ok and password_ok):
        return False

    # Rotate the whole session (not just flip a flag) on privilege
    # escalation, to defend against session fixation: an id issued
    # before authentication should never carry elevated rights after.
    session.clear()
    session["sid"] = secrets.token_urlsafe(32)
    session["is_admin"] = True
    session["admin_at"] = session["admin_seen"] = time.time()
    session.permanent = True
    return True


def logout_admin() -> None:
    for k in ("is_admin", "admin_at", "admin_seen", "csrf_token"):
        session.pop(k, None)


# --------------------------------------------------------------------
# CSRF (double-submit token held in the signed session)
# --------------------------------------------------------------------


def csrf_token() -> str:
    """Issues (or reuses) a per-session CSRF token. Call this when
    rendering any form/page that will POST to a state-changing admin
    endpoint, and embed it as a hidden field / header."""
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def verify_csrf(supplied: str | None) -> bool:
    real = session.get("csrf_token")
    return bool(real and supplied and hmac.compare_digest(real, supplied))


def verify_csrf_request() -> bool:
    """Checks the token from either a form field (`csrf_token`) or the
    `X-CSRF-Token` header, whichever the caller used."""
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    return verify_csrf(supplied)


def client_ip() -> str:
    # See utils/security._client_key: remote_addr only, never the raw header.
    return request.remote_addr or "unknown"
