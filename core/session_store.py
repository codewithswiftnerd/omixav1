"""
Omixa Session Store
In-memory store for uploaded and cleaned DataFrames keyed by session ID.
For production, swap the dict for Redis or a database.
"""

import uuid
import time
import threading
from dataclasses import dataclass, field
import pandas as pd


@dataclass
class Session:
    session_id: str
    filename: str
    original_df: pd.DataFrame
    cleaned_df: pd.DataFrame | None = None
    report: dict | None = None
    domain: str = "general"
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)


class SessionStore:
    """Thread-safe in-memory session store with TTL eviction."""

    def __init__(self, ttl_seconds: int = 3600):
        self._store: dict[str, Session] = {}
        self._lock = threading.Lock()
        self.ttl = ttl_seconds
        self._start_cleanup_thread()

    def create(self, filename: str, df: pd.DataFrame) -> str:
        session_id = str(uuid.uuid4())
        with self._lock:
            self._store[session_id] = Session(
                session_id=session_id,
                filename=filename,
                original_df=df,
            )
        return session_id

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            session = self._store.get(session_id)
            if session:
                session.last_accessed = time.time()
            return session

    def update_cleaned(self, session_id: str, cleaned_df: pd.DataFrame, report: dict) -> bool:
        with self._lock:
            session = self._store.get(session_id)
            if not session:
                return False
            session.cleaned_df = cleaned_df
            session.report = report
            session.last_accessed = time.time()
            return True

    def delete(self, session_id: str) -> bool:
        with self._lock:
            return self._store.pop(session_id, None) is not None

    def _evict_expired(self):
        now = time.time()
        with self._lock:
            expired = [sid for sid, s in self._store.items()
                       if now - s.last_accessed > self.ttl]
            for sid in expired:
                del self._store[sid]

    def _start_cleanup_thread(self):
        def loop():
            while True:
                time.sleep(300)  # clean up every 5 minutes
                self._evict_expired()

        t = threading.Thread(target=loop, daemon=True)
        t.start()


# Singleton instance shared across routes
store = SessionStore(ttl_seconds=3600)
