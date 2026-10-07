"""
Request-level rate limiting: general /api/* traffic
(config.Config.RATE_LIMIT_PER_MINUTE) and, separately, admin login
attempts (LOGIN_RATE_LIMIT_PER_MINUTE). Wired in from app.py's
before_request.

Auth itself (sessions, admin login, CSRF) lives in utils/session.py —
this file is rate limiting only.
"""

from __future__ import annotations

import time
import threading
from collections import defaultdict, deque

from flask import request, jsonify

from config import Config

# EXEMPT_PATHS are never rate limited: a health check needs to work
# even under load, so uptime monitors and load balancers aren't
# affected by traffic on other endpoints.
EXEMPT_PATHS = {"/api/health"}


def _is_api_path(path: str) -> bool:
    return path.startswith("/api/") and path not in EXEMPT_PATHS


# In-memory sliding window per (namespace, client key). Namespacing
# keeps the general API limit and the admin-login limit in separate
# buckets per IP.

_lock = threading.Lock()
_hits: dict[tuple[str, str], deque] = defaultdict(deque)
_WINDOW_SECONDS = 60


def _check_window(namespace: str, key: str, limit: int) -> tuple[bool, int]:
    """Returns (allowed, retry_after_seconds)."""
    if not limit:
        return True, 0

    now = time.time()
    bucket = (namespace, key)

    with _lock:
        window = _hits[bucket]
        while window and now - window[0] > _WINDOW_SECONDS:
            window.popleft()

        if len(window) >= limit:
            retry_after = max(1, int(_WINDOW_SECONDS - (now - window[0])))
            return False, retry_after

        window.append(now)
        return True, 0


def _client_key() -> str:
    # request.remote_addr is already the proxy-corrected address when
    # Config.TRUSTED_PROXY_HOPS > 0 (werkzeug ProxyFix, wired in app.py). The
    # raw X-Forwarded-For header is client-controlled and is never read here.
    return request.remote_addr or "unknown"


def check_rate_limit():
    """Returns a Flask response to short-circuit the request with, or
    None to let it through. General /api/* limit."""
    if request.method == "OPTIONS":
        return None

    if not _is_api_path(request.path):
        return None
    if request.path == "/api/billing/webhook":  # Paystack: authenticated by signature, must never be throttled
        return None

    allowed, retry_after = _check_window("api", _client_key(), Config.RATE_LIMIT_PER_MINUTE)
    if allowed:
        return None

    response = jsonify({"error": "Too many requests. Please slow down and try again shortly."})
    response.headers["Retry-After"] = str(retry_after)
    return response, 429


def check_login_rate_limit():
    """Same shape as check_rate_limit but a separate, stricter bucket
    for admin login attempts specifically — call this from
    routes/admin.py's login view before checking credentials, so
    credential guessing is throttled independent of general API
    traffic."""
    allowed, retry_after = _check_window("login", _client_key(), Config.LOGIN_RATE_LIMIT_PER_MINUTE)
    if allowed:
        return None

    response = jsonify({"error": "Too many login attempts. Please wait before trying again."})
    response.headers["Retry-After"] = str(retry_after)
    return response, 429
