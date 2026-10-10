"""Capitalisation consistency, spelling-variant merging and numbers-stored-as-text.

Regression for a real cleaned file where these survived cleaning: names in ALL CAPS next to Title
Case, `cust-0011` next to `CUST-0012`, a `sales` / `LAGOS` majority that categorical merging preserved,
status labels (`active`, `INACTIVE`, `On hold`) that were never touched because they are different words,
`Port-Harcourt` vs `Port Harcourt`, ages like `44 yrs`, and `1O,OOO` typed with a letter O.
"""
import pandas as pd

from cleaning import casing, detectors
from cleaning.quality_report import generate_report
from cleaning.rules import DEFAULT_RULES, apply_rules


NO_DEDUPE = [r for r in DEFAULT_RULES if r != "duplicates"]   # tiny frames here would otherwise collapse


def clean(df, rules=None):
    out, log = apply_rules(df.copy(), rules=NO_DEDUPE if rules is None else rules)
    return out, log


# ------------------------------------------------------------------ names
def test_person_names_in_one_case_become_title_case():
    df = pd.DataFrame({"full_name": ["DAVID ADEYEMI", "bola okafor", "Ada Eze", "Musa Obi"]})
    out, log = clean(df)
    assert out["full_name"].tolist() == ["David Adeyemi", "Bola Okafor", "Ada Eze", "Musa Obi"]
    assert log["changes"]["case_standardization_changed"] == 2


def test_mixed_case_names_are_never_touched():
    df = pd.DataFrame({"full_name": ["Ronald McDonald", "Sinead O'Brien", "van der Merwe", "ANNE-MARIE SMITH", "Ada Eze"]})
    out, _ = clean(df)
    assert out["full_name"].tolist()[:3] == ["Ronald McDonald", "Sinead O'Brien", "van der Merwe"]
    assert out["full_name"].tolist()[3] == "Anne-Marie Smith"


def test_multiword_all_caps_name_is_not_mistaken_for_an_acronym():
    assert not casing.is_acronym("JOHN EZE")
    assert casing.is_acronym("HR") and casing.is_acronym("NGN/USD") and casing.is_acronym("I.T.")


# ------------------------------------------------------------------ identifiers
def test_id_prefix_follows_the_majority():
    df = pd.DataFrame({"customer_id": ["CUST-0001", "CUST-0002", "cust-0003", "CUST-0004", "CUST-X251"]})
    out, _ = clean(df)
    assert out["customer_id"].tolist() == ["CUST-0001", "CUST-0002", "CUST-0003", "CUST-0004", "CUST-X251"]


def test_consistent_lowercase_ids_are_left_alone():
    df = pd.DataFrame({"customer_id": ["cust-0001", "cust-0002", "cust-0003"]})
    out, _ = clean(df)
    assert out["customer_id"].tolist() == ["cust-0001", "cust-0002", "cust-0003"]


# ------------------------------------------------------------------ labels
def test_status_labels_with_mixed_styles_are_unified():
    df = pd.DataFrame({"account_status": ["active", "INACTIVE", "pending", "On hold", "active", "pending"] * 3})
    out, _ = clean(df)
    assert set(out["account_status"]) == {"Active", "Inactive", "Pending", "On hold"}


def test_majority_lowercase_label_no_longer_wins():
    df = pd.DataFrame({"department": ["sales"] * 6 + ["Marketing", "Customer support", "Finance"] * 2})
    out, _ = clean(df)
    assert "Sales" in set(out["department"]) and "sales" not in set(out["department"])


def test_city_all_caps_is_fixed_but_short_codes_stay():
    df = pd.DataFrame({"city": ["LAGOS"] * 5 + ["Abuja", "Enugu"] * 3 + ["PH"] * 2})
    out, _ = clean(df)
    assert "Lagos" in set(out["city"]) and "PH" in set(out["city"]) and "LAGOS" not in set(out["city"])


def test_department_acronyms_survive():
    df = pd.DataFrame({"department": ["HR", "IT", "Finance", "sales", "Operations"] * 3})
    out, _ = clean(df)
    assert {"HR", "IT"} <= set(out["department"]) and "Sales" in set(out["department"])


