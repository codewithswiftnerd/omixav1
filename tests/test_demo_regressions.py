"""Regressions found while demoing Omixa on a deliberately messy customer file.

1. A valid email with spaces around it is a whitespace fix, never a malformed address to blank.
2. The same person re-entered under a new ID is reported (and removable on an explicit choice); two different
   people who merely share details are not.
3. A missing person name is left blank, never replaced by the made-up name "Unknown".
"""
import pandas as pd

from cleaning import checks, profiling, resolutions
from cleaning.checks import CheckContext


def _ctx(df):
    prof = profiling.profile_dataset(df)["columns"]
    return CheckContext(df=df, profile=prof, now=pd.Timestamp("2026-11-03"))


def _customers():
    rows = []
    for i in range(1, 9):
        rows.append({"customer_id": f"C{1000+i}", "full_name": f"Person {i}", "email": f"p{i}@example.com",
                     "phone": f"0803000{i:04d}", "city": "Lagos", "amount": 1000 * i})
    return pd.DataFrame(rows)


def test_padded_valid_email_is_not_flagged_or_blanked():
    df = _customers()
    df.loc[2, "email"] = "  p3@example.com "
    df.loc[4, "email"] = "no-email"
    found = checks.check_invalid_email(_ctx(df))
    assert found and found[0]["affected"] == 1 and found[0]["sample_values"] == ["no-email"]
    out, n = resolutions._resolve_invalid_email(df.copy(), "email", "blank_invalid")
    assert n == 1
    assert out.loc[2, "email"] == "  p3@example.com "      # untouched; the formatting rule trims it later
    assert pd.isna(out.loc[4, "email"])


def test_same_person_under_new_id_is_reported_and_removed_on_request():
    df = _customers()
    dup = df.loc[[1]].copy()
    dup["customer_id"] = "C1099"
    df = pd.concat([df, dup], ignore_index=True)
    found = checks.check_possible_duplicate_records(_ctx(df))
    assert found and found[0]["issue"] == "possible_duplicate_records" and found[0]["affected"] == 2
    out, n = resolutions._resolve_possible_duplicates(df.copy(), "customer_id", "remove_near_duplicates")
    assert n == 1 and "C1099" not in set(out["customer_id"]) and "C1002" in set(out["customer_id"])


def test_different_people_with_same_details_are_not_grouped():
    df = _customers()
    other = df.loc[[1]].copy()
    other["customer_id"], other["email"], other["phone"] = "C1099", "someone.else@example.com", "08031112222"
    df = pd.concat([df, other], ignore_index=True)
    assert checks.check_possible_duplicate_records(_ctx(df)) == []


def test_missing_person_name_is_never_imputed():
    df = _customers()
    df.loc[3, "full_name"] = None
    prof = profiling.profile_column("full_name", df["full_name"], len(df))
    assert profiling.never_impute(prof)
