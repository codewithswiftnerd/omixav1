"""Value standardisation and mapping engine: countries, booleans, categories, currency labels.
Pure pandas (no Flask app), same style as test_cleaning_engine.py."""
import json
from collections import Counter

import pandas as pd
import pytest

from cleaning.audit import AuditLog, revert
from cleaning.engine import FeatureNotAvailable, RuleConfigError, authorize, parse_profile, run_profile
from cleaning.engine.base import RuleContext
from cleaning.engine.standardize import StandardizeValuesRule
from cleaning.mapping import build_plan, suggest_for_dataframe
from cleaning.mapping.base import load_reference
from cleaning.mapping.country import lookup
from cleaning.quality_report import generate_report

CTX = RuleContext("Country", 10)


def S(values):
    return pd.Series(values, dtype=object)


def run(values, **params):
    return StandardizeValuesRule(params).apply(S(values), CTX)


def profile_run(df, col, decisions=None, **params):
    prof = {"columns": {col: {"rules": [{"type": "standardize_values", **params}]}}}
    if decisions:
        prof["decisions"] = decisions
    audit = AuditLog()
    out, report = run_profile(df.copy(), parse_profile(prof), audit=audit)
    return out, report, audit


# =============================================================== reference data
def test_country_reference_is_complete_and_unambiguous():
    ref = load_reference("countries.json")
    rows = ref["countries"]
    assert len(rows) == 249                                       # ISO 3166-1 has 249 officially assigned codes
    assert len({r["alpha2"] for r in rows}) == 249 and len({r["alpha3"] for r in rows}) == 249
    assert all(len(r["alpha2"]) == 2 and len(r["alpha3"]) == 3 and r["name"] for r in rows)
    assert "XK" not in {r["alpha2"] for r in rows}                # Kosovo has no ISO code: never invented
    # no alias or name may point at two different countries
    seen = {}
    from cleaning.mapping.normalize import fold
    for r in rows:
        for t in [r["name"], r["iso_name"], *r["aliases"]]:
            assert seen.setdefault(fold(t), r["alpha2"]) == r["alpha2"], t


def test_every_country_round_trips_through_every_output_format():
    rows = load_reference("countries.json")["countries"]
    for r in rows:
        for fmt, expect in (("alpha2", r["alpha2"]), ("alpha3", r["alpha3"]), ("name", r["name"])):
            out = run([r["alpha3"], r["iso_name"]], category="country", output_format=fmt).values.tolist()
            # alpha-3 codes that are everyday words are deliberately not auto-applied (checked separately)
            if r["alpha3"] in ("AND", "CAN", "COL", "COM", "DOM", "FIN", "GIN", "MAR", "PER", "SUR", "TON"):
                continue
            assert out[1] == expect, (r["alpha2"], fmt, out)


# =============================================================== countries
@pytest.mark.parametrize("raw,alpha2", [
    ("GH", "GH"), ("GHA", "GH"), ("gh", "GH"), ("Ghana", "GH"), ("Republic of Ghana", "GH"), (" ghana ", "GH"),
    ("NG", "NG"), ("NGA", "NG"), ("Nigeria", "NG"), ("KE", "KE"), ("KEN", "KE"), ("Kenya", "KE"),
    ("US", "US"), ("USA", "US"), ("U.S.A.", "US"), ("United States", "US"), ("United States of America", "US"),
    ("UK", "GB"), ("United Kingdom", "GB"), ("Great Britain", "GB"), ("GBR", "GB"),
    ("Côte d'Ivoire", "CI"), ("Cote d'Ivoire", "CI"), ("Ivory Coast", "CI"), ("Türkiye", "TR"), ("Turkey", "TR"),
    ("Viet Nam", "VN"), ("Vietnam", "VN"), ("Czech Republic", "CZ"), ("Eswatini", "SZ"), ("Swaziland", "SZ"),
    ("Democratic Republic of the Congo", "CD"), ("DR Congo", "CD"), ("Republic of the Congo", "CG"),
    ("South Korea", "KR"), ("Republic of Korea", "KR"), ("North Korea", "KP"), ("Hong Kong", "HK"), ("Taiwan", "TW"),
])
def test_country_aliases_map_to_the_canonical_country(raw, alpha2):
    s = ["Ghana", "Nigeria", "Kenya", "United States", "France", raw]        # a clearly-country column
    out = run(s, category="country", output_format="alpha2").values.tolist()
    assert out[-1] == alpha2


