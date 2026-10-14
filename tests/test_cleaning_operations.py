"""Stages 4-5: missing values, currency, numbers/dates, operation order, report consistency.
Every expectation here was first observed on real pipeline output, then pinned."""
import os, shutil, uuid

import pandas as pd
import pytest

import processing.pipeline as pl
import utils.file_handler as fh
from config import Config
from cleaning.rules import apply_rules, canonical_rule_order


def run(tmp_path, monkeypatch, df, rules=None, **kw):
    monkeypatch.setattr(Config, "TEMP_DIR", str(tmp_path), raising=False)
    p = tmp_path / "in.csv"; df.to_csv(p, index=False)
    job = str(uuid.uuid4()); d = fh.job_dir_path(job); os.makedirs(d, exist_ok=True)
    shutil.copy(p, os.path.join(d, "source.csv"))
    res = pl.run_pipeline(job, rules=rules, **kw)
    return res, pd.read_csv(os.path.join(d, "cleaned.csv"), dtype=str, keep_default_na=False)


# ---------------------------------------------------------------- D: currency
def test_d_currency_symbols_and_valid_amounts_are_preserved_and_invalid_text_is_not_zeroed(tmp_path, monkeypatch):
    amounts = ["₦1,500.50", "$20", "€3.5", "USD 100", "1,500", "abc", "12.5", "₦2,000", "1.234,56", "1,5"]
    df = pd.DataFrame({"id": [f"T{i}" for i in range(len(amounts))], "amount": amounts})
    _, out = run(tmp_path, monkeypatch, df)
    assert out["amount"].tolist() == amounts          # symbols kept; "abc" is not 0; ambiguous 1.234,56 / 1,5 untouched
    assert len(out) == len(df)


# ---------------------------------------------------------------- E: missing values
def test_e_markers_whitespace_and_empty_cells_follow_the_selected_rules(tmp_path, monkeypatch):
    notes = ["N/A", "NULL", "n/a", "None", "  ", "", "UNKNOWN", "ERROR", "real text", "  padded  "]
    df = pd.DataFrame({"id": [f"T{i}" for i in range(len(notes))], "note": notes})
    _, out = run(tmp_path, monkeypatch, df)
    got = out["note"].tolist()
    assert got[:6] == [""] * 6                       # recognised markers, whitespace-only and empty -> blank
    assert got[6:8] == ["UNKNOWN", "ERROR"]          # NOT configured as missing: kept exactly, never silently blanked
    assert got[8:] == ["real text", "padded"]        # only whitespace trimmed


def test_e_nothing_is_filled_unless_the_fill_rule_is_selected(tmp_path, monkeypatch):
    df = pd.DataFrame({"id": ["a", "b", "c"], "city": ["Lagos", "", "Abuja"]})
    res, out = run(tmp_path, monkeypatch, df)
    assert out["city"].tolist() == ["Lagos", "", "Abuja"]
    assert res["integrity_report"]["missing_values"]["filled"] == 0


def test_e_fill_rule_labels_text_gaps_and_never_writes_text_into_numeric_columns(tmp_path, monkeypatch):
    df = pd.DataFrame({"id": [f"T{i}" for i in range(10)],
                       "qty": ["1", "2", "UNKNOWN", "4", "5", "ERROR", "2", "3", "", "1"],
                       "city": ["Lagos", "", "Abuja", "Lagos", "Abuja", "Lagos", "", "Abuja", "Lagos", "Abuja"]})
    res, out = run(tmp_path, monkeypatch, df, rules=["formatting", "missing_token_normalization", "missing_values"])
    assert "Unknown" not in out["qty"].tolist()      # was the defect: object-dtype numeric column got the word "Unknown"
    assert out["city"].tolist().count("Unknown") == 2
    assert out["id"].tolist() == df["id"].tolist()   # identifiers are never imputed
    ir = res["integrity_report"]
    assert any("labelled 'Unknown'" in w for w in ir["warnings"])
    assert ir["imputation"]["labelled_unknown"] == 2
    nm = res["details"]["missing_values"]["not_imputable"]
    assert any(h["column"] == "qty" for h in nm)


def test_report_missing_count_equals_blank_cells_in_the_export(tmp_path, monkeypatch):
    df = pd.DataFrame({"id": [f"T{i}" for i in range(8)], "a": ["x", "", "N/A", "y", "", "z", "x", "NULL"],
                       "b": ["1", "2", "", "4", "5", "", "7", "8"]})
    res, out = run(tmp_path, monkeypatch, df)
    reported = res["integrity_report"]["missing_values"]["after"]
    assert reported == int((out == "").sum().sum())


