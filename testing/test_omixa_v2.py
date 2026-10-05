"""
Regression tests for the "final cleaning improvement" spec additions:
gender/country standardization, extended boolean handling, real phone
punctuation cleanup + scientific-notation recovery, age/gender/country
validation findings, currency-vs-percentage split, and the new
cleaning summary. Complements testing/test_omixa.py, which covers the
V1 core and isn't touched here.

Run with:
    pip install pytest --break-system-packages   # or in a venv
    pytest testing/test_omixa_v2.py -v
"""

import pandas as pd
import pytest

from cleaning.rules import apply_rules, DEFAULT_RULES
from cleaning.quality_report import generate_report
from cleaning.summary import build_cleaning_summary
from cleaning import detectors


# --------------------------------------------------------------------
# Gender standardization
# --------------------------------------------------------------------

def test_gender_standardization_maps_known_variants():
    df = pd.DataFrame({"Gender": ["MALE", "Female", "m", "f", "male"]})
    cleaned, log = apply_rules(df, rules=["column_names", "gender_standardization"])
    assert list(cleaned["gender"]) == ["male", "female", "male", "female", "male"]
    assert log["changes"]["gender_standardization_changed"] == 4  # "male" already correct, not counted


def test_gender_standardization_never_guesses_unrecognized_values():
    df = pd.DataFrame({"Gender": ["Male", "Non-binary", "Prefer not to say"]})
    cleaned, log = apply_rules(df, rules=["column_names", "gender_standardization"])
    assert cleaned["gender"].iloc[0] == "male"  # recognized variant -> canonicalized
    assert cleaned["gender"].iloc[1] == "Non-binary"  # untouched, never guessed
    assert cleaned["gender"].iloc[2] == "Prefer not to say"  # untouched, never guessed
    assert log["changes"]["gender_standardization_changed"] == 1


def test_gender_column_detection_is_name_based_only():
    df = pd.DataFrame({"notes": ["MALE", "FEMALE"]})  # not a gender-named column
    cleaned, log = apply_rules(df, rules=["column_names", "gender_standardization"])
    assert list(cleaned["notes"]) == ["MALE", "FEMALE"]  # untouched


def test_unrecognized_gender_value_is_flagged_not_fixed():
    df = pd.DataFrame({"gender": ["male", "female", "Other"]})
    report = generate_report(df)
    findings = [f for f in report["findings"] if f["issue"] == "unrecognized_gender_value"]
    assert len(findings) == 1
    assert findings[0]["sample_values"] == ["Other"]


# --------------------------------------------------------------------
# Country standardization
# --------------------------------------------------------------------

def test_country_standardization_maps_codes_and_names():
    df = pd.DataFrame({"Country": ["NG", "nigeria", "KE", "Ghana", "gh", "MY"]})
    cleaned, log = apply_rules(df, rules=["column_names", "country_standardization"])
    assert list(cleaned["country"]) == ["Nigeria", "Nigeria", "Kenya", "Ghana", "Ghana", "Malaysia"]
    assert log["changes"]["country_standardization_changed"] == 5  # "Ghana" already correct


def test_unrecognized_country_value_is_flagged_not_guessed():
    df = pd.DataFrame({"country": ["Nigeria", "Kenya", "Wakanda"]})
    cleaned, log = apply_rules(df.copy(), rules=["country_standardization"])
    assert cleaned["country"].iloc[2] == "Wakanda"  # untouched, no fuzzy matching
    report = generate_report(df)
    findings = [f for f in report["findings"] if f["issue"] == "unrecognized_country_value"]
    assert len(findings) == 1


# --------------------------------------------------------------------
# Boolean standardization: extended 1/0 support for name-matched columns
# --------------------------------------------------------------------

def test_boolean_1_0_only_applied_to_flag_named_columns():
    df = pd.DataFrame({
        "is_active": ["1", "0", "1"],
        "priority_level": ["1", "0", "1"],  # NOT a boolean-named column
    })
    cleaned, log = apply_rules(df, rules=["boolean_standardization"])
    assert list(cleaned["is_active"]) == [True, False, True]
    assert list(cleaned["priority_level"]) == ["1", "0", "1"]  # left as text/numeric codes


def test_boolean_numeric_dtype_flag_column_converted():
    df = pd.DataFrame({"is_active": [1, 0, 1, 1]})  # pandas reads this as int64
    assert pd.api.types.is_integer_dtype(df["is_active"])
    cleaned, log = apply_rules(df, rules=["boolean_standardization"])
    assert cleaned["is_active"].dtype == "boolean"
    assert list(cleaned["is_active"]) == [True, False, True, True]