def test_country_output_formats_for_ghana():
    for fmt, expect in (("name", "Ghana"), ("alpha2", "GH"), ("alpha3", "GHA")):
        assert run(["GH", "GHA", "Ghana", "Republic of Ghana", "gh"], category="country", output_format=fmt).values.tolist() == [expect] * 5
    assert run(["KR", "South Korea"], category="country", output_format="iso_name").values.tolist() == ["Korea, Republic of"] * 2


def test_country_case_and_whitespace_are_handled():
    out = run(["  GHANA", "gHaNa ", "\tGh\n", "republic   of   ghana"], category="country", output_format="alpha2").values.tolist()
    assert out == ["GH"] * 4


@pytest.mark.parametrize("raw", ["Atlantis", "Nigera", "Ghanna", "Wakanda", "12345", "Earth"])
def test_unknown_country_values_are_never_assigned_to_a_country(raw):
    res = run(["Ghana", "Nigeria", "Kenya", raw], category="country", output_format="alpha2")
    assert res.values.tolist()[-1] == raw                           # untouched
    assert [f.reason for f in res.flags] == ["mapping_unrecognised"]


def test_niger_and_nigeria_are_never_confused():
    out = run(["Niger", "Nigeria", "Niger Republic", "Nigerr"], category="country", output_format="alpha2").values.tolist()
    assert out[:2] == ["NE", "NG"] and out[2:] == ["Niger Republic", "Nigerr"]


@pytest.mark.parametrize("raw", ["Congo", "Korea", "Virgin Islands"])
def test_ambiguous_names_are_flagged_with_no_target(raw):
    res = run(["Ghana", "Nigeria", "Kenya", raw], category="country", output_format="alpha2")
    assert res.values.tolist()[-1] == raw
    flag = res.flags[0]
    assert flag.reason == "mapping_ambiguous" and flag.suggestion is None


@pytest.mark.parametrize("raw", ["England", "Scotland", "Wales", "Northern Ireland", "Holland", "Macedonia"])
def test_lossy_or_renamed_places_are_proposed_not_applied(raw):
    res = run(["Ghana", "Nigeria", "Kenya", raw], category="country", output_format="alpha2")
    assert res.values.tolist()[-1] == raw                           # not changed without approval
    assert res.flags[0].reason == "mapping_needs_approval" and res.flags[0].suggestion in ("GB", "NL", "MK")


def test_kosovo_has_no_iso_code_and_is_not_invented():
    res = run(["Ghana", "Nigeria", "Kenya", "Kosovo"], category="country", output_format="alpha2")
    assert res.values.tolist()[-1] == "Kosovo" and res.flags[0].reason == "mapping_ambiguous"


@pytest.mark.parametrize("raw", ["NA", "na", "No", "in", "is", "it", "to", "or"])
def test_codes_that_are_also_words_or_placeholders_are_not_auto_mapped(raw):
    res = run(["Ghana", "Nigeria", "Kenya", "France", "Spain", raw], category="country", output_format="alpha2")
    assert res.values.tolist()[-1] == raw


def test_uppercase_word_collision_codes_map_only_in_a_clearly_country_column():
    strong = run(["Ghana", "Nigeria", "Kenya", "France", "IN", "IT"], category="country", output_format="name").values.tolist()
    assert strong[-2:] == ["India", "Italy"]
    lower = run(["Ghana", "Nigeria", "Kenya", "France", "in", "it"], category="country", output_format="name").values.tolist()
    assert lower[-2:] == ["in", "it"]


def test_country_rule_does_nothing_to_a_column_that_is_not_countries():
    vals = ["Gold", "Silver", "Bronze", "NG", "Platinum"]
    res = run(vals, category="country", output_format="alpha2")
    assert res.values.tolist() == vals and res.metrics["column_fit"]["applicable"] is False


def test_column_called_country_with_other_content_is_not_assumed_to_be_countries():
    df = pd.DataFrame({"country": ["red", "blue", "green", "yellow"], "x": [1, 2, 3, 4]})
    assert suggest_for_dataframe(df) == []


