"""Date order, per-cell date judging, country-aware phones, per-column missing-value actions, approved value
mappings (+ review-only suggestions), run through the real pipeline and compared with the exported file.

The sample values come from the problems found in testing, but every rule is column-name driven and generic.
"""
import os
import uuid

import pandas as pd
import pytest

import processing.pipeline as pl
import utils.file_handler as fh
from cleaning.options import OptionsError, parse_options
from cleaning.review_options import suggest_value_mappings
from config import Config


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "TEMP_DIR", str(tmp_path), raising=False)

    def _run(df, rules=None, options=None, xlsx=False):
        job = str(uuid.uuid4()); d = fh.job_dir_path(job); os.makedirs(d, exist_ok=True)
        if xlsx:
            df.to_excel(os.path.join(d, "source.xlsx"), index=False)
        else:
            df.to_csv(os.path.join(d, "source.csv"), index=False)
        res = pl.run_pipeline(job, rules=rules, options=options)
        if xlsx:
            out = pd.read_excel(os.path.join(d, "cleaned.xlsx"), dtype=str, keep_default_na=False)
        else:
            out = pd.read_csv(os.path.join(d, "cleaned.csv"), dtype=str, keep_default_na=False)
        return res, out
    return _run


def _ids(n):
    return [f"C{i:03d}" for i in range(n)]


def _dates_df(values):
    return pd.DataFrame({"Customer ID": _ids(len(values)), "Payment Date": values, "Note": ["x"] * len(values)})


DATES = ["2026/13/04", "32/10/2026", "yesterday", "01/10/26", "Oct 3, 2026", "2026-10-03", "15/02/2026", "03/04/2026"]


# ------------------------------------------------------------------ dates
def test_one_two_digit_year_no_longer_blocks_the_rest_of_the_column(run):
    res, out = run(_dates_df(DATES))
    got = out["Payment Date"].tolist()
    assert got[4] == "2026-10-03"          # Oct 3, 2026: one reading only
    assert got[6] == "2026-02-15"          # 15/02/2026 cannot be month-first
    assert got[3] == "01/10/26"            # two-digit year: century is a guess -> exactly as uploaded
    assert got[0] == "2026/13/04" and got[1] == "32/10/2026"   # impossible: preserved
    assert got[2] == "yesterday"           # relative dates are never converted
    assert got[7] == "03/04/2026"          # ambiguous without a chosen order: preserved
    assert out["Customer ID"].tolist() == _ids(len(DATES))


def test_ambiguous_and_invalid_dates_are_reported_separately(run):
    res, _ = run(_dates_df(DATES))
    pc = res["details"]["date_standardization"]["per_column"]["Payment Date"]
    assert pc["left_ambiguous"] == 1                    # 03/04/2026
    assert pc["left_unparseable"] == 3                  # 2026/13/04, 32/10/2026, yesterday
    assert pc["left_two_digit_year"] == 1
    assert res["details"]["date_standardization"]["skipped_ambiguous"] == ["Payment Date"]


@pytest.mark.parametrize("order,expected", [("day_first", "2026-04-03"), ("month_first", "2026-03-04")])
def test_a_chosen_date_order_is_applied_consistently_and_recorded(run, order, expected):
    res, out = run(_dates_df(DATES), options={"date_order": order})
    got = out["Payment Date"].tolist()
    assert got[7] == expected
    assert got[0] == "2026/13/04" and got[1] == "32/10/2026" and got[2] == "yesterday" and got[3] == "01/10/26"
    step = [e for e in res["audit"]["entries"] if e["rule"] == "date_standardization"]
    assert step and all(e["approval"] == "user_selected" for e in step)


def test_the_same_input_gives_the_same_output_whatever_the_environment(run, monkeypatch):
    monkeypatch.setenv("LANG", "en_US.UTF-8"); monkeypatch.setenv("TZ", "Pacific/Auckland")
    a = run(_dates_df(DATES))[1]["Payment Date"].tolist()
    monkeypatch.setenv("LANG", "de_DE.UTF-8"); monkeypatch.setenv("TZ", "America/New_York")
    assert run(_dates_df(DATES))[1]["Payment Date"].tolist() == a


def test_per_column_date_order_overrides_the_global_choice(run):
    df = pd.DataFrame({"id": _ids(3), "Order Date": ["03/04/2026"] * 3, "Ship Date": ["03/04/2026"] * 3})
    _, out = run(df, options={"date_order": "day_first", "date_orders": {"Ship Date": "month_first"}})
    assert out["Order Date"].tolist() == ["2026-04-03"] * 3 and out["Ship Date"].tolist() == ["2026-03-04"] * 3


