"""Missing-value handling, column type detection, invalid-value detection and the cleaning summary.

Every test checks the resulting DATA and what was REPORTED about it, using the examples from the spec:
Age: 25, 30, comma, 40, ERROR / Quantity: 2, 5, UNKNOWN, 8 / Transaction Date: 2026-01-15, 2026-02-10, invalid-date.
"""
import io

import pandas as pd

from cleaning import validation as V
from cleaning.audit import AuditLog
from cleaning.rules import apply_rules
from cleaning.type_normalization import normalize_confirmed_types, parse_text_number


def col(report, name):
    return next(c for c in report["columns"] if c["column"] == name)


def _run(client, csv: bytes, body=None):
    job = client.post("/api/upload/", data={"file": (io.BytesIO(csv), "d.csv")},
                      content_type="multipart/form-data").get_json()["job_id"]
    r = client.post(f"/api/process/{job}", json=body or {})
    assert r.status_code == 200, r.get_data(as_text=True)
    return job, r.get_json()["summary"]


# ------------------------------------------------------------------ missing-value detection
def test_missing_markers_and_whitespace_are_detected_in_every_type_of_column():
    df = pd.DataFrame({
        "qty": ["2", "5", "N/A", "  ", None, "NULL", "NA", "None", "8", "9"],
        "city": ["Lagos", "Abuja", "Lagos", "Lagos", "Abuja", "Lagos", "Abuja", "Lagos", "n/a", " "],
    })
    v = V.validate_dataframe(df)
    assert col(v, "qty")["counts"]["missing"] == 6          # N/A, blank, None, NULL, NA, "None"
    assert col(v, "city")["counts"]["missing"] == 2         # n/a, whitespace-only


def test_unknown_is_missing_in_a_numeric_column_but_preserved_in_a_text_column():
    df = pd.DataFrame({"quantity": ["2", "5", "UNKNOWN", "8"],
                       "feedback": ["great", "Unknown", "ok", "late delivery"]})
    cleaned, log = apply_rules(df.copy())
    assert pd.isna(cleaned["quantity"].iloc[2])             # numeric column: a word cannot be a quantity
    assert cleaned["feedback"].tolist()[1] == "Unknown"      # text column: kept exactly as written
    kept = log["details"]["missing_token_normalization"]["weak_markers_preserved"]
    assert "feedback" in kept
    v = V.validate_dataframe(cleaned)
    assert col(v, "feedback")["counts"]["unresolved"] == 1   # and surfaced for a decision, not hidden


def test_error_is_never_treated_as_missing_or_zero():
    df = pd.DataFrame({"Age": ["25", "30", "comma", "40", "ERROR"]})
    cleaned, _ = apply_rules(df.copy())
    assert cleaned["Age"].astype(str).tolist()[2] == "comma"  # not "Comma": invalid values are never "tidied"
    assert "ERROR" in cleaned["Age"].astype(str).tolist()    # preserved exactly
    assert cleaned["Age"].isna().sum() == 0                  # nothing silently blanked
    assert (pd.to_numeric(cleaned["Age"], errors="coerce") == 0).sum() == 0   # nothing turned into 0
    assert len(cleaned) == 5                                 # no row dropped


# ------------------------------------------------------------------ type + invalid detection
def test_spec_examples_detect_type_and_flag_invalid_values():
    df = pd.DataFrame({
        "Age": ["25", "30", "comma", "40", "ERROR"],
        "Quantity": ["2", "5", "UNKNOWN", "8"] + [None],
        "Transaction Date": ["2026-01-15", "2026-02-10", "invalid-date", None, None],
    })
    v = V.validate_dataframe(df)
    age, qty, dt = col(v, "Age"), col(v, "Quantity"), col(v, "Transaction Date")
    assert age["effective_type"] == "numeric" and age["counts"]["invalid"] == 2
    assert set(age["issues"]) == {"not_numeric", "error_marker"}
    assert qty["effective_type"] == "numeric"
    assert qty["counts"]["invalid"] == 0 and qty["counts"]["missing"] == 2   # UNKNOWN + empty: MISSING, not invalid
    assert dt["effective_type"] == "date" and dt["counts"]["invalid"] == 1   # ISO dates are not phone numbers
    assert dt["issues"] == {"unrecognised_date_format": 1, "empty": 2}


def test_error_is_valid_text_in_a_text_column():
    df = pd.DataFrame({"status": ["ok", "ERROR", "ok", "retry", "ok", "ERROR"]})
    assert col(V.validate_dataframe(df), "status")["counts"]["invalid"] == 0


def test_boolean_type_is_detected_and_stray_values_are_invalid():
    df = pd.DataFrame({"opted_in": ["Yes", "no", "Y", "N", "true", "maybe", "yes", "No", "yes", "no"]})
    c = col(V.validate_dataframe(df), "opted_in")
    assert c["effective_type"] == "boolean" and c["counts"]["invalid"] == 1
    flags = pd.DataFrame({"flag": [True, False, True, True]})
    assert col(V.validate_dataframe(flags), "flag")["effective_type"] == "boolean"