def test_boolean_numeric_generic_column_untouched():
    df = pd.DataFrame({"score": [1, 0, 1, 0]})  # generic numeric column, not a flag name
    cleaned, log = apply_rules(df, rules=["boolean_standardization"])
    assert pd.api.types.is_integer_dtype(cleaned["score"])
    assert log["changes"].get("boolean_standardization_changed", 0) == 0


def test_boolean_word_based_still_works_for_unnamed_columns():
    df = pd.DataFrame({"subscribed_flag": ["Yes", "No", "yes"]})
    cleaned, log = apply_rules(df, rules=["boolean_standardization"])
    assert list(cleaned["subscribed_flag"]) == [True, False, True]


# --------------------------------------------------------------------
# Phone cleaning: punctuation normalization + scientific notation
# --------------------------------------------------------------------

def test_phone_punctuation_stripped_consistently():
    df = pd.DataFrame({"phone_num": ["+234 806-123-4567", "(234) 806 123 4567", "2348061234567"]})
    cleaned, log = apply_rules(df, rules=["phone_cleaning"])
    assert list(cleaned["phone_num"]) == ["+2348061234567", "2348061234567", "2348061234567"]
    assert cleaned["phone_num"].dtype in ("string", object)  # never numeric


def test_phone_never_becomes_numeric_dtype():
    df = pd.DataFrame({"phone_num": ["2348061234567", "2348061234568"]})
    cleaned, _ = apply_rules(df, rules=DEFAULT_RULES)
    assert not pd.api.types.is_numeric_dtype(cleaned["phone_num"])


def test_phone_scientific_notation_recovered_only_when_lossless():
    df = pd.DataFrame({"phone_num": ["1.343E+12", "2.348061234567E+12"]})
    cleaned, log = apply_rules(df, rules=["phone_cleaning"])
    # first value: mantissa too short to expand safely -> left untouched, flagged
    assert cleaned["phone_num"].iloc[0] == "1.343E+12"
    # second value: mantissa carries enough digits -> safely recovered
    assert cleaned["phone_num"].iloc[1] == "2348061234567"
    unrecoverable = log["details"]["phone_cleaning"]["scientific_notation_unrecoverable"]
    assert "phone_num" in unrecoverable and "1.343E+12" in unrecoverable["phone_num"]


def test_unrecoverable_scientific_notation_flagged_in_report():
    df = pd.DataFrame({"phone_num": ["1.343E+12", "0803317157"]})
    report = generate_report(df)
    findings = [f for f in report["findings"] if f["issue"] == "unrecoverable_scientific_notation"]
    assert len(findings) == 1
    assert findings[0]["severity"] == "critical"


def test_phone_with_extension_left_untouched():
    df = pd.DataFrame({"phone_num": ["+1 555-123-4567 ext 89"]})
    cleaned, log = apply_rules(df, rules=["phone_cleaning"])
    assert cleaned["phone_num"].iloc[0] == "+1 555-123-4567 ext 89"  # not blindly stripped


# --------------------------------------------------------------------
# Missing-token normalization: "unknown"/"Unknown" now included
# --------------------------------------------------------------------

def test_unknown_token_normalized_to_missing_then_consistently_refilled():
    df = pd.DataFrame({"notes": ["Unknown", "unknown", "a real note"]})
    cleaned, log = apply_rules(df, rules=["missing_token_normalization", "missing_values"])
    # round-trips back to the same literal placeholder -- consistent representation,
    # not a mix of "Unknown"/"N/A"/"null"
    assert cleaned["notes"].iloc[0] == "Unknown"
    assert cleaned["notes"].iloc[1] == "Unknown"
    assert cleaned["notes"].iloc[2] == "a real note"  # legitimate text never touched


def test_legitimate_email_containing_word_unknown_is_not_treated_as_missing():
    df = pd.DataFrame({"email": ["unknown@email.com", "john@email.com"]})
    cleaned, log = apply_rules(df, rules=["missing_token_normalization"])
    assert cleaned["email"].iloc[0] == "unknown@email.com"  # whole-cell match only


# --------------------------------------------------------------------
# Age validation (detect-only)
# --------------------------------------------------------------------

def test_impossible_age_flagged_not_auto_fixed():
    df = pd.DataFrame({"age": [34, -5, 200, 41]})
    cleaned, log = apply_rules(df, rules=["numeric_text_cleaning"])
    assert list(cleaned["age"]) == [34, -5, 200, 41]  # never silently changed
    report = generate_report(df)
    findings = [f for f in report["findings"] if f["issue"] == "impossible_age"]
    assert len(findings) == 1
    assert set(findings[0]["sample_values"]) == {-5, 200}


