"""Edge-case datasets through the whole stack, plus API-level integration of the new model."""
import io
import sqlite3

import numpy as np
import pandas as pd
import pytest

from cleaning import model as M
from cleaning.audit import AuditLog, revert
from cleaning.quality_report import generate_report
from cleaning.recommendations import generate_recommendations
from cleaning.rules import apply_rules
from cleaning.summary import build_quality_comparison
from tests.helpers import finding, issues, norm_nulls


def full_run(df):
    """report -> recommendations -> default cleaning with audit -> after report -> revert."""
    before = generate_report(df, project=True)
    rec = generate_recommendations(before)
    audit = AuditLog()
    out, log = apply_rules(df.copy(), audit=audit)
    after = generate_report(out.reset_index(drop=True), provenance={"imputed": log["imputed"]})
    comparison = build_quality_comparison(before, after, log["imputed"])
    return before, rec, out, after, comparison, audit


# ------------------------------------------------------------------- empty / tiny
def test_empty_dataframe():
    r = generate_report(pd.DataFrame(), project=True)
    assert r["score"] == 100 and r["findings"] == [] and r["row_count"] == 0


def test_header_only_dataset_does_not_divide_by_zero():
    df = pd.DataFrame({"a": pd.Series(dtype=object), "email": pd.Series(dtype=object)})
    before, rec, out, after, cmp_, audit = full_run(df)
    assert before["score"] == 100 and out.empty


def test_single_row_dataset():
    before, *_ = full_run(pd.DataFrame({"a": [1], "email": ["bad"]}))
    assert 0 <= before["score"] <= 100


@pytest.mark.parametrize("n", [1, 2, 3, 4])
def test_very_small_datasets_never_produce_critical_unless_the_rule_demands_it(n):
    df = pd.DataFrame({"email": ["bad"] * n, "x": [None] * n})
    for f in generate_report(df)["findings"]:
        if f["issue"] not in ("high_missingness",):
            assert f["level"] != M.CRITICAL, f["issue"]


# ------------------------------------------------------------ duplicate / missing heavy
def test_duplicate_heavy_dataset():
    df = pd.DataFrame({"a": [1, 2] * 50, "b": ["x", "y"] * 50, "c": [5, 6] * 50})
    before, rec, out, after, cmp_, audit = full_run(df)
    f = finding(before, "duplicate_rows")
    assert f["affected_count"] == 98 and f["affected_pct"] == 98.0
    assert len(out) == 2 and audit.summary()["rows_removed"] == 98
    assert after["dimension_scores"]["uniqueness"] > before["dimension_scores"]["uniqueness"]


def test_missing_heavy_dataset_is_flagged_not_filled():
    df = pd.DataFrame({"a": [1] + [None] * 19, "b": range(20)})
    before, rec, out, after, cmp_, audit = full_run(df)
    f = finding(before, "high_missingness", "a")
    assert f["level"] == M.CRITICAL and f["affected_pct"] == 95.0
    assert out["a"].isna().sum() == 19  # too sparse to estimate: left alone


def test_entirely_empty_column():
    df = pd.DataFrame({"a": [None] * 6, "b": range(6)})
    r = generate_report(df)
    assert finding(r, "high_missingness", "a")["affected_pct"] == 100.0


# --------------------------------------------------------------- dates
def test_ambiguous_dates_require_review_and_are_never_auto_rewritten():
    df = pd.DataFrame({"signup_date": ["03/04/2024", "05/06/2024", "01/02/2024", "07/08/2024"]})
    r = generate_report(df)
    f = finding(r, "ambiguous_date_format")
    assert f["remediation"] == M.REQUIRES_REVIEW and f["operation"] == M.INFERENCE
    assert {o["id"] for o in generate_recommendations(r)["ambiguous"][0]["resolution_options"]} >= {"day_first", "month_first"}
    out, _ = apply_rules(df.copy(), rules=["date_standardization"])
    assert out["signup_date"].tolist() == df["signup_date"].tolist()


def test_unambiguous_dates_are_a_safe_deterministic_fix():
    df = pd.DataFrame({"signup_date": ["28/08/2022", "15/06/2024", "31/01/2023", "20/12/2021"]})
    r = generate_report(df)
    f = finding(r, "inconsistent_date_format")
    assert f["remediation"] == M.SAFE_AUTO_FIX and f["operation"] == M.NORMALIZATION
    out, _ = apply_rules(df.copy(), rules=["date_standardization"])
    assert out["signup_date"].tolist() == ["2022-08-28", "2024-06-15", "2023-01-31", "2021-12-20"]


