"""
Firebase sign-in. The browser signs in with the Firebase Web SDK and sends the resulting ID
token here ONCE; the server verifies it with the Admin SDK and remembers the uid in Flask's
signed session cookie. Every later request is authorised from that cookie plus the
server-side subscription record.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from flask import jsonify, session

from accounts.store import get_store, store_error

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
    uid, now = claims["uid"], time.time()
    existing = store.get_user(uid)
    fields = {"email": claims.get("email"), "name": claims.get("name"), "last_seen": now,
              "provider": claims.get("provider")}
    if existing is None:
        fields.update(created_at=now, plan="free", subscription_status="free")
    store.upsert_user(uid, fields)
    session["uid"] = uid
    session.permanent = True
    return store.get_user(uid), None


def sign_out():
    session.pop("uid", None)


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
    if user is None:
        session.pop("uid", None)
        return None, (jsonify({"error": "Please sign in.", "auth_required": True}), 401)
    return user, None
