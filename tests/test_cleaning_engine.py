"""Column-aware cleaning engine: rule behaviour, profile validation, audit, review. Pure pandas, no app."""
import math

import pandas as pd

from cleaning.audit import AuditLog, revert
from cleaning.engine import RuleConfigError, parse_profile, run_profile
from cleaning.engine.base import RuleContext
from cleaning.engine.categories import StandardizeCategoriesRule
from cleaning.engine.dates import NormalizeDateRule, ValidateDateRule
from cleaning.engine.email_rules import ValidateEmailRule
from cleaning.engine.numeric import BasicNumericCleanupRule, NormalizeCurrencyRule, RemoveCurrencySymbolsRule
from cleaning.engine.phone import NormalizePhoneRule, ValidatePhoneRule
from cleaning.engine.text import (CustomReplacementRule, MissingValueRule, NormalizeCaseRule,
                                  NormalizeWhitespaceRule, TrimWhitespaceRule)

CTX = RuleContext("col", 10)


def S(values):
    return pd.Series(values, dtype=object)


def run(rule, values):
    return rule.apply(S(values), CTX)


def isnull(v):
    return v is None or (isinstance(v, float) and math.isnan(v))


# ------------------------------------------------------------------ whitespace
def test_trim_leading_and_trailing():
    assert run(TrimWhitespaceRule(), ["  John", "Doe  ", "  Both  "]).values.tolist() == ["John", "Doe", "Both"]


def test_normalize_whitespace_collapses_internal_runs():
    assert run(NormalizeWhitespaceRule(), ['   John   Doe   ', "a\t\tb", "x\u00a0\u00a0y"]).values.tolist() == ["John Doe", "a b", "x y"]


def test_whitespace_rules_leave_nulls_and_numbers_alone():
    out = run(NormalizeWhitespaceRule(), [None, 42, 3.5, True, "  x "]).values.tolist()
    assert isnull(out[0]) and out[1] == 42 and out[2] == 3.5 and out[3] is True and out[4] == "x"
    assert run(TrimWhitespaceRule(), [42, None]).values.tolist()[0] == 42


# ------------------------------------------------------------------ case
def test_case_modes():
    vals = ["jOhN dOE", None, "MARY"]
    assert run(NormalizeCaseRule({"mode": "lower"}), vals).values.tolist()[0] == "john doe"
    assert run(NormalizeCaseRule({"mode": "upper"}), vals).values.tolist()[0] == "JOHN DOE"
    assert run(NormalizeCaseRule({"mode": "title"}), vals).values.tolist()[0] == "John Doe"
    assert run(NormalizeCaseRule({"mode": "preserve"}), vals).values.tolist()[0] == "jOhN dOE"


def test_title_case_handles_names_sensibly():
    out = run(NormalizeCaseRule({"mode": "title"}), ["o'brien", "john's car", "anne-marie", 7, None]).values.tolist()
    assert out[:3] == ["O'Brien", "John's Car", "Anne-Marie"] and out[3] == 7 and isnull(out[4])


def test_case_requires_a_valid_mode():
    for bad in ({}, {"mode": "shout"}):
        try:
            NormalizeCaseRule(bad)
            assert False, "should reject"
        except RuleConfigError:
            pass


# ------------------------------------------------------------------ phone
NG_FORMS = ["08031234567", "+2348031234567", "2348031234567", "0803-123-4567", " 0803 123 4567 ", "(0803) 123-4567"]


def test_phone_all_nigerian_equivalents_become_one_number():
    r = run(NormalizePhoneRule({"country": "NG"}), NG_FORMS)
    assert set(r.values.tolist()) == {"+2348031234567"} and not r.flags


def test_phone_national_output():
    r = run(NormalizePhoneRule({"country": "NG", "output_format": "national"}), NG_FORMS)
    assert set(r.values.tolist()) == {"08031234567"}


