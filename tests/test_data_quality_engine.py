"""Data-quality engine: validation, missing values, cross-column consistency, duplicates and reporting.

Every dataset here is invented and uses different industries, column names and types on purpose: nothing in the
engine may depend on a particular header. Detection tests assert on what is REPORTED; invariant tests assert that
no value is ever silently changed, invented or dropped.
"""
import os
import threading
import uuid

import numpy as np
import pandas as pd
import pytest

import processing.pipeline as pl
import utils.file_handler as fh
from cleaning import consistency, detectors, profiling
from cleaning.quality_report import generate_report
from config import Config


@pytest.fixture(autouse=True)
def _tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "TEMP_DIR", str(tmp_path))


def run(df, ext="csv", **kw):
    job = str(uuid.uuid4())
    d = fh.job_dir_path(job)
    os.makedirs(d, exist_ok=True)
    if ext == "csv":
        df.to_csv(os.path.join(d, "source.csv"), index=False)
        out_name = "cleaned.csv"
    else:
        df.to_excel(os.path.join(d, "source.xlsx"), index=False)
        out_name = "cleaned.xlsx"
    res = pl.run_pipeline(job, **kw)
    path = os.path.join(d, out_name)
    out = pd.read_csv(path, dtype=str, keep_default_na=False) if ext == "csv" else \
        pd.read_excel(path, dtype=str, keep_default_na=False)
    return res, out


def issues(res, report="quality_report"):
    return {(f["issue"], f["column"]) for f in res[report]["findings"]}


def findings(res, issue, report="quality_report"):
    return [f for f in res[report]["findings"] if f["issue"] == issue]


RETAIL = lambda: pd.DataFrame({
    "OrderRef": ["A1", "A2", "A3", "A4", "A5", "A6"],
    "Qty": [2, 3, 1, 4, 5, 1],
    "Unit Cost": [10.0, 20.0, 50.0, 15.0, 8.0, 120.0],
    "Line Amount": [20.0, 60.0, 500.0, 60.0, 41.0, 120.0],            # A3 and A5 are wrong
    "Ship Date": ["2024-01-05", "2024-02-10", "2024-03-01", "2024-03-05", "2024-03-09", "2024-03-12"],
    "Order Date": ["2024-01-02", "2024-02-12", "2024-02-20", "2024-03-01", "2024-03-04", "2024-03-10"],  # A2 ships first
})


