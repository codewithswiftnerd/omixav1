"""
Security-fix regression tests.

Covers:
  - export/exporter.sanitize_formula_injection (CSV/Excel formula
    injection mitigation)
  - utils/security.check_rate_limit / check_login_rate_limit (the
    rate-limiting gate wired into app.py's before_request and
    routes/admin.py's login view)

The old X-API-Key auth model these tests used to cover is gone
entirely — see utils/session.py and tests/test_sessions_and_jobs.py,
tests/test_admin.py for its replacement (signed session cookies +
per-job ownership + admin login), which need the full app (and so
live under tests/, not here).

Run with:
    pip install pytest --break-system-packages   # or in a venv
    pytest testing/test_omixa_security.py -v
"""

import os

import pandas as pd
import pytest
from flask import Flask, jsonify

from export.exporter import sanitize_formula_injection


# --------------------------------------------------------------------
# Formula injection (CSV/Excel export)
# --------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    "=cmd|'/c calc'!A1",
    "+1+1",
    "-5 apples",
    "@SUM(A1)",
    "\ttabbed",
])
def test_formula_trigger_chars_get_quote_prefixed(payload):
    df = pd.DataFrame({"col": [payload, "safe"]})
    out = sanitize_formula_injection(df)
    assert out["col"].iloc[0] == "'" + payload
    assert out["col"].iloc[1] == "safe"


def test_safe_text_with_internal_dash_untouched():
    # Only a LEADING trigger character is dangerous, "5-10 units"
    # doesn't start with one, so it must pass through unchanged.
    df = pd.DataFrame({"col": ["5-10 units", "call 555-1234"]})
    out = sanitize_formula_injection(df)
    assert list(out["col"]) == ["5-10 units", "call 555-1234"]


def test_numeric_and_null_values_untouched():
    df = pd.DataFrame({"amount": [1, -5, 3], "note": ["x", None, "y"]})
    out = sanitize_formula_injection(df)
    assert list(out["amount"]) == [1, -5, 3]  # genuine negative numbers, not text, never touched
    assert pd.isna(out["note"].iloc[1])


def test_sanitization_runs_automatically_on_export(tmp_path):
    from export.exporter import export_dataframe
    df = pd.DataFrame({"col": ["=2+2", "safe"]})
    out_path = tmp_path / "out.csv"
    export_dataframe(df, str(out_path), ext="csv")
    content = out_path.read_text()
    assert "'=2+2" in content
    assert content.count("=2+2") == 1  # only the defused, quote-prefixed form appears


# --------------------------------------------------------------------
# Rate limit gate
# --------------------------------------------------------------------

def _build_test_app(monkeypatch, rate_limit=0):
    """A minimal Flask app wired the same way app.py wires
    utils/security.check_rate_limit, without needing the full app
    (sessions/admin/etc.) — see tests/ for those."""
    # Patch the live Config class (undone automatically by monkeypatch). Do NOT
    # importlib.reload(config)/reload(utils.security) here: that swaps the
    # Config class for every module that imported it earlier and leaks the
    # tiny test limits into every later test in the same process, which made
    # all of tests/ fail with 429s when run after this file.
    import utils.security as security
    from config import Config
    monkeypatch.setattr(Config, "RATE_LIMIT_PER_MINUTE", rate_limit)
    security._hits.clear()

    app = Flask(__name__)

    @app.before_request
    def _gate():
        return security.check_rate_limit()

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok"})

    @app.get("/api/report/<job_id>")
    def report(job_id):
        return jsonify({"job_id": job_id}), 200

    return app


def test_rate_limit_blocks_after_threshold(monkeypatch):
    app = _build_test_app(monkeypatch, rate_limit=2)
    client = app.test_client()
    assert client.get("/api/report/abc").status_code == 200
    assert client.get("/api/report/abc").status_code == 200
    r = client.get("/api/report/abc")
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_rate_limit_disabled_when_zero(monkeypatch):
    app = _build_test_app(monkeypatch, rate_limit=0)
    client = app.test_client()
    for _ in range(10):
        assert client.get("/api/report/abc").status_code == 200


def test_health_check_exempt_from_rate_limit(monkeypatch):
    app = _build_test_app(monkeypatch, rate_limit=1)
    client = app.test_client()
    for _ in range(5):
        assert client.get("/api/health").status_code == 200


def test_rate_limit_buckets_are_independent_per_namespace(monkeypatch):
    """check_rate_limit (general /api/*) and check_login_rate_limit
    (admin login) must not share one counter — see
    utils/security.py's _check_window namespacing note — otherwise
    ordinary API traffic could lock an operator out of the admin
    login form, or vice versa."""
    import utils.security as security
    from config import Config
    monkeypatch.setattr(Config, "RATE_LIMIT_PER_MINUTE", 1)
    monkeypatch.setattr(Config, "LOGIN_RATE_LIMIT_PER_MINUTE", 1)
    security._hits.clear()

    app = Flask(__name__)

    @app.before_request
    def _gate():
        return security.check_rate_limit()

    @app.get("/api/report/<job_id>")
    def report(job_id):
        return jsonify({"ok": True})

    @app.get("/login-probe")
    def login_probe():
        limited = security.check_login_rate_limit()
        if limited:
            return limited
        return jsonify({"ok": True})

    client = app.test_client()
    assert client.get("/api/report/abc").status_code == 200
    assert client.get("/api/report/abc").status_code == 429  # api bucket now exhausted

    # the login bucket is untouched by the api bucket being exhausted
    assert client.get("/login-probe").status_code == 200
    assert client.get("/login-probe").status_code == 429