def test_impossible_dates_are_accuracy_findings_requiring_review():
    df = pd.DataFrame({"dob": ["1990-01-05", "1850-01-01", "2999-12-31", "1985-03-02"]})
    f = finding(generate_report(df), "impossible_date")
    assert f["dimension"] == M.ACCURACY and f["affected_count"] == 2 and f["remediation"] == M.REQUIRES_REVIEW


def test_a_future_date_is_fine_for_a_due_date_but_impossible_for_a_birth_date():
    due = pd.DataFrame({"due_date": ["2999-01-01", "2024-01-01", "2024-02-01"]})
    dob = pd.DataFrame({"dob": ["2999-01-01", "2024-01-01", "2024-02-01"]})
    assert finding(generate_report(due), "impossible_date") is None
    assert finding(generate_report(dob), "impossible_date") is not None


def test_stale_records_are_timeliness_only_when_a_last_updated_column_exists():
    stale = pd.DataFrame({"last_updated": ["2019-01-01", "2020-05-05", "2025-12-01", "2018-01-01"]})
    r = generate_report(stale, now=pd.Timestamp("2026-10-04"))
    f = finding(r, "stale_data")
    assert f["dimension"] == M.TIMELINESS and f["remediation"] == M.DO_NOT_MODIFY and f["affected_count"] == 3
    assert r["dimension_scores"]["timeliness"] is not None


# --------------------------------------------------- malformed / mixed / identifiers
def test_malformed_values_in_typed_columns_are_found_not_crashed_on():
    df = pd.DataFrame({
        "email": ["a@b.com", "@@", "x@", "no", "ok@ok.org", "a b@c.com"] * 3,
        "age": [30, -4, 999, 25, 41, 18] * 3,
        "phone": ["0803317157", "12", "abcdefghijk", "+234 80 33", "", "9" * 25] * 3,
    })
    r = generate_report(df)
    assert {"invalid_email_format", "impossible_age", "suspicious_phone_format"} <= issues(r)


def test_mixed_data_types_in_one_column_are_reported_for_review():
    df = pd.DataFrame({"amount_notes": ["12", "13", "abc", "14", "xyz", "15", "16", "oops", "17", "18", "19", "20"]})
    f = finding(generate_report(df), "mixed_data_types")
    assert f and f["remediation"] == M.REQUIRES_REVIEW


def test_codes_like_A45_next_to_123_are_not_flagged_as_mixed_types():
    df = pd.DataFrame({"customer_id": ["123", "A45", "999", "B07", "100", "C11", "5", "D3", "77", "E9", "8", "F1"]})
    assert finding(generate_report(df), "mixed_data_types") is None


def test_leading_zero_identifiers_are_never_treated_as_numbers_or_filled():
    df = pd.DataFrame({"phone_num": ["0803317157", None, "0701234567"], "account_no": ["00012", "00013", None]})
    out, _ = apply_rules(df.copy())
    assert out["phone_num"].tolist()[0] == "0803317157" and out["account_no"].tolist()[0] == "00012"
    assert out["phone_num"].isna().sum() == 1 and out["account_no"].isna().sum() == 1


def test_mixed_dtypes_with_numbers_bools_and_text_do_not_crash_the_engine():
    df = pd.DataFrame({"a": [1, "x", 3.5, None, True, "2024-01-01"], "b": [None] * 6})
    before, rec, out, after, cmp_, audit = full_run(df)
    assert 0 <= before["score"] <= 100


def test_unicode_and_odd_column_names():
    df = pd.DataFrame({"名前": ["Ann", " Bob "], "  weird  NAME!! ": [1, 2], "=cmd": ["x", "y"]})
    before, rec, out, after, cmp_, audit = full_run(df)
    assert len(out.columns) == 3


def test_constant_and_single_value_columns():
    df = pd.DataFrame({"c": ["same"] * 10, "n": range(10)})
    f = finding(generate_report(df), "constant_column")
    assert f and f["level"] == M.LOW


def test_inconsistent_categories_are_a_safe_normalization():
    df = pd.DataFrame({"tier": ["Gold", "gold", "GOLD", "Silver", "silver", "Bronze"] * 4})
    f = finding(generate_report(df), "inconsistent_categories")
    assert f["remediation"] == M.SAFE_AUTO_FIX and f["operation"] == M.NORMALIZATION


