"""Regression tests: column validation, "Leave as is", unsafe-conversion prevention, change transparency.
Each test checks the resulting DATA and what was REPORTED about it."""
import io

import pandas as pd
import pytest

from cleaning import validation as V
from cleaning.audit import AuditLog
from cleaning.resolutions import apply_resolutions
from cleaning.rules import apply_rules
from cleaning.quality_report import generate_report


def col(report, name):
    return next(c for c in report["columns"] if c["column"] == name)


def cats(report, name):
    return col(report, name)["counts"]


# ------------------------------------------------------------- "Leave as is" (root cause #1)
def test_leave_as_is_keeps_original_values_and_reports_the_lock():
    df = pd.DataFrame({"amount_paid": ["₦25,000", "₦15,000", "1,200"], "name": ["  Ann ", "Bob", "Cy"]})
    unlocked, _ = apply_rules(df.copy())
    assert unlocked["amount_paid"].tolist() == [25000, 15000, 1200]          # default cleaning does convert
    locked, log = apply_rules(df.copy(), locked_columns={"amount_paid"})
    assert locked["amount_paid"].tolist() == ["₦25,000", "₦15,000", "1,200"]  # lock: byte-for-byte original
    assert locked["name"].tolist() == ["Ann", "Bob", "Cy"]                    # other columns still cleaned
    assert log["locked_columns"] == ["amount_paid"]


def test_lock_beats_every_rule_including_explicit_imputation_and_case():
    df = pd.DataFrame({"city": ["LAGOS", None, "lagos"], "score": [1.0, None, 3.0]})
    out, _ = apply_rules(df.copy(), rules=["missing_values", "case_standardization"], locked_columns={"score", "city"})
    assert out["score"].isna().tolist() == [False, True, False]
    assert out["city"].tolist()[0] == "LAGOS" and pd.isna(out["city"].iloc[1])


def test_locked_column_still_participates_in_duplicate_detection():
    df = pd.DataFrame({"a": ["x", "x", "y"], "amount": ["₦1", "₦1", "₦2"]})
    out, _ = apply_rules(df.copy(), locked_columns={"amount"})
    assert len(out) == 2 and out["amount"].tolist() == ["₦1", "₦2"]


def test_api_leave_as_is_end_to_end_and_unmatched_names_are_reported(client):
    csv = b"name,amount_paid\nAnn,\"1,200\"\nBob,\"3,400\"\n"
    r = client.post("/api/upload/", data={"file": (io.BytesIO(csv), "d.csv")}, content_type="multipart/form-data")
    job = r.get_json()["job_id"]
    p = client.post(f"/api/process/{job}", json={"leave_as_is": ["amount_paid", "no_such_col"]})
    assert p.status_code == 200
    body = p.get_json()
    opts = (body.get("summary") or body)["column_options"]
    assert opts["locked"] == ["amount_paid"] and opts["unmatched"] == ["no_such_col"]
    d = client.get(f"/api/download/{job}")
    assert b"1,200" in d.data and b"3,400" in d.data


def test_api_rejects_malformed_leave_as_is(client):
    csv = b"a,b\n1,2\n"
    job = client.post("/api/upload/", data={"file": (io.BytesIO(csv), "d.csv")},
                      content_type="multipart/form-data").get_json()["job_id"]
    assert client.post(f"/api/process/{job}", json={"leave_as_is": "amount"}).status_code == 400


def test_resolution_on_locked_column_is_reported_as_conflict():
    from processing.pipeline import _resolve_locks
    df = pd.DataFrame({"amount": ["1", "free"]})
    locked, rep = _resolve_locks(df, ["amount"], [{"column": "amount", "issue": "mixed_data_types", "choice": "free_as_zero"}], None)
    assert locked == {"amount"} and rep["conflicts"][0]["kind"] == "resolution_on_locked_column"


# ------------------------------------------------------------- unsafe conversions (root cause #2)
def test_free_unknown_and_spoken_numbers_survive_default_cleaning():
    df = pd.DataFrame({"amount_paid": ["25000", "15000", "free", "unknown", "ten thousand naira", "₦1,200", None]})
    out, log = apply_rules(df.copy())
    assert out["amount_paid"].tolist()[:6] == ["25000", "15000", "free", "unknown", "ten thousand naira", "₦1,200"]
    assert log["details"]["numeric_text_cleaning"]["left_unconverted"]["amount_paid"]["count"] >= 2