def test_column_that_is_consistently_one_style_is_left_alone():
    for style in (["male", "female"] * 4, ["ACTIVE", "INACTIVE", "PENDING"] * 3):
        out, _ = clean(pd.DataFrame({"status": style}), rules=["case_standardization"])
        assert out["status"].tolist() == style


def test_notes_and_free_text_are_not_recased():
    df = pd.DataFrame({"review_note": ["OK", "needs review!!!", "⚠️ check", "possible duplicate", "OK", "✓"] * 3})
    out, _ = clean(df, rules=["case_standardization"])
    assert out["review_note"].tolist() == df["review_note"].tolist()


def test_account_status_is_not_treated_as_an_identifier():
    assert not detectors.is_identifier_name("account_status")
    assert not detectors.is_identifier_name("Account Type")
    assert detectors.is_identifier_name("account_number") and detectors.is_identifier_name("Account No")
    assert detectors.is_identifier_name("account")


def test_case_rule_runs_before_category_merging():
    assert DEFAULT_RULES.index("case_standardization") < DEFAULT_RULES.index("categorical_standardization")


# ------------------------------------------------------------------ spelling variants
def test_variants_that_differ_by_hyphen_space_or_dot_are_merged():
    df = pd.DataFrame({
        "city": ["Port Harcourt"] * 6 + ["Port-Harcourt"] * 3 + ["Abuja"] * 3,
        "source": ["Website"] * 6 + ["Web site"] * 3 + ["Walk-in"] * 3 + ["Walk in"] * 0,
        "dept": ["IT"] * 4 + ["I.T."] * 4 + ["Finance"] * 4,
    })
    out, _ = clean(df)
    assert set(out["city"]) == {"Port Harcourt", "Abuja"}
    assert set(out["source"]) == {"Website", "Walk-in"}
    assert set(out["dept"]) == {"IT", "Finance"}


# ------------------------------------------------------------------ numbers stored as text
def test_age_with_units_becomes_numeric_but_number_words_are_never_guessed():
    # unit suffixes are formatting; "twenty" is language and is only converted by an explicit user choice
    df = pd.DataFrame({"age": ["44 yrs", "26", "39 years", "51"]})
    out, _ = clean(df)
    assert out["age"].tolist() == [44, 26, 39, 51]
    df = pd.DataFrame({"age": ["44 yrs", "26", "twenty", "39 years", "51"]})
    out, _ = clean(df)
    assert out["age"].tolist() == ["44 yrs", "26", "twenty", "39 years", "51"]  # untouched, not invented


def test_letter_o_typed_for_zero_in_a_number():
    assert detectors.strip_numeric_noise("1O,OOO") == "10000"
    assert detectors.strip_numeric_noise("Oct") == "Oct"            # real words are never touched
    assert detectors.parse_number_words("ten thousand") == 10000
    assert detectors.parse_number_words("hello") is None


def test_free_is_never_assumed_to_be_zero_by_default():
    df = pd.DataFrame({"amount_paid": ["free", "$1,200.50", "₦5,000", "free"]})
    out, _ = clean(df)
    assert out["amount_paid"].tolist() == ["free", "$1,200.50", "₦5,000", "free"]
    other = pd.DataFrame({"plan": ["free", "pro", "free", "pro"]})
    out2, _ = clean(other)
    assert out2["plan"].tolist() == ["free", "pro", "free", "pro"]


def test_ambiguous_placeholders_are_kept_in_notes_but_blanked_in_numeric_columns():
    df = pd.DataFrame({"note": ["???", "—", "real", "text"]})
    out, _ = clean(df, rules=["missing_token_normalization"])
    assert out["note"].tolist() == ["???", "—", "real", "text"]          # may carry meaning: preserved
    df = pd.DataFrame({"qty": ["5", "—", "7", "??", "9"]})
    out, _ = clean(df, rules=["missing_token_normalization"])
    assert out["qty"].isna().tolist() == [False, True, False, True, False]


def test_a_real_word_in_a_numeric_column_still_blocks_conversion():
    df = pd.DataFrame({"amount": ["100", "200", "banana"]})
    out, _ = clean(df)
    assert out["amount"].tolist() == ["100", "200", "banana"]