def test_phone_invalid_values_are_flagged_and_untouched():
    vals = ["123456", "abc", "+14155552671", "+233201234567", "0803123"]
    r = run(NormalizePhoneRule({"country": "NG"}), vals)
    assert r.values.tolist() == vals                       # never turned into a valid-looking number
    assert {f.original for f in r.flags} == set(vals)
    reasons = {f.original: f.reason for f in r.flags}
    assert reasons["+14155552671"] == "country_mismatch" and reasons["+233201234567"] == "country_mismatch"


def test_phone_nulls_and_blanks_are_not_flagged():
    r = run(NormalizePhoneRule({"country": "NG"}), [None, "", "  "])
    assert not r.flags and r.metrics["checked"] == 0


def test_phone_missing_trunk_zero_is_only_fixed_when_asked():
    vals = ["8031234567", 8031234567.0]
    assert [f.reason for f in run(NormalizePhoneRule({"country": "NG"}), vals).flags] == ["invalid_phone"] * 2
    r = run(NormalizePhoneRule({"country": "NG", "assume_missing_trunk_prefix": True}), vals)
    assert set(r.values.tolist()) == {"+2348031234567"}


def test_phone_other_country_uses_data_not_code():
    r = run(NormalizePhoneRule({"country": "GH"}), ["0201234567", "+233201234567"])
    assert set(r.values.tolist()) == {"+233201234567"}
    assert run(NormalizePhoneRule({"country": "GH"}), ["0201234567"]).flags == []


def test_validate_phone_changes_nothing():
    vals = ["0803-123-4567", "123"]
    r = run(ValidatePhoneRule({"country": "NG"}), vals)
    assert r.values.tolist() == vals and [f.original for f in r.flags] == ["123"]


def test_phone_rejects_unknown_country():
    try:
        NormalizePhoneRule({"country": "ZZ"})
        assert False
    except RuleConfigError:
        pass


# ------------------------------------------------------------------ dates
def test_date_common_formats_to_iso():
    r = run(NormalizeDateRule({}), ["2026-12-31", "31/12/2026", "12/31/2026", "31-12-2026", "31 Dec 2026", "December 31, 2026"])
    assert set(r.values.tolist()) == {"2026-12-31"} and not r.flags


def test_date_ambiguous_is_flagged_not_guessed():
    r = run(NormalizeDateRule({}), ["01/02/2026"])
    assert r.values.tolist() == ["01/02/2026"]
    assert r.flags[0].reason == "ambiguous_date" and "2026-02-01" in r.flags[0].suggestion and "2026-01-02" in r.flags[0].suggestion


def test_date_column_evidence_resolves_ambiguity_and_says_so():
    r = run(NormalizeDateRule({}), ["31/12/2026", "01/02/2026"])
    assert r.values.tolist() == ["2026-12-31", "2026-02-01"] and r.metrics["day_first_used"] is True
    assert "day-first" in r.metrics["evidence"]


def test_date_conflicting_evidence_is_flagged():
    r = run(NormalizeDateRule({}), ["31/12/2026", "12/31/2026", "01/02/2026"])
    assert [f.original for f in r.flags] == ["01/02/2026"]


def test_date_explicit_day_first_and_output_format():
    assert run(NormalizeDateRule({"day_first": True}), ["01/02/2026"]).values.tolist() == ["2026-02-01"]
    assert run(NormalizeDateRule({"day_first": False}), ["01/02/2026"]).values.tolist() == ["2026-01-02"]
    assert run(NormalizeDateRule({"output_format": "DD/MM/YYYY"}), ["2026-12-31"]).values.tolist() == ["31/12/2026"]


def test_date_invalid_and_garbage_flagged_original_preserved():
    vals = ["31/02/2026", "2026-13-45", "not a date", "05/05/26"]
    r = run(NormalizeDateRule({}), vals)
    assert r.values.tolist() == vals and len(r.flags) == 4
    assert [f.reason for f in r.flags][:2] == ["invalid_date", "invalid_date"]


