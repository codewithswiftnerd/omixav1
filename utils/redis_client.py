"""
Single access point for Redis. Nothing else imports `redis` directly, so tests can
swap in a fake and the app degrades cleanly when Redis is not configured or is down.

Redis here is a *cache / rate limiter / queue delivery channel*. It is never the source
of truth: job state and payments live in the database. Losing Redis must slow Omixa
down, never lose a paid entitlement or a user's job record.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from config import Config

logger = logging.getLogger("omixa.redis")

_client = None
_last_error_log = 0.0


def get_redis():
    """The shared client, or None when REDIS_URL is unset or the library is missing."""
    global _client
    if _client is not None:
        return _client
    if not Config.REDIS_URL:
        return None
    try:
        import redis  # lazy: dev installs without Redis keep working
        _client = redis.Redis.from_url(
            Config.REDIS_URL,
            socket_timeout=1.5,
            socket_connect_timeout=1.5,
            health_check_interval=30,
            decode_responses=True,
        )
    except Exception:
        logger.exception("Could not create the Redis client; continuing without Redis")
        return None
    return _client


def set_redis_for_tests(client) -> None:
    global _client
    _client = client


def note_failure(where: str, exc: Exception) -> None:
    """Rate-limited error log so a Redis outage is visible without flooding the logs."""
    global _last_error_log
    now = time.monotonic()
    if now - _last_error_log > 30:
        _last_error_log = now
        logger.warning("Redis unavailable in %s (%s); degrading", where, type(exc).__name__)


def ping() -> Optional[float]:
    """Round-trip in ms, or None when Redis is unconfigured/unreachable."""
    r = get_redis()
    if r is None:
        return None
    t = time.monotonic()
    try:
        r.ping()
    except Exception as exc:
        note_failure("ping", exc)
        return None
    return round((time.monotonic() - t) * 1000, 2)