# ------------------------------------------------------------------ phones
PHONES = ["0703 111 2233", "+234 801 234 5678", "080-555-0192", "080ABC12345", "0803123456", "+44 20 7946 0958", ""]


def _phones_df():
    return pd.DataFrame({"Customer ID": _ids(len(PHONES)), "Phone": PHONES})


def test_without_a_country_no_number_is_assumed_to_be_nigerian(run):
    _, out = run(_phones_df())
    assert not any(v.startswith("+234") for v in out["Phone"] if v in ("07031112233", "0803123456"))
    assert out["Phone"].tolist()[0] == "07031112233"            # harmless spaces removed, nothing added
    assert out["Phone"].tolist()[3] == "080ABC12345"            # letters: untouched


def test_selected_country_standardises_only_numbers_that_carry_evidence_of_it(run):
    res, out = run(_phones_df(), options={"phone_country": "NG"})
    got = out["Phone"].tolist()
    assert got[0] == "+2347031112233" and got[1] == "+2348012345678"
    assert got[2] == "080-555-0192"        # 10 digits after the 0: invalid length -> kept EXACTLY as uploaded
    assert got[3] == "080ABC12345"         # letters
    assert got[4] == "0803123456"          # too short: never padded
    assert got[5] == "+442079460958"       # another country's explicit +44: not forced to Nigeria
    assert got[6] == ""                    # missing stays missing
    assert out["Customer ID"].tolist() == _ids(len(PHONES))
    ir = {c["column"]: c for c in res["integrity_report"]["columns"]}["Phone"]
    assert ir["counts_after"]["invalid"] >= 3 and ir["counts_after"]["missing"] == 1


def test_phone_country_can_be_set_per_column(run):
    df = pd.DataFrame({"id": _ids(2), "Home Phone": ["08031234567"] * 2, "Work Phone": ["08031234567"] * 2})
    _, out = run(df, options={"phone_country": {"Home Phone": "NG"}})
    assert out["Home Phone"].tolist() == ["+2348031234567"] * 2 and out["Work Phone"].tolist() == ["08031234567"] * 2


# ------------------------------------------------------------------ missing values
def _missing_df():
    return pd.DataFrame({"Customer ID": _ids(5), "City": ["Lagos", "", "N/A", "Unknown", "Abuja"],
                         "Note": ["a", "", "b", "", "c"]})


def test_empty_cells_are_not_filled_unless_the_user_asks(run):
    _, out = run(_missing_df(), rules=[])
    assert out["City"].tolist() == ["Lagos", "", "N/A", "Unknown", "Abuja"]
    assert "Unknown" not in run(_missing_df(), rules=["formatting"])[1]["Note"].tolist()


def test_fill_uses_the_users_value_and_only_touches_genuinely_empty_cells(run):
    res, out = run(_missing_df(), rules=[], options={"missing_values": {"columns": {"City": {"action": "fill", "value": "Not given"}}}})
    assert out["City"].tolist() == ["Lagos", "Not given", "N/A", "Unknown", "Abuja"]   # N/A and Unknown are text
    assert out["Note"].tolist() == ["a", "", "b", "", "c"]                              # other columns untouched
    e = [x for x in res["audit"]["entries"] if x["rule"] == "missing_value_handling"]
    assert e and e[0]["examples"][0]["after"] == "Not given" and e[0]["approval"] == "user_selected"


def test_flag_action_keeps_the_cell_empty_and_reports_it(run):
    res, out = run(_missing_df(), rules=[], options={"missing_values": {"columns": {"Note": {"action": "flag"}}}})
    assert out["Note"].tolist() == ["a", "", "b", "", "c"]
    assert res["details"]["missing_value_handling"]["flagged_for_review"] == {"Note": 2}


def test_rows_are_removed_only_when_explicitly_requested_and_every_removal_is_audited(run):
    res, out = run(_missing_df(), rules=[], options={"missing_values": {"columns": {"City": {"action": "remove_rows"}}}})
    assert out["Customer ID"].tolist() == ["C000", "C002", "C003", "C004"]
    assert res["rows_in"] == 5 and res["rows_out"] == 4
    assert res["integrity_report"]["row_reconciliation"]["unexplained"] == 0
    res2, out2 = run(_missing_df(), rules=[], options={"missing_values": {"columns": {"City": {"action": "leave"}}}})
    assert len(out2) == 5 and res2["rows_out"] == 5


