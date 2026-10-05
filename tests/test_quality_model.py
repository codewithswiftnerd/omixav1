"""
The impact-based quality model: scoring, severity, affected-population weighting,
field importance, confidence, remediation classes, dimensions, expected-score projection.
"""
import pandas as pd
import pytest

from cleaning import model as M
from cleaning import scoring
from cleaning.quality_report import generate_report
from cleaning.recommendations import generate_recommendations
from cleaning.rules import apply_rules
from tests.helpers import finding, issues

REQUIRED_FINDING_FIELDS = {
    "rule_id", "issue", "dimension", "severity", "level", "affected_count", "affected_pct", "confidence",
    "explanation", "recommended_action", "remediation", "operation", "reversibility", "columns", "column",
}


def _emails(n_bad, n_total):
    return ["bad-email"] * n_bad + [f"user{i}@example.com" for i in range(n_total - n_bad)]


# ----------------------------------------------------------------- score basics
def test_clean_dataset_scores_100_in_every_applicable_dimension():
    df = pd.DataFrame({"a": [1, 2, 3, 4], "b": ["x", "y", "z", "w"]})
    r = generate_report(df)
    assert r["score"] == 100 and r["grade"] == "A"
    assert all(v == 100 for v in r["dimension_scores"].values() if v is not None)
    assert r["findings"] == []


def test_overall_and_dimension_scores_are_both_present():
    r = generate_report(pd.DataFrame({"email": _emails(3, 20), "n": range(20)}))
    assert isinstance(r["overall_score"], int) and r["overall_score"] == r["score"]
    for dim in ("completeness", "validity", "uniqueness", "consistency"):
        assert isinstance(r["dimension_scores"][dim], int)
    assert set(r["dimension_scores"]) == set(M.DIMENSIONS)


def test_not_applicable_dimensions_are_reported_as_none_not_perfect():
    r = generate_report(pd.DataFrame({"a": [1, 2, 3]}))
    assert r["dimension_scores"]["timeliness"] is None  # no date column: nothing to be timely about
    assert "timeliness" in r["scorecard"]["dimensions_not_applicable"]


def test_scoring_is_deterministic():
    df = pd.DataFrame({"email": _emails(4, 30), "x": [1, None] * 15})
    a, b = generate_report(df), generate_report(df)
    a.pop("expected_after_safe_fixes", None); b.pop("expected_after_safe_fixes", None)
    assert a == b


# ------------------------------------------------- affected-population weighting
def test_score_depends_on_how_much_data_is_affected_not_how_many_findings():
    """The old model charged a flat amount per finding: 1 bad email cost the same as 100."""
    few = generate_report(pd.DataFrame({"email": _emails(1, 100)}))
    many = generate_report(pd.DataFrame({"email": _emails(60, 100)}))
    assert finding(few, "invalid_email_format")["affected_count"] == 1
    assert finding(many, "invalid_email_format")["affected_count"] == 60
    assert many["score"] < few["score"]
    assert many["dimension_scores"]["validity"] < few["dimension_scores"]["validity"]


def test_same_defect_gets_more_severe_as_share_grows():
    levels = []
    for bad in (1, 10, 30, 70):
        f = finding(generate_report(pd.DataFrame({"email": _emails(bad, 100)})), "invalid_email_format")
        levels.append(M.severity_rank(f["level"]))
        assert f["affected_pct"] == pytest.approx(float(bad))
    assert levels == sorted(levels) and levels[0] < levels[-1]


def test_affected_percentage_is_exact():
    f = finding(generate_report(pd.DataFrame({"email": _emails(25, 200)})), "invalid_email_format")
    assert f["affected_count"] == 25 and f["affected_pct"] == 12.5 and f["population"] == 200