def test_gender_style_columns_and_codes_are_not_mistaken_for_booleans():
    df = pd.DataFrame({"gender": ["M", "F", "M", "F", "M", "F"], "code": ["1", "0", "1", "0", "1", "1"]})
    v = V.validate_dataframe(df)
    assert col(v, "gender")["effective_type"] != "boolean"
    assert col(v, "code")["effective_type"] != "boolean"


def test_numeric_stored_as_text_is_reported_and_text_is_never_read_as_a_number():
    df = pd.DataFrame({"Age": ["35", "41", "forty", "29", "50"]})
    c = col(V.validate_dataframe(df, {"column_types": {"Age": "numeric"}}), "Age")
    assert c["numeric_stored_as_text"] == 4 and c["counts"]["invalid"] == 1
    assert parse_text_number("forty") is None and parse_text_number("comma") is None


# ------------------------------------------------------------------ safe conversion (confirmed types only)
def test_confirmed_numeric_column_converts_safe_text_and_preserves_the_rest():
    df = pd.DataFrame({"Age": ["35", "30", "comma", "40", "ERROR", "N/A", "1,200", "3.5"]})
    audit = AuditLog()
    out, rep = normalize_confirmed_types(df.copy(), {"Age": "numeric"}, audit=audit)
    vals = out["Age"].tolist()
    assert vals[0] == 35 and vals[1] == 30 and vals[3] == 40 and vals[6] == 1200 and vals[7] == 3.5
    assert vals[2] == "comma" and vals[4] == "ERROR" and vals[5] == "N/A"       # untouched
    assert len(out) == len(df)
    assert rep["columns"]["Age"]["converted"] == 5
    assert rep["columns"]["Age"]["left_for_review"] == 2                        # comma + ERROR (N/A is a marker)


def test_conversion_never_runs_without_confirmation_or_on_locked_columns():
    df = pd.DataFrame({"Age": ["35", "comma", "40"]})
    out, rep = normalize_confirmed_types(df.copy(), None)
    assert out["Age"].tolist() == ["35", "comma", "40"] and rep["converted_total"] == 0
    out, rep = normalize_confirmed_types(df.copy(), {"Age": "numeric"}, locked={"Age"})
    assert out["Age"].tolist() == ["35", "comma", "40"] and rep["skipped_locked"] == ["Age"]


def test_conversion_does_not_destroy_leading_zeros_or_round_integers():
    df = pd.DataFrame({"Code": ["007", "012", "5"], "Count": ["3.5", "4", "5"]})
    out, _ = normalize_confirmed_types(df.copy(), {"Code": "numeric", "Count": "integer"})
    assert out["Code"].tolist()[:2] == ["007", "012"]                            # identifier-like: not converted
    assert out["Count"].tolist() == ["3.5", 4, 5]                                # 3.5 kept for review, not rounded


# ------------------------------------------------------------------ end to end: summary + export
CSV = (b"Name,Age,Quantity,Transaction Date,Notes\n"
       b"Ann,25,2,2026-01-15,ok\nBob,30,5,2026-02-10,Unknown\nCy,comma,UNKNOWN,invalid-date,fine\n"
       b"Di,40,8,2026-03-01,\nEd,ERROR,3,2026-03-02,late\nFay,35,4,2026-03-03,ok\n")


def test_summary_reports_missing_before_after_types_invalid_and_review(client):
    _, s = _run(client, CSV, {"column_types": {"Age": "numeric"}})
    ds = s["cleaning_summary"]["data_state"]
    mv = ds["missing_values"]
    # before: the empty Notes cell + "UNKNOWN" in Quantity. After: the marker became a real empty cell, so the
    # count is unchanged, and the report says the marker was standardized rather than "fixed".
    assert (mv["before"], mv["after"]) == (2, 2)
    qty = next(r for r in mv["per_column"] if r["column"] == "Quantity")
    assert qty["markers_standardized"] == 1 and qty["filled"] == 0
    assert any(u["column"] == "Notes" for u in mv["unresolved"])                 # remaining gaps are named, not hidden
    types = {c["column"]: c for c in ds["column_types"]}
    assert types["Age"]["type"] == "numeric" and types["Age"]["confirmed_by_user"] is True
    inv = {r["column"]: r for r in ds["invalid_values"]["per_column"]}
    assert inv["Age"]["after"] == 2                                              # comma + ERROR still flagged afterwards
    assert inv["Transaction Date"]["after"] == 1
    assert ds["needs_review"]["total"] >= 3
    assert ds["normalized"]["type_conversions"]["Age"]["converted"] == 4          # 25, 30, 40, 35: text -> number
    counts = s["cleaning_summary"]["counts"]
    assert counts["missing_values_before"] == mv["before"] and counts["missing_values_after"] == mv["after"]
    text = s["cleaning_summary"]["text"]
    assert "Missing values:" in text and "Detected column types:" in text and "requiring manual review" in text


