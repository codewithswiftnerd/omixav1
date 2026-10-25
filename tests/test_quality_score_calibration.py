"""
Calibration of the cell-based quality score (cleaning/scoring.py).

The score must mean "how much of this file can you trust". These tests pin the calibration targets
(clean ~100, stray spaces 90+, 20% of an important column invalid 70-85, 40-50% of several columns bad < 60,
messy files clearly lower, cleaning raises the score only when cells were fixed) and the properties of the
formula itself (no ceiling, damage not divided by the number of columns, breakdown adds up to the headline).
"""
import pandas as pd
import pytest

from cleaning import model as M
from cleaning.quality_report import generate_report
from cleaning.rules import DEFAULT_RULES, apply_rules
from cleaning.summary import build_quality_comparison
from tests import quality_fixtures as Q
from tests.helpers import finding


def _clean_after_defaults(df):
    out, log = apply_rules(df.copy(), rules=list(DEFAULT_RULES))
    return out.reset_index(drop=True), log


def _score(df):
    return generate_report(df)["score"]


# ------------------------------------------------------------------ C. calibration targets
def test_a_perfectly_clean_file_scores_95_to_100():
    r = generate_report(Q.clean_df())
    # the phone-country offer is advisory (a choice, not a defect), so "nothing wrong" means no NON-advisory finding
    assert 95 <= r["score"] <= 100 and r["grade"] == "A" and [f for f in r["findings"] if not f.get("advisory")] == []


def test_stray_spaces_and_case_differences_score_90_plus():
    assert _score(Q.slightly_messy_df()) >= 90


@pytest.mark.parametrize("column", ["email", "amount"])
def test_20_percent_of_one_important_column_invalid_scores_70_to_85(column):
    s = _score(Q.one_column_invalid_df(0.20, column))
    assert 70 <= s <= 85, s


def test_40_to_50_percent_of_several_columns_bad_scores_below_60():
    r = generate_report(Q.very_messy_df())
    assert r["score"] < 60 and r["grade"] == "F"


def test_messy_sample_v2_scores_clearly_lower_than_a_clean_file():
    clean, messy = _score(Q.clean_df()), _score(Q.messy_sample_df())
    assert messy < 60 and clean - messy >= 40


def test_a_key_column_more_than_30_percent_invalid_cannot_score_above_about_60():
    s = _score(Q.one_column_invalid_df(0.31, "email"))
    assert s <= 62, s


# ------------------------------------------------------- defect types that must reduce the score
def test_letters_in_phone_and_amount_columns_reduce_the_score_a_lot():
    r = generate_report(Q.orders_with_letters_df())
    assert r["score"] <= 60
    m = r["score_model"]
    by_col = {c["column"]: c for c in m["columns"]}
    assert by_col["phone"]["counts"]["invalid"] > 0 and by_col["amount"]["counts"]["invalid"] > 0
    assert by_col["amount"]["valid_pct"] < 75 and by_col["phone"]["valid_pct"] < 85


def test_placeholders_like_na_and_dash_reduce_the_score():
    clean = Q.clean_rows()
    for i, row in enumerate(clean):
        if i % 4 == 0:
            row["phone"] = "N/A"
        if i % 4 == 1:
            row["notes"] = "-"
    s = _score(Q.to_df(clean))
    assert s < _score(Q.clean_df()) - 5
    ledger = {c["column"]: c for c in generate_report(Q.to_df(clean))["score_model"]["columns"]}
    assert ledger["phone"]["counts"]["placeholder"] == 50 and ledger["notes"]["counts"]["placeholder"] == 50


def test_mixed_currencies_in_one_column_reduce_the_score():
    r = generate_report(Q.mixed_currency_df())
    assert r["score"] < _score(Q.clean_df()) - 10
    amount = {c["column"]: c for c in r["score_model"]["columns"]}["amount"]
    assert amount["counts"]["mixed_currency"] > 0 and "different currency" in amount["main_problem"]


def test_one_currency_written_two_ways_is_not_mixed():
    rows = Q.clean_rows()
    for i, row in enumerate(rows):
        row["amount"] = ["₦%d" % (i + 10), "NGN %d" % (i + 10)][i % 2]
    amount = {c["column"]: c for c in generate_report(Q.to_df(rows))["score_model"]["columns"]}["amount"]
    assert "mixed_currency" not in amount["counts"]


def test_duplicate_rows_subtract_in_proportion_to_their_share():
    r10, r25 = generate_report(Q.with_duplicates_df(0.10)), generate_report(Q.with_duplicates_df(0.25))
    assert 88 <= r10["score"] <= 92 and 72 <= r25["score"] <= 78
    assert r10["score_model"]["duplicates"]["share_pct"] == 10.0


