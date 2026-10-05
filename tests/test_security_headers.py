"""
Response-level security posture: headers on every response, the
health endpoint's shape, and CORS behavior for an origin that isn't
on the allow-list.
"""

import re


def test_security_headers_present_on_page_response(client):
    r = client.get("/")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "strict-origin-when-cross-origin" in r.headers["Referrer-Policy"]
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]


def test_csp_uses_a_per_request_nonce_not_unsafe_inline(client):
    r = client.get("/")
    csp = r.headers["Content-Security-Policy"]
    assert "'unsafe-inline'" not in csp.split("style-src")[0]  # not in script-src
    m = re.search(r"script-src 'self' 'nonce-([^']+)'", csp)
    assert m, csp
    nonce = m.group(1)
    assert f'nonce="{nonce}"'.encode() in r.data


def test_health_endpoint_reports_ok(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.get_json()
    assert body["status"] == "ok"
    assert body["checks"]["temp_dir_writable"] is True
    assert body["checks"]["db_reachable"] is True


def test_health_endpoint_exempt_from_rate_limit(client, monkeypatch):
    from config import Config
    monkeypatch.setattr(Config, "RATE_LIMIT_PER_MINUTE", 1)
    for _ in range(5):
        assert client.get("/api/health").status_code == 200


def test_api_rate_limit_applies_and_recovers_per_bucket(client, monkeypatch):
    from config import Config
    monkeypatch.setattr(Config, "RATE_LIMIT_PER_MINUTE", 2)
    assert client.get("/api/report/does-not-exist").status_code == 404  # allowed (1st)
    assert client.get("/api/report/does-not-exist").status_code == 404  # allowed (2nd)
    r = client.get("/api/report/does-not-exist")
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_cors_headers_absent_for_non_listed_origin(client, monkeypatch):
    from config import Config
    monkeypatch.setattr(Config, "ALLOWED_ORIGINS", "https://trusted.example.com")
    r = client.get("/api/health", headers={"Origin": "https://evil.example.com"})
    assert "Access-Control-Allow-Origin" not in r.headers


def test_cors_headers_present_for_listed_origin(client, monkeypatch):
    from config import Config
    monkeypatch.setattr(Config, "ALLOWED_ORIGINS", "https://trusted.example.com")
    r = client.get("/api/health", headers={"Origin": "https://trusted.example.com"})
    assert r.headers["Access-Control-Allow-Origin"] == "https://trusted.example.com"
    assert r.headers["Access-Control-Allow-Credentials"] == "true"


def test_wildcard_cors_never_sets_allow_credentials(client, monkeypatch):
    """A browser refuses Allow-Credentials: true on a wildcard origin
    anyway, but the server must not even offer that combination."""
    from config import Config
    monkeypatch.setattr(Config, "ALLOWED_ORIGINS", "*")
    r = client.get("/api/health", headers={"Origin": "https://anything.example.com"})
    assert r.headers.get("Access-Control-Allow-Origin") == "*"
    assert "Access-Control-Allow-Credentials" not in r.headers