def test_plausible_ages_not_flagged():
    df = pd.DataFrame({"age": [22, 45, 67]})
    report = generate_report(df)
    assert not any(f["issue"] == "impossible_age" for f in report["findings"])


# --------------------------------------------------------------------
# Currency vs percentage split (feeds the cleaning summary)
# --------------------------------------------------------------------

def test_currency_and_percentage_tracked_separately():
    df = pd.DataFrame({
        "Annual Income": ["$22,000", "$8,900"],
        "Satisfaction Score": ["56%", "82%"],
    })
    cleaned, log = apply_rules(df, rules=["column_names", "numeric_text_cleaning"])
    per_col = log["details"]["numeric_text_cleaning"]["per_column"]
    assert per_col["annual_income"]["type"] == "currency"
    assert per_col["satisfaction_score"]["type"] == "percentage"


# --------------------------------------------------------------------
# Cleaning summary
# --------------------------------------------------------------------

def test_cleaning_summary_counts_match_change_log():
    df = pd.DataFrame({
        "Gender": ["MALE", "female"],
        "Country": ["NG", "kenya"],
        "Annual Income": ["$1,000", "$2,000"],
        "Satisfaction Score": ["50%", "60%"],
    })
    cleaned, log = apply_rules(df, rules=DEFAULT_RULES)
    before = generate_report(df)
    after = generate_report(cleaned)
    summary = build_cleaning_summary(
        rows_in=len(df), rows_out=len(cleaned), column_count=len(cleaned.columns),
        change_log=log, quality_before=before, quality_after=after,
    )
    assert summary["counts"]["gender_values_normalized"] == log["changes"]["gender_standardization_changed"]
    assert summary["counts"]["country_values_normalized"] == log["changes"]["country_standardization_changed"]
    assert summary["counts"]["currency_formatting_cleaned"] == 2
    assert summary["counts"]["percentage_values_normalized"] == 2
    assert "DATA CLEANING SUMMARY" in summary["text"]
    assert "gender" in summary["columns_affected"]
    assert "country" in summary["columns_affected"]


def test_cleaning_summary_includes_resolution_driven_date_fixes():
    from cleaning.resolutions import apply_resolutions
    df = pd.DataFrame({"signup_date": ["03/04/2025", "15/06/2024", "20/12/2023"]})
    cleaned, log = apply_rules(df, rules=DEFAULT_RULES)
    resolved, resolution_log = apply_resolutions(
        cleaned, [{"column": "signup_date", "issue": "ambiguous_date_format", "choice": "day_first"}]
    )
    before = generate_report(df)
    after = generate_report(resolved)
    summary = build_cleaning_summary(
        rows_in=len(df), rows_out=len(resolved), column_count=len(resolved.columns),
        change_log=log, quality_before=before, quality_after=after, resolution_log=resolution_log,
    )
    assert summary["counts"]["dates_standardized"] == resolution_log["applied"][0]["changed"]
    assert summary["counts"]["dates_standardized"] > 0


# --------------------------------------------------------------------
# Column type inference (report output)
# --------------------------------------------------------------------

def test_column_types_reports_semantic_and_pandas_dtype():
    df = pd.DataFrame({
        "gender": ["male", "female"],
        "country": ["Nigeria", "Kenya"],
        "age": [34, 29],
        "email": ["a@b.com", "c@d.com"],
    })
    report = generate_report(df)
    types = report["column_types"]
    assert types["gender"]["inferred_type"] == "gender"
    assert types["country"]["inferred_type"] == "country"
    assert types["age"]["inferred_type"] == "age"
    assert types["email"]["inferred_type"] == "email"


# --------------------------------------------------------------------
# Full-dataset regression: nothing above breaks the original sample
# --------------------------------------------------------------------

def test_original_sample_dataset_still_processes_cleanly(tmp_path):
    p = tmp_path / "sample.csv"
    p.write_text("name,age,city\nJohn Smith,34,Lagos\nJane Doe,29,Abuja\n"
                  "John Smith,34,Lagos\n,,\nMike Ross ,41, Lagos\n")
    from processing.pipeline import read_source
    df = read_source(str(p))
    cleaned, log = apply_rules(df, rules=DEFAULT_RULES)
    assert len(cleaned) == 4  # exact duplicate removed
    assert "duplicates" in log["details"]
