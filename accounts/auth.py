"""
Firebase sign-in. The browser signs in with the Firebase Web SDK and sends the resulting ID
token here ONCE; the server verifies it with the Admin SDK and remembers the uid in Flask's
signed session cookie. Every later request is authorised from that cookie plus the
server-side subscription record.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Callable, Optional

from flask import jsonify, session

from accounts.store import get_store, store_error
from config import Config

_verifier: Optional[Callable[[str], dict]] = None


def set_verifier_for_tests(fn):
    global _verifier
    _verifier = fn


def verify_id_token(token: str) -> dict:
    """Returns {"uid", "email", "name", "provider"}; raises ValueError for a bad token."""
    if _verifier is not None:
        return _verifier(token)
    from firebase_admin import auth as fb_auth
    from accounts import store as _s  # make sure the app is initialised
    _s.get_store()
    try:
        claims = fb_auth.verify_id_token(token, check_revoked=True)
    except Exception as exc:
        raise ValueError("Invalid or expired sign-in") from exc
    return {
        "uid": claims["uid"],
        "email": claims.get("email"),
        "name": claims.get("name"),
        "provider": (claims.get("firebase") or {}).get("sign_in_provider"),
        "email_verified": bool(claims.get("email_verified")),
    }


def sign_in(token: str):
    """Verifies the token, creates the account record on first login, starts the session."""
    store = get_store()
    if store is None:
        return None, (jsonify({"error": store_error()}), 503)
    try:
        claims = verify_id_token(token)
    except ValueError as exc:
        return None, (jsonify({"error": str(exc)}), 401)
    if Config.REQUIRE_VERIFIED_EMAIL and claims.get("provider") == "password" and not claims.get("email_verified"):
        return None, (jsonify({"error": "Please verify your email address before signing in."}), 403)
    uid, now = claims["uid"], time.time()
    existing = store.get_user(uid)
    fields = {"email": claims.get("email"), "name": claims.get("name"), "last_seen": now,
              "provider": claims.get("provider")}
    if "email_verified" in claims:
        fields["email_verified"] = bool(claims["email_verified"])
    if existing is None:
        fields.update(created_at=now, plan="free", subscription_status="free")
    store.upsert_user(uid, fields)
    # Signing in starts a fresh identity: nothing elevated or pre-login (admin flag, CSRF token) may carry over.
    # The anonymous "sid" is kept on purpose, because it owns the visitor's in-progress jobs.
    for stale in ("is_admin", "admin_at", "admin_seen", "csrf_token"):
        session.pop(stale, None)
    session["uid"] = uid
    # Server-side revocation: the cookie is a signed client-side blob, so it carries the user's current
    # session epoch and is rejected once sign_out() rotates that epoch (see current_user_record).
    session["ep"] = ((existing or {}).get("session_epoch")) or ""
    session.permanent = True
    if existing is None:
        _welcome_new_account(claims)
    return store.get_user(uid), None


def _welcome_new_account(claims: dict) -> None:
    """Only reached right after the account record was created. Queued in the background and never raises:
    an email problem must not affect sign-in."""
    try:
        from accounts import emails
        emails.send_welcome(claims.get("email"), claims.get("name"))
    except Exception:
        logging.getLogger("omixa.auth").error("welcome email hook failed")


def sign_out():
    """Ends this browser's sign-in AND revokes every copy of the cookie: the user's session epoch is rotated, so a
    stolen or replayed cookie stops working immediately instead of living out its 12 hours."""
    uid = session.pop("uid", None)
    session.pop("ep", None)
    if uid:
        try:
            store = get_store()
            if store is not None:
                store.upsert_user(uid, {"session_epoch": secrets.token_hex(8)})
        except Exception:
            logging.getLogger("omixa.auth").error("could not rotate session epoch on sign-out")


def current_uid() -> Optional[str]:
    return session.get("uid")


def current_user_record():
    """(user_doc, None) or (None, error_response)."""
    uid = current_uid()
    if not uid:
        return None, (jsonify({"error": "Please sign in.", "auth_required": True}), 401)
    store = get_store()
    if store is None:
        return None, (jsonify({"error": store_error()}), 503)
    try:
        user = store.get_user(uid)
    except Exception:
        return None, (jsonify({"error": "Accounts are temporarily unavailable."}), 503)
    if user is None or (user.get("session_epoch") or "") != (session.get("ep") or ""):
        session.pop("uid", None)
        session.pop("ep", None)
        return None, (jsonify({"error": "Please sign in.", "auth_required": True}), 401)
    return user, None