# ================================================================ 3. context-aware consistency
class TestCrossColumnConsistency:
    def test_wrong_total_is_flagged_with_values_and_expected(self):
        res, out = run(RETAIL())
        f = findings(res, "calculation_mismatch")
        assert len(f) == 1 and f[0]["column"] == "Line Amount"
        assert set(f[0]["related_columns"]) == {"Qty", "Unit Cost"}
        assert f[0]["affected_count"] == 2
        by_record = {e["record"]: e for e in f[0]["examples"]}
        assert set(by_record) == {3, 5}
        assert by_record[3]["values"]["Line Amount"] == 500 and by_record[3]["expected"]["Line Amount"] == 50
        assert by_record[5]["expected"]["Line Amount"] == 40

    def test_uncertain_values_are_never_overwritten(self):
        res, out = run(RETAIL())
        assert out["Line Amount"].tolist() == ["20.0", "60.0", "500.0", "60.0", "41.0", "120.0"]
        assert out["Qty"].tolist() == ["2", "3", "1", "4", "5", "1"]

    def test_ship_before_order_is_flagged(self):
        res, _ = run(RETAIL())
        f = findings(res, "contradictory_ordering")
        assert len(f) == 1 and f[0]["affected_count"] == 1
        assert f[0]["examples"][0]["record"] == 2

    def test_review_not_auto_fix(self):
        res, _ = run(RETAIL())
        for issue in ("calculation_mismatch", "contradictory_ordering"):
            assert findings(res, issue)[0]["remediation"] == "requires_review"

    def test_relationships_are_reported_even_when_they_all_hold(self):
        df = RETAIL()
        df["Line Amount"] = df["Qty"] * df["Unit Cost"]
        res, _ = run(df)
        assert not findings(res, "calculation_mismatch")
        rels = res["quality_report"]["relationships"]["relationships"]
        assert any(r["relationship"] == "Line Amount = Qty × Unit Cost" and r["rows_inconsistent"] == 0 for r in rels)

    @pytest.mark.parametrize("names", [
        ("hours", "rate", "pay"), ("length_m", "width_m", "area_m2"), ("Seats", "Fare (USD)", "Revenue"),
        ("kg", "price_per_kg", "basket_total"),
    ])
    def test_product_relationship_in_any_industry_and_naming(self, names):
        a_name, b_name, c_name = names
        a = np.array([2, 3, 4, 5, 6, 7, 8, 9, 10, 11], dtype=float)
        b = np.array([7.5, 12.0, 3.25, 9.0, 14.5, 6.0, 8.75, 11.0, 5.5, 10.0])
        c = (a * b).round(2)
        c[4] += 33.0                                                       # one wrong row
        df = pd.DataFrame({"ref": [f"r{i}" for i in range(10)], a_name: a, b_name: b, c_name: c})
        res, _ = run(df)
        f = findings(res, "calculation_mismatch")
        assert len(f) == 1 and f[0]["column"] == c_name and f[0]["affected_count"] == 1
        assert f[0]["examples"][0]["record"] == 5

    def test_sum_relationship_subtotal_tax_total(self):
        sub = np.array([100.0, 250.0, 80.0, 40.0, 310.0, 95.0, 120.0, 60.0])
        tax = (sub * 0.075).round(2)
        tot = sub + tax
        tot[6] = 999.0
        df = pd.DataFrame({"inv": [f"I{i}" for i in range(8)], "subtotal": sub, "vat": tax, "grand_total": tot})
        res, _ = run(df)
        f = findings(res, "calculation_mismatch")
        assert f and f[0]["column"] == "grand_total" and f[0]["examples"][0]["record"] == 7

    def test_text_formatted_money_is_understood(self):
        df = pd.DataFrame({"sku": list("abcdef"), "units": [2, 3, 4, 5, 6, 7],
                           "each": ["$1,000.00", "$2,000.00", "$500.00", "$250.00", "$100.00", "$40.00"],
                           "line": ["$2,000.00", "$6,000.00", "$2,000.00", "$1,250.00", "$600.00", "$999.00"]})
        res, _ = run(df)
        f = findings(res, "calculation_mismatch")
        assert f and f[0]["affected_count"] == 1 and f[0]["examples"][0]["record"] == 6

    def test_min_max_pair(self):
        df = pd.DataFrame({"station": list("abcdef"), "min_temp": [3, 5, 2, 9, 4, 1], "max_temp": [12, 14, 11, 6, 13, 10]})
        res, _ = run(df)
        f = findings(res, "contradictory_ordering")
        assert f and f[0]["examples"][0]["record"] == 4

    def test_birth_date_after_another_date(self):
        df = pd.DataFrame({"name": list("abcdef"),
                           "dob": ["1990-01-01", "1985-05-05", "2030-01-01", "1970-07-07", "2001-02-02", "1999-09-09"],
                           "joined": ["2020-01-01", "2019-05-05", "2021-03-03", "2018-07-07", "2022-02-02", "2020-09-09"]})
        res, _ = run(df)
        assert any(f["examples"][0]["record"] == 3 for f in findings(res, "contradictory_ordering"))

    def test_ambiguous_dates_are_never_used_as_evidence(self):
        df = pd.DataFrame({"id": list("abcdef"),
                           "start_date": ["01/02/2024", "03/04/2024", "05/06/2024", "07/08/2024", "09/10/2024", "11/12/2024"],
                           "end_date": ["01/01/2024", "01/01/2024", "01/01/2024", "01/01/2024", "01/01/2024", "01/01/2024"]})
        found = consistency.find_relationships(df, profiling.profile_dataset(df)["columns"])
        assert found["relations"] == []

    def test_no_relationship_on_unrelated_data(self):
        rng = np.random.default_rng(3)
        for _ in range(60):
            n, k = int(rng.integers(6, 40)), int(rng.integers(4, 9))
            for gen in (lambda: rng.integers(1, 7, (n, k)), lambda: rng.normal(100, 30, (n, k)).round(2),
                        lambda: rng.poisson(4, (n, k)), lambda: rng.uniform(0, 100, (n, k)).round(1)):
                df = pd.DataFrame(gen(), columns=[f"c{i}" for i in range(k)])
                assert consistency.find_relationships(df, profiling.profile_dataset(df)["columns"])["relations"] == []

    def test_identifier_and_phone_columns_never_take_part(self):
        df = pd.DataFrame({"phone": [8031234567 + i for i in range(8)], "acct_no": [1000 + 2 * i for i in range(8)],
                           "x": [2, 3, 4, 5, 6, 7, 8, 9], "y": [3, 4, 5, 6, 7, 8, 9, 10]})
        found = consistency.find_relationships(df, profiling.profile_dataset(df)["columns"])
        for r in found["relations"]:
            assert "phone" not in r.terms + [r.target] and "acct_no" not in r.terms + [r.target]

    def test_tiny_and_single_column_inputs_do_not_crash(self):
        for df in (pd.DataFrame({"a": [1, 2]}), pd.DataFrame({"a": [1, 2, 3, 4]}), pd.DataFrame()):
            assert consistency.find_relationships(df, {})["relations"] == []

    def test_search_has_a_time_budget_and_is_bounded(self, monkeypatch):
        monkeypatch.setattr(consistency, "SEARCH_BUDGET_SECONDS", 0.0)
        rng = np.random.default_rng(1)
        df = pd.DataFrame(rng.normal(50, 10, (500, 30)).round(2), columns=[f"m{i}" for i in range(30)])
        found = consistency.find_relationships(df, profiling.profile_dataset(df)["columns"])
        assert found["search"]["search_timed_out"] is True


