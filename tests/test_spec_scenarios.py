"""Spec scenarios D (currency), E (missing values), F (numbers/dates), G (several rules), I (report vs export).

These run the real pipeline end to end and compare the exported file with the original, cell by cell.
"""
import os, shutil, uuid

import pandas as pd
import pytest

import processing.pipeline as pl
import utils.file_handler as fh
from cleaning import currencies
from config import Config


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "TEMP_DIR", str(tmp_path), raising=False)

    def _run(df, rules=None, resolutions=None):
        job = str(uuid.uuid4()); d = fh.job_dir_path(job); os.makedirs(d, exist_ok=True)
        df.to_csv(os.path.join(d, "source.csv"), index=False)
        res = pl.run_pipeline(job, rules=rules, resolutions=resolutions)
        out = pd.read_csv(os.path.join(d, "cleaned.csv"), dtype=str, keep_default_na=False)
        return res, out
    return _run


def _ids(n):
    return [f"T{i:03d}" for i in range(n)]


# ------------------------------------------------------------------ D: currency
@pytest.mark.parametrize("value,marker", [
    ("₦1,500.00", "₦"), ("$45.50", "$"), ("€12,00", "€"), ("£30", "£"), ("USD 20", "USD"),
    ("20 usd", None),                       # ISO codes are matched case-sensitively: lowercase text is just text
    ("(₦500)", "₦"), ("-$5", "$"), ("NGN5000", "NGN"),
    ("1500", None), ("1,200", None), ("abc", None), ("N/A", None), ("", None),
])
def test_currency_marker_names_the_symbol_or_code_a_value_carries(value, marker):
    assert currencies.currency_marker(value) == marker


def test_a_column_mixing_currencies_is_never_flattened_into_bare_numbers(run):
    amounts = ["₦1,500.00", "₦2,000.50", "$45.50", "€12,00", "£30", "USD 20", "₦3,250", "$10.00"] * 3
    df = pd.DataFrame({"id": _ids(len(amounts)), "Amount": amounts})
    res, out = run(df)                                              # default rules, "Clean numbers" ticked
    assert out["Amount"].tolist() == amounts                         # every symbol and code preserved, nothing merged
    left = res["details"]["numeric_text_cleaning"]["left_unconverted"]["Amount"]
    assert left["reason"] == "mixed_currencies" and set(left["currencies"]) >= {"₦", "$", "€", "£"}
    assert len(out) == len(df)


def test_a_single_currency_column_is_cleaned_to_accurate_amounts_when_clean_numbers_is_selected(run):
    amounts = ["₦1,500.00", "₦2,000.50", "₦3,250", "₦ 410", "₦0", "₦12,000.75"] * 3
    df = pd.DataFrame({"id": _ids(len(amounts)), "Amount": amounts})
    _, out = run(df, rules=["numeric_text_cleaning"])
    assert [float(x) for x in out["Amount"]] == [1500.0, 2000.5, 3250.0, 410.0, 0.0, 12000.75] * 3


def test_naira_symbol_is_untouched_when_number_cleaning_is_not_selected(run):
    amounts = ["₦1,500.00", "₦2,000.50", "₦3,250"] * 4
    df = pd.DataFrame({"id": _ids(len(amounts)), "Amount": amounts})
    _, out = run(df, rules=["formatting"])
    assert out["Amount"].tolist() == amounts


# ------------------------------------------------------------------ E: missing values
def _missing_frame():
    n = 60
    qty = [("1", "2", "3", "4")[i % 4] for i in range(n)]
    method = [("Cash", "Card", "Wallet")[i % 3] for i in range(n)]
    for i, (q, m) in enumerate([("", ""), ("   ", "   "), ("N/A", "N/A"), ("NULL", "NULL"), ("UNKNOWN", "UNKNOWN"), ("ERROR", "ERROR")]):
        qty[i], method[i] = q, m
    return pd.DataFrame({"id": _ids(n), "Qty": qty, "Method": method})