def test_date_nulls_and_real_datetimes():
    r = run(NormalizeDateRule({}), [None, "", pd.Timestamp("2026-03-04")])
    assert isnull(r.values.tolist()[0]) and r.values.tolist()[2] == "2026-03-04" and not r.flags
    r2 = NormalizeDateRule({}).apply(pd.Series(pd.to_datetime(["2026-01-05", None])), CTX)
    assert r2.values.tolist()[0] == "2026-01-05" and isnull(r2.values.tolist()[1])


def test_validate_date_only_flags():
    r = run(ValidateDateRule({}), ["2026-01-01", "01/02/2026", "x"])
    assert r.values.tolist() == ["2026-01-01", "01/02/2026", "x"] and len(r.flags) == 2


# ------------------------------------------------------------------ amounts
def test_currency_naira_variants():
    r = run(NormalizeCurrencyRule({"currency": "NGN"}), ["₦25,000", "25,000", "NGN 25,000.50", "25000", "25,000 NGN"])
    assert r.values.tolist() == [25000, 25000, 25000.5, 25000, 25000] and not r.flags
    # a bare "N" prefix is too ambiguous to trust: flagged, not converted
    assert run(NormalizeCurrencyRule({"currency": "NGN"}), ["N25,000"]).flags[0].reason == "invalid_amount"


def test_currency_symbol_mismatch_is_flagged():
    r = run(NormalizeCurrencyRule({"currency": "NGN"}), ["$25,000.00"])
    assert r.values.tolist() == ["$25,000.00"] and r.flags[0].reason == "currency_mismatch"
    assert run(NormalizeCurrencyRule({"currency": "USD"}), ["$25,000.00"]).values.tolist() == [25000.0]


def test_currency_space_separator_needs_explicit_config():
    assert run(NormalizeCurrencyRule({"currency": "NGN"}), ["25 000"]).flags[0].reason == "separator_mismatch"
    r = run(NormalizeCurrencyRule({"currency": "NGN", "thousands_separator": "space"}), ["25 000", "1 250 000"])
    assert r.values.tolist() == [25000, 1250000]


def test_currency_european_separators():
    r = run(NormalizeCurrencyRule({"currency": "EUR", "thousands_separator": "dot", "decimal_separator": "comma"}), ["€1.234,50"])
    assert r.values.tolist() == [1234.5]


def test_currency_malformed_values_are_not_converted():
    vals = ["12,34", "1,2,3", "abc", "25,000,", "--5", "₦"]
    r = run(NormalizeCurrencyRule({"currency": "NGN"}), vals)
    assert r.values.tolist() == vals and len(r.flags) == len(vals)


def test_currency_nulls_and_numbers_pass_through():
    r = run(NormalizeCurrencyRule({"currency": "NGN"}), [None, 5, 2.5])
    assert isnull(r.values.tolist()[0]) and r.values.tolist()[1:] == [5, 2.5] and not r.flags


def test_basic_numeric_cleanup_and_symbol_removal():
    assert run(BasicNumericCleanupRule(), [" 1,200 ", "3.5", "1,2", "abc"]).values.tolist() == [1200, 3.5, "1,2", "abc"]
    out = run(RemoveCurrencySymbolsRule(), ["₦1,200", "$5", "5 km", "plain"]).values.tolist()
    assert out[:2] == ["1,200", "5"] and out[2:] == ["5 km", "plain"]


# ------------------------------------------------------------------ categories / email / misc
def test_category_variants_merge_and_typos_are_only_suggested():
    r = run(StandardizeCategoriesRule({"canonical": ["Pending", "Completed"]}),
            ["pending", "Pending", "PENDING", "pendng", "completed", "Unknown thing", None])
    v = r.values.tolist()
    assert v[:3] == ["Pending"] * 3 and v[3] == "pendng" and v[4] == "Completed" and v[5] == "Unknown thing"
    assert [(f.original, f.suggestion) for f in r.flags] == [("pendng", "Pending")]


