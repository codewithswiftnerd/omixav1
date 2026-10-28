"""
Temporary pre-launch access gate.

Purpose: keep the whole site private until launch. When OMIXA_ACCESS_GATE_ENABLED=true, every request must carry a
valid gate cookie, or it is refused before any route, session, rate limiter or database code runs. Nothing here
replaces Omixa's own sign-in: once past the gate, accounts, admin login, Pro entitlements and job ownership behave
exactly as before. The gate is one small module + one template + one stylesheet and can be deleted on launch day
(see docs/ACCESS_GATE_AND_FX.md).

How it works
  * The team password and the cookie-signing secret come ONLY from the environment (OMIXA_ACCESS_GATE_PASSWORD,
    OMIXA_ACCESS_GATE_SECRET). Neither is ever rendered, logged, put in a URL or sent to the browser.
  * A team member submits the password by POST to /__access. It is compared in constant time. On success the server
    sets a signed, expiring, HttpOnly cookie (SameSite=Lax, Secure in production). The cookie carries no user data,
    only "issued at", and is signed with a key derived from BOTH the secret and the password, so changing either one
    instantly signs everybody out.
  * Failed attempts are rate limited per client through the app's existing limiter (Redis-backed when REDIS_URL is set).
  * Everything else is refused with 401 JSON (API paths) or a 403 branded page. That includes unknown URLs, static
    files and the admin area, so nothing can be enumerated or fetched without the cookie.

Always reachable (each has its own protection or exposes nothing private)
  * /healthz, /readyz, /api/health    Railway / orchestrator health checks.
  * POST /api/billing/webhook          Paystack cannot hold a cookie; the route verifies Paystack's HMAC signature.
  * /api/metrics                       needs OMIXA_METRICS_TOKEN (Bearer) and answers 404 otherwise.
  * /robots.txt, /favicon.ico, Google's verification file, and the handful of static files the gate page itself uses.
  * OPTIONS preflights on /api/ (they carry no data and no cookie; the real request is gated).
  The worker (worker.py) never calls the web service over HTTP: it reads jobs from PostgreSQL / Redis / S3 directly,
  so it is unaffected by the gate.

Fail closed
  Enabled but missing/too-short password or secret -> the site stays private for everyone (503 notice, no form) and a
  warning naming the missing VARIABLE (never a value) is logged. The gate switches itself off only when
  OMIXA_ACCESS_GATE_ENABLED is false or the optional OMIXA_ACCESS_GATE_OPEN_AT moment has passed.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from flask import g, jsonify, make_response, redirect, render_template, request, session
from itsdangerous import BadData, URLSafeSerializer

from config import Config

logger = logging.getLogger("omixa.access_gate")

UNLOCK_PATH = "/__access"
LOGOUT_PATH = "/__access/logout"
COOKIE_NAME = "omixa_gate"
MIN_PASSWORD_LENGTH = 12
MIN_SECRET_LENGTH = 32
MAX_FORM_BYTES = 4096

# Paths that must always answer (see module docstring). Kept deliberately short and exact.
EXEMPT_PATHS = frozenset({
    "/healthz", "/readyz", "/api/health", "/api/metrics",
    "/robots.txt", "/favicon.ico",
    "/googlef87d1a0bfb7e854c.html",   # Search Console ownership file (public by design; test pins it to routes/seo.py)
})
# Static files the gate page itself needs. Everything else under /static/ stays behind the gate.
GATE_STATIC_PATHS = frozenset({
    "/static/css/app.css", "/static/css/gate.css",
    "/static/brand/omixa-icon.png", "/static/favicon.ico",
    "/static/favicon-16x16.png", "/static/favicon-32x32.png",
    "/static/android-chrome-192x192.png", "/static/apple-touch-icon.png",
})
WEBHOOK = ("POST", "/api/billing/webhook")

STATE_OFF, STATE_OPEN, STATE_ACTIVE, STATE_MISCONFIGURED = "off", "open", "active", "misconfigured"


# ----------------------------------------------------------------------------------------------- configuration

def _open_at_timestamp() -> float | None:
    """The auto-open moment as a unix timestamp, or None when unset or unparseable. An unparseable value is
    ignored (the gate then stays ON: private is the safe failure)."""
    raw = (Config.ACCESS_GATE_OPEN_AT or "").strip()
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:   # no offset given: read it as UTC rather than guess a timezone
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def config_problems() -> list[str]:
    """Names of the environment variables that are missing or too weak. Never includes a value."""
    problems = []
    if len(Config.ACCESS_GATE_PASSWORD) < MIN_PASSWORD_LENGTH:
        problems.append(f"OMIXA_ACCESS_GATE_PASSWORD (set, at least {MIN_PASSWORD_LENGTH} characters)")
    if len(Config.ACCESS_GATE_SECRET) < MIN_SECRET_LENGTH:
        problems.append(f"OMIXA_ACCESS_GATE_SECRET (set, at least {MIN_SECRET_LENGTH} characters)")
    return problems


def state(now: float | None = None) -> str:
    """off: disabled. open: enabled but the launch moment has passed. active: gate on. misconfigured: gate on but
    unusable, so everybody is kept out (fail closed)."""
    if not Config.ACCESS_GATE_ENABLED:
        return STATE_OFF
    open_at = _open_at_timestamp()
    if open_at is not None and (time.time() if now is None else now) >= open_at:
        return STATE_OPEN
    return STATE_MISCONFIGURED if config_problems() else STATE_ACTIVE


def scope() -> str:
    """"app" (public marketing pages stay open, SEO untouched) or "site" (everything locked). Anything else = "site"."""
    return "app" if (Config.ACCESS_GATE_SCOPE or "").strip().lower() == "app" else "site"


def is_public_page(method: str, path: str) -> bool:
    """In scope "app": exactly the pages routes/seo.py lets search engines index, plus the sitemap and static files they
    need to render. Using the SEO registry itself means the gate cannot disagree with the SEO setup."""
    if scope() != "app" or method not in ("GET", "HEAD"):
        return False
    if path in ("/sitemap.xml",) or path.startswith("/static/"):
        return True
    from routes import seo
    return path in seo.PUBLIC_PAGES


def hides_site() -> bool:
    """True only when the whole site (not just the app) is private: this is what switches robots.txt/sitemap/noindex."""
    return is_blocking() and scope() == "site"


def is_blocking() -> bool:
    """True while the site must be treated as private (gate on, whether usable or not)."""
    return state() in (STATE_ACTIVE, STATE_MISCONFIGURED)


# ------------------------------------------------------------------------------------------------- the cookie

def _serializer() -> URLSafeSerializer:
    """Signing key derived from the secret AND the password: rotating either invalidates every cookie."""
    password_digest = hashlib.sha256(Config.ACCESS_GATE_PASSWORD.encode("utf-8")).digest()
    key = hmac.new(Config.ACCESS_GATE_SECRET.encode("utf-8"), b"omixa-access-gate-v1|" + password_digest,
                   hashlib.sha256).digest()
    return URLSafeSerializer(key, salt="omixa.access-gate.cookie", signer_kwargs={"digest_method": hashlib.sha256})


def _ttl_seconds() -> int:
    return Config.ACCESS_GATE_SESSION_HOURS * 3600


def issue_token(now: float | None = None) -> str:
    return _serializer().dumps({"v": 1, "iat": int(time.time() if now is None else now)})


def token_is_valid(token: str | None, now: float | None = None) -> bool:
    if not token or len(token) > 512 or config_problems():
        return False
    try:
        payload = _serializer().loads(token)
    except BadData:
        return False
    if not isinstance(payload, dict) or payload.get("v") != 1 or not isinstance(payload.get("iat"), int):
        return False
    age = (time.time() if now is None else now) - payload["iat"]
    return -60 <= age <= _ttl_seconds()   # small clock-skew allowance; expired or far-future tokens are refused


def _cookie_secure() -> bool:
    return bool(Config.IS_PRODUCTION or Config.SESSION_COOKIE_SECURE)


def _set_cookie(response) -> None:
    response.set_cookie(COOKIE_NAME, issue_token(), max_age=_ttl_seconds(), httponly=True,
                        secure=_cookie_secure(), samesite="Lax", path="/")


def _clear_cookie(response) -> None:
    response.set_cookie(COOKIE_NAME, "", max_age=0, expires=0, httponly=True,
                        secure=_cookie_secure(), samesite="Lax", path="/")


def has_access() -> bool:
    if token_is_valid(request.cookies.get(COOKIE_NAME)):
        return True
    # An administrator who already holds a valid admin session (signed by OMIXA_SECRET_KEY, only ever issued after the
    # real admin credentials were checked) is not locked out mid-session when the gate is switched on.
    return bool(session.get("is_admin"))


# ------------------------------------------------------------------------------------------------- the checks

def is_exempt(method: str, path: str) -> bool:
    if path in EXEMPT_PATHS or path in GATE_STATIC_PATHS or is_public_page(method, path):
        return True
    if (method, path) == WEBHOOK:
        return True
    return method == "OPTIONS" and path.startswith("/api/")


def _wants_json() -> bool:
    if request.path.startswith(("/api/", "/admin/api/")):
        return True
    return request.accept_mimetypes.best == "application/json"


def _safe_next(raw: str | None) -> str:
    """Only a same-site path is ever redirected to (no scheme, no host, no protocol-relative or backslash tricks)."""
    if not isinstance(raw, str) or not raw.startswith("/") or raw.startswith("//") or "\\" in raw or len(raw) > 512:
        return "/"
    if any(ord(c) < 32 or ord(c) == 127 for c in raw):
        return "/"
    parts = urlsplit(raw)
    if parts.scheme or parts.netloc or parts.path.startswith(UNLOCK_PATH):
        return "/"
    return raw


def _page(status: int, *, error: str | None = None, next_path: str = "/", retry_after: int | None = None):
    misconfigured = state() == STATE_MISCONFIGURED
    html = render_template("access_gate.html", error=error, next_path=next_path, unlock_path=UNLOCK_PATH,
                           show_form=not misconfigured, misconfigured=misconfigured)
    resp = make_response(html, 503 if misconfigured else status)
    resp.headers["Cache-Control"] = "no-store"
    if retry_after:
        resp.headers["Retry-After"] = str(retry_after)
    return resp


def _deny():
    if request.path.startswith("/static/"):
        resp = make_response("Forbidden", 403)
        resp.mimetype = "text/plain"
    elif _wants_json():
        resp = jsonify({"error": "Omixa is private until launch.", "code": "access_gate"})
        resp.status_code = 503 if state() == STATE_MISCONFIGURED else 401
    else:
        return _page(403, next_path=_safe_next(request.full_path.rstrip("?")) if request.method in ("GET", "HEAD") else "/")
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _same_origin() -> bool:
    origin = request.headers.get("Origin")
    return not origin or urlsplit(origin).netloc.lower() == request.host.lower()


def _password_matches(supplied: str) -> bool:
    expected = Config.ACCESS_GATE_PASSWORD
    return hmac.compare_digest(hashlib.sha256(supplied.encode("utf-8")).digest(),
                               hashlib.sha256(expected.encode("utf-8")).digest())


def _rate_limited() -> int | None:
    """Seconds to wait if this client is over the attempt limits, else None."""
    from utils import security   # late import: utils.security pulls in redis_client/observability
    key = security.client_key()
    for namespace, limit, seconds in (("gate_min", Config.ACCESS_GATE_ATTEMPTS_PER_MINUTE, 60),
                                      ("gate_hour", Config.ACCESS_GATE_ATTEMPTS_PER_HOUR, 3600)):
        allowed, retry_after = security.check_limit(namespace, key, limit, seconds)
        if not allowed:
            return retry_after
    return None


def _unlock():
    if request.method not in ("GET", "HEAD", "POST"):
        return make_response("Method Not Allowed", 405)
    if state() == STATE_MISCONFIGURED:
        return _page(503)
    if request.method != "POST":
        return _page(200, next_path=_safe_next(request.args.get("next")))
    if not _same_origin():
        return make_response("Forbidden", 403)
    if (request.content_length or 0) > MAX_FORM_BYTES:
        return make_response("Request too large", 413)
    next_path = _safe_next(request.form.get("next"))
    retry_after = _rate_limited()
    if retry_after:
        return _page(429, error="Too many attempts. Please wait a moment and try again.",
                     next_path=next_path, retry_after=retry_after)
    supplied = (request.form.get("password") or "")[:256]
    if not supplied or not _password_matches(supplied):
        logger.warning("access gate: wrong password from %s", request.remote_addr or "unknown")   # never the attempt itself
        return _page(401, error="That password isn't right. Please try again.", next_path=next_path)
    response = redirect(next_path, code=303)
    response.headers["Cache-Control"] = "no-store"
    _set_cookie(response)
    return response


def _logout():
    if request.method != "POST":
        return make_response("Method Not Allowed", 405)
    response = redirect(UNLOCK_PATH, code=303)
    _clear_cookie(response)
    return response


def check_request():
    """before_request hook. Returns a response to stop the request, or None to let it continue."""
    if not is_blocking():
        return None
    path = request.path
    if is_exempt(request.method, path):
        return None
    if path == UNLOCK_PATH:
        response = _unlock()
    elif path == LOGOUT_PATH:
        response = _logout()
    elif state() != STATE_MISCONFIGURED and has_access():
        return None
    else:
        response = _deny()
    g.gate_denied = True
    # This hook runs before app.py's CSP-nonce hook; give the response headers a real nonce rather than an empty one.
    if not getattr(g, "csp_nonce", None):
        g.csp_nonce = secrets.token_urlsafe(16)
    return response


def add_private_headers(response):
    """after_request hook: while the gate is on, nothing - including static files and the 403 page - may be indexed."""
    # scope "site": every response. scope "app": only the ones the gate itself refused, so nothing the SEO setup
    # serves (pages, robots.txt, sitemap, favicon, static files) gets a different header than it had before.
    if is_blocking() and (scope() == "site" or getattr(g, "gate_denied", False)):
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet, noimageindex"
    return response


def init_app(app) -> None:
    app.before_request(check_request)
    app.after_request(add_private_headers)

    current = state()
    if current == STATE_MISCONFIGURED:
        logger.error("ACCESS GATE IS ON BUT NOT USABLE: the whole site is locked for everyone until these Railway "
                     "variables are fixed: %s", "; ".join(config_problems()))
    elif current == STATE_ACTIVE:
        logger.warning("Access gate is ACTIVE (scope=%s): %s. Cookie lifetime %sh. %s", scope(),
                       "public pages stay open, the app and APIs are private" if scope() == "app" else "the whole site is private",
                       Config.ACCESS_GATE_SESSION_HOURS,
                       "Opens automatically at OMIXA_ACCESS_GATE_OPEN_AT." if _open_at_timestamp() is not None
                       else "It stays on until OMIXA_ACCESS_GATE_ENABLED is set to false.")
    elif current == STATE_OPEN:
        logger.info("Access gate is enabled but its launch time has passed: the site is open.")
    if Config.ACCESS_GATE_ENABLED and (Config.ACCESS_GATE_OPEN_AT or "").strip() and _open_at_timestamp() is None:
        logger.warning("OMIXA_ACCESS_GATE_OPEN_AT could not be read as an ISO 8601 date-time (for example "
                       "2026-11-13T00:00:00+01:00), so it is ignored and the gate stays on.")