# --------------------------------------------------- field importance / criticality
def test_damage_to_an_important_field_costs_more_than_to_a_minor_one():
    n = 50
    important = pd.DataFrame({"customer_id": [f"C{i}" for i in range(n)], "notes": ["free text note " * 5] * n})
    important.loc[:19, "customer_id"] = None
    minor = pd.DataFrame({"customer_id": [f"C{i}" for i in range(n)], "notes": ["free text note " * 5] * n})
    minor.loc[:19, "notes"] = None
    imp = finding(generate_report(important), "missing_values", "customer_id")
    low = finding(generate_report(minor), "missing_values", "notes")
    assert imp["affected_count"] == low["affected_count"] == 20
    assert imp["field_importance"] > low["field_importance"]
    assert imp["impact"] > low["impact"]


def test_caller_can_override_field_importance():
    df = pd.DataFrame({"x": [1, None, 3, 4, 5, 6, 7, 8, 9, 10], "y": range(10)})
    base = finding(generate_report(df), "missing_values", "x")
    boosted = finding(generate_report(df, field_importance={"x": 1.0}), "missing_values", "x")
    assert boosted["field_importance"] == 1.0 and boosted["impact"] >= base["impact"]


def test_rule_criticality_orders_impact_at_equal_share():
    n = 40
    df = pd.DataFrame({"email": _emails(10, n), "name": ["  padded  "] * 10 + ["ok"] * 30})
    r = generate_report(df)
    assert finding(r, "invalid_email_format")["impact"] > finding(r, "whitespace")["impact"]


def test_cosmetic_issue_never_exceeds_low_even_when_it_affects_everything():
    df = pd.DataFrame({"name": ["  a  "] * 20})
    f = finding(generate_report(df), "whitespace")
    assert f["affected_pct"] == 100.0 and f["level"] == M.LOW


# -------------------------------------------------------------------- severity
def test_level_for_respects_bounds_and_floor():
    assert scoring.level_for(0.5, population=100, min_level=M.LOW, max_level=M.LOW) == M.LOW
    assert scoring.level_for(0.0, population=100, min_level=M.HIGH, max_level=M.CRITICAL) == M.HIGH
    assert scoring.level_for(0.01, population=100, min_level=M.LOW, max_level=M.CRITICAL, floor=M.CRITICAL) == M.CRITICAL
    assert scoring.level_for(0.9, population=100, min_level=M.LOW, max_level=M.CRITICAL) == M.CRITICAL


def test_tiny_samples_are_capped_at_medium_unless_the_rule_demands_more():
    assert scoring.level_for(0.9, population=3, min_level=M.LOW, max_level=M.CRITICAL) == M.MEDIUM
    assert scoring.level_for(0.9, population=3, min_level=M.HIGH, max_level=M.CRITICAL) == M.HIGH


def test_legacy_severity_is_derived_from_level():
    r = generate_report(pd.DataFrame({"a": [1, 2, 3], "b": [None, None, None]}))
    for f in r["findings"]:
        assert f["severity"] == M.LEGACY_SEVERITY[f["level"]]
    assert sum(r["counts"].values()) == len(r["findings"])
    assert sum(r["severity_counts"].values()) == len(r["findings"])


def test_critical_issue_caps_the_overall_score_even_in_a_big_clean_dataset():
    n = 5000
    phones = ["0803317157"] * (n - 1) + ["1.343E+12"]
    df = pd.DataFrame({"phone_num": phones, "v": range(n)})
    r = generate_report(df)
    f = finding(r, "unrecoverable_scientific_notation")
    assert f["level"] == M.CRITICAL
    assert r["score"] <= M.SCORE_CAPS[M.CRITICAL]
    assert r["score_capped_by"] == M.CRITICAL and r["score_before_cap"] > r["score"]


