"""
Operational metadata store for the admin dashboard.

Job metadata only (sizes, counts, scores, rule/issue names, timing,
status) — never dataset contents, and session ids/IPs are always
HMAC-hashed before they reach this module (utils/session.hash_value).

Two backends behind one set of functions:

  * SQLite (default; dev, tests, single instance). WAL mode. NOT shared between machines.
  * PostgreSQL when DATABASE_URL is set (production, multi-instance). Pooled connections,
    advisory-locked migrations. SQL here is deliberately the portable subset, `?`
    placeholders are translated to `%s` for Postgres.

Beyond the admin metadata this module is the SOURCE OF TRUTH for job lifecycle
(queued -> running -> processed/failed), leases, retries and dead-lettering, so the API,
workers and reaper on different machines agree. Redis only delivers wake-ups.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from decimal import Decimal
from typing import Optional

from config import Config

logger = logging.getLogger("omixa.db")

try:  # psycopg is only needed when DATABASE_URL is set
    import psycopg as _psycopg
    _DB_ERRORS: tuple = (sqlite3.Error, _psycopg.Error)
except Exception:  # pragma: no cover - exercised only where psycopg is absent
    _psycopg = None
    _DB_ERRORS = (sqlite3.Error,)


def _use_pg() -> bool:
    return bool(Config.DATABASE_URL)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    session_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    original_ext TEXT,
    original_filename TEXT,
    file_size_bytes INTEGER,
    status TEXT NOT NULL DEFAULT 'uploaded',
    rows_in INTEGER,
    rows_out INTEGER,
    columns_in INTEGER,
    quality_score_before INTEGER,
    quality_score_after INTEGER,
    quality_grade_before TEXT,
    quality_grade_after TEXT,
    rules_applied TEXT,
    issues_before TEXT,
    error_type TEXT,
    processing_ms INTEGER,
    downloaded_at REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_session ON jobs(session_hash);

CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    ip_hash TEXT NOT NULL,
    success INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_login_ts ON login_attempts(ts);

CREATE TABLE IF NOT EXISTS error_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    job_id TEXT,
    route TEXT,
    error_type TEXT,
    message TEXT
);
CREATE INDEX IF NOT EXISTS idx_error_ts ON error_log(ts);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    actor TEXT,
    action TEXT NOT NULL,
    target TEXT,
    request_id TEXT,
    meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action, ts);

CREATE TABLE IF NOT EXISTS usage_daily (
    day TEXT NOT NULL,
    principal TEXT NOT NULL,
    jobs INTEGER NOT NULL DEFAULT 0,
    rows_processed BIGINT NOT NULL DEFAULT 0,
    bytes_uploaded BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (day, principal)
);
"""


def _portable_ddl(sql: str) -> list[str]:
    """SQLite DDL -> list of statements, translated for Postgres when needed."""
    if _use_pg():
        sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
        sql = re.sub(r"\bREAL\b", "DOUBLE PRECISION", sql)
    return [stmt.strip() for stmt in sql.split(";") if stmt.strip()]


_PATH_RE = re.compile(r"(?:[A-Za-z]:)?[\\/](?:[^\\/\s'\"]+[\\/])+[^\\/\s'\"]*")
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_LONG_NUMBER_RE = re.compile(r"\d{6,}")


