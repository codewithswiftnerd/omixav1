"""The three "No file? Try one of these" samples on the home page must come out right.

The CSVs are read straight from static/js/samples.js so this test can never drift from what visitors click.
Regressions covered: R-01 turned into -1 (R read as the rand symbol), 1/0 mixed with Yes/No left unstandardized,
dates under a header without the word "date" skipped, and ISO alpha-3 country codes ("eth") not recognised."""
import os, re, shutil, uuid

import pandas as pd
import pytest

import processing.pipeline as pl
import utils.file_handler as fh
from cleaning import detectors
from config import Config

ROOT = os.path.dirname(os.path.dirname(__file__))
DEFAULT_RULES = [
    "formatting", "missing_token_normalization", "numeric_text_cleaning", "gender_standardization",
    "country_standardization", "boolean_standardization", "currency_label_standardization",
    "case_standardization", "categorical_standardization", "email_cleaning", "phone_cleaning",
    "date_standardization", "duplicates",
]


def _sample(name):
    js = open(os.path.join(ROOT, "static", "js", "samples.js"), encoding="utf-8").read()
    m = re.search(name + r": \{\s*filename: '([^']+)',\s*csv: \[(.*?)\]\.join", js, re.S)
    lines = re.findall(r"^\s*'(.*)',?\s*$", m.group(2), re.M)
    return "\n".join(l.replace("\\'", "'") for l in lines)


def _clean(name, tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "TEMP_DIR", str(tmp_path), raising=False)
    job = str(uuid.uuid4())
    d = fh.job_dir_path(job)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "source.csv"), "w", encoding="utf-8") as f:
        f.write(_sample(name))
    res = pl.run_pipeline(job, rules=DEFAULT_RULES)
    out = pd.read_csv(os.path.join(d, "cleaned.csv"), dtype=str, keep_default_na=False)
    return res, out


def test_survey_ids_are_never_read_as_negative_numbers(tmp_path, monkeypatch):
    res, out = _clean("survey", tmp_path, monkeypatch)
    assert list(out["Respondent"]) == [f"R-{i:02d}" for i in range(1, 10)]


def test_survey_flags_countries_and_dates_are_standardized(tmp_path, monkeypatch):
    res, out = _clean("survey", tmp_path, monkeypatch)
    assert set(out["Would Recommend"]) == {"True", "False"}
    assert set(out["Country"]) == {"Nigeria", "Kenya", "Ghana", "Ethiopia"}
    assert all(re.fullmatch(r"2024-04-0\d", v) for v in out["Completed On"])
    assert res["rows_in"] == 10 and res["rows_out"] == 9           # the one repeated respondent is removed


def test_customers_newsletter_and_countries(tmp_path, monkeypatch):
    res, out = _clean("customers", tmp_path, monkeypatch)
    assert set(out["Newsletter"]) <= {"True", "False"}
    assert set(out["Country"]) == {"Nigeria", "Kenya", "Ghana", "Ethiopia"}
    assert all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) for v in out["Signup Date"])
    assert out["Email"].iloc[0] == "ada@mail.com"
    assert res["rows_out"] == 9


def test_sales_totals_and_duplicates(tmp_path, monkeypatch):
    res, out = _clean("sales", tmp_path, monkeypatch)
    assert res["rows_out"] == 9
    assert list(out["Region"]).count("Lagos") == 3             # "lagos" joined "Lagos"
    assert out["Unit Price"].iloc[0] == "4.5" and out["Total"].iloc[0] == "54"
    assert (res["quality_report"]["score"]) >= 80


@pytest.mark.parametrize("value", ["R-01", "Q-3", "A_7", "B/12"])
def test_letter_separator_digits_is_a_code_not_an_amount(value):
    assert detectors.normalize_numeric_cell(value)[0] == "not_numeric"


def test_real_amounts_are_still_cleaned():
    assert detectors.normalize_numeric_cell("$1,200.50")[:2] == ("converted", 1200.5)
    assert detectors.normalize_numeric_cell("-5")[0] == "unchanged"


@pytest.mark.parametrize("code,name", [("eth", "Ethiopia"), ("GHA", "Ghana"), ("ke", "Kenya"), ("NG", "Nigeria")])
def test_country_codes_two_and_three_letter(code, name):
    assert detectors.standardize_country_value(code) == name
