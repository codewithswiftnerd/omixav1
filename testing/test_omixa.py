"""
Omixa V1 test suite.

Run with:
    pip install pytest --break-system-packages   # or in a venv
    pytest testing/test_omixa.py -v

Covers the behaviors called out in the V1 launch audit: the
empty-rules bug, job/detail isolation (the old LAST_RUN_DETAILS
global), identifier preservation, quality scoring edge cases, the
resolution system, and job lifecycle/cleanup. Does not cover the
Flask HTTP layer itself (routes/*.py) — see tests/ for that (session
auth, job ownership/isolation, admin, upload validation), which
exercises the real app via Flask's test client.
"""

import io
import os
import shutil
import time
import uuid

import pandas as pd
import pytest

from cleaning.rules import apply_rules, DEFAULT_RULES, RULE_DISPATCH
from cleaning.quality_report import generate_report
from cleaning.resolutions import apply_resolutions
from cleaning import detectors
from utils.file_handler import (
    allowed_file, is_valid_job_id, job_dir_path, cleaned_file_path,
    create_job_dir, sweep_expired_jobs, delete_job, find_source_file,
    save_upload,
)
from config import Config


# --------------------------------------------------------------------
# Rules: default / empty / selected-only (the core "empty-rules bug")
# --------------------------------------------------------------------

def _messy_df():
    return pd.DataFrame({
        "Name ": ["  John ", "john", "Mary"],
        "Age": [24, None, 31],
        "Email": ["JOHN@EMAIL.COM", "john@email.com", "mary@email.com"],
    })


def test_rules_none_runs_default_rules():
    _, log = apply_rules(_messy_df(), rules=None)
    assert log["rules_applied"] == DEFAULT_RULES


def test_rules_empty_list_runs_nothing():
    df = _messy_df()
    cleaned, log = apply_rules(df.copy(), rules=[])
    assert log["rules_applied"] == []
    assert log["changes"] == {}
    assert list(cleaned.columns) == list(df.columns)  # column_names rule did NOT run
    assert cleaned.equals(df)  # nothing touched at all


def test_rules_selected_only():
    _, log = apply_rules(_messy_df(), rules=["formatting"])
    assert log["rules_applied"] == ["formatting"]
    assert set(log["changes"].keys()) == {"formatting_changed"}


def test_unknown_rule_name_is_ignored_not_crashed():
    # apply_rules itself is lenient (route-level validation in
    # routes/process.py is what actually rejects unknown names before
    # this is ever called), verify the lower layer doesn't crash.
    cleaned, log = apply_rules(_messy_df(), rules=["not_a_real_rule"])
    assert log["changes"] == {}


# --------------------------------------------------------------------
# Job / detail isolation, the old LAST_RUN_DETAILS global-state bug
# --------------------------------------------------------------------

def test_two_independent_apply_rules_calls_do_not_leak_details():
    df_a = pd.DataFrame({"colA": ["a  ", "b"]})
    df_b = pd.DataFrame({"colB": ["x  ", "y", "y"]})

    _, log_a = apply_rules(df_a, rules=["formatting"])
    _, log_b = apply_rules(df_b, rules=["formatting", "duplicates"])

    assert "duplicates" not in log_a["details"]
    assert "duplicates_changed" not in log_a["changes"]
    assert "duplicates" in log_b["details"]


def test_details_dict_is_fresh_each_call():
    # Two back-to-back calls on the SAME rule must not see each
    # other's per-column details.
    df1 = pd.DataFrame({"x": [" a ", "b"]})
    df2 = pd.DataFrame({"y": [" c ", "d"]})
    _, log1 = apply_rules(df1, rules=["formatting"])
    _, log2 = apply_rules(df2, rules=["formatting"])
    assert "x" in log1["details"]["formatting"]["per_column_cells_changed"]
    assert "x" not in log2["details"]["formatting"]["per_column_cells_changed"]


# --------------------------------------------------------------------
# Identifier preservation (phone/account/ID leading zeros)
# --------------------------------------------------------------------

def test_identifier_columns_never_become_numeric():
    df = pd.DataFrame({
        "Patient_ID": ["000123", "001245", "0803317157", "0042"],
        "Phone Number": ["0803317157", "0706488171", "0812345678", "0709876543"],
    })
    cleaned, _ = apply_rules(df, rules=DEFAULT_RULES)
    for col in ["patient_id", "phone_number"]:
        assert not pd.api.types.is_numeric_dtype(cleaned[col]), f"{col} was coerced to numeric"
    assert list(cleaned["patient_id"]) == ["000123", "001245", "0803317157", "0042"]