def test_category_explicit_mapping_wins_and_conflicts_rejected():
    r = run(StandardizeCategoriesRule({"mappings": {"pend": "Pending"}}), ["pend", "Pend", "other"])
    assert r.values.tolist()[:2] == ["Pending", "Pending"]
    try:
        StandardizeCategoriesRule({"mappings": {"a": "X", "A": "Y"}})
        assert False
    except RuleConfigError:
        pass


def test_category_high_cardinality_is_skipped():
    vals = [f"v{i}" for i in range(50)]
    r = run(StandardizeCategoriesRule({"max_distinct": 10}), vals)
    assert r.values.tolist() == vals and r.metrics["skipped"] == "too_many_distinct_values"


def test_email_classification():
    r = run(ValidateEmailRule(), ["a@b.com", "bad", "x@y", "test@example.com", None, "a..b@c.com", "a@@b.com"])
    assert r.metrics == {"valid": 1, "malformed": 4, "suspicious": 1, "missing": 1}
    assert r.values.tolist()[1] == "bad"                     # never modified


def test_missing_values_placeholders_and_optional_fill():
    r = run(MissingValueRule({}), ["N/A", "ok", "-", "", None])
    assert [isnull(x) for x in r.values.tolist()] == [True, False, True, True, True]
    assert run(MissingValueRule({"fill_value": "Unknown"}), ["n/a", "x"]).values.tolist() == ["Unknown", "x"]


def test_custom_replacements_are_literal():
    r = run(CustomReplacementRule({"replacements": [{"from": "N.Y.", "to": "New York", "match": "exact"},
                                                   {"from": ".*", "to": "X", "match": "contains"}]}), ["n.y.", "N.Y.", "abc"])
    assert r.values.tolist() == ["New York", "New York", "abc"]            # ".*" is text, not a pattern
    assert run(CustomReplacementRule({"replacements": [{"from": "a.c", "to": "Z", "match": "contains"}]}), ["xa.cx", "abc"]).values.tolist() == ["xZx", "abc"]


# ------------------------------------------------------------------ profile: validation / security
def _bad(raw):
    try:
        parse_profile(raw)
    except RuleConfigError as exc:
        return exc
    raise AssertionError("profile should have been rejected")


def test_profile_rejects_unknown_rule_types_and_code():
    for t in ("eval", "exec", "__import__('os').system('id')", "os.system", "DROP TABLE users", "../../etc/passwd"):
        e = _bad({"columns": {"A": {"rules": [{"type": t}]}}})
        assert e.code == "UNKNOWN_RULE"


def test_profile_rejects_unknown_parameters_and_wrong_types():
    _bad({"columns": {"A": {"rules": [{"type": "normalize_case", "mode": "title", "code": "print(1)"}]}}})
    _bad({"columns": {"A": {"rules": [{"type": "normalize_phone", "country": ["NG"]}]}}})
    _bad({"columns": {"A": {"rules": [{"type": "normalize_phone", "country": "NG; DROP TABLE x"}]}}})
    _bad({"columns": {"A": {"rules": [{"type": "custom_replacements", "replacements": "rm -rf /"}]}}})
    _bad({"columns": {"A": {"rules": [{"type": "standardize_categories", "typo_cutoff": 0.1}]}}})
    _bad({"columns": {"A": {"rules": [{"type": "normalize_case", "mode": "title", "enabled": "yes"}]}}})


def test_profile_rejects_bad_structure():
    for raw in ("x", [], {}, {"columns": {}}, {"columns": {"A": {}}}, {"columns": {"A": {"rules": []}}},
                {"columns": {"A": {"rules": ["trim_whitespace"]}}}, {"columns": {"": {"rules": [{"type": "trim_whitespace"}]}}},
                {"columns": {"A": {"rules": [{"type": "trim_whitespace"}, {"type": "trim_whitespace"}]}}},
                {"columns": {"A": {"rules": [{"type": "trim_whitespace"}]}}, "run": "import os"}):
        _bad(raw)