# ----------------------------------------------------------------- comparison/verdict
def test_quality_comparison_reports_resolved_remaining_and_unfixable_issues():
    df = pd.DataFrame({
        "phone_num": ["1.343E+12"] + ["0803317157"] * 11,
        "name": ["  a "] * 12, "email": ["x@y.com"] * 8 + ["bad"] * 4,
    })
    before, rec, out, after, cmp_, audit = full_run(df)
    resolved = {i["issue"] for i in cmp_["issues_resolved"]}
    remaining = {i["issue"] for i in cmp_["issues_remaining"]}
    assert "whitespace" in resolved
    assert {"invalid_email_format", "unrecoverable_scientific_notation"} <= remaining
    assert any(i["issue"] == "unrecoverable_scientific_notation" for i in cmp_["still_protected"])
    assert any(i["issue"] == "invalid_email_format" for i in cmp_["still_requires_review"])
    assert cmp_["dimensions"]["consistency"]["after"] > cmp_["dimensions"]["consistency"]["before"]


def test_iso_dates_are_not_mistaken_for_ambiguous_and_are_not_swapped_by_day_first():
    """pandas applied dayfirst=True to ISO strings: 1990-01-05 became 1 May 1990. Pure ISO columns were
    called ambiguous, and choosing 'day first' swapped day and month in every ISO value."""
    from cleaning import detectors
    from cleaning.resolutions import apply_resolutions
    iso = pd.Series(["1990-01-05", "2024-03-04", "2024-12-25"])
    assert detectors.unambiguous_date_parse(iso) is not None
    assert not finding(generate_report(pd.DataFrame({"signup_date": iso})), "ambiguous_date_format")
    def fresh():
        return pd.DataFrame({"signup_date": ["2024-03-04", "03/04/2025", "28/08/2022"]})
    out, _ = apply_resolutions(fresh(), [{"column": "signup_date", "issue": "ambiguous_date_format", "choice": "day_first"}])
    assert out["signup_date"].tolist() == ["2024-03-04", "2025-04-03", "2022-08-28"]
    out, _ = apply_resolutions(fresh(), [{"column": "signup_date", "issue": "ambiguous_date_format", "choice": "month_first"}])
    assert out["signup_date"].tolist() == ["2024-03-04", "2025-03-04", "2022-08-28"]


def test_verdict_says_when_the_headline_is_pinned_by_an_unfixable_critical_issue():
    df = pd.DataFrame({"phone_num": ["1.343E+12"] + ["0803317157"] * 11, "name": ["  a "] * 12})
    *_, cmp_, _ = full_run(df)
    assert cmp_["overall"]["capped_by"] == M.CRITICAL
    assert cmp_["overall"]["verdict"] == "improved_but_capped"
    assert cmp_["overall"]["after_without_cap"] > cmp_["overall"]["before_without_cap"]
    assert any("held down" in n for n in cmp_["notes"])


def test_comparison_calls_out_imputation_as_estimates():
    df = pd.DataFrame({"score": [10, 20, None, 40, None, 60, 70, 80, 90, 100]})
    out, log = apply_rules(df.copy(), rules=["missing_values"])
    cmp_ = build_quality_comparison(generate_report(df), generate_report(out, provenance={"imputed": log["imputed"]}),
                                    log["imputed"])
    assert cmp_["imputed_values"] == 2 and any("estimates" in n for n in cmp_["notes"])


def test_full_run_is_always_reversible_for_every_edge_dataset():
    frames = [
        pd.DataFrame({"a": [1, 2] * 10, "b": ["x", "y"] * 10, "c": [5, 6] * 10}),
        pd.DataFrame({"n": ["  a ", "A", "a", None, "b"], "g": ["M", "f", "male", None, "F"]}),
        pd.DataFrame({"d": ["28/08/2022", "15/06/2024", None, "20/12/2021"], "p": ["+234 80 1", None, "0803", "0701"]}),
    ]
    for df in frames:
        *_, out, after, cmp_, audit = full_run(df)
        back = revert(out, audit)
        back = back[[c for c in df.columns if c in back.columns]]
        assert norm_nulls(back.reset_index(drop=True)).astype(str).equals(norm_nulls(df).astype(str)), df.columns.tolist()


# ------------------------------------------------------------------- API integration
def _upload(client, text, name="d.csv"):
    r = client.post("/api/upload/", data={"file": (io.BytesIO(text.encode()), name)}, content_type="multipart/form-data")
    assert r.status_code == 201, r.get_json()
    return r.get_json()["job_id"]