# ---------------------------------------------------- the formula: no ceiling, no dilution by column count
def test_there_is_no_ceiling_a_file_with_one_bad_cell_can_score_above_89():
    rows = Q.clean_rows(2000)
    rows[0]["email"] = "not-an-email"
    r = generate_report(Q.to_df(rows))
    assert any(f["level"] in (M.HIGH, M.CRITICAL) for f in r["findings"]) or r["score"] > 89
    assert 95 <= r["score"] < 100


def test_100_is_reserved_for_nothing_flagged():
    rows = Q.clean_rows(5000)
    rows[10]["email"] = "bad@@x"
    r = generate_report(Q.to_df(rows))
    assert r["score"] == 99


@pytest.mark.parametrize("extra_columns", [0, 4, 16])
def test_damage_to_a_key_column_is_not_divided_by_the_number_of_columns(extra_columns):
    rows = Q.clean_rows()
    for i, row in enumerate(rows):
        for k in range(extra_columns):
            row[f"extra_{k}"] = f"value {i % 7}"
        if i < 80:
            row["email"] = "not-an-email"                       # 40% of the email column
    s = _score(Q.to_df(rows))
    assert s <= 55, (extra_columns, s)


def test_free_text_notes_count_less_than_an_identifier():
    def with_bad(col):
        rows = Q.clean_rows()
        for row in rows[:60]:
            row[col] = ""                                       # 30% empty
        return generate_report(Q.to_df(rows))
    notes, ident = with_bad("notes"), with_bad("customer_id")
    assert notes["score"] > ident["score"] + 10


def test_fixing_cells_in_a_column_that_is_not_the_weakest_still_raises_the_score():
    rows = Q.clean_rows()
    for row in rows[:80]:
        row["email"] = "not-an-email"
    before = _score(Q.to_df(rows))
    for row in rows[100:140]:
        row["status"] = "?"
    worse = _score(Q.to_df(rows))
    assert worse < before


# --------------------------------------------------- the breakdown is the same points as the headline
@pytest.mark.parametrize("make", [Q.clean_df, Q.slightly_messy_df, Q.very_messy_df, Q.orders_with_letters_df,
                                  Q.mixed_currency_df, Q.messy_sample_df, lambda: Q.with_duplicates_df(0.2)])
def test_dimension_breakdown_adds_up_to_the_headline(make):
    r = generate_report(make())
    lost = sum(d["points_lost"] for d in r["dimensions"].values())
    assert r["score_model"]["points_lost"] == pytest.approx(lost, abs=0.2)
    assert r["score"] == pytest.approx(100 - lost, abs=1.0) or r["score"] == 99
    for name in ("completeness", "validity", "uniqueness", "consistency"):
        assert name in r["dimension_scores"] and r["dimensions"][name]["applicable"] in (True, False)


def test_every_point_lost_is_explained_by_a_reason_with_a_count():
    r = generate_report(Q.very_messy_df())
    m = r["score_model"]
    assert 1 <= len(m["top_reasons"]) <= 3
    for reason in m["top_reasons"]:
        assert reason["count"] > 0 and reason["label"] and reason["points"] > 0
    assert sum(x["points"] for x in m["reasons"]) <= m["points_lost"] + 0.5
    assert m["top_reasons"][0]["points"] == max(x["points"] for x in m["reasons"])


def test_per_column_breakdown_reports_percent_valid_and_main_problem():
    cols = {c["column"]: c for c in generate_report(Q.orders_with_letters_df())["score_model"]["columns"]}
    assert set(cols) == set(Q.clean_rows()[0])
    assert cols["customer_id"]["valid_pct"] == 100.0 and cols["customer_id"]["main_problem"] == "None"
    assert cols["amount"]["valid_pct"] < 100.0 and cols["amount"]["main_problem"] != "None"
    assert all(0 <= c["score"] <= 100 for c in cols.values())


def test_the_score_model_explains_itself_in_one_sentence():
    m = generate_report(Q.clean_df())["score_model"]
    assert m["formula"].count(".") == 1 and "cells" in m["formula"] and "key column" in m["formula"]


def test_scoring_is_deterministic():
    a, b = generate_report(Q.very_messy_df()), generate_report(Q.very_messy_df())
    assert a["score"] == b["score"] and a["score_model"] == b["score_model"]


# ------------------------------------------------------------ the same data after cleaning
def test_cleaning_that_fixes_cells_raises_the_score():
    for make in (Q.slightly_messy_df, Q.messy_sample_df, lambda: Q.with_duplicates_df(0.1)):
        df = make()
        after, _ = _clean_after_defaults(df)
        assert _score(after) > _score(df), make


def test_cleaned_slightly_messy_file_is_100_and_clean():
    after, _ = _clean_after_defaults(Q.slightly_messy_df())
    r = generate_report(after)
    assert r["score"] == 100 and [f for f in r["findings"] if not f.get("advisory")] == []