def test_profile_is_not_executable_data():
    p = parse_profile({"columns": {"A": {"rules": [{"type": "trim_whitespace"}]}}, "name": "__import__('os')"})
    assert p.name == "__import__('os')" and p.rule_types() == {"trim_whitespace"}      # a label, never evaluated


# ------------------------------------------------------------------ executor: column rules, order, audit, review
def make_df():
    return pd.DataFrame({
        "Name": ["  jOhN   dOE ", "mary  smith", None],
        "Phone": ["0803-123-4567", "+2348031234567", "123456"],
        "Date": ["31/12/2026", "01/02/2026", "bad"],
        "Amount": ["₦25,000", "$25,000.00", "25,000"],
        "Status": ["pending", "PENDING", "pendng"],
        "Untouched": ["  keep  ", "me", "as-is"],
    }, dtype=object)


FULL_PROFILE = {"name": "Nigerian Customer Dataset", "columns": {
    "Name": {"rules": [{"type": "trim_whitespace"}, {"type": "normalize_whitespace"}, {"type": "normalize_case", "mode": "title"}]},
    "Phone": {"rules": [{"type": "normalize_phone", "country": "NG", "output_format": "international"}]},
    "Date": {"rules": [{"type": "normalize_date", "output_format": "YYYY-MM-DD"}]},
    "Amount": {"rules": [{"type": "normalize_currency", "currency": "NGN"}]},
    "Status": {"rules": [{"type": "normalize_case", "mode": "title"}, {"type": "standardize_categories", "canonical": ["Pending"]}]},
}}


def test_different_rules_on_different_columns_and_untouched_columns_stay_untouched():
    df = make_df()
    out, rep = run_profile(df.copy(), parse_profile(FULL_PROFILE))
    assert out["Name"].tolist()[:2] == ["John Doe", "Mary Smith"]
    assert out["Phone"].tolist() == ["+2348031234567", "+2348031234567", "123456"]
    assert out["Date"].tolist() == ["2026-12-31", "2026-02-01", "bad"]
    assert out["Amount"].tolist() == [25000, "$25,000.00", 25000]
    assert out["Untouched"].tolist() == df["Untouched"].tolist()
    assert rep["totals"]["review_pending"] == 4


def test_rule_order_follows_phases_not_list_order():
    prof = parse_profile({"columns": {"A": {"rules": [
        {"type": "standardize_categories", "canonical": ["Pending"]}, {"type": "normalize_case", "mode": "lower"},
        {"type": "trim_whitespace"}]}}})
    types = [r.type for r in sorted(prof.columns["A"], key=lambda r: (r.rule.phase, r.order))]
    assert types == ["trim_whitespace", "normalize_case", "standardize_categories"]


def test_disabled_rules_do_not_run_and_do_not_need_a_plan():
    prof = parse_profile({"columns": {"Name": {"rules": [{"type": "trim_whitespace"}, {"type": "normalize_case", "mode": "upper", "enabled": False}]}}})
    out, rep = run_profile(make_df(), prof)
    assert out["Name"].tolist()[0] == "jOhN   dOE" and "normalize_case" not in prof.required_capabilities()
    assert {"type": "normalize_case", "status": "disabled"} in rep["columns"]["Name"]["rules"]


def test_unknown_column_is_rejected_not_skipped():
    prof = parse_profile({"columns": {"Nope": {"rules": [{"type": "trim_whitespace"}]}}})
    try:
        run_profile(make_df(), prof)
        assert False
    except RuleConfigError as exc:
        assert exc.code == "UNKNOWN_COLUMN"