def scrub_error_message(message: str, limit: int = 200) -> str:
    """Exception text can embed server file paths and, for parsing errors, a
    fragment of the user's own data (a cell value, an email, an account
    number). The admin error log is operational metadata only, so paths,
    emails and long digit runs are masked and the text is truncated before
    it is stored."""
    text = str(message or "")
    text = _PATH_RE.sub("<path>", text)
    text = _EMAIL_RE.sub("<email>", text)
    text = _LONG_NUMBER_RE.sub("<number>", text)
    text = " ".join(text.split())
    return text[:limit]


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(Config.DB_PATH), exist_ok=True)
    conn = sqlite3.connect(Config.DB_PATH, timeout=10, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


_pool = None


def _get_pool():
    global _pool
    if _pool is None:
        from psycopg_pool import ConnectionPool  # lazy
        _pool = ConnectionPool(
            Config.DATABASE_URL, min_size=1, max_size=Config.DB_POOL_MAX, timeout=5,
            kwargs={"autocommit": False}, open=True,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def _clean_row(row):
    if row is None:
        return None
    return {k: (float(v) if isinstance(v, Decimal) else v) for k, v in row.items()}


class _PgCursor:
    """Makes a psycopg cursor look like the sqlite3 one the rest of this module was written
    for: `?` placeholders, dict rows, Decimal -> float."""

    def __init__(self, cur):
        self._cur = cur

    @staticmethod
    def _sql(sql: str) -> str:
        return sql.replace("%", "%%").replace("?", "%s")

    def execute(self, sql, params=()):
        self._cur.execute(self._sql(sql), params)
        return self

    def executemany(self, sql, seq):
        self._cur.executemany(self._sql(sql), seq)
        return self

    def fetchone(self):
        return _clean_row(self._cur.fetchone())

    def fetchall(self):
        return [_clean_row(r) for r in self._cur.fetchall()]

    @property
    def rowcount(self):
        return self._cur.rowcount


@contextmanager
def _cursor():
    """One short-lived connection (or pooled checkout) per call; commits on success and
    rolls back on error. The metadata/logging helpers below swallow and log DB errors so
    a logging failure can't break the request it describes; the job-lifecycle functions
    further down deliberately let errors propagate."""
    if _use_pg():
        from psycopg.rows import dict_row
        with _get_pool().connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            try:
                yield _PgCursor(cur)
            finally:
                cur.close()
        return
    conn = _connect()
    try:
        yield conn.cursor()
        conn.commit()
    finally:
        conn.close()


# Columns added after the first release. init_db adds any that an existing database file is
# missing, so deploying a new version never requires wiping the metadata db.
_ADDED_COLUMNS = {
    "jobs": [
        ("dimension_scores_before", "TEXT"),
        ("dimension_scores_after", "TEXT"),
        ("quality_model_version", "INTEGER"),
        # --- job lifecycle (queue mode) ---
        ("owner_uid", "TEXT"),
        ("spec_json", "TEXT"),
        ("result_json", "TEXT"),
        ("cleaned_ext", "TEXT"),
        ("attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("priority", "INTEGER NOT NULL DEFAULT 1"),
        ("available_at", "REAL"),
        ("lease_owner", "TEXT"),
        ("lease_expires_at", "REAL"),
        ("dead_letter", "INTEGER NOT NULL DEFAULT 0"),
        ("request_id", "TEXT"),
        ("purged_at", "REAL"),
        ("error_public", "TEXT"),
        ("report_json", "TEXT"),
    ],
}

# Indexes that depend on the added columns (created after the ALTERs).
_LATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_jobs_owner_uid ON jobs(owner_uid, status)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_lease ON jobs(status, lease_expires_at)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(status, available_at)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_purge ON jobs(purged_at, created_at)",
]


def init_db() -> None:
    if _use_pg():
        with _cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(727001)")  # serialise concurrent boots/migrations
            for stmt in _portable_ddl(_SCHEMA):
                cur.execute(stmt)
            for table, cols in _ADDED_COLUMNS.items():
                for name, decl in cols:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {_pg_decl(decl)}")
            for stmt in _LATE_INDEXES:
                cur.execute(stmt)
        return
    with _cursor() as cur:
        cur.executescript(_SCHEMA)
        for table, cols in _ADDED_COLUMNS.items():
            existing = {row[1] for row in cur.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, decl in cols:
                if name not in existing:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        for stmt in _LATE_INDEXES:
            cur.execute(stmt)


def _pg_decl(decl: str) -> str:
    return re.sub(r"\bREAL\b", "DOUBLE PRECISION", decl)


def record_upload(job_id: str, session_hash: str, ext: str, filename: str, size_bytes: int,
                  owner_uid: Optional[str] = None, request_id: Optional[str] = None) -> bool:
    """Creates the job row. Returns False (and logs) on a DB error so queue-mode callers can
    refuse the request instead of continuing with a job the workers can never see."""
    now = time.time()
    try:
        with _cursor() as cur:
            cur.execute(
                """INSERT INTO jobs
                   (job_id, session_hash, created_at, updated_at, original_ext,
                    original_filename, file_size_bytes, status, owner_uid, request_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'uploaded', ?, ?)
                   ON CONFLICT(job_id) DO UPDATE SET
                    session_hash=excluded.session_hash, updated_at=excluded.updated_at,
                    original_ext=excluded.original_ext, original_filename=excluded.original_filename,
                    file_size_bytes=excluded.file_size_bytes, status='uploaded',
                    owner_uid=excluded.owner_uid, request_id=excluded.request_id""",
                (job_id, session_hash, now, now, ext, filename, size_bytes, owner_uid, request_id),
            )
        return True
    except _DB_ERRORS:
        logger.exception("db: record_upload failed for job %s", job_id)
        return False


def record_processed(
    job_id: str,
    *,
    rows_in: int,
    rows_out: int,
    columns_in: int,
    quality_before: Optional[dict],
    quality_after: Optional[dict],
    rules_applied: list,
    processing_ms: int,
    worker_id: Optional[str] = None,
    result_json: Optional[str] = None,
    cleaned_ext: Optional[str] = None,
) -> bool:
    """Marks the job processed. With worker_id (queue mode) this is a compare-and-set: it only
    applies while that worker still holds the lease, so a zombie worker that was already
    replaced cannot overwrite the result. Returns whether a row was updated."""
    issues_before = []
    if quality_before:
        issues_before = [f.get("issue") for f in quality_before.get("findings", []) if f.get("issue")]
    try:
        with _cursor() as cur:
            cur.execute(
                """UPDATE jobs SET
                    status='processed', updated_at=?, rows_in=?, rows_out=?, columns_in=?,
                    quality_score_before=?, quality_score_after=?,
                    quality_grade_before=?, quality_grade_after=?,
                    rules_applied=?, issues_before=?, processing_ms=?,
                    dimension_scores_before=?, dimension_scores_after=?, quality_model_version=?,
                    result_json=COALESCE(?, result_json), cleaned_ext=COALESCE(?, cleaned_ext),
                    lease_owner=NULL, lease_expires_at=NULL
                   WHERE job_id=?""" + (" AND status='running' AND lease_owner=?" if worker_id else ""),
                (
                    time.time(), rows_in, rows_out, columns_in,
                    (quality_before or {}).get("score"), (quality_after or {}).get("score"),
                    (quality_before or {}).get("grade"), (quality_after or {}).get("grade"),
                    json.dumps(rules_applied or []), json.dumps(issues_before), processing_ms,
                    json.dumps((quality_before or {}).get("dimension_scores")),
                    json.dumps((quality_after or {}).get("dimension_scores")),
                    (quality_after or {}).get("quality_model_version"),
                    result_json, cleaned_ext,
                    job_id,
                ) + ((worker_id,) if worker_id else ()),
            )
            return cur.rowcount > 0
    except _DB_ERRORS:
        logger.exception("db: record_processed failed for job %s", job_id)
        return False


def record_failed(job_id: str, route: str, error_type: str, message: str) -> None:
    now = time.time()
    try:
        with _cursor() as cur:
            cur.execute(
                "UPDATE jobs SET status='failed', updated_at=?, error_type=? WHERE job_id=?",
                (now, error_type, job_id),
            )
            cur.execute(
                "INSERT INTO error_log (ts, job_id, route, error_type, message) VALUES (?, ?, ?, ?, ?)",
                (now, job_id, route, error_type, scrub_error_message(message)),
            )
    except _DB_ERRORS:
        logger.exception("db: record_failed failed for job %s", job_id)


def record_downloaded(job_id: str) -> None:
    now = time.time()
    try:
        with _cursor() as cur:
            cur.execute(
                "UPDATE jobs SET status='downloaded', updated_at=?, downloaded_at=? WHERE job_id=?",
                (now, now, job_id),
            )
    except _DB_ERRORS:
        logger.exception("db: record_downloaded failed for job %s", job_id)


def mark_expired(job_ids: list[str]) -> None:
    if not job_ids:
        return
    now = time.time()
    try:
        with _cursor() as cur:
            cur.executemany(
                "UPDATE jobs SET status='expired', updated_at=? WHERE job_id=? "
                "AND status IN ('uploaded', 'processed')",
                [(now, jid) for jid in job_ids],
            )
    except _DB_ERRORS:
        logger.exception("db: mark_expired failed")


def record_login_attempt(ip_hash: str, success: bool) -> None:
    try:
        with _cursor() as cur:
            cur.execute(
                "INSERT INTO login_attempts (ts, ip_hash, success) VALUES (?, ?, ?)",
                (time.time(), ip_hash, 1 if success else 0),
            )
    except _DB_ERRORS:
        logger.exception("db: record_login_attempt failed")


def recent_login_attempts(limit: int = 20, since: Optional[float] = None) -> list[dict]:
    try:
        with _cursor() as cur:
            if since is not None:
                cur.execute(
                    "SELECT * FROM login_attempts WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
                    (since, limit),
                )
            else:
                cur.execute("SELECT * FROM login_attempts ORDER BY ts DESC LIMIT ?", (limit,))
            return [dict(r) for r in cur.fetchall()]
    except _DB_ERRORS:
        logger.exception("db: recent_login_attempts failed")
        return []


def stats_summary(window_seconds: Optional[int] = None) -> dict:
    """Aggregate figures for the admin dashboard's top-level cards.
    window_seconds=None means "all time"."""
    since = time.time() - window_seconds if window_seconds else 0
    try:
        with _cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM jobs WHERE created_at >= ?", (since,))
            total_jobs = cur.fetchone()["n"]

            cur.execute(
                "SELECT COUNT(DISTINCT session_hash) AS n FROM jobs WHERE created_at >= ?", (since,)
            )
            distinct_sessions = cur.fetchone()["n"]

            cur.execute(
                "SELECT status, COUNT(*) AS n FROM jobs WHERE created_at >= ? GROUP BY status",
                (since,),
            )
            by_status = {r["status"]: r["n"] for r in cur.fetchall()}

            cur.execute(
                "SELECT original_ext, COUNT(*) AS n FROM jobs WHERE created_at >= ? "
                "AND original_ext IS NOT NULL GROUP BY original_ext",
                (since,),
            )
            by_format = {r["original_ext"]: r["n"] for r in cur.fetchall()}

            cur.execute(
                "SELECT AVG(processing_ms) AS avg_ms FROM jobs WHERE created_at >= ? "
                "AND processing_ms IS NOT NULL",
                (since,),
            )
            avg_ms = cur.fetchone()["avg_ms"]

            cur.execute(
                "SELECT AVG(quality_score_before) AS b, AVG(quality_score_after) AS a "
                "FROM jobs WHERE created_at >= ? AND quality_score_after IS NOT NULL",
                (since,),
            )
            row = cur.fetchone()
            avg_before, avg_after = row["b"], row["a"]

            cur.execute(
                "SELECT rules_applied FROM jobs WHERE created_at >= ? AND rules_applied IS NOT NULL",
                (since,),
            )
            rule_counts: dict[str, int] = {}
            for r in cur.fetchall():
                try:
                    for rule in json.loads(r["rules_applied"]):
                        rule_counts[rule] = rule_counts.get(rule, 0) + 1
                except (TypeError, ValueError):
                    continue

            cur.execute(
                "SELECT issues_before FROM jobs WHERE created_at >= ? AND issues_before IS NOT NULL",
                (since,),
            )
            issue_counts: dict[str, int] = {}
            for r in cur.fetchall():
                try:
                    for issue in json.loads(r["issues_before"]):
                        issue_counts[issue] = issue_counts.get(issue, 0) + 1
                except (TypeError, ValueError):
                    continue

            cur.execute(
                "SELECT COUNT(*) AS n FROM error_log WHERE ts >= ?", (since,)
            )
            error_count = cur.fetchone()["n"]

        processed = by_status.get("processed", 0) + by_status.get("downloaded", 0)
        failed = by_status.get("failed", 0)
        denom = processed + failed
        success_rate = round((processed / denom) * 100, 1) if denom else None

        top_rules = sorted(rule_counts.items(), key=lambda kv: -kv[1])[:10]
        top_issues = sorted(issue_counts.items(), key=lambda kv: -kv[1])[:10]

        return {
            "total_jobs": total_jobs,
            "distinct_sessions": distinct_sessions,
            "by_status": by_status,
            "by_format": by_format,
            "processing_success_count": processed,
            "processing_failure_count": failed,
            "processing_success_rate_pct": success_rate,
            "avg_processing_ms": round(avg_ms, 1) if avg_ms is not None else None,
            "avg_quality_score_before": round(avg_before, 1) if avg_before is not None else None,
            "avg_quality_score_after": round(avg_after, 1) if avg_after is not None else None,
            "top_rules": top_rules,
            "top_issues": top_issues,
            "error_count": error_count,
        }
    except _DB_ERRORS:
        logger.exception("db: stats_summary failed")
        return {}


def list_jobs(
    *,
    status: Optional[str] = None,
    fmt: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Filterable/searchable job listing for the admin dashboard.
    `q` matches on job_id or original_filename (metadata only, never
    file contents)."""
    where = []
    params: list = []
    if status:
        where.append("status = ?")
        params.append(status)
    if fmt:
        where.append("original_ext = ?")
        params.append(fmt)
    if q:
        where.append("(job_id LIKE ? OR original_filename LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like])
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    try:
        with _cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM jobs {clause}", params)
            total = cur.fetchone()["n"]
            cur.execute(
                f"SELECT * FROM jobs {clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            )
            rows = [dict(r) for r in cur.fetchall()]
        return rows, total
    except _DB_ERRORS:
        logger.exception("db: list_jobs failed")
        return [], 0


def recent_errors(limit: int = 20) -> list[dict]:
    try:
        with _cursor() as cur:
            cur.execute("SELECT * FROM error_log ORDER BY ts DESC LIMIT ?", (limit,))
            return [dict(r) for r in cur.fetchall()]
    except _DB_ERRORS:
        logger.exception("db: recent_errors failed")
        return []


def get_job_filename(job_id: str) -> Optional[str]:
    try:
        with _cursor() as cur:
            row = cur.execute("SELECT original_filename FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return row["original_filename"] if row else None
    except _DB_ERRORS:
        return None


def get_job_size(job_id: str) -> Optional[int]:
    try:
        with _cursor() as cur:
            row = cur.execute("SELECT file_size_bytes FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return row["file_size_bytes"] if row else None
    except _DB_ERRORS:
        return None


# ====================================================================
# Job lifecycle (source of truth for queue mode)
#
#   uploaded --enqueue--> queued --claim--> running --complete--> processed
#                            ^                  |  \--fail (final)--> failed [dead_letter=1 if retries exhausted]
#                            +---- retry <------+
#
# Every transition is a single conditional UPDATE (compare-and-set), so two workers, a worker
# and the reaper, or a duplicated queue message can never both win. These functions let DB
# errors propagate: callers must not carry on as if a transition happened when it did not.
# ====================================================================

_ACTIVE = ("queued", "running")


def get_job(job_id: str) -> Optional[dict]:
    with _cursor() as cur:
        cur.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def get_job_owner_hash(job_id: str) -> Optional[str]:
    with _cursor() as cur:
        cur.execute("SELECT session_hash FROM jobs WHERE job_id=?", (job_id,))
        row = cur.fetchone()
        return row["session_hash"] if row else None


def enqueue_job(job_id: str, spec: dict, priority: int, request_id: Optional[str] = None) -> bool:
    """uploaded|failed -> queued. Returns False if the job is missing or already queued/running
    (so a double-clicked 'Clean' cannot enqueue twice)."""
    now = time.time()
    with _cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status='queued', spec_json=?, priority=?, attempts=0, available_at=?, "
            "updated_at=?, request_id=COALESCE(?, request_id), error_type=NULL, error_public=NULL, dead_letter=0 "
            "WHERE job_id=? AND status IN ('uploaded','processed','failed')",
            (json.dumps(spec), priority, now, now, request_id, job_id),
        )
        return cur.rowcount > 0


def claim_job(job_id: str, worker_id: str, lease_seconds: int) -> Optional[dict]:
    """queued -> running with a lease. Returns the job row if THIS caller won, else None."""
    now = time.time()
    with _cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status='running', lease_owner=?, lease_expires_at=?, attempts=attempts+1, "
            "updated_at=? WHERE job_id=? AND status='queued' AND (available_at IS NULL OR available_at <= ?)",
            (worker_id, now + lease_seconds, now, job_id, now),
        )
        if cur.rowcount == 0:
            return None
        cur.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def heartbeat(job_id: str, worker_id: str, lease_seconds: int) -> bool:
    """Extends the lease. False means the lease was lost (reaper re-queued it): stop working."""
    now = time.time()
    with _cursor() as cur:
        cur.execute(
            "UPDATE jobs SET lease_expires_at=?, updated_at=? "
            "WHERE job_id=? AND status='running' AND lease_owner=?",
            (now + lease_seconds, now, job_id, worker_id),
        )
        return cur.rowcount > 0


def backoff_seconds(attempt: int) -> int:
    """Exponential backoff with a cap: 5s, 10s, 20s, ... max 5 min."""
    return min(300, Config.JOB_RETRY_BASE_SECONDS * (2 ** max(0, attempt - 1)))


def fail_or_retry(job_id: str, worker_id: Optional[str], error_type: str, message: str,
                  retryable: bool, public_message: Optional[str] = None) -> str:
    """Handles a failed attempt. Returns 'retry', 'failed' or 'dead' ('dead' = retryable but
    attempts exhausted: it is flagged dead_letter for an operator), or 'lost' if the caller no
    longer owns the job."""
    now = time.time()
    with _cursor() as cur:
        cur.execute("SELECT attempts, status, lease_owner FROM jobs WHERE job_id=?", (job_id,))
        row = cur.fetchone()
        if not row or row["status"] != "running" or (worker_id and row["lease_owner"] != worker_id):
            return "lost"
        attempts = int(row["attempts"] or 0)
        if retryable and attempts < Config.JOB_MAX_ATTEMPTS:
            cur.execute(
                "UPDATE jobs SET status='queued', available_at=?, updated_at=?, lease_owner=NULL, "
                "lease_expires_at=NULL, error_type=? WHERE job_id=? AND status='running'",
                (now + backoff_seconds(attempts), now, error_type, job_id),
            )
            return "retry"
        dead = 1 if retryable else 0
        cur.execute(
            "UPDATE jobs SET status='failed', updated_at=?, lease_owner=NULL, lease_expires_at=NULL, "
            "error_type=?, error_public=?, dead_letter=? WHERE job_id=? AND status='running'",
            (now, error_type, public_message, dead, job_id),
        )
        cur.execute(
            "INSERT INTO error_log (ts, job_id, route, error_type, message) VALUES (?, ?, ?, ?, ?)",
            (now, job_id, "worker", error_type, scrub_error_message(message)),
        )
        return "dead" if dead else "failed"


def expired_leases(limit: int = 100) -> list[dict]:
    now = time.time()
    with _cursor() as cur:
        cur.execute(
            "SELECT job_id, lease_owner, attempts FROM jobs WHERE status='running' AND lease_expires_at < ? "
            "ORDER BY lease_expires_at LIMIT ?", (now, limit))
        return [dict(r) for r in cur.fetchall()]


def release_expired_lease(job_id: str) -> str:
    """Stuck-job recovery: a worker died or hung without finishing. Re-queue with backoff, or
    dead-letter when attempts are exhausted. CAS on the *expired* lease so a worker that
    finished a moment ago is not clobbered."""
    now = time.time()
    with _cursor() as cur:
        cur.execute("SELECT attempts FROM jobs WHERE job_id=? AND status='running' AND lease_expires_at < ?",
                    (job_id, now))
        row = cur.fetchone()
        if not row:
            return "skipped"
        attempts = int(row["attempts"] or 0)
        if attempts < Config.JOB_MAX_ATTEMPTS:
            cur.execute(
                "UPDATE jobs SET status='queued', available_at=?, updated_at=?, lease_owner=NULL, "
                "lease_expires_at=NULL, error_type='LeaseExpired' "
                "WHERE job_id=? AND status='running' AND lease_expires_at < ?",
                (now + backoff_seconds(attempts), now, job_id, now))
            return "retry" if cur.rowcount else "skipped"
        cur.execute(
            "UPDATE jobs SET status='failed', updated_at=?, lease_owner=NULL, lease_expires_at=NULL, "
            "error_type='LeaseExpired', dead_letter=1, "
            "error_public='This file could not be processed. Please try again.' "
            "WHERE job_id=? AND status='running' AND lease_expires_at < ?", (now, job_id, now))
        return "dead" if cur.rowcount else "skipped"


def stale_queued(older_than_seconds: int, limit: int = 200) -> list[dict]:
    """Queued rows whose queue message may have been lost (Redis flush/failover): the reaper
    re-publishes them. DB is truth; the queue is only a delivery hint."""
    now = time.time()
    with _cursor() as cur:
        cur.execute(
            "SELECT job_id, priority FROM jobs WHERE status='queued' AND available_at <= ? AND updated_at < ? "
            "ORDER BY available_at LIMIT ?", (now, now - older_than_seconds, limit))
        return [dict(r) for r in cur.fetchall()]


def due_queued(limit: int = 200) -> list[dict]:
    now = time.time()
    with _cursor() as cur:
        cur.execute(
            "SELECT job_id, priority FROM jobs WHERE status='queued' AND available_at <= ? "
            "ORDER BY priority, available_at LIMIT ?", (now, limit))
        return [dict(r) for r in cur.fetchall()]


def count_by_status(status: str) -> int:
    with _cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM jobs WHERE status=?", (status,))
        return int(cur.fetchone()["n"])


def count_active_for(owner_uid: Optional[str], session_hash: str) -> int:
    """Queued+running jobs for one principal (signed-in uid, else the anonymous session)."""
    with _cursor() as cur:
        if owner_uid:
            cur.execute("SELECT COUNT(*) AS n FROM jobs WHERE owner_uid=? AND status IN ('queued','running')",
                        (owner_uid,))
        else:
            cur.execute("SELECT COUNT(*) AS n FROM jobs WHERE session_hash=? AND owner_uid IS NULL "
                        "AND status IN ('queued','running')", (session_hash,))
        return int(cur.fetchone()["n"])


def purge_candidates(ttl_seconds: int, limit: int = 200) -> list[dict]:
    """Jobs past their TTL whose stored files have not been deleted yet. Active jobs are left
    to the lease reaper."""
    cutoff = time.time() - ttl_seconds
    with _cursor() as cur:
        cur.execute(
            "SELECT job_id, original_ext, cleaned_ext, status FROM jobs "
            "WHERE purged_at IS NULL AND created_at < ? AND status NOT IN ('queued','running') "
            "ORDER BY created_at LIMIT ?", (cutoff, limit))
        return [dict(r) for r in cur.fetchall()]


def mark_purged(job_ids: list[str]) -> None:
    if not job_ids:
        return
    now = time.time()
    with _cursor() as cur:
        cur.executemany(
            "UPDATE jobs SET purged_at=?, result_json=NULL, spec_json=NULL, "
            "status=CASE WHEN status IN ('uploaded','processed') THEN 'expired' ELSE status END, "
            "updated_at=? WHERE job_id=?", [(now, now, j) for j in job_ids])


def set_downloaded_and_clear(job_id: str) -> None:
    record_downloaded(job_id)


def audit(actor: Optional[str], action: str, target: Optional[str] = None,
          request_id: Optional[str] = None, meta: Optional[dict] = None) -> None:
    """Append-only audit trail (payments granted, admin logins, job dead-letters). Never raises:
    auditing must not break the action it records. Never put secrets or file contents in meta."""
    try:
        with _cursor() as cur:
            cur.execute(
                "INSERT INTO audit_log (ts, actor, action, target, request_id, meta) VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), actor, action, target, request_id, json.dumps(meta or {})[:2000]))
    except _DB_ERRORS:
        logger.exception("db: audit write failed (%s)", action)


def usage_add(principal: str, rows: int, size_bytes: int) -> None:
    day = time.strftime("%Y-%m-%d", time.gmtime())
    try:
        with _cursor() as cur:
            cur.execute(
                "INSERT INTO usage_daily (day, principal, jobs, rows_processed, bytes_uploaded) "
                "VALUES (?, ?, 1, ?, ?) ON CONFLICT(day, principal) DO UPDATE SET "
                "jobs=usage_daily.jobs+1, rows_processed=usage_daily.rows_processed+excluded.rows_processed, "
                "bytes_uploaded=usage_daily.bytes_uploaded+excluded.bytes_uploaded",
                (day, principal, int(rows or 0), int(size_bytes or 0)))
    except _DB_ERRORS:
        logger.exception("db: usage_add failed")


def complete_analysis(job_id: str, worker_id: str, report_json: str) -> bool:
    """Analysis job finished: store the report and put the job back to 'uploaded' so the user can
    clean it. CAS on the lease so a replaced worker cannot write."""
    now = time.time()
    with _cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status='uploaded', report_json=?, updated_at=?, lease_owner=NULL, "
            "lease_expires_at=NULL, error_type=NULL WHERE job_id=? AND status='running' AND lease_owner=?",
            (report_json, now, job_id, worker_id))
        return cur.rowcount > 0


def ping() -> Optional[float]:
    """Round-trip to the database in ms, or None if unreachable (used by /readyz and metrics)."""
    t = time.monotonic()
    try:
        with _cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            cur.fetchone()
    except _DB_ERRORS:
        return None
    return round((time.monotonic() - t) * 1000, 2)


def count_dead_letters() -> int:
    with _cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM jobs WHERE dead_letter=1 AND purged_at IS NULL")
        return int(cur.fetchone()["n"])