# ============================================================================ 1. validation
class TestIntelligentValidation:
    @pytest.mark.parametrize("header", ["Age", "Patient Age", "yrs_old", "PatientAge", "customer_age"])
    def test_impossible_ages_are_found_under_any_header_wording(self, header):
        df = pd.DataFrame({"id": list("abcdefgh"), header: [34, 250, 41, -3, 29, 57, 63, 45]})
        res, out = run(df)
        assert any(f["column"] == header and "age" in f["issue"] for f in res["quality_report"]["findings"])
        assert out[header].tolist() == ["34", "250", "41", "-3", "29", "57", "63", "45"]   # reported, not changed

    @pytest.mark.parametrize("header", ["Age Group", "Vehicle Age", "account_age_days", "Age (months)", "Image", "Mileage"])
    def test_headers_that_are_not_a_persons_age_are_not_range_checked(self, header):
        assert detectors.is_age_column(header) is False

    def test_percentage_out_of_range(self):
        df = pd.DataFrame({"patient": list("abcdefg"), "SpO2 %": [98, 97, 140, 95, 96, 99, 94]})
        res, out = run(df)
        f = findings(res, "out_of_range_percentage")
        assert f and f[0]["column"] == "SpO2 %" and 140 in f[0]["sample_values"]
        assert out["SpO2 %"].tolist()[2] == "140"

    def test_percentage_that_may_legitimately_exceed_100_is_left_alone(self):
        df = pd.DataFrame({"q": list("abcdef"), "revenue growth %": [12, 250, -40, 8, 15, 300]})
        res, _ = run(df)
        assert not findings(res, "out_of_range_percentage")

    def test_gross_outlier_in_a_small_file(self):
        df = pd.DataFrame({"p": list("abcdefg"), "Heart Rate": [72, 80, 75, 900, 68, 71, 66]})
        res, out = run(df)
        assert ("potential_outliers", "Heart Rate") in issues(res)
        assert out["Heart Rate"].tolist()[3] == "900"

    def test_ordinary_spread_in_a_small_file_is_not_an_outlier(self):
        df = pd.DataFrame({"p": list("abcdefg"), "Heart Rate": [60, 72, 85, 95, 68, 110, 77]})
        res, _ = run(df)
        assert ("potential_outliers", "Heart Rate") not in issues(res)

    def test_impossible_calendar_dates_are_flagged_not_guessed(self):
        df = pd.DataFrame({"id": list("abcd"), "Visit": ["2024-03-04", "2024-02-30", "2024-13-01", "2024-05-06"]})
        res, out = run(df)
        assert res["validation_after"]["totals"]["invalid"] >= 2
        assert out["Visit"].tolist()[1] == "2024-02-30"

    def test_text_in_a_numeric_column_is_reported_and_kept(self):
        df = pd.DataFrame({"s": list("abcdef"), "Result": ["12.5", "13.1", "abc", "12.9", "13.0", "13.4"]})
        res, out = run(df)
        assert res["validation_after"]["totals"]["invalid"] >= 1
        assert "abc" in out["Result"].tolist()


