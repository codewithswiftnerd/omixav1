"""
Omixa backend entry point.

No accounts. Each job is a temp folder scoped to the session that
created it (utils/session.py, utils/file_handler.py), deleted after
download or after 30 min idle. Job API: routes/upload.py, process.py,
download.py, report.py. Admin dashboard: routes/admin.py.
"""

import logging
import os
import secrets
import time

from flask import Flask, g, jsonify, render_template, request, send_from_directory

from config import Config
import db as metadata_db

from routes.upload import upload_bp
from routes.process import process_bp
from routes.download import download_bp
from routes.report import report_bp
from routes.auth_routes import auth_bp
from routes.billing import billing_bp
from routes.profiles import profiles_bp
from routes.sessions import sessions_bp
from routes.pages import pages_bp
from routes.workspace import workspace_bp
from routes.admin import admin_bp
from utils.security import check_rate_limit
from utils.session import ensure_session


def _configure_logging():
    level = logging.DEBUG if not Config.IS_PRODUCTION else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def create_app():
    _configure_logging()
    logger = logging.getLogger("omixa.app")

    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.config.from_object(Config)

    if Config.TRUSTED_PROXY_HOPS > 0:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=Config.TRUSTED_PROXY_HOPS, x_proto=1, x_host=0)

    os.makedirs(Config.TEMP_DIR, exist_ok=True)
    metadata_db.init_db()

    if not Config.ADMIN_USERNAME or not Config.ADMIN_PASSWORD_HASH:
        logger.warning(
            "OMIXA_ADMIN_USERNAME / OMIXA_ADMIN_PASSWORD_HASH not set — "
            "the /admin dashboard is unreachable (login always fails "
            "closed) until both are configured."
        )

    # API blueprints (JSON, session-scoped)
    app.register_blueprint(upload_bp, url_prefix="/api/upload")
    app.register_blueprint(process_bp, url_prefix="/api/process")
    app.register_blueprint(download_bp, url_prefix="/api/download")
    app.register_blueprint(report_bp, url_prefix="/api/report")
    app.register_blueprint(auth_bp, url_prefix="/api/auth")
    app.register_blueprint(billing_bp, url_prefix="/api/billing")
    app.register_blueprint(profiles_bp, url_prefix="/api/profiles")
    app.register_blueprint(sessions_bp, url_prefix="/api/sessions")

    # Page blueprints (server-rendered HTML)
    app.register_blueprint(pages_bp)
    app.register_blueprint(workspace_bp)

    # Admin command center (its own session-gated auth, see routes/admin.py)
    app.register_blueprint(admin_bp, url_prefix="/admin")

    # Assign an anonymous session cookie before anything else runs.
    @app.before_request
    def _assign_session():
        if request.path.startswith("/static/"):
            return None
        ensure_session()
        return None

    @app.before_request
    def _security_gate():
        # /api/* rate limit, no-op unless configured. Admin login has
        # its own stricter limiter, see routes/admin.py.
        return check_rate_limit()

    # Manual CORS (no flask-cors): auth is cookies now, and a wildcard
    # origin can never get Access-Control-Allow-Credentials, so that
    # combination is refused rather than silently allowed. See
    # config.Config.allowed_origins_list().
    @app.before_request
    def _cors_preflight():
        if request.method == "OPTIONS" and request.path.startswith("/api/"):
            resp = app.make_default_options_response()
            return _apply_cors(resp)
        return None

    def _apply_cors(response):
        if not request.path.startswith("/api/"):
            return response
        origin = request.headers.get("Origin")
        if not origin:
            return response

        allowed = Config.allowed_origins_list()
        if allowed is None:
            response.headers["Access-Control-Allow-Origin"] = "*"
        elif origin in allowed:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers.add("Vary", "Origin")
        else:
            return response  # not an allowed origin, no CORS headers at all

        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-CSRF-Token"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Expose-Headers"] = "Content-Disposition, Retry-After"
        return response

    # Cross-site request forgery for the cookie-authenticated JSON API: a state-changing request
    # (POST) that carries an Origin header must come from this app itself or from an origin the
    # operator explicitly listed in ALLOWED_ORIGINS. Browsers always send Origin on cross-site
    # POSTs, so this holds even when the session cookie is SameSite=None for a split deploy.
    @app.before_request
    def _origin_check():
        if request.method not in ("POST", "PUT", "PATCH", "DELETE") or not request.path.startswith("/api/"):
            return None
        origin = request.headers.get("Origin")
        if not origin:
            return None  # non-browser clients (curl, server-to-server) send no Origin
        if origin.rstrip("/") == request.host_url.rstrip("/"):
            return None
        allowed = Config.allowed_origins_list()
        if allowed and origin in allowed:
            return None
        return jsonify({"error": "Cross-origin request not allowed"}), 403

    # Security headers on every response. CSP script-src uses a
    # per-request nonce instead of 'unsafe-inline'; style-src still
    # allows 'unsafe-inline' for a few templates' inline style="" (see
    # README's Security section).
    @app.before_request
    def _csp_nonce():
        g.csp_nonce = secrets.token_urlsafe(16)

    @app.context_processor
    def _inject_csp_nonce():
        return {"csp_nonce": getattr(g, "csp_nonce", "")}

    @app.after_request
    def _security_headers(response):
        response = _apply_cors(response)

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"

        nonce = getattr(g, "csp_nonce", "")
        # Firebase Auth's web SDK is only allowed on the sign-in page.
        on_login = request.path == "/login"
        fb_script = " https://www.gstatic.com" if on_login else ""
        fb_connect = (" https://identitytoolkit.googleapis.com https://securetoken.googleapis.com"
                      " https://www.googleapis.com https://*.firebaseapp.com") if on_login else ""
        fb_frame = "frame-src https://*.firebaseapp.com https://accounts.google.com; " if on_login else ""
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}'{fb_script}; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            f"connect-src 'self'{fb_connect}; "
            f"{fb_frame}"
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'"
        )

        if Config.FORCE_HTTPS:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        return response

    # Request logging: method, path, status, duration only — never
    # headers or bodies (could carry session tokens or file contents).
    @app.before_request
    def _start_timer():
        g._start_time = time.monotonic()

    @app.after_request
    def _log_request(response):
        started = getattr(g, "_start_time", None)
        duration_ms = round((time.monotonic() - started) * 1000, 1) if started else None
        # request.path is attacker-controlled and URL-decoded: escape control characters so a
        # request for "/x%0AINFO fake log line" cannot forge log entries.
        safe_path = request.path.encode("unicode_escape").decode("ascii")[:300]
        logger.info(
            "%s %s -> %s (%sms)",
            request.method, safe_path, response.status_code, duration_ms,
        )
        return response

    def _wants_json() -> bool:
        return request.path.startswith("/api/")

    @app.errorhandler(413)
    def too_large(e):
        mb = Config.MAX_CONTENT_LENGTH // (1024 * 1024)
        message = f"File too large, the limit is {mb}MB."
        if _wants_json():
            return jsonify({"error": message}), 413
        return render_template("errors/error.html", code=413, message=message), 413

    @app.errorhandler(404)
    def not_found(e):
        if _wants_json():
            return jsonify({"error": "Not found"}), 404
        return render_template("errors/404.html"), 404

    @app.errorhandler(429)
    def too_many(e):
        message = "Too many requests. Please slow down and try again shortly."
        if _wants_json():
            return jsonify({"error": message}), 429
        return render_template("errors/error.html", code=429, message=message), 429

    @app.errorhandler(500)
    def server_error(e):
        logging.exception("Unhandled server error")
        if _wants_json():
            return jsonify({"error": "Something went wrong on our end. Please try again."}), 500
        return render_template(
            "errors/error.html", code=500, message="Something went wrong on our end. Please try again."
        ), 500

    @app.get("/api/health")
    def health():
        checks = {"temp_dir_writable": False, "db_reachable": False}
        try:
            probe = os.path.join(Config.TEMP_DIR, ".healthcheck")
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
            checks["temp_dir_writable"] = True
        except OSError:
            pass
        try:
            metadata_db.stats_summary(window_seconds=1)
            checks["db_reachable"] = True
        except Exception:
            pass

        healthy = all(checks.values())
        body = {"status": "ok" if healthy else "degraded", "service": "omixa-backend", "checks": checks}
        return jsonify(body), (200 if healthy else 503)

    @app.get("/favicon.ico")
    def favicon():
        return send_from_directory(app.static_folder, "favicon.ico")

    return app


app = create_app()

if __name__ == "__main__":
    # Debug mode enables Werkzeug's debugger (RCE risk) — dev only.
    debug = os.environ.get("FLASK_DEBUG") == "1" and not Config.IS_PRODUCTION
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=debug, port=port)
