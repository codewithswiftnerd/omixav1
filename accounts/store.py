"""
Persistence for accounts, Quality Profiles, processing history and payments.

Only metadata is ever stored (never dataset rows). Two implementations share one interface:
  FirestoreStore  production (Firebase Admin SDK, bypasses client rules, so ALL access goes
                  through this server and every method below is scoped by uid)
  MemoryStore     local development and tests

Firestore layout
  users/{uid}                          account + subscription state (server-written only)
  users/{uid}/qualityProfiles/{id}     reusable Quality Profiles
  users/{uid}/sessions/{id}            processing history (compact report, no raw data)
  payments/{reference}                 checkout + processed-payment records (server only)
"""

from __future__ import annotations

import copy
import json
import logging
import threading
import time
import uuid
from typing import Optional

from config import Config

logger = logging.getLogger("omixa.store")


class MemoryStore:
    def __init__(self):
        self._lock = threading.Lock()
        self.users: dict[str, dict] = {}
        self.profiles: dict[str, dict[str, dict]] = {}
        self.sessions: dict[str, dict[str, dict]] = {}
        self.payments: dict[str, dict] = {}

    # users
    def get_user(self, uid):
        with self._lock:
            u = self.users.get(uid)
            return copy.deepcopy(u) if u else None

    def upsert_user(self, uid, fields: dict):
        with self._lock:
            self.users.setdefault(uid, {"uid": uid}).update(copy.deepcopy(fields))

    def find_user(self, field, value):
        with self._lock:
            for u in self.users.values():
                if value and u.get(field) == value:
                    return copy.deepcopy(u)
        return None

    def user_stats(self, now=None):
        now = now or time.time()
        with self._lock:
            users = list(self.users.values())
        return _stats(users, now)

    # profiles
    def list_profiles(self, uid):
        with self._lock:
            items = [copy.deepcopy(p) for p in self.profiles.get(uid, {}).values()]
        return sorted(items, key=lambda p: -p.get("updated_at", 0))

    def get_profile(self, uid, pid):
        with self._lock:
            p = self.profiles.get(uid, {}).get(pid)
            return copy.deepcopy(p) if p else None

    def save_profile(self, uid, pid, data):
        now = time.time()
        with self._lock:
            bucket = self.profiles.setdefault(uid, {})
            pid = pid or uuid.uuid4().hex[:16]
            existing = bucket.get(pid)
            if pid in bucket and existing is None:
                return None
            doc = copy.deepcopy(data)
            doc.update(id=pid, created_at=(existing or {}).get("created_at", now), updated_at=now)
            bucket[pid] = doc
            return copy.deepcopy(doc)

    def delete_profile(self, uid, pid):
        with self._lock:
            return self.profiles.get(uid, {}).pop(pid, None) is not None

    def count_profiles(self, uid):
        with self._lock:
            return len(self.profiles.get(uid, {}))

    # sessions
    def add_session(self, uid, record):
        with self._lock:
            sid = uuid.uuid4().hex[:16]
            doc = copy.deepcopy(record)
            doc["id"] = sid
            self.sessions.setdefault(uid, {})[sid] = doc
            return sid

    def list_sessions(self, uid, limit=50):
        with self._lock:
            items = [copy.deepcopy(s) for s in self.sessions.get(uid, {}).values()]
        return sorted(items, key=lambda s: -s.get("created_at", 0))[:limit]

    def get_session(self, uid, sid):
        with self._lock:
            s = self.sessions.get(uid, {}).get(sid)
            return copy.deepcopy(s) if s else None

    def delete_session(self, uid, sid):
        with self._lock:
            return self.sessions.get(uid, {}).pop(sid, None) is not None

    def count_sessions_since(self, uid, since):
        with self._lock:
            return sum(1 for s in self.sessions.get(uid, {}).values() if s.get("created_at", 0) >= since)

    # payments
    def put_payment(self, ref, data):
        with self._lock:
            self.payments.setdefault(ref, {}).update(copy.deepcopy(data))

    def get_payment(self, ref):
        with self._lock:
            p = self.payments.get(ref)
            return copy.deepcopy(p) if p else None

    def claim_payment(self, ref) -> bool:
        """True exactly once per reference: makes webhook + redirect verification idempotent."""
        with self._lock:
            p = self.payments.setdefault(ref, {})
            if p.get("processed"):
                return False
            p["processed"] = True
            p["processed_at"] = time.time()
            return True


def _stats(users, now):
    def pro_active(u):
        return str(u.get("plan", "")).startswith("pro") and u.get("subscription_status") in ("active", "cancelled") \
            and (u.get("subscription_expires") or 0) > now
    total = len(users)
    pro = sum(1 for u in users if pro_active(u))
    return {
        "total_users": total,
        "pro_users": pro,
        "free_users": total - pro,
        "new_last_7d": sum(1 for u in users if (u.get("created_at") or 0) >= now - 7 * 86400),
        "new_last_30d": sum(1 for u in users if (u.get("created_at") or 0) >= now - 30 * 86400),
        "by_plan": {
            "pro_monthly": sum(1 for u in users if pro_active(u) and u.get("plan") == "pro_monthly"),
            "pro_annual": sum(1 for u in users if pro_active(u) and u.get("plan") == "pro_annual"),
        },
    }


def _load_service_account(raw: str) -> dict:
    """Parse FIREBASE_SERVICE_ACCOUNT_JSON. Accepts the JSON on ONE line, or the same JSON base64-encoded.
    A multi-line value pasted into .env / Railway gets cut after the first line (just "{"), so say so clearly."""
    import base64

    raw = (raw or "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        raw = raw[1:-1].strip()
    if not raw.startswith("{"):
        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except Exception:
            pass
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "FIREBASE_SERVICE_ACCOUNT_JSON is not valid JSON (it must be the whole service-account JSON on a "
            "single line, or base64 of it). Got %d characters." % len(raw)
        ) from exc


