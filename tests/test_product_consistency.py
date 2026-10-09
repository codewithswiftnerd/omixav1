"""The product copy must describe what the backend does (limits, plans, accounts, batch)."""
import re
import time

import pytest

from accounts import auth as auth_mod
from accounts import store as store_mod
from cleaning.rules import DEFAULT_RULES, RULE_DISPATCH, apply_rules
from config import Config
import pandas as pd


def _text(client, path):
    r = client.get(path)
    assert r.status_code == 200
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.get_data(as_text=True)))


def test_landing_limits_match_config(client):
    t = _text(client, "/")
    assert f"{Config.FREE_MAX_UPLOAD_MB} MB on Free" in t and f"{Config.PRO_MAX_UPLOAD_MB} MB with Omixa Pro" in t
    assert f"{Config.BATCH_MAX_FILES} files per batch" in t
    for fmt in ("CSV", "XLSX", "XLS"):
        assert fmt in t
    assert Config.ALLOWED_EXTENSIONS == {"csv", "xlsx", "xls"}


def test_faq_separates_free_and_account_features(client):
    t = _text(client, "/")
    assert "Free cleaning: no." in t and "Omixa Pro: yes." in t
    for feature in ("batch processing", "Quality Profiles", "processing history", "(PDF) reports"):
        assert feature in t
    assert "No. Upload a file and clean it straight away." not in t  # the old, incomplete answer


def test_no_false_privacy_claims_anywhere(client):
    for path in ("/", "/pricing", "/login", "/batch"):
        t = _text(client, path).lower()
        assert "never stored" not in t and "deleted right after you download" not in t
    assert f"{max(1, Config.JOB_TTL_SECONDS // 60)} minutes" in _text(client, "/")


def test_pricing_matches_entitlements(client):
    t = _text(client, "/pricing")
    assert f"{Config.PRO_MAX_UPLOAD_MB} MB" in t and f"{Config.FREE_MAX_UPLOAD_MB} MB" in t
    free, pro = t.split("Pro Monthly")[0], t.split("Pro Monthly")[1]
    assert "Batch processing" in pro and "Batch processing" not in free
    assert "one file at a time" in free


def test_free_and_anonymous_get_upgrade_state_not_upload_ui(app, client, monkeypatch):
    monkeypatch.setattr(Config, "PROCESSING_MODE", "queue")
    html = client.get("/batch").get_data(as_text=True)
    assert "Batch processing is available with Omixa Pro." in html
    assert 'id="batchFiles"' not in html and "batch.js" not in html
    store = store_mod.MemoryStore()
    store_mod.set_store_for_tests(store)
    auth_mod.set_verifier_for_tests(lambda t: {"uid": t, "email": f"{t}@x.com", "name": t})
    try:
        c = app.test_client()
        c.post("/api/auth/session", json={"idToken": "free-user"})
        html = c.get("/batch").get_data(as_text=True)
        assert 'id="batchFiles"' not in html and "available with Omixa Pro" in html
        store.upsert_user("free-user", {"plan": "pro_monthly", "subscription_status": "active",
                                        "subscription_expires": time.time() + 999, "subscription_start": time.time()})
        html = c.get("/batch").get_data(as_text=True)
        assert 'id="batchFiles"' in html and f'data-max-files="{Config.BATCH_MAX_FILES}"' in html
    finally:
        store_mod.set_store_for_tests(None)


def test_batch_limits_endpoint_matches_config(client):
    lim = client.get("/api/batches/limits").get_json()
    assert lim["max_files"] == Config.BATCH_MAX_FILES and lim["formats"] == ["csv", "xls", "xlsx"]
    assert lim["max_total_mb"] == Config.BATCH_MAX_TOTAL_MB


def test_single_upload_error_uses_configured_pro_limit(client, monkeypatch):
    import io
    monkeypatch.setattr(Config, "FREE_MAX_UPLOAD_MB", 1)
    monkeypatch.setattr(Config, "PRO_MAX_UPLOAD_MB", 7)
    big = b"a,b\n" + b"1234567,89\n" * 120000
    r = client.post("/api/upload/", data={"file": (io.BytesIO(big), "b.csv")}, content_type="multipart/form-data")
    assert r.status_code == 402 and "7 MB" in r.get_json()["error"]


# ------------------------------------------------------------------ cleaning-rule policy

def test_column_names_are_not_renamed_by_default():
    assert "column_names" not in DEFAULT_RULES and "column_names" in RULE_DISPATCH
    df = pd.DataFrame({"Customer Name": ["Ada"], "Date of Birth": ["1990-01-02"]})
    out, _ = apply_rules(df.copy())
    assert list(out.columns) == ["Customer Name", "Date of Birth"]
    out2, _ = apply_rules(df.copy(), rules=["column_names"])
    assert list(out2.columns) == ["customer_name", "date_of_birth"]
    from cleaning.rules import handle_column_names
    details = {}
    handle_column_names(df.copy(), details)
    assert details["column_names"]["renamed"] == {"Customer Name": "customer_name", "Date of Birth": "date_of_birth"}  # reversible


def test_identifier_columns_keep_leading_zeros_and_are_not_reformatted():
    df = pd.DataFrame({"account_number": ["000123", "000456"], "phone": ["0803317157", "0803317158"]})
    out, _ = apply_rules(df.copy())
    assert list(out["account_number"]) == ["000123", "000456"]
    assert list(out["phone"]) == ["0803317157", "0803317158"]


def test_ambiguous_dates_are_not_silently_rewritten():
    df = pd.DataFrame({"d": ["01/02/2024", "03/04/2024", "05/06/2024"]})
    out, _ = apply_rules(df.copy())
    assert list(out["d"]) == list(df["d"])


def test_duplicates_only_removes_exact_duplicate_rows():
    df = pd.DataFrame({"n": ["a", "a", "b"], "v": [1, 1, 2]})
    out, _ = apply_rules(df.copy(), rules=["duplicates"])
    assert len(out) == 2
    df2 = pd.DataFrame({"n": ["a", "a"], "v": [1, 2]})
    assert len(apply_rules(df2.copy(), rules=["duplicates"])[0]) == 2


def test_email_and_boolean_rules_do_not_touch_non_matching_columns():
    df = pd.DataFrame({"notes": ["Yes sir", "NO WAY"], "email": [" Ada@X.com ", "bad"]})
    out, _ = apply_rules(df.copy())
    assert list(out["notes"]) == ["Yes sir", "NO WAY"]
    assert out["email"].iloc[0] == "ada@x.com" and out["email"].iloc[1] == "bad"


def test_unchecking_every_rule_changes_nothing():
    df = pd.DataFrame({"A b": [" x ", " x "]})
    out, _ = apply_rules(df.copy(), rules=[])
    assert out.equals(df)


def test_production_queue_mode_refuses_local_storage(monkeypatch):
    import app as app_module
    monkeypatch.setattr(Config, "IS_PRODUCTION", True)
    monkeypatch.setattr(Config, "PROCESSING_MODE", "queue")
    monkeypatch.setattr(Config, "STORAGE_BACKEND", "local")
    monkeypatch.setattr(Config, "DATABASE_URL", "")
    monkeypatch.delenv("OMIXA_ALLOW_SINGLE_NODE", raising=False)
    with pytest.raises(RuntimeError, match="shared services"):
        app_module.create_app()
    monkeypatch.setenv("OMIXA_ALLOW_SINGLE_NODE", "1")
    app_module.create_app()  # explicit single-node override still boots
