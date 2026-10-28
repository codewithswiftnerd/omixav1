"""
One public hostname.

The same app answers on getomixa.com and on Railway-generated hostnames. Search engines index every hostname that
returns a page, which is how the Railway URLs showed up as duplicate results. This module:

  * redirects GET/HEAD requests on the allowlisted Railway hostnames to the same path and query on
    OMIXA_PUBLIC_BASE_URL with a permanent 301;
  * marks any response still served from one of those allowlisted hostnames `X-Robots-Tag: noindex`.

Safety rules:
  * The redirect target ORIGIN always comes from configuration, never from the request, so a forged Host header
    cannot send anyone to another site. Only the path and query are copied, and the path is re-quoted.
  * Only hostnames in the allowlist redirect. The canonical host, Railway's health-check host, localhost and any
    unknown host are untouched, so there is no redirect loop and no way to redirect the official domain.
  * POST/PUT/PATCH/DELETE are NOT redirected: a 3xx can break webhooks and API clients that do not follow it
    (for example a payment webhook configured with a Railway URL). They keep working until the caller is moved.
  * /healthz, /readyz and /health stay answerable on every host so platform health checks never see a redirect.
"""
from __future__ import annotations

from urllib.parse import quote, urlsplit

from flask import redirect, request

from config import Config

# Tail of the path that must always answer directly, whatever the host.
EXEMPT_PATHS = ("/healthz", "/readyz", "/health")


def _host(value: str) -> str:
    """Lower-case hostname without port. Handles `host:port`; an IPv6 literal is returned unchanged."""
    value = (value or "").strip().lower()
    if value.startswith("["):
        return value
    return value.split(":", 1)[0].rstrip(".")


def canonical_host() -> str:
    return _host(urlsplit(Config.PUBLIC_BASE_URL).netloc) if Config.PUBLIC_BASE_URL else ""


def is_enabled() -> bool:
    return bool(Config.IS_PRODUCTION and Config.PUBLIC_BASE_URL and canonical_host())


def redirect_target() -> str | None:
    """The permanent URL for this request, or None when no redirect applies."""
    if not is_enabled() or request.method not in ("GET", "HEAD"):
        return None
    host = _host(request.host)
    if host == canonical_host() or host not in Config.REDIRECT_HOSTS:
        return None
    if request.path in EXEMPT_PATHS:
        return None
    path = quote(request.path, safe="/%:@!$&'()*+,;=-._~")
    if not path.startswith("/"):
        path = "/" + path
    query = request.query_string.decode("latin-1")
    return f"{Config.PUBLIC_BASE_URL}{path}" + (f"?{query}" if query else "")


def init_app(app) -> None:
    @app.before_request
    def _canonical_host_redirect():   # registered first: runs before sessions, rate limits and CSRF checks
        target = redirect_target()
        return redirect(target, code=301) if target else None

    @app.after_request
    def _noindex_off_canonical(response):
        # Only the allowlisted duplicate hostnames. Never "every host that is not the canonical one": a typo in
        # OMIXA_PUBLIC_BASE_URL must not be able to noindex the real site.
        if is_enabled() and _host(request.host) in Config.REDIRECT_HOSTS and _host(request.host) != canonical_host():
            response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    if Config.IS_PRODUCTION and not Config.PUBLIC_BASE_URL:
        app.logger.warning("OMIXA_PUBLIC_BASE_URL is not set: canonical tags and the sitemap follow whatever host "
                           "the request used, and Railway hostnames are NOT redirected. Set it to https://getomixa.com.")