# ============================================================================ 2. missing values
class TestMissingValues:
    def test_default_markers_including_dotted_abbreviations(self):
        for tok in ["N/A", "N.E.", "n.d.", "NULL", "None", "NaN", "#N/A", "#DIV/0!", "#VALUE!", ""]:
            assert detectors.is_missing_token(tok) or tok == "", tok

    def test_ne_is_recognised_in_a_numeric_column(self):
        df = pd.DataFrame({"id": list("abcdefgh"), "Score": ["10", "N.E.", "12", "NULL", "14", "None", "16", "n/a"]})
        res, out = run(df)
        assert out["Score"].tolist() == ["10", "", "12", "", "14", "", "16", ""]
        assert res["validation_after"]["totals"]["invalid"] == 0

    def test_unknown_is_a_category_not_a_missing_value(self):
        df = pd.DataFrame({"id": list("abcdefgh"), "Region": ["North", "Unknown", "South", "Unknown", "East", "N/A", "West", "North"]})
        res, out = run(df)
        assert out["Region"].tolist().count("Unknown") == 2          # kept
        assert out["Region"].tolist()[5] == ""                       # the real marker was blanked

    def test_user_configured_marker(self):
        df = pd.DataFrame({"id": list("abcdef"), "Qty": ["5", "TBC", "7", "8", "9", "TBC"]})
        res, out = run(df, options={"missing_markers": ["TBC"]})
        assert out["Qty"].tolist() == ["5", "", "7", "8", "9", ""]

    def test_a_real_value_is_never_invented_for_a_missing_one(self):
        df = pd.DataFrame({"id": list("abcdef"), "Weight": ["70", "N/A", "82", "", "64", "None"]})
        res, out = run(df)
        assert out["Weight"].tolist() == ["70", "", "82", "", "64", ""]


