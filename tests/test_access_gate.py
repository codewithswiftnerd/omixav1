"""Pre-launch access gate (utils/access_gate.py): unauthorized, authorized, direct API, file routes, expiry, bypasses."""
import hashlib
import hmac
import io
import re
import types
from pathlib import Path

import pytest

import app as app_module
import db as metadata_db
from config import Config
from utils import access_gate, redis_client
from tests.conftest import make_csv

PASSWORD = "correct-horse-battery-staple"
SECRET = "g" * 48
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.setattr(Config, "ACCESS_GATE_ENABLED", True)
    monkeypatch.setattr(Config, "ACCESS_GATE_PASSWORD", PASSWORD)
    monkeypatch.setattr(Config, "ACCESS_GATE_SECRET", SECRET)
    monkeypatch.setattr(Config, "ACCESS_GATE_OPEN_AT", "")
    monkeypatch.setattr(Config, "ACCESS_GATE_SCOPE", "site")   # the strict mode; the default "app" scope is tested at the end
    monkeypatch.setattr(Config, "ACCESS_GATE_SESSION_HOURS", 12)
    monkeypatch.setattr(Config, "ACCESS_GATE_ATTEMPTS_PER_MINUTE", 6)
    monkeypatch.setattr(Config, "ACCESS_GATE_ATTEMPTS_PER_HOUR", 40)
    return access_gate


@pytest.fixture
def unlocked(client, gate):
    r = client.post("/__access", data={"password": PASSWORD, "next": "/"})
    assert r.status_code == 303
    return client


def _rules():
    for rule in app_module.app.url_map.iter_rules():
        path = re.sub(r"<[^>]+>", "x", rule.rule)
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            if access_gate.is_exempt(method, path):
                continue
            yield method, path


# ------------------------------------------------------------------------------------------ unauthorized

@pytest.mark.parametrize("path", ["/", "/clean", "/pricing", "/login", "/dashboard", "/batch", "/about", "/contact",
                                  "/admin/login", "/admin/", "/sessions/x", "/profiles/new", "/sitemap.xml", "/nope"])
def test_pages_are_blocked_without_the_cookie(client, gate, path):
    r = client.get(path)
    assert r.status_code == 403
    body = r.get_data(as_text=True)
    assert "preparing for launch" in body
    assert "Clean a file" not in body            # none of the real app is rendered
    assert r.headers["Cache-Control"] == "no-store"
    assert "noindex" in r.headers["X-Robots-Tag"]
    assert "omixa_session" not in r.headers.get("Set-Cookie", "")   # no session is created for a blocked visitor


def test_every_registered_route_is_gated_for_every_method(client, gate):
    checked = 0
    for method, path in _rules():
        r = client.open(path, method=method)
        if path.startswith(("/api/", "/admin/api/")):
            assert r.status_code == 401 and r.get_json()["code"] == "access_gate", (method, path, r.status_code)
        else:
            assert r.status_code == 403, (method, path, r.status_code)
        checked += 1
    assert checked > 40


def test_static_files_are_blocked_except_the_ones_the_gate_page_uses(client, gate):
    assert client.get("/static/js/pricing.js").status_code == 403
    assert client.get("/static/css/site.css").status_code == 403
    assert client.get("/static/js/admin.js").status_code == 403
    assert client.get("/static/css/gate.css").status_code == 200
    assert client.get("/static/brand/omixa-icon.png").status_code == 200


@pytest.mark.parametrize("path", ["/static/../config.py", "/api//health", "/healthz/", "//api/sessions/", "/API/sessions/"])
def test_path_tricks_do_not_slip_through(client, gate, path):
    assert client.get(path).status_code in (401, 403)


def test_forged_and_stale_cookies_are_rejected(client, gate):
    good = access_gate.issue_token()
    other_secret = access_gate.URLSafeSerializer(b"x" * 32, salt="omixa.access-gate.cookie").dumps({"v": 1, "iat": 1})
    for bad in ["", "garbage", good[:-2] + "xx", good + "a", other_secret, "a" * 600]:
        client.set_cookie(access_gate.COOKIE_NAME, bad)
        assert client.get("/pricing").status_code == 403, bad[:20]
    client.set_cookie(access_gate.COOKIE_NAME, good)
    assert client.get("/pricing").status_code == 200