def test_country_values_already_in_target_form_are_unchanged_and_counted_as_such():
    res = run(["GH", "GH", "Ghana"], category="country", output_format="alpha2")
    assert res.metrics["already_canonical"] == 2 and res.metrics["applied"] == 1


# =============================================================== booleans
@pytest.mark.parametrize("raw,expect", [("Yes", "Yes"), ("Y", "Yes"), ("yes", "Yes"), ("TRUE", "Yes"), ("true", "Yes"),
                                        ("No", "No"), ("N", "No"), ("no", "No"), ("FALSE", "No"), ("false", "No"),
                                        (True, "Yes"), (False, "No")])
def test_boolean_words_map_to_yes_no(raw, expect):
    out = run(["Yes", "No", "yes", "no", raw], category="boolean", output_format="yes_no").values.tolist()
    assert out[-1] == expect


@pytest.mark.parametrize("fmt,t,f", [("yes_no", "Yes", "No"), ("true_false", "True", "False"), ("one_zero", "1", "0"), ("y_n", "Y", "N")])
def test_boolean_output_formats(fmt, t, f):
    out = run(["Yes", "N", "TRUE", "false", "y"], category="boolean", output_format=fmt).values.tolist()
    assert out == [t, f, t, f, t]


def test_one_zero_alongside_words_is_read_as_boolean():
    out = run(["Yes", "No", "1", "0", "yes"], category="boolean", output_format="yes_no").values.tolist()
    assert out == ["Yes", "No", "Yes", "No", "Yes"]


def test_a_numeric_column_is_never_converted_to_boolean():
    vals = [0, 1, 2, 3, 5, 1, 0, 8]
    res = run(vals, category="boolean")
    assert res.values.tolist() == vals and res.metrics["column_fit"]["applicable"] is False


def test_bare_zero_one_column_is_proposed_for_review_not_converted():
    vals = [0, 1, 1, 0, 1]
    res = run(vals, category="boolean", output_format="yes_no")
    assert res.values.tolist() == vals and {f.reason for f in res.flags} == {"mapping_needs_approval"}
    assert res.metrics["needs_approval"] == 5 and res.metrics["applied"] == 0


def test_bare_zero_one_converts_only_when_the_user_confirms_they_are_flags():
    res = run([0, 1, 1, 0], category="boolean", output_format="yes_no", confirm_numeric_flags=True)
    assert res.values.tolist() == ["No", "Yes", "Yes", "No"] and not res.flags


def test_boolean_stray_values_are_left_as_typed_and_reported():
    res = run(["Yes", "No", "Yes", "No", "Yes", "No", "Yes", "No", "Yes", "No", "maybe"], category="boolean")
    assert res.values.tolist()[-1] == "maybe" and res.metrics["unrecognised"] == 1


def test_status_column_is_not_assumed_to_be_boolean():
    df = pd.DataFrame({"status": ["Active", "Inactive", "Pending", "Active", "Yes"]})
    assert [s for s in suggest_for_dataframe(df) if s["category"] == "boolean"] == []


# =============================================================== categories
def test_category_case_and_spacing_variants_are_merged_automatically():
    res = run(["Enterprise", "enterprise", " ENTERPRISE ", "Enterprise", "Startup"], category="category")
    assert res.values.tolist() == ["Enterprise"] * 4 + ["Startup"] and not res.flags


def test_category_punctuation_variants_are_proposed_not_applied():
    res = run(["Small Business", "Small Business", "Small-Business", "small business"], category="category")
    assert res.values.tolist()[2] == "Small-Business"                       # hyphen variant: needs approval
    assert res.values.tolist()[3] == "Small Business"                       # case only: applied
    assert [f.reason for f in res.flags] == ["mapping_needs_approval"] and res.flags[0].suggestion == "Small Business"


def test_category_alias_is_never_assumed_equivalent():
    res = run(["SMB", "Small Business", "Small Business", "Enterprise"], category="category")
    assert res.values.tolist() == ["SMB", "Small Business", "Small Business", "Enterprise"] and not res.flags


def test_similar_looking_categories_are_not_merged():
    vals = ["Active", "Inactive", "Active", "Inactive", "Pending", "Pendng"]
    assert run(vals, category="category").values.tolist() == vals