def test_strong_markers_are_blanked_weak_and_unlisted_ones_are_preserved_and_nothing_is_invented(run):
    df = _missing_frame()
    res, out = run(df, rules=["missing_token_normalization"])
    for col in ("Qty", "Method"):
        assert out[col].iloc[0:4].tolist() == ["", "", "", ""]          # empty, whitespace-only, N/A, NULL -> missing
        assert out[col].iloc[5] == "ERROR"                              # not a built-in marker: preserved, not blanked or zeroed
    assert out["Method"].iloc[4] == "UNKNOWN"                          # weak marker in a text column: kept and flagged
    assert out["Qty"].iloc[4] == ""                                    # weak marker in a clearly numeric column: missing
    assert len(out) == len(df)
    assert out["Qty"].tolist()[6:] == df["Qty"].tolist()[6:]            # valid values untouched
    assert "missing_values" not in res["rules_applied"]                # no fill rule ran, so nothing was filled


def test_filling_gaps_only_happens_when_selected_and_never_invents_numbers(run):
    df = _missing_frame()
    res, out = run(df, rules=["missing_token_normalization", "missing_values"])
    assert out["Method"].iloc[0:4].tolist() == ["Unknown"] * 4         # text gaps get the literal label, not a guessed value
    assert out["Qty"].iloc[0:4].tolist() == ["", "", "", ""]            # numeric gaps stay empty
    imp = res["integrity_report"]["imputation"]
    assert imp["estimated"] == 0 and imp["labelled_unknown"] == 4       # reported as labels, not as estimates
    assert res["missing_tracking"]["Method"]["filled"] == 4


def test_without_selecting_any_missing_rule_nothing_is_blanked_or_filled(run):
    df = _missing_frame()
    _, out = run(df, rules=["formatting"])
    assert out["Qty"].iloc[2:6].tolist() == ["N/A", "NULL", "UNKNOWN", "ERROR"]


# ------------------------------------------------------------------ F: numbers and dates
def test_dates_are_standardised_only_when_the_reading_is_unambiguous(run):
    dates = ["2023-01-05", "05/01/2023", "13/01/2023", "01/13/2023", "31/02/2023", "2023/02/30",
             "", "UNKNOWN", "Jan 5, 2023", "5 January 2023", "not a date", "2023-03-09"] * 3
    df = pd.DataFrame({"id": _ids(len(dates)), "Date": dates})
    res, out = run(df, rules=["date_standardization"])
    got = out["Date"].tolist()[:12]
    assert got[0] == "2023-01-05"
    assert got[1] == "05/01/2023"                                      # DD/MM vs MM/DD: not guessed
    assert got[2] == "2023-01-13" and got[3] == "2023-01-13"            # only one valid reading each
    assert got[4] == "31/02/2023" and got[5] == "2023/02/30"            # impossible dates preserved as uploaded
    assert got[6] == "" and got[7] == "UNKNOWN" and got[10] == "not a date"
    assert got[8] == "2023-01-05" and got[9] == "2023-01-05"            # named months are unambiguous
    assert len(out) == len(df)


def test_invalid_numbers_are_flagged_not_turned_into_zero(run):
    vals = ["12.5", "1,250.00", "abc", "forty", "7", "9.99", "3", "UNKNOWN"] * 5
    df = pd.DataFrame({"id": _ids(len(vals)), "Price": vals})
    res, out = run(df, rules=["numeric_text_cleaning"])
    got = out["Price"].tolist()
    assert got[2] == "abc" and got[3] == "forty" and got[7] == "UNKNOWN"
    assert "0" not in got and "0.0" not in got                          # no invalid string became zero
    assert float(got[1]) == 1250.0
    assert res["details"]["numeric_text_cleaning"]["left_unconverted"]["Price"]["count"] > 0


# ------------------------------------------------------------------ G: several rules together
def _multi_frame():
    n = 30
    return pd.DataFrame({
        "id": _ids(n),
        "Name": [("  alice  ", "carol", "dave ", "erin")[i % 4] for i in range(n)],
        "Price": [("$1,200.50", "$300", "45", "$7.25")[i % 4] for i in range(n)],
        "Phone": [("0803 123 4567", "08021234567")[i % 2] for i in range(n)],
    })