def test_unfixable_values_survive_in_the_downloaded_file(client):
    job, s = _run(client, CSV, {"column_types": {"Age": "numeric"}})
    out = pd.read_csv(io.BytesIO(client.get(f"/api/download/{job}").data), dtype=str, keep_default_na=False)
    age_col = next(c for c in out.columns if c.lower() == "age")
    assert out[age_col].tolist().count("comma") == 1 and out[age_col].tolist().count("ERROR") == 1
    assert len(out) == 6                                                         # no row deleted
    assert s["rows_out"] == 6
    assert "0" not in out[age_col].tolist()                                      # nothing replaced with zero


def test_quality_report_reflects_the_cleaned_data(client):
    _, s = _run(client, CSV, {"column_types": {"Age": "numeric"}})
    after = s["cleaning_summary"]["data_state"]["invalid_values"]["after"]
    assert after == 3                                                            # comma, ERROR, invalid-date
    assert s["validation_after"]["totals"]["invalid"] == after                   # one source of truth
    assert s["quality_report"]["score"] is not None


def test_process_rejects_bad_column_types(client):
    job = client.post("/api/upload/", data={"file": (io.BytesIO(b"a,b\n1,2\n"), "d.csv")},
                      content_type="multipart/form-data").get_json()["job_id"]
    assert client.post(f"/api/process/{job}", json={"column_types": {"a": "wat"}}).status_code == 400
    assert client.post(f"/api/process/{job}", json={"column_types": ["a"]}).status_code == 400
    assert client.post(f"/api/process/{job}", json={"column_types": {"a": "numeric"}}).status_code == 200


def test_default_run_without_column_types_still_works_and_is_backward_compatible(client):
    _, s = _run(client, CSV)
    assert s["type_normalization"] is None
    assert "data_state" in s["cleaning_summary"] and "counts" in s["cleaning_summary"]


# ------------------------------------------------------------------ UI motion layer (static assets)
def test_pages_load_the_motion_stylesheet_and_the_progress_bar(client):
    html = client.get("/").get_data(as_text=True)
    assert "css/motion.css" in html and 'class="omx-progress"' in html
    css = client.get("/static/css/motion.css")
    assert css.status_code == 200
    body = css.get_data(as_text=True)
    assert "prefers-reduced-motion: no-preference" in body and "prefers-reduced-motion: reduce" in body
    # entrance animations must not leave a transform behind (it would re-anchor fixed overlays)
    assert "forwards" not in body.replace("omx-closing .16s ease forwards", "")
    # nothing animated may block clicks on visible content
    assert "pointer-events: none" not in body.split("/* ---- dropdowns")[0]


def test_frontend_sends_confirmed_types_and_offers_boolean():
    import pathlib
    js = pathlib.Path("static/js/clean.js").read_text()
    assert "column_types: confirmedColumnTypes()" in js and "'boolean'" in js
    assert "prefers-reduced-motion" in js


# ------------------------------------------------------------------ generic column names (survey-style files)
def _survey():
    return pd.DataFrame({
        "B": [24, 31, 29, 45, 38, 27, 52, 33, 41],
        "D": ["Nigeria", "Kenya", "Ghana", "Nigeria", "Kenya", "Nigeria", "Eth", "Ghana", "Nigeria"],
        "E": ["Yes", "No", "Yes", "Y", "Unknown", "No", "Yes", "Yes", "Y"],
        "F": ["Y", "N", "1", "TRUE", "0", "No", "Yes", "y", "N"],
        "G": ["April 2, 2024", "02-Apr-2024", "2024/04/03", "2024-04-03", "April 4, 2024", "04-Apr-2024",
              "2024/04/05", "2024-04-06", "April 7, 2024"],
    })


def test_generic_named_yes_no_columns_are_unified_and_strays_are_kept():
    out, _ = apply_rules(_survey())
    assert out["E"].tolist() == ["Yes", "No", "Yes", "Yes", "Unknown", "No", "Yes", "Yes", "Yes"]  # Unknown untouched
    assert out["F"].astype(str).tolist() == ["True", "False", "True", "True", "False", "False", "True", "True", "False"]


def test_generic_dates_with_month_names_become_one_iso_format():
    out, _ = apply_rules(_survey())
    assert out["G"].tolist() == ["2024-04-02", "2024-04-02", "2024-04-03", "2024-04-03", "2024-04-04",
                                 "2024-04-04", "2024-04-05", "2024-04-06", "2024-04-07"]


def test_ambiguous_generic_dates_and_plain_numeric_one_zero_columns_are_not_touched():
    df = pd.DataFrame({"X": ["03/04/2024", "05/06/2024", "07/08/2024", "09/10/2024"],
                       "Y": ["1", "0", "1", "1", "0"][:4]})
    out, _ = apply_rules(df.copy())
    assert out["X"].tolist() == df["X"].tolist()          # day/month ambiguous: left for a human
    assert out["Y"].astype(str).tolist() == ["1", "0", "1", "1"]   # no yes/no words: not a boolean column


def test_a_generically_named_country_column_flags_the_unknown_country():
    from cleaning.quality_report import generate_report
    out, _ = apply_rules(_survey())
    assert out["D"].tolist()[6] == "Eth"                   # never guessed
    assert ("unrecognized_country_value", "D") in [(f["issue"], f["column"]) for f in generate_report(out)["findings"]]