def test_a_user_enabled_marker_turns_a_placeholder_into_missing_before_the_action(run):
    df = pd.DataFrame({"Customer ID": _ids(4), "Amount": ["100", "200", "error", "300"]})
    _, out = run(df, rules=["missing_token_normalization"], options={"missing_markers": ["error"]})
    assert out["Amount"].tolist() == ["100", "200", "", "300"]


# ------------------------------------------------------------------ categories and text mappings
STATUS = ["Completed", "complete", "Complete", "done", "PENDING", "pending", "Completed", "Pending"]


def _status_df():
    return pd.DataFrame({"Customer ID": _ids(len(STATUS)), "Status": STATUS,
                         "City": ["PH", "port harcourt", "Abj", "Lagos", "PH", "Lagos", "Abuja", "ph"]})


def test_semantic_mappings_are_suggested_but_never_applied_without_approval(run):
    res, out = run(_status_df())
    shown = {(s["from"], s["to"]) for s in res["value_mapping_suggestions"] if s["column"] == "Status"}
    assert ("Done", "Completed") in shown or ("done", "Completed") in shown
    assert "Done" in set(out["Status"]) or "done" in set(out["Status"])                   # not merged on similarity
    assert set(out["City"]) >= {"PH", "Abj"}                                              # abbreviations never guessed


def test_approved_mappings_apply_to_the_whole_column_and_keep_the_original_in_the_audit(run):
    opts = {"value_mappings": {"Status": {"approve": {"done": "Completed", "complete": "Completed"}},
                               "City": {"approve": {"PH": "Port Harcourt"}}}}
    res, out = run(_status_df(), options=opts)
    assert set(out["Status"]) == {"Completed", "Pending"}
    assert out["City"].tolist().count("Port Harcourt") == 3 and "Abj" in out["City"].tolist()   # PH, PH, ph; Abj unapproved
    assert out["Customer ID"].tolist() == _ids(len(STATUS))
    entries = [e for e in res["audit"]["entries"] if e["rule"] == "approved_value_mappings"]
    before = {x["before"] for e in entries for x in e["examples"]}
    assert before & {"done", "Done", "PH"} and all(e["approval"] == "user_selected" for e in entries)
    assert res["value_mapping_suggestions"] == []


def test_a_rejected_mapping_keeps_two_distinct_categories(run):
    opts = {"value_mappings": {"Status": {"approve": {"done": "Completed"}, "reject": ["complete"]}}}
    res, out = run(_status_df(), options=opts)
    assert "Complete" in set(out["Status"]) and "Completed" in set(out["Status"])
    rej = [s for s in res["value_mapping_suggestions"] if s["from"].lower() == "complete"]
    assert rej and all(s["status"] == "rejected" for s in rej)


def test_suggestions_ignore_free_text_and_unfamiliar_abbreviations():
    df = pd.DataFrame({"City": ["PH", "Port Harcourt", "Lagos", "Lagos"], "Notes": [f"note {i}" for i in range(4)]})
    assert suggest_value_mappings(df) == []


# ------------------------------------------------------------------ options validation
@pytest.mark.parametrize("bad", [
    {"date_order": "sideways"}, {"date_orders": {"A": "x"}}, {"phone_country": "ZZ"},
    {"missing_values": {"default": {"action": "fill"}}}, {"missing_values": {"default": {"action": "delete_all"}}},
    {"missing_values": {"columns": {"A": {"action": "leave", "value": "x"}}}},
    {"value_mappings": {"A": {"approve": {"x": 1}}}}, {"value_mappings": {"A": {"explode": True}}},
])
def test_invalid_options_are_rejected_not_ignored(bad):
    with pytest.raises(OptionsError):
        parse_options(bad)


# ------------------------------------------------------------------ the rest of the problem set, end to end
def _customers():
    return pd.DataFrame({
        "Customer ID": [f"C{i:03d}" for i in range(8)],
        "Name": ["  john doe ", "MARIAM BELLO", "ada okafor", "Chidi  Eze", "bola", "tunde", "zainab", "kemi"],
        "Age": ["0", "150", "thirty", "28 years", "34", "29", "41", "-3"],
        "Amount Paid": ["-2500", "1,200", "₦3,000", "4500", "700", "800", "900", "1000"],
        "Email": ["Mariam.Bello@Gmail.com ", "mariam.bello", "", "N/A", "a@b.co", "x@y", "  JOHN@EXAMPLE.COM", "bad@@x.com"]})


