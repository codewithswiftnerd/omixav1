import hashlib
import os
import secrets
import warnings

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_IS_PRODUCTION = os.environ.get("FLASK_ENV", "production").strip().lower() == "production"


def _env_flag(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


# SHA-256 fingerprints (never the values) of credentials that were committed to
# the repository in the past. They must be treated as public: the app refuses to
# boot in production with any of them, so a forgotten Railway variable cannot
# silently keep a compromised secret alive.
_COMPROMISED_SECRET_SHA256 = {
    "60b8f3b77b815291ce00c38b687a632c080d7867b34a446dd21fecbcb936b5f6",
}
_MIN_PRODUCTION_SECRET_LENGTH = 32


def _secret_key() -> str:
    """Signs the session cookie and CSRF tokens. Must be stable across
    restarts and shared by every gunicorn worker. Required in
    production; a random key is generated for local dev only."""
    key = os.environ.get("OMIXA_SECRET_KEY")
    if key:
        if _IS_PRODUCTION:
            if hashlib.sha256(key.encode("utf-8")).hexdigest() in _COMPROMISED_SECRET_SHA256:
                raise RuntimeError(
                    "OMIXA_SECRET_KEY is a value that was previously committed to the "
                    "repository and must be treated as compromised. Generate a new one "
                    "with `python scripts/generate_secrets.py` and update your host's variables."
                )
            if len(key) < _MIN_PRODUCTION_SECRET_LENGTH:
                raise RuntimeError(
                    f"OMIXA_SECRET_KEY is too short for production "
                    f"(need at least {_MIN_PRODUCTION_SECRET_LENGTH} characters)."
                )
        return key
    if _IS_PRODUCTION:
        raise RuntimeError(
            "OMIXA_SECRET_KEY is not set. Generate one with:\n"
            "  python -c \"import secrets; print(secrets.token_urlsafe(48))\"\n"
            "and set it in the environment before running in production "
            "(FLASK_ENV=production). Refusing to start with a random "
            "per-process key in production: sessions/CSRF tokens would "
            "silently stop validating on every restart and disagree "
            "between gunicorn workers."
        )
    warnings.warn(
        "OMIXA_SECRET_KEY is not set — using a random key for this "
        "process only. Fine for local dev; set OMIXA_SECRET_KEY before "
        "deploying anywhere public.",
        RuntimeWarning,
    )
    return secrets.token_urlsafe(48)


class Config:
    IS_PRODUCTION = _IS_PRODUCTION

    # Where uploaded/cleaned files live temporarily, see utils/file_handler.py
    TEMP_DIR = os.environ.get("OMIXA_TEMP_DIR", os.path.join(BASE_DIR, "temp"))

    # SQLite database for operational metadata only (job records, admin
    # login attempts, error log) — never the uploaded/cleaned dataset
    # contents themselves. See db.py.
    DB_PATH = os.environ.get("OMIXA_DB_PATH", os.path.join(BASE_DIR, "data", "omixa.db"))

    # 25 MB, flat for every user, no accounts/plans to vary it by.
    MAX_CONTENT_LENGTH = int(os.environ.get("OMIXA_MAX_UPLOAD_MB", "25")) * 1024 * 1024

    ALLOWED_EXTENSIONS = {"csv", "xlsx", "xls"}

    # How long a job's temp files live before cleanup sweeps them.
    JOB_TTL_SECONDS = 60 * 30  # 30 minutes

    # Comma-separated origins allowed to call the API cross-origin.
    # "*" (default) fine for local dev / same-origin deploys; session
    # cookies need an explicit list, see README's "Cross-origin deploys".
    ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*")

    @classmethod
    def allowed_origins_list(cls):
        """None means "*" (any origin, no credentials). Otherwise the
        explicit allow-list, cookies are only ever sent back for an
        origin on this list."""
        raw = cls.ALLOWED_ORIGINS.strip()
        if raw == "*" or not raw:
            return None
        return [o.strip() for o in raw.split(",") if o.strip()]

    # Requests per IP per rolling 60s window on /api/*. In-memory,
    # per-worker-process (see README). 0 disables.
    RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "30"))

    # Separate, stricter limit for admin login attempts.
    LOGIN_RATE_LIMIT_PER_MINUTE = int(os.environ.get("LOGIN_RATE_LIMIT_PER_MINUTE", "8"))

    # --- Sessions ---------------------------------------------------
    SECRET_KEY = _secret_key()
    SESSION_COOKIE_NAME = "omixa_session"
    SESSION_COOKIE_HTTPONLY = True
    # A split frontend needs SameSite=None + HTTPS, see README.
    SESSION_COOKIE_SAMESITE = os.environ.get("OMIXA_SESSION_SAMESITE", "Lax")
    SESSION_COOKIE_SECURE = _env_flag("OMIXA_SESSION_SECURE", _IS_PRODUCTION)
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 12  # 12 hours

    # --- Admin --------------------------------------------------------
    # Single operator account. With either unset, /admin fails closed
    # rather than falling open.
    #   python -c "from werkzeug.security import generate_password_hash as g; print(g('your-password'))"
    ADMIN_USERNAME = os.environ.get("OMIXA_ADMIN_USERNAME")
    ADMIN_PASSWORD_HASH = os.environ.get("OMIXA_ADMIN_PASSWORD_HASH")

    # Number of reverse proxies in front of the app whose X-Forwarded-For entry
    # may be trusted (Railway/Render/Heroku add exactly one). 0 = trust nothing
    # and use the socket address. Client-supplied X-Forwarded-For values are
    # never used directly: they are trivially spoofable and would let an
    # attacker dodge the login rate limit by sending a fresh fake IP per try.
    TRUSTED_PROXY_HOPS = int(os.environ.get("OMIXA_TRUSTED_PROXY_HOPS", "1" if _IS_PRODUCTION else "0"))

    # Hard ceilings so a small-on-disk upload cannot balloon in memory
    # (zip bombs, million-column CSVs).
    MAX_ROWS = int(os.environ.get("OMIXA_MAX_ROWS", "500000"))
    MAX_COLUMNS = int(os.environ.get("OMIXA_MAX_COLUMNS", "500"))
    MAX_XLSX_UNCOMPRESSED_MB = int(os.environ.get("OMIXA_MAX_XLSX_UNCOMPRESSED_MB", "200"))

    # Force HTTPS cookies/HSTS outside FLASK_ENV=production too (e.g.
    # behind a TLS-terminating proxy).
    FORCE_HTTPS = _env_flag("OMIXA_FORCE_HTTPS", _IS_PRODUCTION)