@pytest.mark.parametrize("make", [Q.orders_with_letters_df, Q.one_column_invalid_df])
def test_when_nothing_was_fixable_the_score_stays_the_same_and_says_why(make):
    df = make()
    before = generate_report(df)
    after_df, log = _clean_after_defaults(df)
    after = generate_report(after_df)
    assert after["score"] == before["score"]
    cmp_ = build_quality_comparison(before, after, cells_changed=0)
    assert cmp_["overall"]["verdict"] == "unchanged"
    assert "could be fixed automatically" in cmp_["overall"]["why_unchanged"]
    assert cmp_["review"]["clean"] is False


def test_tidied_but_already_valid_cells_get_an_honest_unchanged_reason():
    clean = generate_report(Q.clean_df())
    cmp_ = build_quality_comparison(clean, clean, cells_changed=12)
    assert "12 cell(s) were tidied" in cmp_["overall"]["why_unchanged"]


def test_imputed_values_are_credited_as_estimates_not_as_real_data():
    rows = Q.clean_rows()
    for row in rows[:40]:
        row["amount"] = ""
    df = Q.to_df(rows)
    before = generate_report(df)
    filled = df.copy()
    filled["amount"] = pd.to_numeric(filled["amount"], errors="coerce")
    filled["amount"] = filled["amount"].fillna(filled["amount"].median())
    after = generate_report(filled, provenance={"imputed": {"amount": 40}})
    assert after["score"] > before["score"]                      # the gaps are filled ...
    assert after["score"] < 100                                  # ... but estimates never count as real observations
    assert {c["column"]: c for c in after["score_model"]["columns"]}["amount"]["counts"]["estimated"] == 40


def test_filling_a_text_gap_with_unknown_does_not_count_as_data():
    """'Unknown' is itself a placeholder, so it must not be credited as a real value."""
    rows = Q.clean_rows()
    for row in rows[:40]:
        row["notes"] = "Unknown"
    notes = {c["column"]: c for c in generate_report(Q.to_df(rows))["score_model"]["columns"]}["notes"]
    assert notes["counts"]["placeholder"] == 40 and notes["valid_pct"] == 80.0


# ----------------------------------------------------------------------- the honest result block
def test_review_block_never_says_clean_while_a_finding_remains():
    df = Q.orders_with_letters_df()
    before = generate_report(df)
    after_df, _ = _clean_after_defaults(df)
    review = build_quality_comparison(before, generate_report(after_df), cells_changed=3)["review"]
    assert review["clean"] is False and review["count"] == len(review["items"]) > 0
    assert review["headline"] == f"Cleaned, {review['count']} items still need your review"
    assert "clean" not in review["headline"].split(",")[0].lower().replace("cleaned", "")


def test_review_block_says_clean_only_when_nothing_is_left():
    after_df, _ = _clean_after_defaults(Q.slightly_messy_df())
    after = generate_report(after_df)
    review = build_quality_comparison(generate_report(Q.slightly_messy_df()), after, cells_changed=30)["review"]
    assert review["clean"] is True and review["headline"] == "Your data is clean" and review["items"] == []


def test_single_leftover_item_uses_the_singular():
    rep = generate_report(Q.one_column_invalid_df(0.2, "email"))
    one = {**rep, "findings": [f for f in rep["findings"] if not f.get("advisory")][:1]}
    assert build_quality_comparison(rep, one, cells_changed=1)["review"]["headline"] == "Cleaned, 1 item still needs your review"


# ----------------------------------------------------------------------- shape / compatibility
def test_legacy_response_keys_are_kept_and_new_ones_added():
    r = generate_report(Q.very_messy_df())
    for key in ("score", "overall_score", "grade", "score_before_cap", "score_capped_by", "dimension_scores",
                "dimensions", "findings", "counts", "severity_counts", "scorecard", "column_types"):
        assert key in r
    assert r["score_capped_by"] is None and r["score_before_cap"] == r["score"]
    for key in ("score_model", "grade_by_score", "grade_limited_by", "grade_note"):
        assert key in r
    assert r["quality_model_version"] == 3


def test_empty_and_header_only_frames_do_not_break_the_score():
    assert generate_report(pd.DataFrame())["score"] == 100
    r = generate_report(pd.DataFrame({"a": pd.Series(dtype=object), "email": pd.Series(dtype=object)}))
    assert r["score"] == 100 and r["score_model"]["columns"] == []


def test_a_column_that_is_entirely_empty_cannot_zero_the_file():
    rows = Q.clean_rows()
    for row in rows:
        row["email"] = ""
    s = _score(Q.to_df(rows))
    assert 20 <= s <= 40                      # one key column removes at most 70% of the score


def test_scoring_a_large_file_stays_fast():
    import time
    big = pd.concat([Q.clean_df()] * 500, ignore_index=True)          # 100,000 rows x 8 columns
    t = time.perf_counter()
    generate_report(big)
    assert time.perf_counter() - t < 60                                # generous: guards against a per-cell Python loop