def test_column_match_tolerates_case_and_spaces_when_unique():
    df = pd.DataFrame({"Signup Date": ["2026-01-01"]})
    out, rep = run_profile(df, parse_profile({"columns": {"signup_date": {"rules": [{"type": "normalize_date", "output_format": "DD/MM/YYYY"}]}}}))
    assert out["Signup Date"].tolist() == ["01/01/2026"]


def test_audit_trail_records_every_change_and_is_reversible():
    df = make_df()
    audit = AuditLog()
    out, rep = run_profile(df.copy(), parse_profile(FULL_PROFILE), audit=audit)
    by_col = {}
    for e in audit.public_entries():
        by_col.setdefault(e["column"], []).append(e)
    assert sum(e["cells_changed"] for e in by_col["Phone"]) == 1
    assert by_col["Phone"][0]["rule"] == "normalize_phone" and by_col["Phone"][0]["rule_id"].startswith("OMX-ENG-")
    assert any(ex["before"] == "0803-123-4567" and ex["after"] == "+2348031234567" for ex in by_col["Phone"][0]["examples"])
    assert revert(out, audit).to_string() == df.to_string()               # original values preserved


def test_review_items_preserve_original_and_explain():
    out, rep = run_profile(make_df(), parse_profile(FULL_PROFILE))
    items = {(r["column"], r["original"]): r for r in rep["review"]["items"]}
    amb = items[("Amount", "$25,000.00")]
    assert amb["reason"] == "currency_mismatch" and amb["status"] == "pending" and amb["message"]
    typo = items[("Status", "pendng")]
    assert typo["suggestion"] == "Pending" and typo["value_when_flagged"] == "Pendng"


def test_review_decisions_accept_and_reject():
    raw = dict(FULL_PROFILE, decisions=[
        {"column": "Status", "original": "pendng", "action": "accept"},               # takes the rule's suggestion
        {"column": "Amount", "original": "$25,000.00", "action": "accept", "value": 25000},
        {"column": "Date", "original": "bad", "action": "reject"},
        {"column": "Phone", "original": "no such value", "action": "accept", "value": "x"}])
    out, rep = run_profile(make_df(), parse_profile(raw), audit=AuditLog())
    assert out["Status"].tolist()[2] == "Pending" and out["Amount"].tolist()[1] == 25000
    assert out["Date"].tolist()[2] == "bad"
    st = {(r["column"], r["original"]): r["status"] for r in rep["review"]["items"]}
    assert st[("Status", "pendng")] == "accepted" and st[("Date", "bad")] == "rejected" and st[("Phone", "123456")] == "pending"
    assert rep["unmatched_decisions"][0]["original"] == "no such value"


def test_before_after_metrics_per_column():
    out, rep = run_profile(make_df(), parse_profile(FULL_PROFILE))
    ph = rep["columns"]["Phone"]
    assert ph["detected"]["type"] and ph["inconsistency_before_pct"] > ph["inconsistency_after_pct"] - 1e-9
    assert ph["rules"][0]["changed"] == 1 and ph["rules"][0]["flagged"] == 1


def test_engine_is_deterministic():
    a, _ = run_profile(make_df(), parse_profile(FULL_PROFILE))
    b, _ = run_profile(make_df(), parse_profile(FULL_PROFILE))
    assert a.to_string() == b.to_string()


def test_scales_without_row_loops_on_100k_rows():
    import time
    n = 100_000
    df = pd.DataFrame({"Phone": ["0803-123-4567", "+2348031234567", "08031234568"] * (n // 3 + 1),
                       "Date": ["31/12/2026", "2026-01-05", "05 Mar 2026"] * (n // 3 + 1)}).iloc[:n].astype(object)
    t = time.time()
    out, rep = run_profile(df, parse_profile({"columns": {"Phone": {"rules": [{"type": "normalize_phone", "country": "NG"}]},
                                                          "Date": {"rules": [{"type": "normalize_date"}]}}}))
    assert time.time() - t < 20 and out["Phone"].nunique() == 2