def test_rotating_the_password_or_secret_signs_everyone_out(client, gate, monkeypatch):
    token = access_gate.issue_token()
    client.set_cookie(access_gate.COOKIE_NAME, token)
    assert client.get("/pricing").status_code == 200
    monkeypatch.setattr(Config, "ACCESS_GATE_PASSWORD", "a-different-long-password")
    assert client.get("/pricing").status_code == 403
    monkeypatch.setattr(Config, "ACCESS_GATE_PASSWORD", PASSWORD)
    assert client.get("/pricing").status_code == 200
    monkeypatch.setattr(Config, "ACCESS_GATE_SECRET", "h" * 48)
    assert client.get("/pricing").status_code == 403


def test_having_an_omixa_account_session_does_not_pass_the_gate(client, gate):
    with client.session_transaction() as s:
        s["uid"] = "some-user"
    assert client.get("/dashboard").status_code == 403
    assert client.get("/api/profiles/").status_code == 401


# ------------------------------------------------------------------------------------------ direct API + files

def test_file_routes_refuse_direct_calls_and_create_nothing(client, gate):
    before = metadata_db.stats_summary(window_seconds=3600)
    calls = [
        client.post("/api/upload/", data={"file": (make_csv(), "t.csv")}, content_type="multipart/form-data"),
        client.post("/api/batches/", data={"files": [(make_csv(), "t.csv")]}, content_type="multipart/form-data"),
        client.post("/api/process/abc", json={}),
        client.get("/api/download/abc"), client.get("/api/report/abc"),
        client.get("/api/jobs/abc"), client.post("/api/validate/", json={}),
        client.post("/api/cleaning/anything", json={}), client.get("/api/sessions/"),
        client.post("/api/billing/checkout", json={"plan": "pro_monthly"}),
        client.post("/api/auth/session", json={"idToken": "x"}), client.post("/api/contact", json={}),
    ]
    assert [c.status_code for c in calls] == [401] * len(calls)
    assert list(Path(Config.TEMP_DIR).iterdir()) == []
    assert metadata_db.stats_summary(window_seconds=3600) == before
    for c in calls:
        assert PASSWORD not in c.get_data(as_text=True) and SECRET not in c.get_data(as_text=True)


def test_upload_process_download_work_after_unlocking(unlocked):
    r = unlocked.post("/api/upload/", data={"file": (make_csv(), "t.csv")}, content_type="multipart/form-data")
    assert r.status_code == 201
    job = r.get_json()["job_id"]
    assert unlocked.post(f"/api/process/{job}", json={}).status_code == 200
    d = unlocked.get(f"/api/download/{job}")
    assert d.status_code == 200 and b"John" in d.data


def test_a_second_browser_without_the_cookie_cannot_use_the_first_ones_job(unlocked, app):
    job = unlocked.post("/api/upload/", data={"file": (make_csv(), "t.csv")}, content_type="multipart/form-data").get_json()["job_id"]
    stranger = app.test_client()
    assert stranger.get(f"/api/download/{job}").status_code == 401


def test_existing_session_auth_still_works_behind_the_gate(unlocked):
    assert unlocked.get("/api/profiles/").status_code == 401           # Omixa's own sign-in still decides
    assert unlocked.get("/api/billing/plans").status_code == 200
    assert unlocked.get("/login").status_code == 200


# ------------------------------------------------------------------------------------------ authorized flow

def test_correct_password_sets_a_hardened_cookie_and_redirects_to_next(client, gate):
    r = client.post("/__access", data={"password": PASSWORD, "next": "/pricing"})
    assert r.status_code == 303 and r.headers["Location"] == "/pricing"
    cookie = r.headers["Set-Cookie"]
    assert cookie.startswith("omixa_gate=")
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Path=/" in cookie
    assert f"Max-Age={12 * 3600}" in cookie
    assert PASSWORD not in cookie
    assert client.get("/pricing").status_code == 200