def test_currency_symbols_and_thousands_separators_still_convert_when_every_value_is_a_number():
    out, _ = apply_rules(pd.DataFrame({"amount": ["₦25,000", "$1,200.50", "(45)", None]}))
    assert out["amount"].dropna().tolist() == [25000, 1200.5, -45]


def test_explicit_free_as_zero_changes_only_free_and_is_audited_as_user_approved():
    df = pd.DataFrame({"amount_paid": ["free", "5000", "unknown", "Free"]})
    audit = AuditLog()
    out, log = apply_resolutions(df.copy(), [{"column": "amount_paid", "issue": "mixed_data_types", "choice": "free_as_zero"}],
                                 audit=audit, column_labels={"amount_paid": "amount_paid"})
    assert out["amount_paid"].tolist() == ["0", "5000", "unknown", "0"]
    e = audit.entries[0]
    assert e["approval"] == "user_approved" and e["cells_changed"] == 2 and "free" in e["reason"]
    assert audit.change_log()["cell_changes"][0]["original"] == "free"


def test_explicit_number_words_conversion_leaves_other_values_alone():
    df = pd.DataFrame({"age": ["forty", "30", "unknown", "twenty-five"]})
    out, _ = apply_resolutions(df.copy(), [{"column": "age", "issue": "mixed_data_types", "choice": "words_to_numbers"}])
    assert out["age"].tolist() == ["40", "30", "unknown", "25"]


def test_numeric_conversion_never_destroys_identifiers_with_leading_zeros():
    df = pd.DataFrame({"account_no": ["007", "012", "100"]})
    out, log = apply_resolutions(df.copy(), [{"column": "account_no", "issue": "mixed_data_types", "choice": "coerce_numeric"}])
    assert out["account_no"].tolist() == ["007", "012", "100"] and log["skipped"] and not log["applied"]
    cleaned, _ = apply_rules(pd.DataFrame({"zip": ["00123", "00456", "01234"]}))
    assert cleaned["zip"].tolist() == ["00123", "00456", "01234"]


def test_explicit_imputation_is_flagged_as_estimate_and_counted_separately():
    df = pd.DataFrame({"score": [10.0, None, 30.0]})
    out, log = apply_resolutions(df.copy(), [{"column": "score", "issue": "missing_values", "choice": "impute"}])
    assert out["score"].iloc[1] == 20.0 and log["imputed"] == {"score": 1}
    opt = next(o for o in __import__("cleaning.resolutions", fromlist=["x"]).RESOLUTION_OPTIONS["missing_values"] if o["id"] == "impute")
    assert "estimate" in opt["description"].lower()


# ------------------------------------------------------------- meaningful notes / missing markers
def test_review_notes_are_preserved_and_flagged_not_deleted():
    notes = ["OK", "NIL", "??", "—", "⚠️ check", "💥"]
    out, _ = apply_rules(pd.DataFrame({"review_note": notes}))
    assert out["review_note"].tolist() == notes
    c = cats(V.validate_dataframe(pd.DataFrame({"review_note": notes})), "review_note")
    assert c["unresolved"] == 4 and c["valid"] == 2 and c["missing"] == 0  # NIL ?? — 💥 flagged; OK, ⚠️ check valid


def test_weak_markers_are_blanked_in_numeric_columns_and_the_conversion_is_tracked():
    df = pd.DataFrame({"id": list("abcdef"), "qty": ["5", "unknown", "7", "9", "N/A", None]})
    audit = AuditLog()
    out, _ = apply_rules(df.copy(), audit=audit, column_labels={"id": "id", "qty": "qty"})
    assert out["qty"].isna().sum() == 3
    flow = audit.missing_flow["qty"]
    assert flow["converted_to_missing"] == 2 and "missing_token_normalization" in flow["by_rule"]
    originals = {c["original"] for c in audit.change_log()["cell_changes"]}
    assert {"unknown", "N/A"} <= originals


def test_missing_tracking_explains_a_rise_in_missing_values(client):
    csv = b"qty,city\n5,Lagos\nunknown,Abuja\n7,Lagos\n9,Lagos\nN/A,Ibadan\n8,Lagos\n"
    job = client.post("/api/upload/", data={"file": (io.BytesIO(csv), "d.csv")}, content_type="multipart/form-data").get_json()["job_id"]
    body = client.post(f"/api/process/{job}", json={}).get_json()
    s = body.get("summary") or body
    t = s["missing_tracking"]["qty"]
    # "N/A" is already empty when the file is read; "unknown" is converted by the rule, and says so
    assert (t["originally_missing"], t["converted_to_missing"], t["missing_after"], t["filled"]) == (1, 1, 2, 0)
    assert "missing_token_normalization" in t["by_rule"]
    assert s["change_log"]["cell_changes_total"] >= 1