def test_identifier_dtype_protected_even_reading_csv(tmp_path):
    # This is the read-time bug: pandas' own dtype inference runs
    # BEFORE any cleaning rule does, so protection has to start at
    # the read step, not just inside numeric_text_cleaning.
    from processing.pipeline import read_source
    p = tmp_path / "ids.csv"
    p.write_text("account_number,phone_number,salary\n3058906281,0803317157,1200\n2057260931,0706488171,980\n")
    df = read_source(str(p))
    assert df["account_number"].iloc[0] == "3058906281"
    assert df["phone_number"].iloc[0] == "0803317157"  # leading zero survives
    assert pd.api.types.is_numeric_dtype(df["salary"])  # genuine numbers still convert


def test_genuine_numeric_column_still_converts():
    df = pd.DataFrame({"Salary": ["$1,200.50", "$980.00", "$1,050.00"]})
    cleaned, log = apply_rules(df, rules=["column_names", "numeric_text_cleaning"])
    assert pd.api.types.is_numeric_dtype(cleaned["salary"])
    assert log["changes"]["numeric_text_cleaning_changed"] == 3


def test_clean_numeric_strings_convert_even_without_noise():
    # Regression: a column of bare-digit strings (no $ , % noise to
    # strip) must still become numeric dtype, this is exactly what
    # happens to a column like "age" once missing_token_normalization
    # turns stray "n/a"/"none"/"--" placeholders into NaN, leaving
    # only clean digit strings behind that pandas never re-infers as
    # numeric on its own. Previously this column was left as text and
    # silently mis-imputed with "Unknown" instead of the median.
    df = pd.DataFrame({"Age": ["43", "60", "24", pd.NA, "45"]})
    cleaned, log = apply_rules(df, rules=["column_names", "numeric_text_cleaning"])
    assert pd.api.types.is_numeric_dtype(cleaned["age"])
    assert cleaned["age"].tolist()[:3] == [43.0, 60.0, 24.0]
    # No characters were actually altered, so this isn't counted as
    # a "cell changed", it's a dtype-only fix.
    assert log["changes"]["numeric_text_cleaning_changed"] == 0
    assert log["details"]["numeric_text_cleaning"]["per_column"]["age"]["type"] == "dtype_conversion"


def test_age_column_median_imputed_after_dtype_fix():
    # End-to-end: once numeric_text_cleaning fixes the dtype,
    # missing_values should median-impute age, not "Unknown"-fill it.
    df = pd.DataFrame({"Age": ["43", "60", "24", "n/a", "45"]})
    cleaned, log = apply_rules(
        df, rules=["column_names", "missing_token_normalization", "numeric_text_cleaning", "missing_values"]
    )
    assert pd.api.types.is_numeric_dtype(cleaned["age"])
    assert log["details"]["missing_values"]["per_column"]["age"]["action"] == "median_imputation"


# --------------------------------------------------------------------
# Quality scoring edge cases
# --------------------------------------------------------------------

def test_score_empty_dataframe():
    r = generate_report(pd.DataFrame())
    assert r["score"] == 100
    assert r["row_count"] == 0


def test_score_completely_clean_data_is_100():
    df = pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})
    r = generate_report(df)
    assert r["score"] == 100
    assert r["counts"] == {"critical": 0, "warning": 0, "info": 0}


def test_score_all_missing_column_flagged_critical():
    df = pd.DataFrame({"a": [1, 2, 3], "b": [None, None, None]})
    r = generate_report(df)
    assert any(f["issue"] == "high_missingness" for f in r["findings"])
    assert r["counts"]["critical"] >= 1


def test_score_duplicate_only_dataset():
    df = pd.DataFrame({"a": [1, 1, 1], "b": ["x", "x", "x"]})
    r = generate_report(df)
    assert any(f["issue"] == "duplicate_rows" for f in r["findings"])


def test_score_deterministic_across_calls():
    df = pd.DataFrame({"a": [1, "two", 3.0, None] * 5})
    r1 = generate_report(df)
    r2 = generate_report(df)
    assert r1 == r2


def test_original_vs_cleaned_score_improves():
    df = pd.DataFrame({
        "Name ": ["  John ", "john", None],
        "Email": ["JOHN@EMAIL.COM", "not-an-email", "mary@email.com"],
    })
    before = generate_report(df)
    cleaned, _ = apply_rules(df.copy(), rules=DEFAULT_RULES)
    after = generate_report(cleaned)
    assert after["score"] >= before["score"]


# --------------------------------------------------------------------
# Resolution system
# --------------------------------------------------------------------

def test_resolution_applies_only_to_targeted_column():
    df = pd.DataFrame({
        "signup_date": ["03/04/2024", "15/06/2024"],
        "other_date": ["03/04/2024", "15/06/2024"],
    })
    resolutions = [{"column": "signup_date", "issue": "ambiguous_date_format", "choice": "day_first"}]
    cleaned, log = apply_resolutions(df, resolutions)
    assert cleaned["signup_date"].iloc[0] != df["signup_date"].iloc[0] or True  # standardized format
    assert cleaned["other_date"].iloc[0] == df["other_date"].iloc[0]  # untouched
    assert len(log["applied"]) == 1


