"""Phone-number standardization (spec Test C).

Policy: a number is only rewritten to E.164 when the value itself, or a region the USER chose for the
column, supports it. Otherwise the original value stays in place (never deleted, never guessed) so it
can be reviewed. There is no libphonenumber dependency; the curated table in cleaning/phone_formats.py
is used, so exotic numbering plans beyond its length rules are left untouched rather than "validated".
"""
import os, shutil, uuid

import pandas as pd
import pytest

import processing.pipeline as pl
import utils.file_handler as fh
from cleaning.phone_formats import normalize_phone
from cleaning.resolutions import apply_resolutions
from config import Config

PICK = lambda col, region: [{"column": col, "issue": "suspicious_phone_format", "choice": region}]


@pytest.mark.parametrize("raw,expected", [
    ("0803 123 4567", "+2348031234567"),        # local format, spaces
    ("08031234567", "+2348031234567"),
    ("(0803) 123-4567", "+2348031234567"),      # brackets and hyphen
    ("+234 803 123 4567", "+2348031234567"),    # already international, spaced
    ("+2348031234567", "+2348031234567"),
    ("00234 803 123 4567", "+2348031234567"),   # 00 international prefix
    ("8031234567", "+2348031234567"),           # leading 0 lost (NG chosen, mobile prefix 8)
    ("  0803 123 4567  ", "+2348031234567"),    # surrounding whitespace
])
def test_nigerian_forms_normalise_when_region_is_ng(raw, expected):
    assert normalize_phone(raw, "NG") == expected


@pytest.mark.parametrize("raw", [
    "abc", "", "0803123", "080312345678",      # not a number / too short / too long
    "+234 0803 123 4567",                       # trunk 0 left after +234: ambiguous, review
    "4155552671", "2125551234",                 # bare 10 digits NOT starting 7/8/9: no evidence of Nigeria
    "+12345",                                   # international prefix but impossible length
])
def test_anything_unsupported_is_left_untouched(raw):
    assert normalize_phone(raw, "NG") is None


def test_valid_international_numbers_normalise_whatever_region_was_chosen():
    assert normalize_phone("+1 415 555 2671", "NG") == "+14155552671"
    assert normalize_phone("+44 20 7946 0958", "NG") == "+442079460958"
    assert normalize_phone("0044 20 7946 0958", "GB") == "+442079460958"


def test_a_bare_number_is_never_assumed_to_be_nigerian_for_other_regions():
    # The lost-leading-0 rule exists for Nigeria only, and only because Nigeria was chosen.
    assert normalize_phone("8031234567", "US") is None
    assert normalize_phone("8031234567", "GH") is None


def _frame():
    return pd.DataFrame({
        "id": [f"R{i}" for i in range(8)],
        "Phone": ["0803 123 4567", "+234 803 123 4567", "8031234567", "abc", "0803123",
                  "+1 415 555 2671", "", "4155552671"],
    })


def test_resolver_keeps_every_row_and_preserves_unconvertible_values_for_review():
    df = _frame()
    out, log = apply_resolutions(df.copy(), PICK("Phone", "NG"))
    assert len(out) == len(df)                                              # nothing deleted
    assert out["Phone"].tolist() == ["+2348031234567"] * 3 + ["abc", "0803123", "+14155552671", "", "4155552671"]
    assert out["id"].tolist() == df["id"].tolist()


def test_default_phone_cleaning_rule_never_assumes_a_country():
    from cleaning.rules import apply_rules
    df = _frame()
    out = apply_rules(df.copy(), ["phone_cleaning"])
    out = out[0] if isinstance(out, tuple) else out
    assert len(out) == len(df)
    gained_234 = out["Phone"].astype(str).str.startswith("+234") & ~df["Phone"].astype(str).str.startswith("+234")
    assert not gained_234.any()                                              # no country code was invented
    assert out.loc[2, "Phone"].replace(" ", "") == "8031234567"             # bare number left as it was


def test_pipeline_end_to_end_reports_changes_and_loses_no_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "TEMP_DIR", str(tmp_path), raising=False)
    src = tmp_path / "p.csv"; _frame().to_csv(src, index=False)
    job = str(uuid.uuid4()); d = fh.job_dir_path(job); os.makedirs(d, exist_ok=True)
    shutil.copy(src, os.path.join(d, "source.csv"))
    res = pl.run_pipeline(job, rules=["formatting"], resolutions=PICK("Phone", "NG"))
    out = pd.read_csv(os.path.join(d, "cleaned.csv"), dtype=str, keep_default_na=False)
    assert res["rows_in"] == res["rows_out"] == len(out) == 8
    assert out["Phone"].tolist()[:3] == ["+2348031234567"] * 3 and out["Phone"].tolist()[3] == "abc"
    applied = [x for x in res["resolutions_applied"] if x["issue"] == "suspicious_phone_format"]
    assert applied and applied[0]["changed"] == 4                           # 3 NG forms + 1 international


@pytest.mark.parametrize("name,expected", [
    ("Contact Number", True), ("contact_number", True), ("ContactNo", True), ("Tel No", True),
    ("Phone", True), ("Mobile No", True),
    ("Contact", False), ("Contact Name", False), ("Contact Email", False),
])
def test_phone_column_name_detection(name, expected):
    from cleaning import detectors
    assert detectors.is_phone_column(name) is expected


def test_country_picker_is_offered_for_a_contact_number_column():
    from cleaning.quality_report import generate_report
    df = pd.DataFrame({"Contact Number": ["0803 123 4567", "0802 555 1212", "0701 000 2222", "8031234567"]})
    issues = [f["issue"] for f in generate_report(df)["findings"] if f["column"] == "Contact Number"]
    assert "suspicious_phone_format" in issues


def test_excel_numeric_phone_cells_get_the_picker_and_normalise_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "TEMP_DIR", str(tmp_path), raising=False)
    x = tmp_path / "p.xlsx"
    pd.DataFrame({"Name": ["a", "b", "c"], "Phone": [8031234567, 8021234567, 7011234567]}).to_excel(x, index=False)
    job = str(uuid.uuid4()); d = fh.job_dir_path(job); os.makedirs(d, exist_ok=True)
    shutil.copy(x, os.path.join(d, "source.xlsx"))
    res = pl.run_pipeline(job, rules=["formatting"], resolutions=PICK("Phone", "NG"))
    offered = [f["issue"] for f in res["quality_report_before"]["findings"] if f["column"] == "Phone"]
    assert "suspicious_phone_format" in offered
    out = pd.read_excel(os.path.join(d, "cleaned.xlsx"), dtype=str)
    assert out["Phone"].tolist() == ["+2348031234567", "+2348021234567", "+2347011234567"]
    assert res["rows_in"] == res["rows_out"] == 3
