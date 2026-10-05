"""Rule architecture (registry consistency, extensibility) and semantic profiling."""
import pandas as pd
import pytest

from cleaning import model as M
from cleaning import profiling, rule_registry as reg
from cleaning import builtin_rules  # noqa: F401
from cleaning.quality_report import generate_report
from cleaning.resolutions import RESOLUTION_OPTIONS, _RESOLVERS
from cleaning.rule_registry import RuleSpec
from cleaning.rules import RULE_DISPATCH
from tests.helpers import finding


# ------------------------------------------------------------------ registry
def test_every_rule_has_the_full_consistent_structure():
    for spec in reg.all_specs():
        assert spec.id.startswith("OMX-") and spec.issue and spec.title
        assert spec.dimension in M.DIMENSIONS
        assert spec.remediation in M.REMEDIATION_LABELS
        assert spec.operation in M.OPERATION_KINDS
        assert spec.reversibility in (M.REVERSIBLE, M.PARTIALLY_REVERSIBLE, M.IRREVERSIBLE, M.NOT_APPLICABLE)
        assert 0.0 <= spec.criticality <= 1.0 and 0.0 <= spec.confidence <= 1.0
        assert M.severity_rank(spec.min_level) <= M.severity_rank(spec.max_level)
        assert spec.explanation.strip(), f"{spec.id} has no explanation"
        if not spec.issue.startswith("fix:"):
            assert spec.recommendation.strip(), f"{spec.id} has no recommendation"


def test_rule_ids_and_issue_keys_are_unique():
    ids = [s.id for s in reg.all_specs()]
    issues = [s.issue for s in reg.all_specs()]
    assert len(ids) == len(set(ids)) and len(issues) == len(set(issues))


def test_every_cleaning_rule_is_documented_for_the_audit_log():
    for name in RULE_DISPATCH:
        spec = reg.spec_for_fixer(name)
        assert spec is not None, f"cleaning rule {name!r} has no fixer spec"
        assert spec.fix_confidence is not None


def test_every_resolver_named_by_a_spec_actually_exists():
    for spec in reg.all_specs():
        if spec.resolver and not spec.issue.startswith("fix:"):
            assert spec.resolver in RULE_DISPATCH or spec.resolver in _RESOLVERS, spec.id


def test_every_resolution_offered_to_users_is_registered_and_never_for_protected_findings():
    for issue in RESOLUTION_OPTIONS:
        spec = reg.get_spec(issue)
        assert spec is not None, issue
        assert spec.remediation != M.DO_NOT_MODIFY, f"{issue} offers choices but is marked do-not-modify"


def test_detectors_only_emit_registered_issues():
    df = pd.DataFrame({
        "Email": ["bad", "a@b.com"] * 6, "age": [200, 30] * 6, "phone_num": ["1.343E+12", "08033"] * 6,
        "Gender": ["x", "m"] * 6, "Country": ["Atlantis", "NG"] * 6, "signup_date": ["03/04/2024", "15/06/2024"] * 6,
        "amt": ["$1,200", "$2"] * 6, "c": ["Yes", "no"] * 6, "n": [None, 1] * 6, "d": ["a", "a"] * 6,
    })
    r = generate_report(df)
    assert r["findings"]
    for f in r["findings"]:
        assert f["rule_id"] != "OMX-UNREGISTERED", f["issue"]


def test_a_custom_rule_can_be_added_without_touching_the_engine():
    def check_shouting(ctx):
        out = []
        for col in ctx.text_columns():
            s = ctx.df[col].dropna().astype(str)
            n = int((s.str.len() > 3).where(s.str.isupper(), False).sum())
            if n:
                out.append({"column": col, "issue": "custom_all_caps", "affected": n, "population": len(s),
                            "detail": f"{n} values are ALL CAPS.", "suggestion": "Review."})
        return out

    spec = RuleSpec(id="OMX-CUS-001", issue="custom_all_caps", title="All caps", dimension=M.CONSISTENCY,
                    explanation="Shouting values.", recommendation="Review.", criticality=0.2,
                    max_level=M.LOW, detector=check_shouting)
    reg.register_rule(spec)
    try:
        r = generate_report(pd.DataFrame({"w": ["HELLO THERE", "ok", "FINE THANKS"]}))
        f = finding(r, "custom_all_caps")
        assert f and f["rule_id"] == "OMX-CUS-001" and f["level"] == M.LOW and f["dimension"] == M.CONSISTENCY
    finally:
        reg.unregister_rule("custom_all_caps")
    assert finding(generate_report(pd.DataFrame({"w": ["HELLO THERE"]})), "custom_all_caps") is None


def test_registering_a_duplicate_rule_is_rejected():
    spec = reg.get_spec("whitespace")
    with pytest.raises(ValueError):
        reg.register_rule(spec)


def test_a_rule_spec_validates_its_own_fields():
    with pytest.raises(AssertionError):
        RuleSpec(id="X", issue="x", title="x", dimension="not-a-dimension", explanation="", recommendation="")


# ----------------------------------------------------------------- profiling
def _p(name, values, n=None):
    s = pd.Series(values)
    return profiling.profile_column(name, s, n or len(s))