def test_cookie_is_secure_in_production(client, gate, monkeypatch):
    monkeypatch.setattr(Config, "IS_PRODUCTION", True)
    r = client.post("/__access", data={"password": PASSWORD})
    assert "Secure" in r.headers["Set-Cookie"]


def test_wrong_password_is_refused_without_a_cookie_and_is_not_logged(client, gate, caplog):
    caplog.set_level("DEBUG")
    r = client.post("/__access", data={"password": "wrong-guess-123", "next": "/"})
    assert r.status_code == 401
    assert "isn't right" in __import__("html").unescape(r.get_data(as_text=True))
    assert "omixa_gate" not in r.headers.get("Set-Cookie", "")
    assert "wrong-guess-123" not in caplog.text and PASSWORD not in caplog.text and SECRET not in caplog.text
    assert client.get("/pricing").status_code == 403


def test_password_in_a_url_does_nothing(client, gate):
    r = client.get(f"/__access?password={PASSWORD}")
    assert r.status_code == 200 and "omixa_gate" not in r.headers.get("Set-Cookie", "")
    assert PASSWORD not in r.get_data(as_text=True)
    assert client.get(f"/pricing?password={PASSWORD}").status_code == 403


@pytest.mark.parametrize("nxt", ["//evil.example", "https://evil.example", "/\\evil.example", "javascript:alert(1)",
                                 "/__access", "/a\nb", "x" * 600])
def test_next_can_never_redirect_off_site(client, gate, nxt):
    r = client.post("/__access", data={"password": PASSWORD, "next": nxt})
    assert r.status_code == 303 and r.headers["Location"] == "/"


