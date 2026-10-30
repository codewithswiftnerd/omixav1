"""
Omixa User Store
SQLite-backed user accounts (stdlib only, no extra dependencies).
The `plan` / `pro_until` columns are reserved for Omixa Pro billing:
auth never changes them, so your payment/Pro logic can update them freely.
"""

import os
import sqlite3
import threading
import time
import uuid

DB_PATH = os.environ.get("OMIXA_DB_PATH", os.path.join(os.path.dirname(os.path.dirname(__file__)), "omixa_users.db"))
_lock = threading.Lock()


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _lock, _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                password_hash TEXT,
                google_id TEXT,
                plan TEXT NOT NULL DEFAULT 'free',
                pro_until REAL,
                created_at REAL NOT NULL
            )
        """)


def _row(r):
    return dict(r) if r else None


def get_by_email(email: str):
    with _lock, _conn() as c:
        return _row(c.execute("SELECT * FROM users WHERE email = ?", (email.lower(),)).fetchone())


def get_by_id(user_id: str):
    with _lock, _conn() as c:
        return _row(c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())


def create_user(email: str, name: str, password_hash: str | None = None, google_id: str | None = None):
    uid = str(uuid.uuid4())
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO users (id, email, name, password_hash, google_id, created_at) VALUES (?,?,?,?,?,?)",
            (uid, email.lower(), name, password_hash, google_id, time.time()),
        )
    return get_by_id(uid)


def set_password(user_id: str, password_hash: str):
    with _lock, _conn() as c:
        c.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user_id))


def link_google(user_id: str, google_id: str):
    with _lock, _conn() as c:
        c.execute("UPDATE users SET google_id = ? WHERE id = ?", (google_id, user_id))


def public_user(u: dict) -> dict:
    """Safe-to-return user fields (never the hash)."""
    return {
        "id": u["id"],
        "email": u["email"],
        "name": u["name"],
        "plan": u["plan"],
        "pro_until": u["pro_until"],
    }