def test_several_rules_compose_and_do_not_undo_each_other(run):
    df = _multi_frame()
    res, out = run(df, rules=["formatting", "case_standardization", "numeric_text_cleaning", "duplicates"])
    assert out["Name"].iloc[0:4].tolist() == ["Alice", "Carol", "Dave", "Erin"]      # trimmed AND capitalised
    assert [float(x) for x in out["Price"].iloc[0:4]] == [1200.5, 300.0, 45.0, 7.25]
    assert len(out) == len(df) and out["id"].tolist() == df["id"].tolist()            # distinct ids: nothing removed as duplicate
    assert "phone_cleaning" not in res["rules_applied"]
    assert out["Phone"].tolist() == df["Phone"].tolist()                              # an unselected rule changed nothing


def test_request_order_does_not_change_the_result(run):
    df = _multi_frame()
    rules = ["formatting", "case_standardization", "numeric_text_cleaning", "duplicates"]
    _, a = run(df, rules=rules)
    _, b = run(df, rules=list(reversed(rules)))
    assert a.equals(b)


# ------------------------------------------------------------------ I: report vs export
def test_report_counts_reconcile_with_the_exported_file(run):
    n = 80
    df = pd.DataFrame({
        "id": _ids(n),
        "Item": [("Tea", "Cake", "", "N/A", "UNKNOWN")[i % 5] for i in range(n)],
        "Qty": [("1", "2", "", "N/A", "ERROR", "3")[i % 6] for i in range(n)],
        "Date": [("2023-01-05", "", "UNKNOWN", "5 Jan 2023")[i % 4] for i in range(n)],
    })
    res, out = run(df, rules=["formatting", "missing_token_normalization", "numeric_text_cleaning",
                              "date_standardization", "duplicates", "missing_values"])
    # rows
    assert res["rows_in"] == len(df) and res["rows_out"] == len(out) == len(df)
    assert res["change_log"]["removed_rows_total"] == 0
    assert res["integrity_report"]["export_verified"] is True              # the export was re-read and matched
    # Step-level log vs net cells: 16 cells go 'N/A' -> blank -> 'Unknown' (two logged steps, one net change).
    summary = res["integrity_report"]["issue_summary"]
    differing = int((df.fillna("").astype(str).values != out.values).sum())
    assert not res["change_log"]["truncated"]
    assert summary["unique_cells_changed"] == differing                      # exact: every net-changed cell, once
    assert res["change_log"]["cell_changes_total"] == summary["cells_normalised"] \
        == sum(o["cells_changed"] for o in res["integrity_report"]["operations_performed"])
    assert summary["cells_normalised"] > summary["unique_cells_changed"]     # and the two are never confused
    # missing counts equal the blank cells really present in the export
    blanks = int((out == "").sum().sum())
    assert sum(v["missing_after"] for v in res["missing_tracking"].values()) == blanks
    # gaps filled are exactly the blanks that became the 'Unknown' label, none estimated
    filled = int(((df["Item"].isin(["", "N/A"])) & (out["Item"] == "Unknown")).sum())
    imp = res["integrity_report"]["imputation"]
    assert imp["estimated"] == 0 and imp["labelled_unknown"] == sum(v["filled"] for v in res["missing_tracking"].values())
    assert imp["labelled_unknown"] >= filled


@pytest.mark.parametrize("values,mixed", [
    (["₦2,500", "NGN 5000"], False),        # one currency written two ways
    (["₦1", "Naira 5"], False),
    (["$5", "USD 7"], False),               # '$' can be USD
    (["₦25,000", "$1,200.50", "(45)"], True),
    (["£3", "€4"], True),
    (["R 10", "₦ 2"], True),
    (["1", "2", "abc"], False),             # no markers at all
])
def test_mixed_currency_detection_compares_currencies_not_spellings(values, mixed):
    assert bool(currencies.mixed_currency_markers(values)) is mixed
