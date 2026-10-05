"""
Fixtures for the Flask-app-level test suite (session auth, job
ownership/isolation, admin, upload validation). The root conftest.py
(one level up) has already set the environment variables config.py
needs before this module is even imported.
"""

import io

import pytest

# Root conftest.py sets env vars before any of these imports happen.
import app as app_module
import db as metadata_db

# Mirrors the plaintext behind root conftest.py's
# OMIXA_ADMIN_PASSWORD_HASH — kept as a separate constant here rather
# than imported from the root conftest module, since relying on how
# pytest names/caches conftest.py as an importable module is fragile;
# duplicating one string is not.
TEST_ADMIN_PASSWORD = "pytest-admin-password"


@pytest.fixture
def app():
    flask_app = app_module.app
    flask_app.config.update(TESTING=True)
    yield flask_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Every test gets its own throwaway sqlite file, so job/analytics
    assertions in one test can never see rows written by another."""
    db_path = tmp_path / "omixa-test.db"
    monkeypatch.setattr(metadata_db.Config, "DB_PATH", str(db_path))
    metadata_db.init_db()
    yield


@pytest.fixture(autouse=True)
def _isolated_temp_dir(tmp_path, monkeypatch):
    """Every test gets its own throwaway job-storage directory."""
    import utils.file_handler as file_handler
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir()
    monkeypatch.setattr(file_handler.Config, "TEMP_DIR", str(temp_dir))
    yield


def make_csv(rows=None):
    if rows is None:
        rows = b"Name,Age\nJohn,24\nMary,31\n"
    return io.BytesIO(rows)


def admin_password():
    return TEST_ADMIN_PASSWORD
