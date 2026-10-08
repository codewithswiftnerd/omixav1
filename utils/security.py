"""
Request-level rate limiting, distributed across every API instance.

Backends
  * Redis (when REDIS_URL is set): fixed-window counters (INCR + EXPIRE), so a limit of
    N/min means N/min for the *whole fleet*, not N per worker process.
  * In-process sliding window: used when Redis is not configured (dev, single instance)
    and as the fallback when Redis errors. In fallback mode the limit applies per process
    only, which is looser than intended but keeps the site up instead of failing closed.
    (Failing closed on a limiter outage would turn a Redis blip into a full outage.)

Buckets (each is its own namespace, so uploads cannot starve status polling and vice versa):
  api (general)  - anonymous by IP, signed-in by uid, Pro by uid with a higher limit
  upload, process, status, payment, admin
  login          - admin login attempts, stricter (see check_login_rate_limit)

Principal = signed-in uid when there is one, otherwise the proxy-corrected client IP.
The Paystack webhook is never throttled (authenticated by signature, must not be dropped).
"""

from __future__ import annotations

import time
import threading
from collections import defaultdict, deque

from flask import request, jsonify, session

from config import Config
from utils import observability, redis_client

EXEMPT_PATHS = {"/api/health", "/api/metrics"}


def _is_api_path(path: str) -> bool:
    return path.startswith("/api/") and path not in EXEMPT_PATHS


# ------------------------------------------------------------ in-memory backend

_lock = threading.Lock()
_hits: dict[tuple[str, str], deque] = defaultdict(deque)
_WINDOW_SECONDS = 60
_last_gc = 0.0
_MAX_KEYS = 50_000


def _gc(now: float) -> None:
    """Drop idle keys so the table cannot grow without bound (the old limiter leaked one
    deque per distinct IP forever)."""
    global _last_gc
    if now - _last_gc < 30 and len(_hits) < _MAX_KEYS:
        return
    _last_gc = now
    for k in [k for k, w in _hits.items() if not w or now - w[-1] > _WINDOW_SECONDS]:
        _hits.pop(k, None)
    if len(_hits) > _MAX_KEYS:  # under attack: shed the oldest half rather than grow
        for k in sorted(_hits, key=lambda k: _hits[k][-1] if _hits[k] else 0)[: len(_hits) // 2]:
            _hits.pop(k, None)


def _check_memory(namespace: str, key: str, limit: int) -> tuple[bool, int]:
    now = time.time()
    bucket = (namespace, key)
    with _lock:
        _gc(now)
        window = _hits[bucket]
        while window and now - window[0] > _WINDOW_SECONDS:
            window.popleft()
        if len(window) >= limit:
            return False, max(1, int(_WINDOW_SECONDS - (now - window[0])))
        window.append(now)
        return True, 0


# ---------------------------------------------------------------- redis backend

def _check_redis(r, namespace: str, key: str, limit: int) -> tuple[bool, int]:
    window = int(time.time()) // _WINDOW_SECONDS
    rkey = f"omx:rl:{namespace}:{key}:{window}"
    pipe = r.pipeline()
    pipe.incr(rkey)
    pipe.expire(rkey, _WINDOW_SECONDS + 5)
    count = int(pipe.execute()[0])
    if count > limit:
        return False, max(1, _WINDOW_SECONDS - int(time.time()) % _WINDOW_SECONDS)
    return True, 0


def _check_window(namespace: str, key: str, limit: int) -> tuple[bool, int]:
    """Returns (allowed, retry_after_seconds). limit 0 means disabled."""
    if not limit:
        return True, 0
    r = redis_client.get_redis()
    if r is not None:
        try:
            return _check_redis(r, namespace, key, limit)
        except Exception as exc:
            redis_client.note_failure("rate_limit", exc)
            observability.inc("omixa_ratelimit_fallback_total")
    return _check_memory(namespace, key, limit)


# ------------------------------------------------------------------ principals

def _client_key() -> str:
    # request.remote_addr is already the proxy-corrected address when TRUSTED_PROXY_HOPS > 0
    # (werkzeug ProxyFix, wired in app.py). The raw X-Forwarded-For header is never read.
    return request.remote_addr or "unknown"


def _principal() -> tuple[str, bool]:
    """(key, signed_in)."""
    uid = session.get("uid")
    if uid:
        return f"u:{uid}", True
    return f"ip:{_client_key()}", False


def _cached_is_pro(principal_key: str) -> bool:
    """Pro tier for rate limiting only (never for authorisation). Reads a short-lived cache
    that the routes populate whenever they compute entitlements server-side."""
    r = redis_client.get_redis()
    if r is None:
        return False
    try:
        return r.get(f"omx:ent:{principal_key}") == "1"
    except Exception as exc:
        redis_client.note_failure("entitlement_cache", exc)
        return False


def remember_entitlement(uid: str, is_pro: bool) -> None:
    r = redis_client.get_redis()
    if r is None or not uid:
        return
    try:
        r.set(f"omx:ent:u:{uid}", "1" if is_pro else "0", ex=Config.ENTITLEMENT_CACHE_SECONDS)
    except Exception as exc:
        redis_client.note_failure("entitlement_cache", exc)


def _bucket_for(path: str, method: str, principal_key: str, signed_in: bool) -> tuple[str, int]:
    if path.startswith("/api/upload") and method == "POST":
        return "upload", Config.RATE_LIMIT_UPLOAD_PER_MINUTE
    if path.startswith("/api/process") and method == "POST":
        return "process", Config.RATE_LIMIT_PROCESS_PER_MINUTE
    if path.startswith("/api/jobs"):
        return "status", Config.RATE_LIMIT_STATUS_PER_MINUTE
    if path.startswith("/api/billing"):
        return "payment", Config.RATE_LIMIT_PAYMENT_PER_MINUTE
    if not signed_in:
        return "api", Config.RATE_LIMIT_PER_MINUTE
    if _cached_is_pro(principal_key):
        return "api_pro", Config.RATE_LIMIT_PRO_PER_MINUTE
    return "api_user", Config.RATE_LIMIT_USER_PER_MINUTE


def _too_many(message: str, retry_after: int):
    observability.inc("omixa_ratelimited_total")
    response = jsonify({"error": message})
    response.headers["Retry-After"] = str(retry_after)
    return response, 429


def check_rate_limit():
    """Returns a Flask response to short-circuit the request with, or None to let it through."""
    if request.method == "OPTIONS":
        return None
    if not _is_api_path(request.path):
        return None
    if request.path == "/api/billing/webhook":  # Paystack: authenticated by signature, never throttled
        return None

    key, signed_in = _principal()
    namespace, limit = _bucket_for(request.path, request.method, key, signed_in)
    allowed, retry_after = _check_window(namespace, key, limit)
    if allowed:
        return None
    return _too_many("Too many requests. Please slow down and try again shortly.", retry_after)


def check_admin_rate_limit():
    """Authenticated admin API traffic (separate from login attempts)."""
    allowed, retry_after = _check_window("admin", _client_key(), Config.RATE_LIMIT_ADMIN_PER_MINUTE)
    if allowed:
        return None
    return _too_many("Too many requests. Please slow down.", retry_after)


def check_login_rate_limit():
    """Separate, stricter bucket for admin login attempts, so credential guessing is
    throttled independent of general API traffic."""
    allowed, retry_after = _check_window("login", _client_key(), Config.LOGIN_RATE_LIMIT_PER_MINUTE)
    if allowed:
        return None
    return _too_many("Too many login attempts. Please wait before trying again.", retry_after)