# -------------------------------------------------------------------- findings
def test_every_finding_carries_the_full_model():
    df = pd.DataFrame({
        "Email": _emails(5, 30), "age": [25] * 28 + [200, -1], "dup": ["a"] * 30,
        "signup_date": ["03/04/2024"] * 15 + ["15/06/2024"] * 15, "n": [None] * 25 + [1] * 5,
    })
    r = generate_report(df)
    assert r["findings"]
    for f in r["findings"]:
        assert REQUIRED_FINDING_FIELDS <= set(f), f"{f['issue']} missing {REQUIRED_FINDING_FIELDS - set(f)}"
        assert f["rule_id"].startswith("OMX-") and f["rule_id"] != "OMX-UNREGISTERED"
        assert f["dimension"] in M.DIMENSIONS
        assert f["remediation"] in M.REMEDIATION_LABELS
        assert f["operation"] in M.OPERATION_KINDS
        assert 0.0 < f["confidence"] <= 1.0
        assert f["explanation"] and f["recommended_action"]
        assert 0.0 <= f["affected_pct"] <= 100.0


def test_findings_are_ordered_most_serious_first():
    df = pd.DataFrame({"phone_num": ["1.343E+12"] + ["0803317157"] * 9, "name": ["  a "] * 10})
    ranks = [M.severity_rank(f["level"]) for f in generate_report(df)["findings"]]
    assert ranks == sorted(ranks, reverse=True)


# ------------------------------------------------------------------ confidence
def test_confidence_drops_when_the_column_meaning_is_uncertain():
    sure = pd.DataFrame({"email": ["a@b.com"] * 10 + ["oops"] * 2})
    unsure = pd.DataFrame({"contact": ["a@b.com"] * 10 + ["oops"] * 2})  # only the values hint 'email'
    cs = finding(generate_report(sure), "invalid_email_format")["confidence"]
    cu = finding(generate_report(unsure), "invalid_email_format")["confidence"]
    assert cu <= cs


def test_confidence_is_lower_on_very_small_datasets():
    big = pd.DataFrame({"email": ["a@b.com"] * 40 + ["oops"] * 2})
    small = pd.DataFrame({"email": ["a@b.com", "oops", "c@d.com"]})
    assert finding(generate_report(small), "invalid_email_format")["confidence"] < \
        finding(generate_report(big), "invalid_email_format")["confidence"]


# ------------------------------------------------------- remediation classes
def test_remediation_classes_follow_the_three_way_policy():
    df = pd.DataFrame({
        "phone_num": ["1.343E+12", "0803317157", None] * 4,
        "email": ["A@B.COM", "bad", "c@d.com"] * 4,
        "full_name": ["  Ann", "Bob  ", "Cy"] * 4,
        "score": [1, None, 3] * 4,
    })
    r = generate_report(df)
    assert finding(r, "whitespace")["remediation"] == M.SAFE_AUTO_FIX
    assert finding(r, "email_formatting")["remediation"] == M.SAFE_AUTO_FIX
    assert finding(r, "invalid_email_format")["remediation"] == M.REQUIRES_REVIEW
    assert finding(r, "missing_values", "score")["remediation"] == M.REQUIRES_REVIEW        # imputation
    assert finding(r, "unrecoverable_scientific_notation")["remediation"] == M.DO_NOT_MODIFY  # digits are gone
    assert finding(r, "missing_values", "phone_num")["remediation"] == M.DO_NOT_MODIFY        # never invent a phone


def test_exact_duplicates_are_only_safe_when_the_evidence_is_strong():
    wide = pd.DataFrame({"a": [1, 1, 2], "b": ["x", "x", "y"], "c": [5, 5, 6]})
    narrow = pd.DataFrame({"a": [1, 1, 1, 1, 2]})  # one column: identical rows may be genuine repeats
    assert finding(generate_report(wide), "duplicate_rows")["remediation"] == M.SAFE_AUTO_FIX
    assert finding(generate_report(narrow), "duplicate_rows")["remediation"] == M.REQUIRES_REVIEW


def test_a_fix_is_never_called_safe_when_confidence_is_low():
    from cleaning.quality_report import enrich_finding
    from cleaning.checks import CheckContext
    from cleaning import profiling
    df = pd.DataFrame({"a": [1, 2]})
    ctx = CheckContext(df=df, profile=profiling.profile_dataset(df)["columns"], now=pd.Timestamp.now())
    raw = {"column": "a", "issue": "whitespace", "affected": 1, "population": 2, "fix_confidence": 0.5}
    assert enrich_finding(raw, ctx)["remediation"] == M.REQUIRES_REVIEW


