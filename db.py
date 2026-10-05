"""
Operational metadata store for the admin dashboard.

Job metadata only (sizes, counts, scores, rule/issue names, timing,
status) — never dataset contents, and session ids/IPs are always
HMAC-hashed before they reach this module (utils/session.hash_value).

Plain sqlite3, WAL mode.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from typing import Optional

from config import Config

logger = logging.getLogger("omixa.db")

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
"""


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


@contextmanager
def _cursor():
    """One short-lived connection per call. Never raises into the
    caller — every function below swallows and logs instead, so a
    metadata-logging failure can't break the request it's describing.
    """
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
    ],
}


def init_db() -> None:
    with _cursor() as cur:
        cur.executescript(_SCHEMA)
        for table, cols in _ADDED_COLUMNS.items():
            existing = {row[1] for row in cur.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, decl in cols:
                if name not in existing:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def record_upload(job_id: str, session_hash: str, ext: str, filename: str, size_bytes: int) -> None:
    now = time.time()
    try:
        with _cursor() as cur:
            cur.execute(
                """INSERT OR REPLACE INTO jobs
                   (job_id, session_hash, created_at, updated_at, original_ext,
                    original_filename, file_size_bytes, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'uploaded')""",
                (job_id, session_hash, now, now, ext, filename, size_bytes),
            )
    except sqlite3.Error:
        logger.exception("db: record_upload failed for job %s", job_id)


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
) -> None:
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
                    dimension_scores_before=?, dimension_scores_after=?, quality_model_version=?
                   WHERE job_id=?""",
                (
                    time.time(), rows_in, rows_out, columns_in,
                    (quality_before or {}).get("score"), (quality_after or {}).get("score"),
                    (quality_before or {}).get("grade"), (quality_after or {}).get("grade"),
                    json.dumps(rules_applied or []), json.dumps(issues_before), processing_ms,
                    json.dumps((quality_before or {}).get("dimension_scores")),
                    json.dumps((quality_after or {}).get("dimension_scores")),
                    (quality_after or {}).get("quality_model_version"),
                    job_id,
                ),
            )
    except sqlite3.Error:
        logger.exception("db: record_processed failed for job %s", job_id)


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
    except sqlite3.Error:
        logger.exception("db: record_failed failed for job %s", job_id)


def record_downloaded(job_id: str) -> None:
    now = time.time()
    try:
        with _cursor() as cur:
            cur.execute(
                "UPDATE jobs SET status='downloaded', updated_at=?, downloaded_at=? WHERE job_id=?",
                (now, now, job_id),
            )
    except sqlite3.Error:
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
    except sqlite3.Error:
        logger.exception("db: mark_expired failed")


def record_login_attempt(ip_hash: str, success: bool) -> None:
    try:
        with _cursor() as cur:
            cur.execute(
                "INSERT INTO login_attempts (ts, ip_hash, success) VALUES (?, ?, ?)",
                (time.time(), ip_hash, 1 if success else 0),
            )
    except sqlite3.Error:
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
    except sqlite3.Error:
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
    except sqlite3.Error:
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
    except sqlite3.Error:
        logger.exception("db: list_jobs failed")
        return [], 0


def recent_errors(limit: int = 20) -> list[dict]:
    try:
        with _cursor() as cur:
            cur.execute("SELECT * FROM error_log ORDER BY ts DESC LIMIT ?", (limit,))
            return [dict(r) for r in cur.fetchall()]
    except sqlite3.Error:
        logger.exception("db: recent_errors failed")
        return []