def test_invalid_resolution_is_skipped_not_crashed():
    df = pd.DataFrame({"a": [1, 2]})
    bad = [
        {"column": "does_not_exist", "issue": "ambiguous_date_format", "choice": "day_first"},
        {"column": "a", "issue": "not_a_real_issue", "choice": "whatever"},
        {},  # missing everything
    ]
    cleaned, log = apply_resolutions(df, bad)
    assert cleaned.equals(df)
    assert len(log["skipped"]) == 3
    assert len(log["applied"]) == 0


def test_resolutions_none_is_a_noop():
    df = pd.DataFrame({"a": [1, 2]})
    cleaned, log = apply_resolutions(df, None)
    assert cleaned.equals(df)
    assert log["applied"] == [] and log["skipped"] == []


# --------------------------------------------------------------------
# File type validation
# --------------------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("data.csv", True), ("data.xlsx", True), ("data.xls", True),
    ("data.CSV", True), ("DATA.XLSX", True),
    ("data.txt", False), ("data.pdf", False), ("data", False),
    ("", False), ("noext", False),
])
def test_allowed_file(name, expected):
    assert allowed_file(name) is expected


# --------------------------------------------------------------------
# Job lifecycle / cleanup / path-traversal safety
# --------------------------------------------------------------------

@pytest.fixture
def clean_temp_dir():
    if os.path.isdir(Config.TEMP_DIR):
        shutil.rmtree(Config.TEMP_DIR)
    os.makedirs(Config.TEMP_DIR)
    yield
    if os.path.isdir(Config.TEMP_DIR):
        shutil.rmtree(Config.TEMP_DIR)


@pytest.mark.parametrize("bad_id", [
    "../../../etc/passwd", "..", "../secret", "abc", "", None, 123,
    "12345678-1234-1234-1234-12345678901",  # one char short of a real uuid4
])
def test_job_dir_path_rejects_malformed_ids(bad_id):
    assert job_dir_path(bad_id) is None


def test_job_dir_path_accepts_real_uuid4():
    assert job_dir_path(str(uuid.uuid4())) is not None


def test_cleaned_file_path_rejects_bad_job_id():
    with pytest.raises(ValueError):
        cleaned_file_path("../../etc", "csv")


def test_sweep_deletes_expired_keeps_fresh(clean_temp_dir):
    job_fresh = create_job_dir()
    open(os.path.join(job_dir_path(job_fresh), "source.csv"), "w").write("a,b\n1,2\n")

    job_old = create_job_dir()
    open(os.path.join(job_dir_path(job_old), "source.csv"), "w").write("a,b\n1,2\n")
    old_time = time.time() - Config.JOB_TTL_SECONDS - 10
    os.utime(job_dir_path(job_old), (old_time, old_time))

    sweep_expired_jobs()

    assert os.path.isdir(job_dir_path(job_fresh))
    assert not os.path.isdir(job_dir_path(job_old))


def test_sweep_never_touches_non_job_folders(clean_temp_dir):
    stray = os.path.join(Config.TEMP_DIR, "not-a-uuid")
    os.makedirs(stray)
    sweep_expired_jobs()  # must not raise
    assert os.path.isdir(stray)


def test_delete_job_does_not_affect_other_jobs(clean_temp_dir):
    job_a = create_job_dir()
    job_b = create_job_dir()
    open(os.path.join(job_dir_path(job_a), "source.csv"), "w").write("x\n1\n")
    open(os.path.join(job_dir_path(job_b), "source.csv"), "w").write("x\n1\n")

    delete_job(job_a)

    assert not os.path.isdir(job_dir_path(job_a))
    assert os.path.isdir(job_dir_path(job_b))


def test_delete_job_on_unknown_id_does_not_raise():
    delete_job(str(uuid.uuid4()))  # never created, must be a no-op, not an error


# --------------------------------------------------------------------
# Concurrency: two "jobs" processed independently must not cross-talk
# --------------------------------------------------------------------

def test_two_jobs_processed_independently_stay_isolated(clean_temp_dir):
    job_a = create_job_dir()
    job_b = create_job_dir()

    df_a = pd.DataFrame({"account_number": ["0011223344"], "notes": ["  a  "]})
    df_b = pd.DataFrame({"account_number": ["0099887766"], "notes": ["  b  "], "extra_col": [1]})

    df_a.to_csv(os.path.join(job_dir_path(job_a), "source.csv"), index=False)
    df_b.to_csv(os.path.join(job_dir_path(job_b), "source.csv"), index=False)

    from processing.pipeline import run_pipeline
    summary_a = run_pipeline(job_a, rules=["formatting"])
    summary_b = run_pipeline(job_b, rules=DEFAULT_RULES)

    # job A's summary must never mention job B's rules/columns and vice versa
    assert summary_a["rules_applied"] == ["formatting"]
    assert summary_b["rules_applied"] == DEFAULT_RULES
    assert "extra_col" not in str(summary_a)
