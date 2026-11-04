"""Regressions found by running the three built-in sample files (customers / sales / survey) end to end."""
import pandas as pd

from cleaning import detectors, validation
from cleaning.rules import apply_rules


def _survey():
    return pd.DataFrame({
        "Respondent": [f"R-{i:02d}" for i in range(1, 10)],
        "Country": ["Nigeria", "Kenya", "Ghana", "Nigeria", "Kenya", "Nigeria", "eth", "GH", "Nigeria"],
        "Would Recommend": ["Y", "N", "1", "TRUE", "0", "No", "Yes", "y", "N"],
        "Completed On": ["April 2, 2024", "02-Apr-2024", "2024/04/03", "2024-04-03", "April 4, 2024",
                         "04-Apr-2024", "2024/04/05", "2024-04-06", "April 7, 2024"],
    })


def test_letter_prefixed_ids_are_never_read_as_numbers():
    out, _ = apply_rules(_survey())
    assert out["Respondent"].tolist() == [f"R-{i:02d}" for i in range(1, 10)]


def test_validation_types_letter_number_codes_as_identifiers():
    ids = pd.Series([f"R-{i:02d}" for i in range(1, 10)])
    assert validation.detect_type("Respondent", ids)["type"] == validation.IDENTIFIER


def test_yes_no_words_mixed_with_one_zero_make_a_boolean_column_whatever_its_name():
    out, _ = apply_rules(_survey())
    assert out["Would Recommend"].astype(str).tolist() == ["True", "False", "True", "True", "False", "False", "True", "True", "False"]


def test_plain_one_zero_codes_are_still_left_alone():
    assert not detectors.words_prove_boolean({"0", "1"})
    assert detectors.words_prove_boolean({"y", "n", "1", "0"})


def test_dates_with_written_months_are_standardised_in_a_column_not_named_date():
    out, _ = apply_rules(_survey())
    assert out["Completed On"].tolist() == ["2024-04-02", "2024-04-02", "2024-04-03", "2024-04-03", "2024-04-04",
                                            "2024-04-04", "2024-04-05", "2024-04-06", "2024-04-07"]


def test_three_letter_iso_country_codes_are_understood():
    assert detectors.standardize_country_value("eth") == "Ethiopia"
    assert detectors.standardize_country_value("NGA") == "Nigeria"
    assert detectors.standardize_country_value("Atlantis") is None