# ---------------------------------------------------------------- F: numbers and dates
def test_f_invalid_dates_are_preserved_and_distinguished_from_missing(tmp_path, monkeypatch):
    when = ["2024-01-05", "2024-01-06", "2024-01-07", "2024-02-30", "", "not a date", "2024-03-09", "2024-04-01"]
    df = pd.DataFrame({"id": [f"T{i}" for i in range(8)], "order_date": when})
    res, out = run(tmp_path, monkeypatch, df)
    assert out["order_date"].tolist()[3] == "2024-02-30" and out["order_date"].tolist()[5] == "not a date"
    assert out["order_date"].tolist()[4] == ""
    col = [c for c in res["validation_after"]["columns"] if c["column"] == "order_date"][0]
    assert col["counts"]["missing"] == 1             # the empty cell is "missing" ...
    assert col["counts"]["invalid"] + col["counts"].get("unresolved", 0) >= 2   # ... the bad ones are not


def test_f_ambiguous_day_month_is_not_guessed_but_follows_an_explicit_choice(tmp_path, monkeypatch):
    when = ["01/02/2024", "03/04/2024", "05/06/2024", "07/08/2024"]   # every value is valid both ways
    df = pd.DataFrame({"id": list("abcd"), "signup_date": when})
    _, untouched = run(tmp_path, monkeypatch, df)
    assert untouched["signup_date"].tolist() == when  # no silent DD/MM vs MM/DD guess
    _, day_first = run(tmp_path, monkeypatch, df, rules=["formatting"], resolutions=[
        {"column": "signup_date", "issue": "ambiguous_date_format", "choice": "day_first"}])
    assert day_first["signup_date"].tolist()[0] == "2024-02-01"
    _, month_first = run(tmp_path, monkeypatch, df, rules=["formatting"], resolutions=[
        {"column": "signup_date", "issue": "ambiguous_date_format", "choice": "month_first"}])
    assert month_first["signup_date"].tolist()[0] == "2024-01-02"


# ---------------------------------------------------------------- G: several operations, in order
def test_g_rule_execution_order_is_canonical_not_request_order():
    assert canonical_rule_order(["missing_values", "duplicates", "formatting", "formatting", "column_names"]) == \
        ["column_names", "formatting", "duplicates", "missing_values"]


def test_g_request_order_does_not_change_the_result_and_unselected_rules_do_not_run():
    df = pd.DataFrame({"name": ["  Ann ", "Ann", "Bob", "  Bob  "], "mail": ["A@X.COM ", "a@x.com", "b@x.com", "b@x.com"]})
    a, _ = apply_rules(df.copy(), rules=["duplicates", "formatting"])      # dedupe asked for BEFORE trimming
    b, _ = apply_rules(df.copy(), rules=["formatting", "duplicates"])
    assert a.reset_index(drop=True).equals(b.reset_index(drop=True))
    assert len(a) == 3                                                        # trimmed first, so Bob/Bob collapse
    c, log = apply_rules(df.copy(), rules=["formatting"])
    assert log["rules_applied"] == ["formatting"] and c["mail"].tolist()[0] == "A@X.COM"   # email_cleaning did not run


def test_g_fill_never_runs_before_duplicate_removal():
    df = pd.DataFrame({"k": ["a", "a", "b"], "v": ["x", "x", None]})
    out, log = apply_rules(df, rules=["missing_values", "duplicates"])
    assert log["rules_applied"] == ["duplicates", "missing_values"]
    assert len(out) == 2


# ---------------------------------------------------------------- I: report consistency
def test_i_report_counts_match_the_input_and_the_exported_file(tmp_path, monkeypatch):
    df = pd.DataFrame({"id": [f"T{i}" for i in range(6)], "name": [" a ", "b", "N/A", "d ", "e", "b"],
                       "city": ["Lagos", "", "Abuja", "Lagos", "", "Abuja"]})
    res, out = run(tmp_path, monkeypatch, df)
    ir = res["integrity_report"]
    assert res["rows_in"] == len(df) and res["rows_out"] == len(out)
    assert ir["row_reconciliation"]["unexplained"] == 0
    assert ir["missing_values"]["after"] == int((out == "").sum().sum())
    changes = res["change_log"]["cell_changes_total"] if "cell_changes_total" in res["change_log"] else None
    assert ir["row_reconciliation"]["cells_modified"] >= 3     # " a ", "d ", "N/A"->blank were real changes
    claimed = {e["rule"] for e in res["change_log"]["cell_changes"]}
    assert claimed <= set(res["rules_applied"])                # every claimed change belongs to a rule that ran


def test_f_only_a_value_that_can_be_one_format_is_converted_ambiguous_neighbours_are_kept(tmp_path, monkeypatch):
    when = ["01/02/2024", "03/04/2024", "13/01/2024", "05/06/2024"]    # 13/01 can only be day-first
    df = pd.DataFrame({"id": list("abcd"), "signup_date": when})
    res, out = run(tmp_path, monkeypatch, df)
    assert out["signup_date"].tolist() == ["01/02/2024", "03/04/2024", "2024-01-13", "05/06/2024"]
    assert any(f["issue"] == "ambiguous_date_format" for f in res["quality_report"]["findings"])   # still asks the user