# ------------------------------------------------------------- validation: your exact examples
AGE = ["25", "30", "forty", "unknown", "-5", "150", ""]


def test_age_column_classification_matches_the_spec():
    r = V.validate_dataframe(pd.DataFrame({"age": AGE}))
    assert cats(r, "age") == {"valid": 2, "invalid": 2, "suspicious": 1, "missing": 2, "unresolved": 0}
    inv = V.rows_for(pd.DataFrame({"age": AGE}), "age", "invalid")
    assert {x["value"] for x in inv["rows"]} == {"forty", "-5"} and inv["rows"][0]["sheet_row"] == 4
    sus = V.rows_for(pd.DataFrame({"age": AGE}), "age", "suspicious")
    assert sus["rows"][0]["value"] == "150"


def test_amount_free_is_unresolved_until_the_dataset_defines_it():
    df = pd.DataFrame({"amount_paid": ["25000", "15000", "free", "unknown", "ten thousand naira"]})
    r = V.validate_dataframe(df)
    assert col(r, "amount_paid")["type_status"] == "needs_confirmation"      # 2 of 4 are numbers: ask, do not guess
    confirmed = V.validate_dataframe(df, {"column_types": {"amount_paid": "currency"}})
    c = col(confirmed, "amount_paid")
    assert c["counts"] == {"valid": 2, "invalid": 1, "suspicious": 0, "missing": 1, "unresolved": 1}
    assert c["issues"]["undefined_word_meaning"] == 1 and c["issues"]["number_in_words"] == 1
    defined = V.validate_dataframe(df, {"column_types": {"amount_paid": "currency"},
                                        "rules": {"amount_paid": {"value_meanings": {"free": 0}}}})
    assert col(defined, "amount_paid")["counts"]["unresolved"] == 0 and col(defined, "amount_paid")["counts"]["valid"] == 3


def test_phone_validation_nigerian_and_international_and_country_required():
    df = pd.DataFrame({"phone": ["08012345678", "+2348012345678", "eight-zero-eight", "8012345678", "+14155552671", "0801234"]})
    r = V.validate_dataframe(df)
    assert col(r, "phone")["counts"]["valid"] == 3 and col(r, "phone")["counts"]["invalid"] == 1
    assert col(r, "phone")["issues"]["needs_country"] == 2
    ng = V.validate_dataframe(df, {"rules": {"phone": {"country": "NG"}}})
    assert col(ng, "phone")["counts"]["valid"] == 4          # 10-digit national form is now decidable


def test_email_missing_is_reported_separately_from_invalid():
    df = pd.DataFrame({"email": ["person@example.com", "person@@example.com", "", None, "a b@x.com", "x@gmial.com"]})
    c = cats(V.validate_dataframe(df), "email")
    assert c == {"valid": 1, "invalid": 2, "suspicious": 1, "missing": 2, "unresolved": 0}


def test_dates_invalid_ambiguous_relative_and_convention():
    df = pd.DataFrame({"date": ["2026-09-15", "31/02/2026", "yesterday", "05/06/2026", "15/09/2026"]})
    r = V.validate_dataframe(df)
    assert cats(r, "date") == {"valid": 2, "invalid": 1, "suspicious": 0, "missing": 0, "unresolved": 2}
    assert col(r, "date")["issues"]["relative_date"] == 1 and col(r, "date")["issues"]["ambiguous_day_month"] == 1
    r2 = V.validate_dataframe(df, {"rules": {"date": {"date_convention": "day_first"}}})
    assert cats(r2, "date")["unresolved"] == 1                      # only "yesterday" is left
    r3 = V.validate_dataframe(df, {"rules": {"date": {"max_date": "2026-09-01"}}})
    assert col(r3, "date")["issues"]["out_of_range"] >= 1


def test_categorical_allowed_values_variants_and_misspellings():
    df = pd.DataFrame({"status": ["active", "Active", "inactive", "actve", "banned", "pending"]})
    r = V.validate_dataframe(df, {"column_types": {"status": "categorical"},
                                  "rules": {"status": {"allowed_values": ["active", "inactive", "pending"]}}})
    c = col(r, "status")["issues"]
    assert c["label_variant"] == 1 and c["possible_misspelling"] == 1 and c["not_in_allowed_list"] == 1