def test_category_mapping_can_be_accepted_rejected_and_edited():
    df = pd.DataFrame({"Segment": ["Small Business", "Small Business", "Small-Business", "Enterprise", "Enterprise-Plus"]})
    # accept as proposed
    out, rep, _ = profile_run(df, "Segment", category="category",
                              decisions=[{"column": "Segment", "original": "Small-Business", "action": "accept"}])
    assert out["Segment"].tolist()[2] == "Small Business"
    # reject: the dataset is exactly as before
    out, rep, _ = profile_run(df, "Segment", category="category",
                              decisions=[{"column": "Segment", "original": "Small-Business", "action": "reject"}])
    assert out["Segment"].tolist() == df["Segment"].tolist()
    assert [r["status"] for r in rep["review"]["items"]] == ["rejected"]
    # edit the target
    out, rep, audit = profile_run(df, "Segment", category="category",
                                  decisions=[{"column": "Segment", "original": "Small-Business", "action": "accept", "value": "SMB"}])
    assert out["Segment"].tolist()[2] == "SMB"
    assert audit.entries[-1]["mapping"]["modes"] == {"user_edited": 1}


def test_custom_mapping_smb_to_small_business_and_other_scopes_of_use():
    vals = ["SMB", "smb", "Small Business", "Enterprise"]
    res = run(vals, category="category", mappings={"SMB": "Small Business"})
    assert res.values.tolist() == ["Small Business"] * 3 + ["Enterprise"]
    assert res.metrics["custom_applied"] == 2
    assert run(["Univ. of Port Harcourt", "UNIPORT"], category="category",
               mappings={"Univ. of Port Harcourt": "University of Port Harcourt"}).values.tolist()[0] == "University of Port Harcourt"


def test_custom_mappings_do_not_leak_into_other_runs_or_files():
    run(["SMB", "x"], category="category", mappings={"SMB": "Small Business"})
    assert run(["SMB", "x"], category="category").values.tolist() == ["SMB", "x"]          # a later run: no memory of it
    assert build_plan(S(["SMB"]), "c", "category").items[0].status == "canonical"


def test_custom_mapping_validation():
    with pytest.raises(RuleConfigError, match="two different"):
        StandardizeValuesRule({"category": "category", "mappings": {"SMB": "A", "smb": "B"}})
    with pytest.raises(RuleConfigError, match="empty"):
        StandardizeValuesRule({"category": "category", "mappings": {"SMB": "  "}})


def test_custom_mapping_can_extend_countries_without_touching_reference_data():
    res = run(["Ghana", "Nigeria", "Kenya", "Gold Coast"], category="country", output_format="name",
              mappings={"Gold Coast": "Ghana"})
    assert res.values.tolist()[-1] == "Ghana"
    assert lookup("Gold Coast") == (None, None)                                           # the reference is unchanged


# =============================================================== currency labels
@pytest.mark.parametrize("raw,code", [("USD", "USD"), ("US$", "USD"), ("US dollars", "USD"), ("US Dollar", "USD"),
                                      ("NGN", "NGN"), ("₦", "NGN"), ("Naira", "NGN"), ("GHS", "GHS"), ("GH₵", "GHS"),
                                      ("Ghanaian cedi", "GHS"), ("€", "EUR"), ("Euro", "EUR"), ("pound sterling", "GBP")])
def test_currency_labels_are_identified(raw, code):
    out = run(["USD", "NGN", "EUR", raw], category="currency", output_format="code").values.tolist()
    assert out[-1] == code


@pytest.mark.parametrize("raw", ["$", "dollars", "£", "pounds", "¥", "kr", "shillings", "dirham", "CFA"])
def test_ambiguous_symbols_are_not_treated_as_one_currency(raw):
    res = run(["USD", "NGN", "EUR", "GBP", raw], category="currency")
    assert res.values.tolist()[-1] == raw
    assert res.flags[0].reason == "mapping_ambiguous" and len(res.flags[0].message) > 5


def test_currency_amounts_are_never_extracted_or_changed():
    vals = ["USD 250", "₦1,500.00", "NGN 1500", "250 dollars", "Unknown"]
    res = run(vals, category="currency")
    assert res.values.tolist() == vals and res.metrics["column_fit"]["applicable"] is False


