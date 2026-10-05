"""
Root pytest conftest.

Sets the environment variables config.py needs BEFORE any test module
(old or new) imports `config` or `app` — config.Config reads these at
class-definition time, so they must exist before the first import
anywhere in the test session, hence this lives at the repo root
(loaded first by pytest, ahead of anything under testing/ or tests/)
rather than inside tests/.

Deliberately does NOT rely on a real .env file: this must produce a
clean, working test run on a fresh clone with no .env at all (CI,
another contributor's machine), matching FLASK_ENV=production's
"fail closed without a real secret" behavior in config.py.
"""

import os
import tempfile

_TEST_TMP = tempfile.mkdtemp(prefix="omixa-pytest-")

os.environ.setdefault("FLASK_ENV", "development")
os.environ.setdefault("OMIXA_SECRET_KEY", "pytest-fixed-secret-key-not-for-production-use")
os.environ.setdefault("OMIXA_ADMIN_USERNAME", "admin")
# Hash of "pytest-admin-password" (werkzeug scrypt) — fine to hardcode,
# this only ever runs against the throwaway TEMP_DIR/DB_PATH below.
os.environ.setdefault(
    "OMIXA_ADMIN_PASSWORD_HASH",
    "scrypt:32768:8:1$p5FvHWOgJvmOmuuK$"
    "a537bfaa3c1b44d580c6b205d62e6449ee58bb3586eeec8f01e36b5c1560179"
    "778e354d40440dbdcaac1174949f7076920ac3adc828d45e8f303448ec173bcca",
)
TEST_ADMIN_PASSWORD = "pytest-admin-password"  # the plaintext behind the hash above
os.environ.setdefault("OMIXA_DB_PATH", os.path.join(_TEST_TMP, "omixa.db"))
os.environ.setdefault("OMIXA_TEMP_DIR", os.path.join(_TEST_TMP, "temp"))
os.environ.setdefault("RATE_LIMIT_PER_MINUTE", "0")  # tests fire many requests quickly
os.environ.setdefault("LOGIN_RATE_LIMIT_PER_MINUTE", "0")


import pytest  # noqa: E402  (must come after the env vars above are set)


@pytest.fixture(autouse=True)
def _reset_rate_limit_state():
    """The rate limiter keeps an in-process sliding window keyed by client IP,
    and every Flask test client is 127.0.0.1. Without this, hits recorded by one
    test leak into the next and make rate-limit assertions order-dependent."""
    import utils.security as security
    security._hits.clear()
    yield
    security._hits.clear()


@pytest.fixture(autouse=True, scope="session")
def _init_metadata_db():
    """Some unit tests call code that logs to the metadata db without going
    through the Flask app factory; make sure the schema exists so they do not
    spray 'no such table' errors."""
    import db as metadata_db
    metadata_db.init_db()
    yield