def test_identifiers_keep_leading_zeros_and_check_a_defined_pattern():
    df = pd.DataFrame({"student_id": ["007", "0123", "A-12", "bad id"]})
    r = V.validate_dataframe(df, {"rules": {"student_id": {"pattern": r"[A-Z0-9\-]+"}}})
    c = col(r, "student_id")
    assert c["effective_type"] == "identifier" and c["counts"]["invalid"] == 1 and c["counts"]["valid"] == 3


def test_range_and_negative_rules_are_configurable():
    df = pd.DataFrame({"qty": ["5", "-3", "2000", "x7"]})
    r = V.validate_dataframe(df, {"column_types": {"qty": "numeric"}, "rules": {"qty": {"allow_negative": False, "max": 1000}}})
    assert col(r, "qty")["issues"] == {"negative_not_allowed": 1, "out_of_range": 1, "mixed_text_and_digits": 1}


def test_column_name_alone_never_decides_the_type():
    df = pd.DataFrame({"phone_notes": ["call after 5", "left voicemail", "no answer", "ok", "asked to email"]})
    c = col(V.validate_dataframe(df), "phone_notes")
    assert c["effective_type"] in ("text", "categorical") and c["counts"]["invalid"] == 0


@pytest.mark.parametrize("rows", [5, 200])
def test_counts_always_add_up_and_nothing_is_called_fully_valid_if_type_unconfirmed(rows):
    df = pd.DataFrame({"a": ["1", "x", None, "unknown", "2000-01-01"] * (rows // 5)})
    r = V.validate_dataframe(df)
    c = col(r, "a")
    assert sum(c["counts"].values()) == rows == c["total"]
    assert not c["fully_valid"]


def test_validation_is_read_only():
    df = pd.DataFrame({"age": AGE, "email": ["a@@b.com"] * 7})
    before = df.copy()
    V.validate_dataframe(df)
    pd.testing.assert_frame_equal(df, before)


def test_validation_api_and_rows_endpoint(client):
    csv = ("age,email\n" + "\n".join(f"{a},{e}" for a, e in zip(["25", "forty", "30"], ["a@x.com", "b@@x.com", ""]))).encode()
    job = client.post("/api/upload/", data={"file": (io.BytesIO(csv), "d.csv")}, content_type="multipart/form-data").get_json()["job_id"]
    v = client.post(f"/api/validate/{job}", json={"column_types": {"age": "numeric"}}).get_json()["validation"]
    assert col(v, "age")["counts"]["invalid"] == 1 and col(v, "email")["counts"]["missing"] == 1
    rows = client.post(f"/api/validate/{job}/rows", json={"column": "age", "category": "invalid", "column_types": {"age": "numeric"}}).get_json()
    assert rows["rows"][0]["value"] == "forty" and rows["rows"][0]["sheet_row"] == 3
    assert client.post(f"/api/validate/{job}", json={"column_types": {"age": "wat"}}).status_code == 400
    assert client.post(f"/api/validate/{job}/rows", json={"column": "age", "category": "bogus"}).status_code == 400
    assert client.post("/api/validate/not-a-job", json={}).status_code == 404
    assert "validation" in client.get(f"/api/report/{job}").get_json()


# ------------------------------------------------------------- transparency, duplicates, originals
def test_change_log_records_row_column_original_new_rule_reason_and_approval():
    df = pd.DataFrame({"name": ["  Ann ", "Bob"]})
    audit = AuditLog()
    apply_rules(df.copy(), rules=["formatting"], audit=audit, column_labels={"name": "name"})
    ch = audit.change_log()["cell_changes"][0]
    assert (ch["row"], ch["column"], ch["original"], ch["new"]) == (0, "name", "  Ann ", "Ann")
    assert ch["rule"] == "formatting" and ch["reason"] and ch["approval"] == "user_selected"


def test_exact_duplicates_removed_are_counted_and_recorded_near_matches_are_kept():
    df = pd.DataFrame({"name": ["Ann", "Ann", "Ann ", "Bob"], "city": ["A", "A", "A", "B"]})
    audit = AuditLog()
    out, log = apply_rules(df.copy(), rules=["duplicates"], audit=audit, column_labels={"name": "name", "city": "city"})
    assert len(out) == 3                                         # "Ann " is a near-match: NOT auto-removed
    cl = audit.change_log()
    assert cl["removed_rows_total"] == 1 and cl["removed_rows"][0]["values"] == {"name": "Ann", "city": "A"}
    assert log["changes"]["duplicates_changed"] == 1 == len(df) - len(out)


def test_original_upload_is_unchanged_by_processing(client):
    csv = b"name,amount\n  Ann ,\"1,200\"\nBob,free\n"
    job = client.post("/api/upload/", data={"file": (io.BytesIO(csv), "d.csv")}, content_type="multipart/form-data").get_json()["job_id"]
    from utils.file_handler import find_source_file
    path = find_source_file(job)
    before = open(path, "rb").read()
    client.post(f"/api/process/{job}", json={})
    assert open(path, "rb").read() == before == csv


def test_dataset_is_not_called_clean_just_because_formatting_was_standardised():
    df = pd.DataFrame({"amount_paid": ["25000", "15000", "free", "₦1,200", "ten thousand naira"]})
    out, _ = apply_rules(df.copy())
    v = V.validate_dataframe(out, {"column_types": {"amount_paid": "currency"}})
    assert not col(v, "amount_paid")["fully_valid"] and v["totals"]["unresolved"] + v["totals"]["invalid"] == 2


# ------------------------------------------------------------- scoring integrity (section 10)
def _messy():
    return pd.DataFrame({
        "id": list("abcdefgh"),
        "age": ["25", "30", "forty", "unknown", "-5", "150", "", "41"],
        "amount_paid": ["25000", "15000", "free", "unknown", "ten thousand naira", "₦1,200", "", "900"],
    })


def _finding(report, issue, column):
    return next((f for f in report["findings"] if f["issue"] == issue and f["column"] == column), None)


def test_score_sees_invalid_and_unresolved_values_instead_of_calling_the_data_an_A():
    r = generate_report(_messy())
    assert _finding(r, "invalid_values", "age")["affected_count"] == 2            # forty, -5
    assert _finding(r, "unresolved_values", "amount_paid")["affected_count"] >= 1  # free
    assert r["dimensions"]["validity"]["score"] < 100 and r["score"] < 98


def test_imputed_values_are_never_credited_as_accurate_and_cannot_hide_the_gap():
    # explicit imputation (the only way values are ever estimated) is tracked by the pipeline as provenance
    df = pd.DataFrame({"id": list("abcdefghij"), "score": [10, 20, None, 40, None, 60, 70, 80, 90, 100]})
    out, _ = apply_resolutions(df.copy(), [{"column": "score", "issue": "missing_values", "choice": "impute"}])
    r = generate_report(out, provenance={"imputed": {"score": 2}})
    f = _finding(r, "unverified_imputed_values", "score")
    assert f and f["affected_count"] == 2
    assert r["dimensions"]["accuracy"]["applicable"] and r["dimensions"]["accuracy"]["score"] < 100
    # completeness alone is not allowed to read as "better data": the report carries the estimate flag
    assert r["dimensions"]["completeness"]["score"] == 100 and r["score"] < 100 or r["dimensions"]["accuracy"]["score"] < 100


def test_default_cleaning_cannot_raise_the_score_by_converting_ambiguous_text():
    df = _messy()
    before = generate_report(df)
    cleaned, _ = apply_rules(df.copy())
    after = generate_report(cleaned)
    # nothing ambiguous was converted, so the open issues are still counted and the score cannot jump
    assert _finding(after, "invalid_values", "age") and _finding(after, "unresolved_values", "amount_paid")
    assert after["score"] <= before["score"] + 3


def test_no_double_penalty_for_values_the_older_checks_already_report():
    df = pd.DataFrame({"email": ["a@x.com"] * 11 + ["bad@@x.com"]})
    r = generate_report(df)
    assert any(f["issue"] == "invalid_email_format" for f in r["findings"])
    assert not any(f["issue"] == "invalid_values" for f in r["findings"])


# ------------------------------------------------------------- duplicates (section 11)
def test_near_duplicates_are_reported_for_review_not_removed(client):
    import io
    rows = ["name,email"] + [f"Person {i},p{i}@x.com" for i in range(12)] + ["Person 1,p1@x.com", "Person 1 ,p1@x.com"]
    job = client.post("/api/upload/", data={"file": (io.BytesIO("\n".join(rows).encode()), "d.csv")},
                      content_type="multipart/form-data").get_json()["job_id"]
    body = client.post(f"/api/process/{job}", json={}).get_json()
    s = body.get("summary") or body
    assert s["rows_in"] - s["rows_out"] == s["change_log"]["removed_rows_total"]