class FirestoreStore:
    """Firebase Admin SDK backed store. Imported lazily so Free-only deployments need nothing."""

    def __init__(self):
        import firebase_admin
        from firebase_admin import credentials, firestore

        if not firebase_admin._apps:
            if Config.FIREBASE_SERVICE_ACCOUNT_JSON:
                cred = credentials.Certificate(_load_service_account(Config.FIREBASE_SERVICE_ACCOUNT_JSON))
            else:
                cred = credentials.ApplicationDefault()
            firebase_admin.initialize_app(cred, {"projectId": Config.FIREBASE_PROJECT_ID} if Config.FIREBASE_PROJECT_ID else None)
        self.db = firestore.client()
        self._firestore = firestore

    def _user(self, uid):
        return self.db.collection("users").document(uid)

    def get_user(self, uid):
        d = self._user(uid).get()
        return d.to_dict() if d.exists else None

    def upsert_user(self, uid, fields):
        self._user(uid).set({"uid": uid, **fields}, merge=True)

    def find_user(self, field, value):
        if not value:
            return None
        for d in self.db.collection("users").where(field, "==", value).limit(1).stream():
            return d.to_dict()
        return None

    def user_stats(self, now=None):
        now = now or time.time()
        users = self.db.collection("users")

        def count(q):
            return int(q.count().get()[0][0].value)

        total = count(users)
        active = users.where("subscription_status", "in", ["active", "cancelled"]).where("subscription_expires", ">", now)
        monthly = count(active.where("plan", "==", "pro_monthly"))
        annual = count(active.where("plan", "==", "pro_annual"))
        return {
            "total_users": total,
            "pro_users": monthly + annual,
            "free_users": total - monthly - annual,
            "new_last_7d": count(users.where("created_at", ">=", now - 7 * 86400)),
            "new_last_30d": count(users.where("created_at", ">=", now - 30 * 86400)),
            "by_plan": {"pro_monthly": monthly, "pro_annual": annual},
        }

    def _profiles(self, uid):
        return self._user(uid).collection("qualityProfiles")

    def list_profiles(self, uid):
        return [d.to_dict() for d in self._profiles(uid).order_by("updated_at", direction="DESCENDING").limit(200).stream()]

    def get_profile(self, uid, pid):
        d = self._profiles(uid).document(pid).get()
        return d.to_dict() if d.exists else None

    def save_profile(self, uid, pid, data):
        now = time.time()
        pid = pid or uuid.uuid4().hex[:16]
        ref = self._profiles(uid).document(pid)
        existing = ref.get()
        doc = dict(data)
        doc.update(id=pid, created_at=(existing.to_dict() or {}).get("created_at", now) if existing.exists else now, updated_at=now)
        ref.set(doc)
        return doc

    def delete_profile(self, uid, pid):
        ref = self._profiles(uid).document(pid)
        if not ref.get().exists:
            return False
        ref.delete()
        return True

    def count_profiles(self, uid):
        return int(self._profiles(uid).count().get()[0][0].value)

    def _sessions(self, uid):
        return self._user(uid).collection("sessions")

    def add_session(self, uid, record):
        sid = uuid.uuid4().hex[:16]
        self._sessions(uid).document(sid).set({**record, "id": sid})
        return sid

    def list_sessions(self, uid, limit=50):
        return [d.to_dict() for d in self._sessions(uid).order_by("created_at", direction="DESCENDING").limit(limit).stream()]

    def get_session(self, uid, sid):
        d = self._sessions(uid).document(sid).get()
        return d.to_dict() if d.exists else None

    def delete_session(self, uid, sid):
        ref = self._sessions(uid).document(sid)
        if not ref.get().exists:
            return False
        ref.delete()
        return True

    def count_sessions_since(self, uid, since):
        return int(self._sessions(uid).where("created_at", ">=", since).count().get()[0][0].value)

    def put_payment(self, ref, data):
        self.db.collection("payments").document(ref).set(data, merge=True)

    def get_payment(self, ref):
        d = self.db.collection("payments").document(ref).get()
        return d.to_dict() if d.exists else None

    def claim_payment(self, ref) -> bool:
        firestore = self._firestore
        doc = self.db.collection("payments").document(ref)

        @firestore.transactional
        def _claim(txn):
            snap = doc.get(transaction=txn)
            if snap.exists and (snap.to_dict() or {}).get("processed"):
                return False
            txn.set(doc, {"processed": True, "processed_at": time.time()}, merge=True)
            return True

        return _claim(self.db.transaction())


_store = None
_store_error: Optional[str] = None


def get_store():
    """The configured store, or None when accounts are not configured (Free keeps working)."""
    global _store, _store_error
    if _store is not None:
        return _store
    try:
        if Config.FIREBASE_SERVICE_ACCOUNT_JSON or Config.FIREBASE_PROJECT_ID:
            _store = FirestoreStore()
        elif Config.ALLOW_MEMORY_STORE:
            _store = MemoryStore()
        else:
            _store_error = "Accounts are not configured on this server."
    except Exception as exc:  # never let account problems break the Free tool
        logger.exception("Could not initialise the account store")
        _store_error = f"Accounts are temporarily unavailable ({type(exc).__name__})."
    return _store


def set_store_for_tests(store):
    global _store
    _store = store


def store_error() -> str:
    return _store_error or "Accounts are temporarily unavailable."