def test_do_not_modify_findings_offer_no_resolution_choices():
    df = pd.DataFrame({"phone_num": ["1.343E+12"] + ["0803317157"] * 9})
    rec = generate_recommendations(generate_report(df))
    protected = [f for f in rec["do_not_modify"] if f["issue"] == "unrecoverable_scientific_notation"]
    assert protected and all(f["resolution_options"] == [] for f in protected)
    assert rec["summary"][M.DO_NOT_MODIFY] >= 1


def test_recommendations_keep_the_legacy_shape_and_group_by_class():
    df = pd.DataFrame({"name": ["  a "] * 6 + ["b"] * 6, "email": _emails(3, 12)})
    rec = generate_recommendations(generate_report(df))
    assert {"safe", "ambiguous", "recommended_rules"} <= set(rec)
    assert "formatting" in rec["recommended_rules"]
    assert "missing_values" not in rec["recommended_rules"]  # imputation is never pre-selected
    assert {f["issue"] for f in rec["ambiguous"]} >= {"invalid_email_format"}
    assert all(r["operation"] == M.NORMALIZATION or r["rule"] == "duplicates" for r in rec["safe"])


# ------------------------------------------------------- imputation honesty
def test_imputation_improves_completeness_but_not_accuracy():
    df = pd.DataFrame({"score": [10, 20, None, 40, None, 60, 70, 80, 90, 100]})
    before = generate_report(df)
    cleaned, log = apply_rules(df.copy(), rules=["missing_values"])
    after = generate_report(cleaned, provenance={"imputed": log["imputed"]})
    assert after["dimension_scores"]["completeness"] > before["dimension_scores"]["completeness"]
    assert finding(after, "unverified_imputed_values", "score")["affected_count"] == 2
    assert after["dimension_scores"]["accuracy"] is not None
    assert after["dimension_scores"]["accuracy"] < 100            # estimates are not observations
    assert (before["dimension_scores"]["accuracy"] or 100) >= after["dimension_scores"]["accuracy"]


def test_expected_score_after_safe_fixes_never_credits_imputation():
    df = pd.DataFrame({"score": [10, 20, None, 40, None, 60, 70, 80, 90, 100]})
    r = generate_report(df, project=True)
    assert r["expected_after_safe_fixes"] is None  # nothing safe to apply; imputation is not safe
    assert "expected_score_after_safe_fixes" not in r["scorecard"]


def test_expected_score_after_safe_fixes_is_a_real_simulation():
    df = pd.DataFrame({"name": ["  a ", " b", "c  ", "d"] * 5, "Email": ["A@B.COM"] * 20, "x": [None] * 3 + [1] * 17})
    r = generate_report(df, project=True)
    exp = r["expected_after_safe_fixes"]
    assert exp and exp["score"] >= r["score"]
    assert exp["dimension_scores"]["completeness"] == r["dimension_scores"]["completeness"]  # gaps stay gaps
    assert "missing_values" not in exp["rules_assumed"]
    assert exp["dimension_scores"]["consistency"] > r["dimension_scores"]["consistency"]


def test_scorecard_summarises_decisions_and_protected_items():
    df = pd.DataFrame({"phone_num": ["1.343E+12"] + ["0803317157"] * 11, "email": _emails(4, 12), "n": ["  x"] * 12})
    sc = generate_report(df)["scorecard"]
    assert sc["issues_by_severity"].keys() == {M.CRITICAL, M.HIGH, M.MEDIUM, M.LOW}
    assert sc["safe_fixes_available"] >= 1 and sc["decisions_required"] >= 1 and sc["do_not_modify"] >= 1
    assert sc["affected"]["cells_flagged"] > 0 and "Upper bounds" in sc["affected"]["note"]