class TestUserDeclaredRealValues:
    ALLERGIES = lambda self: pd.DataFrame({"id": list("abcdef"),
                                           "Allergies": ["None", "Peanuts", "None", "Dust", "Latex", "N/A"]})

    def test_default_treats_none_as_missing(self):
        _, out = run(self.ALLERGIES())
        assert out["Allergies"].tolist() == ["", "Peanuts", "", "Dust", "Latex", ""]

    def test_user_can_declare_none_a_real_value(self):
        res, out = run(self.ALLERGIES(), options={"not_missing": ["None"]})
        assert out["Allergies"].tolist() == ["None", "Peanuts", "None", "Dust", "Latex", ""]   # N/A is still a marker
        assert res["validation_after"]["totals"]["missing"] == 1

    def test_not_missing_beats_a_configured_marker(self):
        _, out = run(pd.DataFrame({"id": list("abcdef"), "Qty": ["5", "TBC", "7", "8", "9", "TBC"]}),
                     options={"missing_markers": ["TBC"], "not_missing": ["tbc"]})
        assert out["Qty"].tolist().count("TBC") == 2

    def test_bad_option_is_rejected_with_a_clear_message(self):
        from cleaning.options import OptionsError, parse_options
        for bad in ("none", [1, 2], [""], ["x" * 100], ["a"] * 60):
            with pytest.raises(OptionsError):
                parse_options({"not_missing": bad})

    def test_setting_is_scoped_to_the_job_and_always_restored(self):
        run(self.ALLERGIES(), options={"not_missing": ["none"]})
        assert detectors.protected_values() == frozenset()
        assert detectors.is_missing_token("None")
        with pytest.raises(Exception):
            pl.run_pipeline("no-such-job-" + uuid.uuid4().hex, options={"not_missing": ["none"]})
        assert detectors.protected_values() == frozenset()

    def test_concurrent_jobs_with_different_settings_do_not_leak(self):
        outs, errors = {}, []

        def work(name, opts):
            try:
                _, out = run(self.ALLERGIES(), options=opts)
                outs[name] = out["Allergies"].tolist()
            except Exception as e:      # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=work, args=(f"keep{i}", {"not_missing": ["none"]})) for i in range(4)] + \
                  [threading.Thread(target=work, args=(f"blank{i}", None)) for i in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert not errors
        assert all(outs[f"keep{i}"][0] == "None" for i in range(4))
        assert all(outs[f"blank{i}"][0] == "" for i in range(4))


# ============================================================================ 4. duplicates
class TestDuplicates:
    HR = lambda self: pd.DataFrame({
        "Emp No": ["E100", "E100", "E101", "E102", "E103", "E103", "E104"],
        "Full Name": ["Ada Obi", "Ada Obi", "John Okoro", "John Okoro", "Mary Eze", "Mary  Eze", "Paul Ade"],
        "Dept": ["Eng", "Eng", "Ops", "Fin", "HR", "HR", "Eng"], "Salary": [500, 500, 400, 420, 380, 385, 610]})

    def test_exact_duplicate_removed_and_reported_with_its_twin(self):
        res, out = run(self.HR())
        assert res["rows_out"] == res["rows_in"] - 1
        rem = res["change_log"]["removed_rows"]
        assert len(rem) == 1 and rem[0]["row"] == 1 and rem[0]["duplicate_of"] == 0
        assert rem[0]["duplicate_of_source_row"] == 2 and rem[0]["source_row"] == 3
        assert "duplicate" in rem[0]["reason"].lower()

    def test_same_name_different_person_is_kept(self):
        _, out = run(self.HR())
        assert out["Full Name"].tolist().count("John Okoro") == 2

    def test_inferred_identifier_collision_is_surfaced_for_review_not_removed(self):
        res, out = run(self.HR())
        assert ("duplicate_identifier_values", "Emp No") in issues(res)
        assert out["Emp No"].tolist().count("E103") == 2
        assert res["integrity_report"]["duplicates"]["potential"]["inferred_identifier_columns"] == ["Emp No"]

    def test_grouping_keys_that_are_meant_to_repeat_are_not_flagged(self):
        orders = pd.DataFrame({"Order No": ["O1", "O1", "O1", "O2", "O2", "O3", "O3", "O3", "O4", "O4"],
                               "Line": [1, 2, 3, 1, 2, 1, 2, 3, 1, 2], "Item": list("abcdefghij")})
        res, _ = run(orders)
        assert not [f for f in res["quality_report"]["findings"] if "duplicate" in f["issue"]]

    def test_user_selected_key_reports_groups(self):
        res, out = run(self.HR(), options={"duplicate_keys": ["Emp No"]})
        assert res["integrity_report"]["duplicates"]["by_key"]["groups"] >= 1
        assert len(out) == res["rows_out"]

    @pytest.mark.parametrize("cols", [("Invoice Ref", "Client"), ("Serial No", "Device"), ("sku", "title")])
    def test_works_for_other_identifier_names(self, cols):
        ident, label = cols
        df = pd.DataFrame({ident: [f"X{i}" for i in range(9)] + ["X3"], label: [f"n{i}" for i in range(9)] + ["different"]})
        res, _ = run(df)
        assert (("duplicate_identifier_values", ident) in issues(res))


# ============================================================================ 5. reporting + invariants
class TestReportingAndInvariants:
    def test_before_and_after_reports_refer_to_the_same_uploaded_rows(self):
        df = pd.DataFrame({"Q": [2, 2, 3, 4, 5], "P": [10.0, 10.0, 20.0, 15.0, 8.0], "T": [20.0, 20.0, 60.0, 61.0, 40.0]})
        res, _ = run(df)
        before = findings(res, "calculation_mismatch", "quality_report_before")[0]["examples"][0]
        after = findings(res, "calculation_mismatch")[0]["examples"][0]
        assert before["record"] == after["record"] == 4 and before["source_row"] == after["source_row"] == 5
        assert after["cleaned_record"] == 3                              # one duplicate row was removed before it

    def test_header_less_file_row_numbers(self):
        # No header means no column names to hint at an ordering, so the engine needs real evidence: 30 rows,
        # one of which ships before it was ordered.
        order = pd.date_range("2024-01-01", periods=30, freq="D")
        ship = order + pd.Timedelta(days=3)
        ship = ship.to_series().reset_index(drop=True)
        ship.iloc[11] = order[11] - pd.Timedelta(days=5)
        df = pd.DataFrame({"a": [f"R{i}" for i in range(30)], "b": order.strftime("%Y-%m-%d"),
                           "c": ship.dt.strftime("%Y-%m-%d")})
        job = str(uuid.uuid4())
        d = fh.job_dir_path(job)
        os.makedirs(d, exist_ok=True)
        df.to_csv(os.path.join(d, "source.csv"), index=False, header=False)
        res = pl.run_pipeline(job, has_header=False)
        f = findings(res, "contradictory_ordering")
        assert f and f[0]["examples"][0]["record"] == 12 and f[0]["examples"][0]["source_row"] == 12   # no header row

    def test_same_file_with_a_header_shifts_the_spreadsheet_row_by_one(self):
        order = pd.date_range("2024-01-01", periods=30, freq="D")
        ship = (order + pd.Timedelta(days=3)).to_series().reset_index(drop=True)
        ship.iloc[11] = order[11] - pd.Timedelta(days=5)
        df = pd.DataFrame({"ref": [f"R{i}" for i in range(30)], "order_date": order.strftime("%Y-%m-%d"),
                           "ship_date": ship.dt.strftime("%Y-%m-%d")})
        res, _ = run(df)
        e = findings(res, "contradictory_ordering")[0]["examples"][0]
        assert e["record"] == 12 and e["source_row"] == 13

    @pytest.mark.parametrize("make", [
        lambda: RETAIL(),
        lambda: pd.DataFrame({"Patient": list("abcdefgh"), "yrs_old": [34, 250, 41, -3, 29, 57, 63, 45],
                              "SpO2 %": [98, 97, 140, 95, 96, 99, 94, 97], "Result": ["1.5", "N.E.", "2.1", "NULL", "1.9", "2.0", "None", "1.7"]}),
        lambda: pd.DataFrame({"Emp No": ["E1", "E1", "E2", "E3"], "Name": ["a", "a", "b", "c"], "Pay": ["N/A", "5", "6", "7"]}),
    ])
    def test_nothing_is_silently_changed_or_dropped(self, make):
        df = make()
        res, out = run(df)
        src = df.astype(str).reset_index(drop=True)
        assert list(out.columns) == list(df.columns), "original column structure is preserved"
        log = res["change_log"]
        assert not log["truncated"]
        ledger = {(c["row"], c["column"]): c for c in log["cell_changes"]}
        removed = {r["row"] for r in log["removed_rows"]}
        assert len(out) + len(removed) == len(df), "every dropped row is accounted for in the report"
        kept = [i for i in range(len(df)) if i not in removed]
        for pos, i in enumerate(kept):
            for col in df.columns:
                a, b = src.at[i, col], out.at[pos, col]
                if a != b:
                    entry = ledger.get((i, col))
                    assert entry is not None, f"cell ({i}, {col}) changed {a!r} -> {b!r} without a ledger entry"
                    assert str(entry["original"]) == a, "the ledger records the true original value"

    def test_xlsx_input_end_to_end(self):
        res, out = run(RETAIL(), ext="xlsx")
        assert findings(res, "calculation_mismatch") and findings(res, "contradictory_ordering")

    def test_report_is_json_serialisable(self):
        import json
        res, _ = run(RETAIL())
        json.dumps(res["quality_report"], default=str)
        json.dumps(res["change_log"], default=str)


# ============================================================================ safety & scale
class TestConcurrencyAndScale:
    def test_concurrent_jobs_do_not_interfere(self):
        results, errors = {}, []

        def work(name, mk):
            try:
                res, _ = run(mk())
                results[name] = (len(findings(res, "calculation_mismatch")), len(findings(res, "contradictory_ordering")))
            except Exception as e:                        # pragma: no cover
                errors.append(e)

        clean = RETAIL()
        clean["Line Amount"] = clean["Qty"] * clean["Unit Cost"]
        clean["Ship Date"] = clean["Order Date"]
        threads = [threading.Thread(target=work, args=(f"bad{i}", RETAIL)) for i in range(3)] + \
                  [threading.Thread(target=work, args=(f"ok{i}", lambda: clean.copy())) for i in range(3)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert not errors
        assert all(results[f"bad{i}"] == (1, 1) for i in range(3))
        assert all(results[f"ok{i}"] == (0, 0) for i in range(3))

    def test_large_file_stays_fast_and_finds_the_relationship(self):
        import time
        rng = np.random.default_rng(5)
        n = 30000
        q = rng.integers(1, 20, n)
        p = rng.uniform(1, 99, n).round(2)
        t = (q * p).round(2)
        t[::5000] += 50
        df = pd.DataFrame({"qty": q, "price": p, "total": t, "note": rng.choice(["a", "b", "c"], n)})
        start = time.time()
        report = generate_report(df)
        assert time.time() - start < 30
        f = [x for x in report["findings"] if x["issue"] == "calculation_mismatch"]
        assert f and f[0]["affected_count"] == 6
