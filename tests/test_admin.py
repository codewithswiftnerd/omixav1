"""
Admin auth, CSRF, and dashboard data tests.

Login itself is exercised against the real Config.ADMIN_USERNAME /
ADMIN_PASSWORD_HASH set up by the root conftest.py (see
TEST_ADMIN_PASSWORD there and in tests/conftest.py).
"""

import re

from tests.conftest import TEST_ADMIN_PASSWORD


def _csrf_from(html: bytes) -> str:
    m = re.search(rb'name="csrf_token" value="([^"]+)"', html)
    assert m, "csrf token not found in rendered page"
    return m.group(1).decode()


def _login(client, password=TEST_ADMIN_PASSWORD, username="admin"):
    r = client.get("/admin/login")
    token = _csrf_from(r.data)
    return client.post(
        "/admin/login",
        data={"username": username, "password": password, "csrf_token": token},
    )


def test_dashboard_page_redirects_to_login_when_unauthenticated(client):
    r = client.get("/admin/")
    assert r.status_code == 302
    assert "/admin/login" in r.headers["Location"]


def test_dashboard_api_returns_401_when_unauthenticated(client):
    r = client.get("/admin/api/stats")
    assert r.status_code == 401


def test_login_with_wrong_password_fails(client):
    r = client.get("/admin/login")
    token = _csrf_from(r.data)
    r = client.post(
        "/admin/login",
        data={"username": "admin", "password": "definitely-wrong", "csrf_token": token},
    )
    assert r.status_code == 401


def test_login_with_wrong_username_fails(client):
    r = client.get("/admin/login")
    token = _csrf_from(r.data)
    r = client.post(
        "/admin/login",
        data={"username": "not-admin", "password": TEST_ADMIN_PASSWORD, "csrf_token": token},
    )
    assert r.status_code == 401


def test_login_without_csrf_token_fails(client):
    r = client.post("/admin/login", data={"username": "admin", "password": TEST_ADMIN_PASSWORD})
    assert r.status_code == 400


def test_login_with_correct_credentials_succeeds_and_grants_dashboard_access(client):
    r = _login(client)
    assert r.status_code == 302
    assert "/admin/" in r.headers["Location"]

    r = client.get("/admin/api/stats")
    assert r.status_code == 200


def test_logged_in_admin_session_does_not_grant_job_access(client, app):
    """Being an authenticated admin must not implicitly make every
    job_id readable — admin analytics (db.py) are metadata only, the
    actual job files still require session ownership (see
    tests/test_sessions_and_jobs.py)."""
    stranger = app.test_client()
    r = stranger.post(
        "/api/upload/",
        data={"file": (__import__("io").BytesIO(b"Name\nJohn\n"), "x.csv")},
        content_type="multipart/form-data",
    )
    job_id = r.get_json()["job_id"]

    admin_client = client
    _login(admin_client)
    r = admin_client.get(f"/api/report/{job_id}")
    assert r.status_code == 404


def test_purge_requires_csrf_token(client):
    _login(client)
    r = client.post("/admin/api/jobs/purge")
    assert r.status_code == 403


def test_purge_succeeds_with_valid_csrf_token(client):
    _login(client)
    r = client.get("/admin/")
    m = re.search(rb'OMIXA_ADMIN_CSRF = "([^"]+)"', r.data)
    assert m
    token = m.group(1).decode()

    r = client.post("/admin/api/jobs/purge", headers={"X-CSRF-Token": token})
    assert r.status_code == 200


def test_logout_requires_admin_and_csrf(client):
    r = client.post("/admin/logout")
    assert r.status_code in (302, 401)  # not logged in -> redirected by admin_required

    _login(client)
    r = client.post("/admin/logout")
    assert r.status_code == 403  # logged in but no csrf token

    r = client.get("/admin/")
    m = re.search(rb'OMIXA_ADMIN_CSRF = "([^"]+)"', r.data)
    token = m.group(1).decode()
    r = client.post("/admin/logout", headers={"X-CSRF-Token": token})
    assert r.status_code == 302

    r = client.get("/admin/api/stats")
    assert r.status_code == 401  # session no longer admin


def test_stats_reflect_uploaded_and_processed_jobs(client, app):
    stranger = app.test_client()
    r = stranger.post(
        "/api/upload/",
        data={"file": (__import__("io").BytesIO(b"Name,Age\nJohn,24\n"), "x.csv")},
        content_type="multipart/form-data",
    )
    job_id = r.get_json()["job_id"]
    stranger.post(f"/api/process/{job_id}", json={})

    _login(client)
    r = client.get("/admin/api/stats")
    stats = r.get_json()
    assert stats["total_jobs"] >= 1
    assert stats["by_format"].get("csv", 0) >= 1
    assert stats["processing_success_count"] >= 1


def test_jobs_listing_filters_by_status(client, app):
    stranger = app.test_client()
    r = stranger.post(
        "/api/upload/",
        data={"file": (__import__("io").BytesIO(b"Name,Age\nJohn,24\n"), "x.csv")},
        content_type="multipart/form-data",
    )
    job_id = r.get_json()["job_id"]

    _login(client)
    r = client.get("/admin/api/jobs?status=uploaded")
    jobs = r.get_json()["jobs"]
    assert any(j["job_id"] == job_id for j in jobs)
    assert all(j["status"] == "uploaded" for j in jobs)

    r = client.get("/admin/api/jobs?status=processed")
    jobs = r.get_json()["jobs"]
    assert not any(j["job_id"] == job_id for j in jobs)


def test_jobs_listing_never_includes_session_hash(client, app):
    stranger = app.test_client()
    stranger.post(
        "/api/upload/",
        data={"file": (__import__("io").BytesIO(b"Name\nJohn\n"), "x.csv")},
        content_type="multipart/form-data",
    )
    _login(client)
    r = client.get("/admin/api/jobs")
    for job in r.get_json()["jobs"]:
        assert "session_hash" not in job


def test_admin_login_attempts_are_rate_limited(client, monkeypatch):
    from config import Config
    monkeypatch.setattr(Config, "LOGIN_RATE_LIMIT_PER_MINUTE", 2)

    for _ in range(2):
        r = client.get("/admin/login")
        token = _csrf_from(r.data)
        resp = client.post(
            "/admin/login",
            data={"username": "admin", "password": "wrong", "csrf_token": token},
        )
        assert resp.status_code == 401

    r = client.get("/admin/login")
    token = _csrf_from(r.data)
    resp = client.post(
        "/admin/login",
        data={"username": "admin", "password": "wrong", "csrf_token": token},
    )
    assert resp.status_code == 429
