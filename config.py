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


def _env_int(name: str, default: int) -> int:
    """Integer env var. A malformed value must not stop the app booting (email is optional), so it
    becomes 0 and utils/mailer.py reports it by name when someone tries to send."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return 0


def _env_float(name: str, default: float) -> float:
    """Float env var; a malformed value falls back to the default rather than stopping the app booting."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


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

    # Hard ceiling for any single request body (the per-plan limits FREE/PRO_MAX_UPLOAD_MB are enforced
    # below it). The batch endpoint has its own, larger body limit: see app.OmixaRequest.
    MAX_CONTENT_LENGTH = int(os.environ.get("OMIXA_MAX_UPLOAD_MB", "25")) * 1024 * 1024

    ALLOWED_EXTENSIONS = {"csv", "xlsx", "xls"}


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

    # When on, password (email + password) sign-ins are refused until Firebase reports the address as verified.
    # Off by default: the sign-in page must first send the verification email (Firebase sendEmailVerification),
    # otherwise nobody with a password account could ever get in. Google sign-in is unaffected.
    REQUIRE_VERIFIED_EMAIL = _env_flag("OMIXA_REQUIRE_VERIFIED_EMAIL", False)

    # --- Plans -----------------------------------------------------------
    # Free users get the whole cleaning engine; the limits below are the only caps.
    FREE_MAX_UPLOAD_MB = int(os.environ.get("OMIXA_FREE_MAX_UPLOAD_MB", "10"))
    PRO_MAX_UPLOAD_MB = int(os.environ.get("OMIXA_PRO_MAX_UPLOAD_MB", "25"))

    # --- Firebase (server side: Admin SDK; secrets never reach the browser) -------
    # Either FIREBASE_SERVICE_ACCOUNT_JSON (the JSON itself) or GOOGLE_APPLICATION_CREDENTIALS (a path).
    FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "").strip()
    FIREBASE_SERVICE_ACCOUNT_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
    # PUBLIC web config (apiKey, authDomain, projectId, appId ...) as JSON. Safe to expose.
    FIREBASE_WEB_CONFIG_JSON = os.environ.get("FIREBASE_WEB_CONFIG_JSON", "").strip()
    # Local development / tests only: keep accounts in memory instead of Firestore.
    ALLOW_MEMORY_STORE = _env_flag("OMIXA_ALLOW_MEMORY_STORE", False)

    # --- Paystack ---------------------------------------------------------
    PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY", "").strip()
    PAYSTACK_PLAN_MONTHLY = os.environ.get("PAYSTACK_PLAN_MONTHLY", "").strip()  # PLN_xxx, created in the dashboard
    PAYSTACK_PLAN_ANNUAL = os.environ.get("PAYSTACK_PLAN_ANNUAL", "").strip()
    # Prices are set in USD. The naira amount is derived from the live USD->NGN rate (utils/fx.py), so it follows the
    # dollar without a code change. PRICE_NGN_* are now only a last-resort fallback for when no rate can be fetched
    # (and the whole source of truth when OMIXA_FX_MODE=fixed). Leave them empty to rely on the live rate.
    PRICE_USD_MONTHLY = float(os.environ.get("OMIXA_PRICE_USD_MONTHLY", "5"))
    PRICE_USD_ANNUAL = float(os.environ.get("OMIXA_PRICE_USD_ANNUAL", "50"))
    PRICE_NGN_MONTHLY = _env_int("OMIXA_PRICE_NGN_MONTHLY", 0)
    PRICE_NGN_ANNUAL = _env_int("OMIXA_PRICE_NGN_ANNUAL", 0)

    # --- Live USD -> NGN conversion (utils/fx.py, docs/LIVE_NAIRA_PRICING.md) ---------------------------------
    FX_MODE = os.environ.get("OMIXA_FX_MODE", "auto").strip().lower()           # auto | fixed
    FX_URL = os.environ.get("OMIXA_FX_URL", "https://open.er-api.com/v6/latest/USD").strip()
    FX_CACHE_SECONDS = max(300, _env_int("OMIXA_FX_CACHE_SECONDS", 6 * 3600))    # how long a fetched rate is reused
    FX_TIMEOUT_SECONDS = max(1, _env_int("OMIXA_FX_TIMEOUT_SECONDS", 3))
    FX_FALLBACK_USD_NGN = _env_float("OMIXA_FX_FALLBACK_USD_NGN", 0.0)           # used only if no live rate was ever obtained
    FX_BUFFER_PERCENT = max(0.0, min(25.0, _env_float("OMIXA_FX_BUFFER_PERCENT", 0.0)))  # optional cushion added to the rate
    FX_ROUND_TO_NGN = max(1, _env_int("OMIXA_FX_ROUND_TO_NGN", 100))             # round the naira price to this step
    FX_MIN_RATE = _env_float("OMIXA_FX_MIN_RATE", 100.0)                         # sanity bounds: a rate outside is rejected
    FX_MAX_RATE = _env_float("OMIXA_FX_MAX_RATE", 20000.0)
    # Keep the NGN amount Paystack actually charges (the plan) in step with the displayed price. New subscribers only:
    # existing subscriptions keep the amount they signed up at (update_existing_subscriptions=false).
    PAYSTACK_AUTO_PRICE = _env_flag("OMIXA_PAYSTACK_AUTO_PRICE", True)
    PAYSTACK_AUTO_PRICE_TOLERANCE_PERCENT = max(0.0, _env_float("OMIXA_PAYSTACK_AUTO_PRICE_TOLERANCE_PERCENT", 2.0))

    PUBLIC_BASE_URL = os.environ.get("OMIXA_PUBLIC_BASE_URL", "").strip().rstrip("/")
    # Hostnames that serve this same app but must not be public: GET/HEAD requests to them get a permanent redirect to
    # PUBLIC_BASE_URL (see utils/canonical_host.py). An allowlist, never derived from the request. Only active when
    # PUBLIC_BASE_URL is set. Override with OMIXA_REDIRECT_HOSTS (comma-separated hostnames, no scheme).
    REDIRECT_HOSTS = tuple(h.strip().lower() for h in os.environ.get(
        "OMIXA_REDIRECT_HOSTS",
        "omixaa.up.railway.app,web-production-888aa.up.railway.app,web-production-01c985.up.railway.app",
    ).split(",") if h.strip())


    # ===================================================================
    # Scalability settings. Every default below reproduces the previous
    # single-instance behaviour, so nothing changes until you opt in.
    # ===================================================================

    # --- Processing mode -------------------------------------------------
    # "inline": the web process cleans the file inside the request (old behaviour).
    # "queue":  the API only creates a job; dedicated workers (worker.py) clean it.
    PROCESSING_MODE = os.environ.get("OMIXA_PROCESSING_MODE", "inline").strip().lower()

    # --- Shared infrastructure -------------------------------------------
    REDIS_URL = os.environ.get("REDIS_URL", "").strip()
    # Empty = SQLite at DB_PATH (dev / single instance). postgres://... = Postgres.
    DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
    DB_POOL_MAX = int(os.environ.get("OMIXA_DB_POOL_MAX", "10"))

    # --- Object storage (S3-compatible: AWS S3, Cloudflare R2, Backblaze B2, MinIO) ---
    STORAGE_BACKEND = os.environ.get("OMIXA_STORAGE_BACKEND", "local").strip().lower()
    S3_BUCKET = os.environ.get("OMIXA_S3_BUCKET", "").strip()
    S3_ENDPOINT_URL = os.environ.get("OMIXA_S3_ENDPOINT_URL", "").strip()
    S3_REGION = os.environ.get("OMIXA_S3_REGION", "auto").strip()
    S3_ACCESS_KEY_ID = os.environ.get("OMIXA_S3_ACCESS_KEY_ID", "").strip()
    S3_SECRET_ACCESS_KEY = os.environ.get("OMIXA_S3_SECRET_ACCESS_KEY", "").strip()
    S3_PREFIX = os.environ.get("OMIXA_S3_PREFIX", "jobs").strip().strip("/")
    S3_SSE = os.environ.get("OMIXA_S3_SSE", "AES256").strip()  # "" to disable (some providers reject it)
    SIGNED_URL_TTL_SECONDS = int(os.environ.get("OMIXA_SIGNED_URL_TTL_SECONDS", "300"))

    # --- Memory guard ----------------------------------------------------
    # Row and column limits alone allow 250M cells. This caps rows x columns.
    MAX_CELLS = int(os.environ.get("OMIXA_MAX_CELLS", "3000000"))

    # --- Job queue protection --------------------------------------------
    QUEUE_SOFT_LIMIT = int(os.environ.get("OMIXA_QUEUE_SOFT_LIMIT", "200"))   # above: "high traffic" message
    QUEUE_HARD_LIMIT = int(os.environ.get("OMIXA_QUEUE_HARD_LIMIT", "5000"))  # above: 503 + Retry-After
    MAX_ACTIVE_JOBS_ANON = int(os.environ.get("OMIXA_MAX_ACTIVE_JOBS_ANON", "2"))
    MAX_ACTIVE_JOBS_USER = int(os.environ.get("OMIXA_MAX_ACTIVE_JOBS_USER", "3"))
    MAX_ACTIVE_JOBS_PRO = int(os.environ.get("OMIXA_MAX_ACTIVE_JOBS_PRO", "8"))
    GLOBAL_MAX_RUNNING = int(os.environ.get("OMIXA_GLOBAL_MAX_RUNNING", "50"))
    # Independent worker processes one `python worker.py` container supervises. Each runs ONE job at a time in
    # its own isolated child, so total concurrency = replicas x this. Default 1 keeps the old behaviour.
    # Size by memory: peak usage is roughly WORKER_PROCESSES x OMIXA_JOB_MEMORY_LIMIT_MB per container.
    WORKER_PROCESSES = max(1, min(16, int(os.environ.get("OMIXA_WORKER_PROCESSES", "1") or 1)))
    JOB_TIMEOUT_SECONDS = int(os.environ.get("OMIXA_JOB_TIMEOUT_SECONDS", "900"))
    JOB_MEMORY_LIMIT_MB = int(os.environ.get("OMIXA_JOB_MEMORY_LIMIT_MB", "1536"))  # 0 disables
    JOB_MAX_ATTEMPTS = int(os.environ.get("OMIXA_JOB_MAX_ATTEMPTS", "3"))
    JOB_RETRY_BASE_SECONDS = int(os.environ.get("OMIXA_JOB_RETRY_BASE_SECONDS", "5"))
    JOB_LEASE_SECONDS = int(os.environ.get("OMIXA_JOB_LEASE_SECONDS", "90"))
    QUEUED_STALE_SECONDS = int(os.environ.get("OMIXA_QUEUED_STALE_SECONDS", "120"))
    # Finished/abandoned job data is deleted by the worker reaper after this long.
    JOB_TTL_SECONDS = int(os.environ.get("OMIXA_JOB_TTL_SECONDS", str(60 * 30)))

    # ---- Pro batch processing (all server-enforced; every value is overridable by env) ----
    BATCH_MAX_FILES = int(os.environ.get("OMIXA_BATCH_MAX_FILES", "20"))
    BATCH_MAX_TOTAL_MB = int(os.environ.get("OMIXA_BATCH_MAX_TOTAL_MB", "100"))
    # 0 = fall back to the Pro per-file upload limit (PRO_MAX_UPLOAD_MB)
    BATCH_MAX_FILE_MB = int(os.environ.get("OMIXA_BATCH_MAX_FILE_MB", "0"))
    # 0 = fall back to MAX_CELLS
    BATCH_MAX_CELLS_PER_FILE = int(os.environ.get("OMIXA_BATCH_MAX_CELLS_PER_FILE", "0"))
    BATCH_MAX_TOTAL_CELLS = int(os.environ.get("OMIXA_BATCH_MAX_TOTAL_CELLS", "10000000"))
    # files of ONE batch that may be queued/running at the same time (the rest wait, un-queued)
    BATCH_MAX_CONCURRENT_FILES = int(os.environ.get("OMIXA_BATCH_MAX_CONCURRENT_FILES", "3"))
    # batches per user that may be unfinished at the same time
    BATCH_MAX_ACTIVE_PER_USER = int(os.environ.get("OMIXA_BATCH_MAX_ACTIVE_PER_USER", "2"))
    BATCH_TIMEOUT_SECONDS = int(os.environ.get("OMIXA_BATCH_TIMEOUT_SECONDS", str(60 * 60)))
    BATCH_RETENTION_SECONDS = int(os.environ.get("OMIXA_BATCH_RETENTION_SECONDS", str(60 * 60 * 24)))
    SWEEP_MIN_INTERVAL_SECONDS = float(os.environ.get("OMIXA_SWEEP_MIN_INTERVAL_SECONDS", "15"))

    # --- Distributed rate limits (requests per minute per principal) -----
    # principal = signed-in uid, else client IP. 0 disables that bucket.
    RATE_LIMIT_USER_PER_MINUTE = int(os.environ.get("RATE_LIMIT_USER_PER_MINUTE", "120"))
    RATE_LIMIT_PRO_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PRO_PER_MINUTE", "300"))
    RATE_LIMIT_UPLOAD_PER_MINUTE = int(os.environ.get("RATE_LIMIT_UPLOAD_PER_MINUTE", "20"))
    RATE_LIMIT_PROCESS_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PROCESS_PER_MINUTE", "10"))
    RATE_LIMIT_STATUS_PER_MINUTE = int(os.environ.get("RATE_LIMIT_STATUS_PER_MINUTE", "180"))
    RATE_LIMIT_PAYMENT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PAYMENT_PER_MINUTE", "10"))
    RATE_LIMIT_ADMIN_PER_MINUTE = int(os.environ.get("RATE_LIMIT_ADMIN_PER_MINUTE", "120"))

    # --- Observability ---------------------------------------------------
    LOG_FORMAT = os.environ.get("OMIXA_LOG_FORMAT", "json" if _IS_PRODUCTION else "text").strip().lower()
    METRICS_TOKEN = os.environ.get("OMIXA_METRICS_TOKEN", "").strip()  # empty = /api/metrics disabled
    ENTITLEMENT_CACHE_SECONDS = int(os.environ.get("OMIXA_ENTITLEMENT_CACHE_SECONDS", "60"))

    # --- Email (Namecheap Private Email over SMTP; see utils/mailer.py and docs/EMAIL.md) -------------
    # Everything is optional: with SMTP unset the site works exactly as before, the contact form says it
    # is unavailable, and no email is claimed to have been sent. SMTP_USERNAME / SMTP_PASSWORD /
    # MAIL_DEFAULT_SENDER are set in Railway Variables, never in code or git.
    # Port 465 = implicit SSL (SMTP_USE_SSL=true, SMTP_USE_TLS=false).
    # Port 587 = STARTTLS     (SMTP_USE_SSL=false, SMTP_USE_TLS=true). Never both.
    SUPPORT_EMAIL = os.environ.get("OMIXA_SUPPORT_EMAIL", "support@getomixa.com").strip()
    SMTP_HOST = os.environ.get("SMTP_HOST", "mail.privateemail.com").strip()
    SMTP_PORT = _env_int("SMTP_PORT", 465)
    SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "").strip()
    SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "").strip("\r\n")
    SMTP_USE_SSL = _env_flag("SMTP_USE_SSL", True)
    SMTP_USE_TLS = _env_flag("SMTP_USE_TLS", False)
    MAIL_DEFAULT_SENDER = os.environ.get("MAIL_DEFAULT_SENDER", "").strip()
    SMTP_TIMEOUT_SECONDS = max(3, _env_int("SMTP_TIMEOUT_SECONDS", 10))
    # Background emails (welcome, reset, billing) retry transient failures only: timeouts, dropped connections,
    # 4xx server replies. Total attempts include the first; the wait doubles each time (base, 2x base, ...).
    MAIL_RETRY_ATTEMPTS = max(1, min(6, _env_int("MAIL_RETRY_ATTEMPTS", 3)))
    MAIL_RETRY_BASE_SECONDS = max(0, _env_int("MAIL_RETRY_BASE_SECONDS", 5))
    # scripts/send_expiry_reminders.py: how many days before expiry the reminder goes out.
    MAIL_EXPIRY_REMINDER_DAYS = max(1, min(30, _env_int("MAIL_EXPIRY_REMINDER_DAYS", 3)))
    # Where contact-form messages are delivered (defaults to the support address).
    CONTACT_FORM_RECIPIENT = os.environ.get("CONTACT_FORM_RECIPIENT", "").strip() or SUPPORT_EMAIL
    # Contact form / reset-email abuse limits (all server-enforced; 0 disables a bucket).
    RATE_LIMIT_CONTACT_PER_MINUTE = _env_int("RATE_LIMIT_CONTACT_PER_MINUTE", 3)
    RATE_LIMIT_CONTACT_PER_HOUR = _env_int("RATE_LIMIT_CONTACT_PER_HOUR", 10)
    RATE_LIMIT_CONTACT_GLOBAL_PER_HOUR = _env_int("RATE_LIMIT_CONTACT_GLOBAL_PER_HOUR", 60)  # whole site, protects the inbox
    RATE_LIMIT_RESET_EMAIL_PER_MINUTE = _env_int("RATE_LIMIT_RESET_EMAIL_PER_MINUTE", 5)
    RATE_LIMIT_RESET_EMAIL_PER_ADDRESS_HOUR = _env_int("RATE_LIMIT_RESET_EMAIL_PER_ADDRESS_HOUR", 3)