SAMPLE = ("Full Name,Email Addr,Phone Num,Signup Date,Age\n"
          "Ann Lee,ANN@X.COM,0803317157,28/08/2022,34\n"
          "Bob,bad-email,0701234567,15/06/2024,200\n"
          "Bob,bad-email,0701234567,15/06/2024,200\n"
          "Cy,cy@x.com,1.343E+12,20/12/2021,41\n")


def test_report_endpoint_returns_scorecard_dimensions_and_projection(client):
    job = _upload(client, SAMPLE)
    body = client.get(f"/api/report/{job}").get_json()
    r, rec = body["report"], body["recommendations"]
    sc = r["scorecard"]
    assert {"overall_score", "dimension_scores", "issues_by_severity", "safe_fixes_available",
            "decisions_required", "do_not_modify", "affected"} <= set(sc)
    assert r["findings"] and all("rule_id" in f and "remediation" in f for f in r["findings"])
    assert "expected_after_safe_fixes" in r
    assert rec["summary"][M.DO_NOT_MODIFY] >= 1 and "operation_legend" in rec
    assert "expected_score_after_safe_fixes" in sc


def test_process_endpoint_returns_audit_and_before_after_comparison(client):
    job = _upload(client, SAMPLE)
    body = client.post(f"/api/process/{job}", json={
        "rules": ["column_names", "formatting", "email_cleaning", "duplicates"],
        "resolutions": [{"column": "Age", "issue": "impossible_age", "choice": "blank_invalid"}],
    }).get_json()
    assert body["status"] == "completed"
    s = body["summary"]
    assert s["audit"]["summary"]["user_approved_changes"] >= 1
    assert s["audit"]["summary"]["rows_removed"] == 1
    approvals = {e["approval"] for e in s["audit"]["entries"]}
    assert M.APPROVAL_USER_APPROVED in approvals and M.APPROVAL_USER_SELECTED in approvals
    cmp_ = s["quality_comparison"]
    assert cmp_["overall"]["after"] >= cmp_["overall"]["before"]
    assert set(cmp_["dimensions"]) >= {"completeness", "validity", "uniqueness", "consistency"}
    assert "Dimension scores" in s["cleaning_summary"]["text"]
    for e in s["audit"]["entries"]:
        assert e["examples"] and e["reason"] and e["rule_id"]


def test_processing_does_not_apply_gap_filling_unless_the_user_asks(client):
    job = _upload(client, "name,score\nA,1\nB,\nC,3\nD,\n")
    body = client.post(f"/api/process/{job}", json={"rules": ["formatting"]}).get_json()
    assert body["summary"]["audit"]["summary"]["by_operation"].get("imputation", 0) == 0
    csv = client.get(f"/api/download/{job}").get_data(as_text=True)
    assert "Unknown" not in csv


def test_downloaded_csv_is_free_of_formula_injection(client):
    job = _upload(client, 'name,note\nAnn,"=HYPERLINK(""http://evil"")"\nBob,@SUM(1)\n')
    client.post(f"/api/process/{job}", json={"rules": []})
    csv = client.get(f"/api/download/{job}").get_data(as_text=True)
    assert "\n=" not in csv and ",=" not in csv and ",@" not in csv


def test_db_migration_adds_new_columns_to_an_existing_database(tmp_path):
    import db
    import config
    import pytest as _pt
    old = tmp_path / "old.db"
    con = sqlite3.connect(old)
    con.executescript(db._SCHEMA)   # the schema as shipped before the quality-model columns existed
    con.commit()
    before = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
    con.close()
    assert "dimension_scores_before" not in before
    mp = _pt.MonkeyPatch()
    try:
        mp.setattr(config.Config, "DB_PATH", str(old))
        db.init_db()
        db.init_db()  # idempotent
        after = {r[1] for r in sqlite3.connect(old).execute("PRAGMA table_info(jobs)")}
    finally:
        mp.undo()
    assert {"dimension_scores_before", "dimension_scores_after", "quality_model_version"} <= after


def test_dimension_scores_are_persisted_per_job(client):
    import json, db
    job = _upload(client, SAMPLE)
    client.post(f"/api/process/{job}", json={"rules": ["formatting"]})
    with db._cursor() as cur:
        row = cur.execute("SELECT dimension_scores_before, dimension_scores_after, quality_model_version "
                          "FROM jobs WHERE job_id=?", (job,)).fetchone()
    assert json.loads(row[0])["completeness"] is not None and row[2] == 2