def test_mixed_currencies_in_a_label_column_are_flagged_not_merged():
    plan = build_plan(S(["USD", "NGN", "ngn", "US$", "GHS"]), "Currency", "currency")
    assert plan.analysis.details["mixed_currencies"] is True and plan.analysis.details["currencies_found"] == ["GHS", "NGN", "USD"]


def test_currency_output_name_and_no_conversion():
    out = run(["USD", "₦"], category="currency", output_format="name").values.tolist()
    assert out == ["US Dollar", "Nigerian Naira"]


# =============================================================== audit, review, reversibility, score
def test_applied_mappings_appear_in_the_audit_trail_with_provenance():
    df = pd.DataFrame({"Country": ["GH", "gha", "Ghana", "england", "Nigeria", "Kenya"]})
    out, rep, audit = profile_run(df, "Country", category="country", output_format="name",
                                  decisions=[{"column": "Country", "original": "england", "action": "accept", "value": "Great Britain"}])
    assert out["Country"].tolist() == ["Ghana", "Ghana", "Ghana", "Great Britain", "Nigeria", "Kenya"]
    logs = [c for c in audit.cell_log if "mapping" in c]
    assert len(logs) == audit.cell_changes_total == 3
    auto = [c for c in logs if c["mapping"]["mode"] == "automatic_safe"]
    approved = [c for c in logs if c["mapping"]["mode"] != "automatic_safe"]
    assert {c["original"] for c in auto} == {"GH", "gha"} and approved[0]["original"] == "england"
    m = auto[0]["mapping"]
    assert m["category"] == "country" and m["origin"] == "reference" and m["reference"] == "iso3166-1" and m["reference_version"]
    assert approved[0]["mapping"]["mode"] == "user_edited" and approved[0]["mapping"]["applied_value"] == "Great Britain"
    assert approved[0]["mapping"]["proposed"] == "United Kingdom"                        # what Omixa proposed is kept beside what the user chose
    # the numbers the rule reports are the numbers in the ledger
    assert rep["columns"]["Country"]["rules"][0]["changed"] + 1 == audit.cell_changes_total


def test_accepting_the_proposal_as_is_is_recorded_as_user_approved():
    df = pd.DataFrame({"Country": ["Ghana", "Nigeria", "Kenya", "England"]})
    out, _, audit = profile_run(df, "Country", category="country", output_format="name",
                                decisions=[{"column": "Country", "original": "England", "action": "accept"}])
    assert out["Country"].tolist()[3] == "United Kingdom"
    assert audit.cell_log[-1]["mapping"]["mode"] == "user_approved" and audit.cell_log[-1]["approval"] == "user_approved"


def test_custom_rule_provenance_says_custom_not_reference():
    df = pd.DataFrame({"Segment": ["SMB", "Enterprise", "Enterprise"]})
    _, _, audit = profile_run(df, "Segment", category="category", mappings={"SMB": "Small Business"})
    m = audit.cell_log[0]["mapping"]
    assert m["origin"] == "custom_file" and m["mode"] == "user_defined" and m["rule_id"] == "user-mapping:file"


def test_mapping_is_reversible_and_the_original_is_never_lost():
    df = pd.DataFrame({"Country": ["GH", "gha", "Nigeria", "NG"], "Active": ["Y", "n", "yes", "No"]})
    prof = parse_profile({"columns": {"Country": {"rules": [{"type": "standardize_values", "category": "country", "output_format": "alpha3"}]},
                                      "Active": {"rules": [{"type": "standardize_values", "category": "boolean", "output_format": "true_false"}]}}})
    audit = AuditLog()
    original = df.copy()
    out, _ = run_profile(df.copy(), prof, audit=audit)
    assert out["Country"].tolist() == ["GHA", "GHA", "NGA", "NGA"] and out["Active"].tolist() == ["True", "False", "True", "False"]
    assert df.equals(original)                                                            # the input frame was not mutated
    assert revert(out, audit).astype(object).equals(original.astype(object))


def test_rejected_mappings_leave_the_dataset_untouched_and_are_reported():
    df = pd.DataFrame({"Country": ["Ghana", "Nigeria", "Kenya", "England"]})
    out, rep, audit = profile_run(df, "Country", category="country", output_format="alpha2",
                                  decisions=[{"column": "Country", "original": "England", "action": "reject"}])
    assert out["Country"].tolist()[3] == "England"
    assert [r["status"] for r in rep["review"]["items"]] == ["rejected"]
    assert rep["columns"]["Country"]["review_pending"] == 0