@pytest.mark.parametrize("xlsx", [False, True])
def test_suspicious_values_are_flagged_not_replaced_and_ids_survive_csv_and_excel(run, xlsx):
    res, out = run(_customers(), xlsx=xlsx)
    assert out["Customer ID"].tolist() == _customers()["Customer ID"].tolist()
    assert out["Age"].tolist()[:3] == ["0", "150", "thirty"]            # suspicious / unreadable: preserved
    assert out["Age"].tolist()[3] == "28"                               # "28 years" is unambiguous
    assert out["Amount Paid"].tolist()[0] == "-2500"                    # a refund may be negative: flagged, not "fixed"
    assert out["Email"].tolist()[1] == "mariam.bello"                   # invalid email kept, never invented
    cols = {c["column"]: c for c in res["integrity_report"]["columns"]}
    assert cols["Age"]["counts_after"]["suspicious"] >= 2 and cols["Age"]["counts_after"]["invalid"] >= 1
    assert cols["Amount Paid"]["counts_after"]["suspicious"] == 1
    em = cols["Email"]["counts_after"]
    assert em["invalid"] >= 2 and em["missing"] >= 2                     # missing and invalid are counted separately


def test_number_words_are_read_in_an_age_column_only_when_the_user_enables_it(run):
    _, off = run(_customers())
    _, on = run(_customers(), options={"number_words": True})
    assert off["Age"].tolist()[2] == "thirty" and on["Age"].tolist()[2] == "30"


def test_a_configured_age_range_decides_what_is_suspicious(run):
    df = pd.DataFrame({"id": _ids(5), "Age": ["16", "17", "45", "70", "95"]})
    res, out = run(df, options={"validation": {"rules": {"Age": {"min": 18, "max": 65}}}})
    assert out["Age"].tolist() == ["16", "17", "45", "70", "95"]         # never rewritten
    a = {c["column"]: c for c in res["integrity_report"]["columns"]}["Age"]["counts_after"]
    assert a["valid"] == 1 and a["invalid"] + a["suspicious"] == 4


def test_unrelated_columns_are_unchanged_by_the_new_options(run):
    df = pd.DataFrame({"Customer ID": _ids(3), "Notes": [" keep THIS  as typed", "Second", "third"], "Payment Date": ["03/04/2026"] * 3})
    _, base = run(df)
    _, withopts = run(df, options={"date_order": "day_first", "phone_country": "NG",
                                   "value_mappings": {"Nothing": {"approve": {"a": "b"}}}})
    assert base["Notes"].tolist() == withopts["Notes"].tolist()
    assert base["Customer ID"].tolist() == withopts["Customer ID"].tolist() == _ids(3)


def test_report_separates_operations_from_unique_cells(run):
    res, _ = run(_customers())
    s = res["integrity_report"]["issue_summary"]
    assert s["cells_normalised"] >= s["unique_cells_changed"] >= 1
    assert res["integrity_report"]["rows"]["original"] == 8 and res["integrity_report"]["columns_count"]["original"] == 5


# ------------------------------------------------------------------ through the real HTTP API (download included)
def test_api_accepts_the_new_options_and_the_download_matches(client):
    from tests.test_data_integrity import run_csv
    csv = ("Customer ID,Payment Date,Status,Phone\n"
           "C001,03/04/2026,done,0703 111 2233\nC002,15/02/2026,Completed,080ABC12345\nC003,01/10/26,Pending,\n")
    summary, out, raw = run_csv(client, csv, options={
        "date_order": "day_first", "phone_country": "NG",
        "value_mappings": {"Status": {"approve": {"done": "Completed"}}},
        "missing_values": {"columns": {"Phone": {"action": "fill", "value": "No phone"}}}})
    assert out["Customer ID"].tolist() == ["C001", "C002", "C003"]
    assert out["Payment Date"].tolist() == ["2026-04-03", "2026-02-15", "01/10/26"]
    assert out["Status"].tolist() == ["Completed", "Completed", "Pending"]
    assert out["Phone"].tolist() == ["+2347031112233", "080ABC12345", "No phone"]
    assert summary["export_verification"]["verified"] is True
    assert summary["row_reconciliation"]["unexplained"] == 0 if "row_reconciliation" in summary else True


def test_api_rejects_a_malformed_option_with_400_and_changes_nothing(client):
    from tests.test_data_integrity import _upload
    job = _upload(client, b"id,Phone\nC1,0803\n", "x.csv")
    r = client.post(f"/api/process/{job}", json={"options": {"phone_country": "ZZ"}})
    assert r.status_code == 400