@pytest.mark.parametrize("name,values,expected", [
    ("email", ["a@b.com", "c@d.org", "e@f.net"], profiling.EMAIL),
    ("contact", ["a@b.com", "c@d.org", "e@f.net", "g@h.io"], profiling.EMAIL),
    ("phone_num", ["0803317157", "0701234567", "08031112222"], profiling.PHONE),
    ("signup_date", ["2024-01-05", "2024-02-06", "2024-03-07"], profiling.DATE),
    ("created_at", ["2024-01-05 10:30", "2024-02-06 11:45", "2024-03-07 09:00"], profiling.DATETIME),
    ("price", [10.5, 20.0, 30.25], profiling.CURRENCY),
    ("amount_paid", ["$1,200.00", "$35.50", "$9.99"], profiling.CURRENCY),
    ("customer_id", ["C001", "C002", "C003"], profiling.IDENTIFIER),
    ("ref", ["0042", "0043", "0044"], profiling.IDENTIFIER),
    ("first_name", ["Ada", "Grace", "Alan"], profiling.PERSON_NAME),
    ("city", ["Lagos", "Abuja", "Kano"], profiling.GEOGRAPHIC),
    ("country", ["Nigeria", "Ghana", "Kenya"], profiling.GEOGRAPHIC),
    ("is_active", ["yes", "no", "yes", "no"], profiling.BOOLEAN),
    ("height_cm", [170.2, 181.0, 165.5, 175.1], profiling.NUMERIC_MEASUREMENT),
])
def test_semantic_type_inference(name, values, expected):
    assert _p(name, values).semantic_type == expected


def test_categorical_needs_repetition():
    p = _p("tier", ["gold", "silver", "gold", "bronze", "silver", "gold", "gold", "silver"])
    assert p.semantic_type == profiling.CATEGORICAL and p.confidence >= 0.5


def test_each_inference_explains_itself_with_confidence_and_evidence():
    p = _p("email", ["a@b.com", "c@d.org", "e@f.net"])
    assert 0.0 < p.confidence <= 1.0 and p.evidence
    d = p.to_dict()
    assert d["semantic_type"] == profiling.EMAIL and "importance" in d


def test_name_based_inference_is_not_fooled_by_substrings():
    assert _p("candidate_name", ["Ada", "Grace", "Alan"]).semantic_type == profiling.PERSON_NAME
    # not a date just because the word contains 'date'
    assert _p("validated_by", ["ann", "bob", "cy"]).semantic_type != profiling.DATE
    assert _p("company_name", ["Acme", "Initech", "Globex"]).semantic_type != profiling.PERSON_NAME


def test_placeholders_do_not_change_what_a_column_is():
    p = _p("satisfaction", ["56%", "44%", "Unknown", "82%", "N/A", "71%"])
    assert p.semantic_type == profiling.NUMERIC_MEASUREMENT
    p = _p("is_active", ["Yes", "No", "unknown", "Yes", "-", "No"])
    assert p.semantic_type == profiling.BOOLEAN


def test_an_empty_or_unclear_column_is_unknown_with_low_confidence():
    assert _p("x", [None, None, None]).confidence == 0.0
    weak = _p("misc", ["a", "b", "c", "d", "e", "f"])
    assert weak.confidence < profiling.CONFIDENT and weak.semantic_type == profiling.UNKNOWN


def test_never_impute_blocks_identifiers_emails_phones_and_dates_but_not_ordinary_numbers():
    assert profiling.never_impute(_p("customer_id", ["C1", "C2", "C3"]))
    assert profiling.never_impute(_p("email", ["a@b.com", "c@d.org", "e@f.net"]))
    assert profiling.never_impute(_p("phone", ["0803317157", "0701234567", "0801112222"]))
    assert profiling.never_impute(_p("dob", ["1990-01-05", "1991-02-06", "1992-03-07"]))
    assert not profiling.never_impute(_p("height_cm", [170.2, 181.0, 165.5, 175.1]))


def test_low_confidence_guess_is_not_confident_enough_to_act_on():
    weak = _p("misc", ["a", "b", "c", "d", "e", "f"])
    assert not profiling.is_confident(weak, profiling.EMAIL, profiling.PHONE)


def test_profile_dataset_covers_every_column_and_honours_importance_overrides():
    df = pd.DataFrame({"a": [1, 2, 3], "email": ["a@b.com", "c@d.org", "e@f.net"]})
    prof = profiling.profile_dataset(df, field_importance={"a": 0.1})
    assert set(prof["columns"]) == {"a", "email"}
    assert prof["columns"]["a"].importance == 0.1
    assert prof["row_count"] == 3 and prof["column_count"] == 2


def test_report_exposes_semantic_profile_alongside_legacy_inferred_type():
    r = generate_report(pd.DataFrame({"Gender": ["m", "f", "m"], "email": ["a@b.com", "c@d.org", "e@f.net"]}))
    ct = r["column_types"]
    assert ct["Gender"]["inferred_type"] == "gender"                 # legacy value unchanged
    assert ct["email"]["semantic_type"] == profiling.EMAIL and ct["email"]["semantic_confidence"] > 0.5
    assert ct["email"]["evidence"]