def test_pending_review_items_are_reported_as_pending_not_as_done():
    df = pd.DataFrame({"Country": ["Ghana", "Nigeria", "Kenya", "England", "Congo"]})
    _, rep, _ = profile_run(df, "Country", category="country", output_format="alpha2")
    assert rep["totals"]["review_pending"] == 2
    assert {r["status"] for r in rep["review"]["items"]} == {"pending"}


def test_suggestions_alone_do_not_change_data_or_the_quality_score():
    df = pd.DataFrame({"Country": ["Ghana", "GH", "gha", "Nigeria", "NG", "Nigeria"] * 3, "Id": range(18)})
    before = df.copy()
    score_before = generate_report(df)["score"]
    suggestions = suggest_for_dataframe(df)
    assert any(s["column"] == "Country" and s["category"] == "country" for s in suggestions)
    assert df.equals(before) and generate_report(df)["score"] == score_before


def test_applied_mapping_improves_consistency_and_the_score_reflects_the_final_data_only():
    df = pd.DataFrame({"Country": ["Ghana", "GH", "gha", "Nigeria", "NG", "Nigeria", "Ghana", "Kenya", "KEN"] * 3, "Id": range(27)})
    before = generate_report(df)
    out, _, _ = profile_run(df, "Country", category="country", output_format="name")
    after = generate_report(out)
    assert out["Country"].nunique() == 3
    assert after["score"] >= before["score"]
    assert not any(f["issue"] == "country_variants" for f in after["findings"])


def test_unresolved_ambiguity_stays_visible_after_cleaning():
    df = pd.DataFrame({"Country": ["Ghana", "Nigeria", "Kenya", "Congo", "Ghana", "Nigeria"] * 3, "Id": range(18)})
    out, rep, _ = profile_run(df, "Country", category="country", output_format="name")
    assert rep["totals"]["review_pending"] == 3 and out["Country"].tolist().count("Congo") == 3
    assert rep["columns"]["Country"]["rules"][0]["metrics"]["ambiguous"] == 3


# =============================================================== configuration, access
def test_invalid_configuration_is_rejected_not_coerced():
    with pytest.raises(RuleConfigError):
        StandardizeValuesRule({"category": "country", "output_format": "alpha4"})
    with pytest.raises(RuleConfigError):
        StandardizeValuesRule({"category": "planets"})
    with pytest.raises(RuleConfigError):
        StandardizeValuesRule({})
    with pytest.raises(RuleConfigError):
        StandardizeValuesRule({"category": "boolean", "output_format": "alpha2"})
    with pytest.raises(RuleConfigError):
        StandardizeValuesRule({"category": "country", "unexpected": 1})


def test_standardize_values_is_a_pro_capability_enforced_server_side():
    prof = parse_profile({"columns": {"C": {"rules": [{"type": "standardize_values", "category": "country"}]}}})
    with pytest.raises(FeatureNotAvailable):
        authorize(prof.required_capabilities(), is_pro=False)
    authorize(prof.required_capabilities(), is_pro=True)


def test_rule_catalog_exposes_the_new_rule_and_its_formats():
    from cleaning.engine import catalog
    entry = next(r for r in catalog(True) if r["type"] == "standardize_values")
    assert entry["parameters"]["category"]["choices"] == ["country", "boolean", "currency", "category"] and entry["available"]


def test_suggestions_preview_shape_and_json_serialisable():
    df = pd.DataFrame({"Country": ["Ghana", "GH", "NG", "England"] * 3, "Active": ["Y", "n", "yes", "No"] * 3})
    sug = suggest_for_dataframe(df)
    json.dumps(sug)
    c = next(s for s in sug if s["category"] == "country")
    assert set(c["output_formats"]) == {"name", "iso_name", "alpha2", "alpha3"}
    item = next(i for i in c["items"] if i["original"] == "England")
    assert item["needs_approval"] is True and item["applied_automatically"] is False and item["reason"]
    assert any(s["category"] == "boolean" for s in sug)