def test_cross_origin_unlock_is_refused(client, gate):
    r = client.post("/__access", data={"password": PASSWORD}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_attempts_are_rate_limited(client, gate, monkeypatch):
    monkeypatch.setattr(Config, "ACCESS_GATE_ATTEMPTS_PER_MINUTE", 3)
    codes = [client.post("/__access", data={"password": f"bad-guess-{i}"}).status_code for i in range(5)]
    assert codes == [401, 401, 401, 429, 429]
    r = client.post("/__access", data={"password": PASSWORD})
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1 and "omixa_gate" not in r.headers.get("Set-Cookie", "")


def test_logout_clears_the_cookie(unlocked):
    r = unlocked.post("/__access/logout")
    assert r.status_code == 303 and "omixa_gate=;" in r.headers["Set-Cookie"]
    assert unlocked.get("/pricing").status_code == 403


def test_an_administrator_already_signed_in_is_not_locked_out(client, gate):
    with client.session_transaction() as s:
        s["is_admin"] = True
    assert client.get("/admin/").status_code != 403


# ------------------------------------------------------------------------------------------ session expiry

def _clock(monkeypatch, now):
    monkeypatch.setattr(access_gate, "time", types.SimpleNamespace(time=lambda: now[0]))


def test_session_expires_after_the_configured_hours(client, gate, monkeypatch):
    now = [1_800_000_000.0]
    _clock(monkeypatch, now)
    assert client.post("/__access", data={"password": PASSWORD}).status_code == 303
    now[0] += 12 * 3600 - 5
    assert client.get("/pricing").status_code == 200
    now[0] += 10
    assert client.get("/pricing").status_code == 403
    assert client.get("/api/sessions/").status_code == 401


def test_session_hours_setting_is_respected(client, gate, monkeypatch):
    monkeypatch.setattr(Config, "ACCESS_GATE_SESSION_HOURS", 1)
    now = [1_800_000_000.0]
    _clock(monkeypatch, now)
    r = client.post("/__access", data={"password": PASSWORD})
    assert "Max-Age=3600" in r.headers["Set-Cookie"]
    now[0] += 3601
    assert client.get("/pricing").status_code == 403


def test_a_token_dated_in_the_future_is_refused(gate):
    assert not access_gate.token_is_valid(access_gate.issue_token(now=2_000_000_000), now=1_800_000_000)


# ------------------------------------------------------------------------------------------ always-reachable paths

def test_health_checks_work_without_the_cookie(client, gate):
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200
    assert client.get("/api/health").status_code == 200


def test_paystack_webhook_is_reachable_and_still_signature_checked(client, gate, monkeypatch):
    monkeypatch.setattr(Config, "PAYSTACK_SECRET_KEY", "sk_test_secret")
    bad = client.post("/api/billing/webhook", data=b"{}", headers={"x-paystack-signature": "nope"})
    assert bad.status_code == 401 and bad.get_json() == {"error": "bad signature"}      # the route's answer, not the gate's
    body = b'{"event":"ping","data":{}}'
    sig = hmac.new(b"sk_test_secret", body, hashlib.sha512).hexdigest()
    ok = client.post("/api/billing/webhook", data=body, headers={"x-paystack-signature": sig})
    assert ok.status_code in (200, 503) and ok.get_json().get("code") != "access_gate"
    assert client.get("/api/billing/webhook").status_code == 401   # only POST is exempt


def test_metrics_keeps_its_own_token_auth(client, gate, monkeypatch):
    monkeypatch.setattr(Config, "METRICS_TOKEN", "m" * 32)
    assert client.get("/api/metrics").status_code == 404
    assert client.get("/api/metrics", headers={"Authorization": "Bearer " + "m" * 32}).status_code == 200


def test_cors_preflight_is_not_broken(client, gate):
    assert client.options("/api/upload/").status_code != 401


def test_worker_does_not_talk_to_the_web_service_over_http():
    for name in ("worker.py", "jobs/service.py", "jobs/queue.py", "jobs/batches.py"):
        src = (ROOT / name).read_text()
        assert not re.search(r"^\s*(import requests|from requests|import urllib|import httpx)", src, re.M), name


def test_worker_runs_jobs_while_the_gate_is_on(unlocked, monkeypatch):
    import worker
    from tests.fakes import FakeRedis
    redis_client.set_redis_for_tests(FakeRedis())
    try:
        monkeypatch.setattr(Config, "PROCESSING_MODE", "queue")
        monkeypatch.setattr(Config, "JOB_MEMORY_LIMIT_MB", 0)
        job = unlocked.post("/api/upload/", data={"file": (make_csv(), "t.csv")}, content_type="multipart/form-data").get_json()["job_id"]
        assert unlocked.post(f"/api/process/{job}", json={}).status_code in (200, 202)
        for _ in range(10):
            if worker.run_one("w-gate") == "idle":
                break
        assert metadata_db.get_job(job)["status"] == "processed"
    finally:
        redis_client.set_redis_for_tests(None)


def test_google_verification_path_matches_the_seo_module():
    from routes import seo
    assert f"/{seo.GOOGLE_VERIFICATION_FILE}" in access_gate.EXEMPT_PATHS


# ------------------------------------------------------------------------------------------ search engines

def test_robots_and_sitemap_while_gated(client, gate, monkeypatch):
    monkeypatch.setattr(Config, "IS_PRODUCTION", True)
    assert client.get("/robots.txt").get_data(as_text=True) == "User-agent: *\nDisallow: /\n"
    assert client.get("/sitemap.xml").status_code in (403, 404)
    from routes import seo
    assert not seo.is_indexable_path("/")
    for path in ("/healthz", "/robots.txt", "/static/css/gate.css"):
        assert "noindex" in client.get(path).headers["X-Robots-Tag"]


def test_robots_returns_to_normal_when_the_gate_is_off(client, monkeypatch):
    monkeypatch.setattr(Config, "IS_PRODUCTION", True)
    assert "Allow: /" in client.get("/robots.txt").get_data(as_text=True)


# ------------------------------------------------------------------------------------------ switching on/off

def test_gate_is_off_by_default_and_changes_nothing(client):
    r = client.get("/pricing")
    assert r.status_code == 200 and "noarchive" not in r.headers.get("X-Robots-Tag", "")   # the gate's header is absent
    assert client.get("/__access").status_code == 404
    assert client.get("/api/billing/plans").status_code == 200


def test_misconfigured_gate_fails_closed_without_leaking(client, gate, monkeypatch, caplog):
    monkeypatch.setattr(Config, "ACCESS_GATE_SECRET", "")
    assert access_gate.state() == access_gate.STATE_MISCONFIGURED
    page = client.get("/pricing")
    assert page.status_code == 503 and "<form" not in page.get_data(as_text=True)
    assert client.get("/api/sessions/").status_code == 503
    assert client.post("/__access", data={"password": PASSWORD}).status_code == 503
    assert client.get("/healthz").status_code == 200
    names = " ".join(access_gate.config_problems())
    assert "OMIXA_ACCESS_GATE_SECRET" in names and PASSWORD not in names
    client.set_cookie(access_gate.COOKIE_NAME, "anything")
    assert client.get("/pricing").status_code == 503


def test_short_password_counts_as_misconfigured(gate, monkeypatch):
    monkeypatch.setattr(Config, "ACCESS_GATE_PASSWORD", "short")
    assert access_gate.state() == access_gate.STATE_MISCONFIGURED


def test_gate_opens_itself_at_the_launch_moment(client, gate, monkeypatch):
    monkeypatch.setattr(Config, "ACCESS_GATE_OPEN_AT", "2026-11-13T00:00:00+01:00")
    before = 1_794_524_400 - 1      # 2026-11-12T22:59:59Z
    assert access_gate.state(before) == access_gate.STATE_ACTIVE
    assert access_gate.state(before + 1) == access_gate.STATE_OPEN
    now = [before]
    _clock(monkeypatch, now)
    assert client.get("/pricing").status_code == 403
    now[0] += 1
    assert client.get("/pricing").status_code == 200
    assert client.get("/api/sessions/").status_code == 401   # Omixa's own auth, not the gate (no code field)
    assert client.get("/api/sessions/").get_json().get("code") != "access_gate"


def test_unreadable_open_at_keeps_the_site_private(gate, monkeypatch):
    monkeypatch.setattr(Config, "ACCESS_GATE_OPEN_AT", "13th November")
    assert access_gate.state(9_999_999_999) == access_gate.STATE_ACTIVE


def test_naive_open_at_is_read_as_utc(gate, monkeypatch):
    monkeypatch.setattr(Config, "ACCESS_GATE_OPEN_AT", "2026-11-13T00:00:00")
    assert access_gate._open_at_timestamp() == 1_794_528_000


# ------------------------------------------------------------------------------------------ the gate page

def test_gate_page_is_branded_mobile_friendly_and_script_free(client, gate):
    html = client.get("/").get_data(as_text=True)
    assert "Omixa is preparing for launch" in html
    assert 'name="viewport"' in html and 'name="robots" content="noindex' in html
    assert 'type="password"' in html and 'method="post"' in html
    assert "<script" not in html and "style=" not in html
    assert PASSWORD not in html and SECRET not in html


def test_gate_css_uses_only_brand_tokens():
    css = re.sub(r"/\*.*?\*/", "", (ROOT / "static/css/gate.css").read_text(), flags=re.S)
    assert re.findall(r"#[0-9a-fA-F]{3,8}\b", css) == []
    for decl in re.findall(r"font-family\s*:\s*([^;}]+)", css):
        assert decl.strip().startswith("var(--font-sans)")
    allowed = {(255, 255, 255), (14, 5, 36), (56, 21, 147), (165, 84, 247), (104, 52, 214), (244, 238, 253)}
    for r, g, b in re.findall(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", css):
        assert (int(r), int(g), int(b)) in allowed
    assert "font-size: 16px" in css


def test_env_example_documents_the_gate_variables():
    text = (ROOT / ".env.example").read_text()
    for name in ("OMIXA_ACCESS_GATE_ENABLED", "OMIXA_ACCESS_GATE_PASSWORD", "OMIXA_ACCESS_GATE_SECRET", "OMIXA_ACCESS_GATE_OPEN_AT"):
        assert name in text


# ------------------------------------------------------------------------------------------ scope "app": SEO untouched

def _norm(html: str) -> str:
    return re.sub(r'nonce="[^"]+"', 'nonce=""', html)


@pytest.fixture
def app_scope(gate, monkeypatch):
    monkeypatch.setattr(Config, "ACCESS_GATE_SCOPE", "app")
    monkeypatch.setattr(Config, "IS_PRODUCTION", True)
    return gate


def test_default_scope_is_app(monkeypatch):
    assert Config.ACCESS_GATE_SCOPE == "app"
    monkeypatch.setattr(Config, "ACCESS_GATE_SCOPE", "typo")
    assert access_gate.scope() == "site"          # anything unrecognised is the strict mode


def test_seo_pages_are_identical_with_the_gate_on_or_off(app, monkeypatch):
    from routes import seo
    monkeypatch.setattr(Config, "IS_PRODUCTION", True)
    monkeypatch.setattr(Config, "PUBLIC_BASE_URL", "https://getomixa.com")
    paths = sorted(seo.PUBLIC_PAGES) + ["/sitemap.xml", "/robots.txt", "/static/css/site.css", "/static/js/home.js", "/favicon.ico"]
    before = {p: app.test_client().get(p, base_url="https://getomixa.com") for p in paths}
    monkeypatch.setattr(Config, "ACCESS_GATE_ENABLED", True)
    monkeypatch.setattr(Config, "ACCESS_GATE_PASSWORD", PASSWORD)
    monkeypatch.setattr(Config, "ACCESS_GATE_SECRET", SECRET)
    monkeypatch.setattr(Config, "ACCESS_GATE_SCOPE", "app")
    for p in paths:
        after = app.test_client().get(p, base_url="https://getomixa.com")
        assert after.status_code == before[p].status_code == 200, p
        assert after.headers.get("X-Robots-Tag") == before[p].headers.get("X-Robots-Tag"), p
        assert after.headers.get("Cache-Control") == before[p].headers.get("Cache-Control"), p
        assert _norm(after.get_data().decode("latin-1")) == _norm(before[p].get_data().decode("latin-1")), p


def test_app_scope_keeps_indexing_signals_on(client, app_scope):
    robots = client.get("/robots.txt").get_data(as_text=True)
    assert "Allow: /" in robots and "Disallow: /\n" not in robots and "Sitemap:" in robots
    assert client.get("/sitemap.xml").status_code == 200
    home = client.get("/").get_data(as_text=True)
    assert 'content="index,follow"' in home and 'rel="canonical"' in home and "application/ld+json" in home
    assert "noindex" not in client.get("/pricing").headers.get("X-Robots-Tag", "")


def test_app_scope_still_locks_the_app_and_every_api(client, app_scope):
    for path in ("/dashboard", "/batch", "/profiles/new", "/sessions/x", "/admin/login", "/login", "/contact"):
        r = client.get(path)
        assert r.status_code == 403 and "noindex" in r.headers["X-Robots-Tag"], path
    for method, path in _rules():
        if path.startswith(("/api/", "/admin/")):
            r = client.open(path, method=method)
            assert r.status_code in (401, 403), (method, path, r.status_code)
    r = client.post("/api/upload/", data={"file": (make_csv(), "t.csv")}, content_type="multipart/form-data")
    assert r.status_code == 401 and r.get_json()["code"] == "access_gate"
    assert list(Path(Config.TEMP_DIR).iterdir()) == []


def test_app_scope_public_pages_are_get_only(client, app_scope):
    assert client.post("/pricing").status_code in (401, 403)


def test_app_scope_team_member_unlocks_as_usual(client, app_scope):
    assert client.post("/__access", data={"password": PASSWORD, "next": "/dashboard"}).status_code == 303
    assert client.get("/dashboard").status_code == 200
    assert client.get("/api/billing/plans").status_code == 200


def test_misconfigured_app_scope_locks_the_app_but_not_the_seo_pages(client, app_scope, monkeypatch):
    monkeypatch.setattr(Config, "ACCESS_GATE_SECRET", "")
    assert client.get("/pricing").status_code == 200
    assert client.get("/dashboard").status_code == 503
    assert client.get("/api/sessions/").status_code == 503
