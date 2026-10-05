"""Auditable, reversible transformations."""
import pandas as pd
import pytest

from cleaning import model as M
from cleaning.audit import AuditLog, changed_cells_mask, revert
from cleaning.resolutions import apply_resolutions
from cleaning.rules import apply_rules
from tests.helpers import norm_nulls

RAW = pd.DataFrame({
    "Full Name": ["  Ann Lee ", "Bob", "Bob", "Cy"],
    "Email Addr": ["ANN@X.COM", "bob@x.com", "bob@x.com", "cy@x.com"],
    "Score": [10, None, None, 40],
    "Gender": ["MALE", "f", "f", "Male"],
})


def run(df=RAW, rules=None, resolutions=None):
    audit = AuditLog()
    labels = {c: c for c in df.columns}
    work = df.copy()
    blanked = None
    if resolutions:
        work, rlog = apply_resolutions(work, resolutions, audit=audit, column_labels=labels)
        blanked = rlog["blanked"]
    out, log = apply_rules(work, rules=rules, audit=audit, column_labels=labels, protected_blank=blanked)
    return out, audit, log


def entry(audit, rule, column=None):
    hits = [e for e in audit.entries if e["rule"] == rule and (column is None or e["column"] == column)]
    assert hits, f"no audit entry for {rule}/{column}"
    return hits[0]


# ----------------------------------------------------------------- what/why/which rule
def test_every_entry_records_what_why_rule_counts_examples_confidence_and_approval():
    _, audit, _ = run()
    assert audit.entries
    for e in audit.entries:
        assert e["rule"] and e["rule_id"].startswith("OMX-")
        assert e["reason"], "why it changed"
        assert e["operation"] in M.OPERATION_KINDS
        assert e["approval"] in (M.APPROVAL_AUTOMATIC, M.APPROVAL_USER_SELECTED, M.APPROVAL_USER_APPROVED)
        assert e["reversibility"] in (M.REVERSIBLE, M.PARTIALLY_REVERSIBLE, M.IRREVERSIBLE)
        assert e["confidence"] is None or 0 < e["confidence"] <= 1
        assert "cells_changed" in e and "rows_affected" in e and e["examples"]


def test_examples_show_real_before_and_after_values():
    _, audit, _ = run(rules=["formatting"])
    e = entry(audit, "formatting", "Full Name")
    assert e["cells_changed"] == 1
    assert e["examples"][0]["before"] == "  Ann Lee " and e["examples"][0]["after"] == "Ann Lee"
    assert e["examples"][0]["row"] == 0


def test_counts_come_from_the_data_not_from_what_a_rule_claims():
    out, audit, _ = run(rules=["gender_standardization"])
    e = entry(audit, "gender_standardization", "Gender")
    assert e["cells_changed"] == 4  # MALE, f, f, Male all differ from the canonical form
    assert e["cells_changed"] == int(changed_cells_mask(RAW["Gender"], out["Gender"]).sum())


def test_operation_kinds_distinguish_normalization_imputation_and_deletion():
    _, audit, _ = run(rules=["formatting", "missing_values", "duplicates"])
    assert entry(audit, "formatting").get("operation") == M.NORMALIZATION
    assert entry(audit, "missing_values", "Score")["operation"] == M.IMPUTATION
    dup = entry(audit, "duplicates")
    assert dup["operation"] == M.DELETION and dup["rows_removed"] == 1


def test_automatic_versus_user_selected_versus_user_approved():
    _, a_default, _ = run(rules=None, resolutions=None)
    assert {e["approval"] for e in a_default.entries} == {M.APPROVAL_AUTOMATIC}

    _, a_explicit, _ = run(rules=["formatting"])
    assert {e["approval"] for e in a_explicit.entries} == {M.APPROVAL_USER_SELECTED}

    df = pd.DataFrame({"age": [30, 200, 40, 50]})
    _, a_res, _ = run(df, rules=[], resolutions=[{"column": "age", "issue": "impossible_age", "choice": "blank_invalid"}])
    e = a_res.entries[0]
    assert e["approval"] == M.APPROVAL_USER_APPROVED and e["rule_id"] == "OMX-ACC-001" and e["issue"] == "impossible_age"


def test_summary_totals_and_split_between_automatic_and_user_approved():
    df = pd.DataFrame({"age": [30, 200, 40, 50], "name": [" a", "b", "c", "d"]})
    _, audit, _ = run(df, resolutions=[{"column": "age", "issue": "impossible_age", "choice": "blank_invalid"}])
    s = audit.summary()
    assert s["user_approved_changes"] >= 1 and s["total_changes"] == s["automatic_changes"] + s["user_approved_changes"]
    assert s["reversibility"] == M.REVERSIBLE and s["ledger_truncated"] is False


def test_nothing_recorded_when_nothing_changed():
    clean = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
    _, audit, _ = run(clean, rules=["formatting", "duplicates"])
    assert audit.entries == [] and audit.summary()["reversibility"] == M.NOT_APPLICABLE


# --------------------------------------------------------------- change detection
@pytest.mark.parametrize("before,after,changed", [
    ("2348061234567", "+2348061234567", True),   # a '+' is a real change even though both parse as one number
    ("43", 43, False),                            # storage change only
    (43.0, 43, False),
    ("$22,000", 22000, True),
    ("YES", True, True), ("1", True, True),       # text becoming a boolean is always a change
    ("a", "a", False), (None, None, False), (None, "x", True), ("x", None, True),
])
def test_change_detection_is_exact_for_text_and_ignores_pure_storage_changes(before, after, changed):
    mask = changed_cells_mask(pd.Series([before], dtype=object), pd.Series([after], dtype=object))
    assert bool(mask.iloc[0]) is changed