# ------------------------------------------------------------------ reporting
def test_quality_report_flags_inconsistent_casing_and_clears_after_cleaning():
    df = pd.DataFrame({"full_name": ["DAVID ADEYEMI", "bola okafor", "Ada Eze", "Musa Obi"]})
    before = generate_report(df)
    assert any(f["issue"] == "inconsistent_casing" for f in before["findings"])
    out, _ = clean(df)
    assert not any(f["issue"] == "inconsistent_casing" for f in generate_report(out)["findings"])


def test_repeated_negative_amount_is_reported_as_a_placeholder():
    df = pd.DataFrame({"amount_paid": [-100.0] * 6 + [500.0, 1200.0, 90.0]})
    r = generate_report(df)
    f = next(f for f in r["findings"] if f["issue"] == "negative_amount")
    assert f["affected_count"] == 6 and "placeholder" in f["detail"]
    out, _ = clean(df)
    assert (out["amount_paid"] == -100.0).sum() == 6              # reported, never silently changed


# ------------------------------------------------------------------ second round (found by running a messy file end to end)
def test_shouting_variant_of_a_word_is_not_an_acronym():
    df = pd.DataFrame({"plan": ["Pro"] * 3 + ["PRO"] * 5 + ["pro"] * 2 + ["Basic"] * 4})
    out, _ = clean(df)
    assert set(out["plan"]) == {"Pro", "Basic"}


def test_ok_and_OK_resolve_to_the_proper_form_not_the_lowercase_majority():
    assert casing.canonical_variant(["OK", "ok"], pd.Series({"OK": 5, "ok": 20})) == "OK"
    assert casing.canonical_variant(["Pro", "PRO", "pro"], pd.Series({"Pro": 1, "PRO": 5, "pro": 9})) == "Pro"
    assert casing.canonical_variant(["I.T.", "IT"], pd.Series({"I.T.": 9, "IT": 2})) == "IT"
    assert casing.canonical_variant(["Walk-in", "Walk in"], pd.Series({"Walk-in": 9, "Walk in": 2})) == "Walk-in"


def test_acronym_stays_when_only_it_and_its_lowercase_exist():
    assert casing.plan_category_labels(pd.Series(["IT", "it", "Finance", "Finance"])) == {"it": "IT"}


def test_flag_column_with_a_stray_value_still_gets_yes_no_unified():
    df = pd.DataFrame({"is_verified": ["yes", "Y", "TRUE", "1", "no", "N", "false", "0"] * 4 + ["maybe"]})
    out, _ = clean(df)
    assert set(out["is_verified"]) == {"Yes", "No", "maybe"} or set(out["is_verified"]) == {"Yes", "No", "Maybe"}


def test_generic_column_with_yes_no_and_junk_is_not_touched():
    df = pd.DataFrame({"remarks_flag_x": ["yes", "no", "maybe", "n/a-ish"] * 3, "answer": ["yes", "no", "perhaps"] * 4})
    out, _ = clean(df, rules=["boolean_standardization"])
    assert out["answer"].tolist() == df["answer"].tolist()


def test_currency_labels_are_brought_to_iso_codes():
    df = pd.DataFrame({"currency_label": ["NGN", "naira", "₦", "USD", "$", "GBP", "£", "NGN/USD", "mystery"]})
    out, _ = clean(df, rules=["currency_label_standardization"])
    assert out["currency_label"].tolist() == ["NGN", "NGN", "NGN", "USD", "USD", "GBP", "GBP", "NGN/USD", "mystery"]
    other = pd.DataFrame({"plan": ["naira", "$", "£"]})
    assert clean(other, rules=["currency_label_standardization"])[0]["plan"].tolist() == ["naira", "$", "£"]


def test_worded_placeholders_count_as_missing_and_naija_is_nigeria():
    df = pd.DataFrame({"signup_date": ["2024-01-02", "not known", "TBD", "Not Available"]})
    out, _ = clean(df, rules=["missing_token_normalization"])
    assert out["signup_date"].isna().tolist() == [False, True, True, True]
    assert detectors.standardize_country_value("Naija") == "Nigeria"