# ------------------------------------------------------------------ reversibility
def test_reverting_all_steps_restores_the_original_exactly():
    out, audit, _ = run(rules=None)
    back = revert(out, audit)[list(RAW.columns)]
    assert norm_nulls(back).equals(norm_nulls(RAW))


def test_revert_restores_removed_rows_at_their_original_positions():
    out, audit, _ = run(rules=["duplicates"])
    assert len(out) == 3
    back = revert(out, audit)
    assert len(back) == 4 and list(back.index) == [0, 1, 2, 3]
    assert norm_nulls(back).equals(norm_nulls(RAW))


def test_revert_restores_renamed_headers_and_dropped_columns():
    df = pd.DataFrame({"Const Col": ["x"] * 6, "Val": [1, 2, 3, 4, 5, 6]})
    audit = AuditLog()
    labels = {c: c for c in df.columns}
    work, _ = apply_resolutions(df.copy(), [{"column": "Const Col", "issue": "constant_column", "choice": "drop_column"}],
                                audit=audit, column_labels=labels)
    out, _ = apply_rules(work, rules=["column_names"], audit=audit, column_labels=labels)
    assert list(out.columns) == ["val"]
    back = revert(out, audit)
    assert list(back.columns) == ["Const Col", "Val"]
    assert norm_nulls(back).equals(norm_nulls(df))


def test_revert_can_undo_only_the_most_recent_steps():
    audit = AuditLog()
    df = pd.DataFrame({"n": ["  a ", "b", "b"]})
    out, _ = apply_rules(df.copy(), rules=["formatting", "duplicates"], audit=audit)
    one_back = revert(out, audit, steps_back=1)  # undo the de-duplication only
    assert len(one_back) == 3 and one_back["n"].tolist() == ["a", "b", "b"]


def test_a_truncated_ledger_refuses_to_pretend_it_can_revert(monkeypatch):
    import cleaning.audit as audit_mod
    monkeypatch.setattr(audit_mod, "LEDGER_MAX_CELLS", 2)
    out, audit, _ = run(rules=["formatting", "gender_standardization"])
    assert audit.ledger_truncated and audit.summary()["reversibility"] == M.PARTIALLY_REVERSIBLE
    assert any(e["reversibility"] == M.PARTIALLY_REVERSIBLE for e in audit.entries)
    with pytest.raises(ValueError):
        revert(out, audit)


def test_audit_keeps_original_row_identity_even_after_rows_are_removed():
    df = pd.DataFrame({"a": ["x", "dup", "dup", "y"], "b": [1, 2, 2, 3], "c": ["p", "q", "q", "r"]})
    audit = AuditLog()
    out, _ = apply_rules(df, rules=["duplicates"], audit=audit)
    assert list(out.index) == [0, 1, 3]
    assert audit.entries[0]["examples"][0]["row"] == 2


# --------------------------------------------- user decisions are not silently undone
def test_cells_a_user_blanked_are_not_refilled_by_a_later_gap_fill():
    df = pd.DataFrame({"age": [30, 200, 40, None, 50, 60]})
    out, audit, log = run(df, rules=["missing_values"],
                          resolutions=[{"column": "age", "issue": "impossible_age", "choice": "blank_invalid"}])
    assert pd.isna(out.loc[1, "age"])               # the user's blank stays blank
    assert out.loc[3, "age"] == pytest.approx(45.0)  # the ordinary gap is still an (audited) estimate
    assert log["imputed"]["age"] == 1


def test_resolution_log_reports_why_a_resolution_was_skipped():
    df = pd.DataFrame({"a": [1, 2, 3]})
    _, log = apply_resolutions(df, [
        {"column": "zzz", "issue": "impossible_age", "choice": "blank_invalid"},
        {"column": "a", "issue": "nope", "choice": "x"},
        {"column": "a", "issue": "impossible_age", "choice": "wrong"},
    ])
    assert [s["reason"] for s in log["skipped"]] == ["column not found", "unknown issue", "unknown choice"]


def test_imputation_is_refused_for_identifier_like_columns_even_when_the_user_asks():
    df = pd.DataFrame({"customer_id": ["C1", None, "C3", "C4"]})
    out, log = apply_resolutions(df, [{"column": "customer_id", "issue": "missing_values", "choice": "impute"}])
    assert out["customer_id"].isna().sum() == 1
    assert "cannot be guessed" in log["skipped"][0]["reason"]


def test_default_gap_fill_never_invents_phones_emails_identifiers_or_dates():
    df = pd.DataFrame({
        "phone_num": ["0803317157", None, "0701234567", "0801112222"],
        "email": ["a@b.com", None, "c@d.org", "e@f.net"],
        "customer_id": ["C1", "C2", None, "C4"],
        "signup_date": ["2024-01-05", None, "2024-03-07", "2024-04-08"],
        "score": [1.0, None, 3.0, 4.0],
    })
    out, log = apply_rules(df, rules=["missing_values"])
    for col in ("phone_num", "email", "customer_id", "signup_date"):
        assert out[col].isna().sum() == 1, f"{col} was filled with an invented value"
    assert out["score"].isna().sum() == 0
    assert {h["column"] for h in log["details"]["missing_values"]["not_imputable"]} == \
        {"phone_num", "email", "customer_id", "signup_date"}
