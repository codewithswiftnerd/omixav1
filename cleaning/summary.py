"""
Cleaning Summary

Turns the machine-oriented change_log (cleaning/rules.apply_rules)
and the before/after quality reports (cleaning/quality_report.py)
into the human-readable "DATA CLEANING SUMMARY" the improvement spec
asks for, without inventing any number that isn't already tracked
elsewhere in the pipeline. Nothing here mutates a dataframe or makes
a cleaning decision; it's purely a reporting layer on top of what
processing/pipeline.py already computed.
"""

from __future__ import annotations

from cleaning import model as M


def _numeric_type_totals(details: dict) -> tuple[int, int]:
    """Splits numeric_text_cleaning's single combined count into
    currency vs percentage, using the per-column "type" tag each rule
    already attaches (cleaning/rules.handle_numeric_text_cleaning)."""
    per_column = (details.get("numeric_text_cleaning") or {}).get("per_column", {})
    currency = sum(v.get("changed", 0) for v in per_column.values() if v.get("type") == "currency")
    percentage = sum(v.get("changed", 0) for v in per_column.values() if v.get("type") == "percentage")
    return currency, percentage


def _columns_affected(details: dict) -> list[str]:
    """Every column touched by any rule this run, deduplicated, read from the same per-column detail dicts the frontend already
    renders, so this can never disagree with what's shown elsewhere."""
    cols: set[str] = set()
    for rule_detail in details.values():
        if not isinstance(rule_detail, dict):
            continue
        per_column = rule_detail.get("per_column")
        if per_column:
            cols.update(per_column.keys())
        renamed = rule_detail.get("renamed")
        if renamed:
            cols.update(renamed.values())
    return sorted(cols)


# Findings that represent something Omixa found in the (post-clean)
# file but deliberately did NOT touch, the "invalid values" spec
# item 13/14 refers to, as opposed to purely advisory notes like
# outliers or a constant column that aren't "invalid" so much as
# "worth a look".
_INVALID_VALUE_ISSUES = {
    "invalid_email_format",
    "ambiguous_date_format",
    "suspicious_phone_format",
    "unrecoverable_scientific_notation",
    "impossible_age",
    "impossible_date",
    "unrecognized_gender_value",
    "unrecognized_country_value",
    "mixed_data_types",
    "inconsistent_categories",
}


def _invalid_values_detected(quality_after: dict) -> int:
    return sum(1 for f in quality_after.get("findings", []) if f.get("issue") in _INVALID_VALUE_ISSUES)


def _resolution_totals(resolution_log: dict) -> dict:
    """Resolutions (cleaning/resolutions.py) are a human resolving an
    ambiguity cleaning/rules.py deliberately left alone, the fix
    still belongs in the same summary buckets a rule's own count
    would land in, just credited to the issue the user resolved
    rather than to a rule name."""
    totals = {"dates_standardized": 0, "phone_numbers_normalized": 0, "missing_values_filled": 0}
    issue_to_key = {
        "ambiguous_date_format": "dates_standardized",
        "suspicious_phone_format": "phone_numbers_normalized",
    }
    for item in resolution_log.get("applied", []):
        key = issue_to_key.get(item.get("issue"))
        if key:
            totals[key] += item.get("changed", 0)
        elif item.get("issue") == "high_missingness" and item.get("choice") == "fill_anyway":
            totals["missing_values_filled"] += item.get("changed", 0)
    return totals


def _finding_keys(report: dict) -> dict:
    return {(f["issue"], f.get("column")): f for f in report.get("findings", [])}


def build_quality_comparison(before: dict, after: dict, imputed: dict | None = None) -> dict:
    """Did the data actually improve? Compares dimension scores and issues, and says what
    was resolved, what remains (and who must decide), and what is new.

    Completeness gained by imputation is called out explicitly: filling a gap makes the
    dataset complete, not correct."""
    imputed_total = sum((imputed or {}).values())
    dims = {}
    for d in M.DIMENSIONS:
        b = (before.get("dimensions") or {}).get(d) or {}
        a = (after.get("dimensions") or {}).get(d) or {}
        if not (b.get("applicable") or a.get("applicable")):
            continue
        bs, as_ = b.get("score"), a.get("score")
        dims[d] = {"before": bs, "after": as_,
                   "delta": (as_ - bs) if (bs is not None and as_ is not None) else None}

    bk, ak = _finding_keys(before), _finding_keys(after)
    resolved = [k for k in bk if k not in ak]
    remaining = [k for k in bk if k in ak]
    introduced = [k for k in ak if k not in bk]

    def brief(f):
        return {"rule_id": f["rule_id"], "issue": f["issue"], "column": f.get("column"), "level": f["level"],
                "affected_count": f["affected_count"], "affected_pct": f["affected_pct"], "title": f["title"],
                "remediation": f["remediation"]}

    sb, sa = before.get("score"), after.get("score")
    delta = (sa - sb) if (sb is not None and sa is not None) else None
    ub, ua = before.get("score_before_cap"), after.get("score_before_cap")
    if delta is None:
        verdict = "unknown"
    elif delta > 0:
        verdict = "improved"
    elif delta < 0:
        verdict = "worse"
    elif after.get("score_capped_by") and ub is not None and ua is not None and ua != ub:
        # The headline is pinned by an unresolved critical/high issue. Say so, and use the score
        # before the cap to tell whether the data moved at all, instead of reporting "no change".
        verdict = "improved_but_capped" if ua > ub else "worse_but_capped"
    else:
        verdict = "unchanged"

    notes = []
    if imputed_total:
        notes.append(
            f"{imputed_total} value(s) were filled in by imputation. They make the data complete but they "
            f"are estimates, so accuracy is not credited for them."
        )
    if after.get("score_capped_by"):
        cap_level = after["score_capped_by"]
        notes.append(
            f"The overall score is held down by an unresolved {cap_level} issue. "
            f"Without that cap it would be {ua} (was {ub} before cleaning)."
        )
    still_review = [brief(ak[k]) for k in remaining + introduced if ak[k]["remediation"] == M.REQUIRES_REVIEW]
    still_protected = [brief(ak[k]) for k in remaining + introduced if ak[k]["remediation"] == M.DO_NOT_MODIFY]
    if still_review:
        notes.append(f"{len(still_review)} issue(s) still need your decision.")
    if still_protected:
        notes.append(f"{len(still_protected)} issue(s) were deliberately not modified.")

    return {
        "overall": {"before": sb, "after": sa, "delta": delta, "before_without_cap": ub, "after_without_cap": ua,
                    "capped_by": after.get("score_capped_by"),
                    "grade_before": before.get("grade"), "grade_after": after.get("grade"), "verdict": verdict},
        "dimensions": dims,
        "severity_counts": {"before": before.get("severity_counts"), "after": after.get("severity_counts")},
        "issues_resolved": [brief(bk[k]) for k in resolved],
        "issues_remaining": [brief(ak[k]) for k in remaining],
        "issues_introduced": [brief(ak[k]) for k in introduced],
        "still_requires_review": still_review,
        "still_protected": still_protected,
        "imputed_values": imputed_total,
        "notes": notes,
    }


def build_cleaning_summary(
    rows_in: int,
    rows_out: int,
    column_count: int,
    change_log: dict,
    quality_before: dict,
    quality_after: dict,
    resolution_log: dict | None = None,
    audit=None,
    imputed: dict | None = None,
) -> dict:
    """
    Returns:
        {
          "counts": { ... every figure the spec's summary lists ... },
          "columns_affected": ["age", "country", "gender", ...],
          "quality_score_before": 62,
          "quality_score_after": 91,
          "quality_grade_before": "D",
          "quality_grade_after": "A",
          "text": "DATA CLEANING SUMMARY\\n\\nRows processed: 22\\n..."
        }
    """
    changes = change_log.get("changes", {})
    details = change_log.get("details", {})
    currency_changed, percentage_changed = _numeric_type_totals(details)
    duplicates_detail = details.get("duplicates", {})
    resolution_totals = _resolution_totals(resolution_log or {})

    counts = {
        "rows_processed": rows_in,
        "rows_output": rows_out,
        "columns_processed": column_count,
        "missing_values_standardized": changes.get("missing_token_normalization_changed", 0),
        "missing_values_filled": changes.get("missing_values_changed", 0) + resolution_totals["missing_values_filled"],
        "column_names_standardized": changes.get("column_names_changed", 0),
        "gender_values_normalized": changes.get("gender_standardization_changed", 0),
        "country_values_normalized": changes.get("country_standardization_changed", 0),
        "boolean_values_normalized": changes.get("boolean_standardization_changed", 0),
        "dates_standardized": changes.get("date_standardization_changed", 0) + resolution_totals["dates_standardized"],
        "phone_numbers_normalized": changes.get("phone_cleaning_changed", 0) + resolution_totals["phone_numbers_normalized"],
        "currency_formatting_cleaned": currency_changed,
        "percentage_values_normalized": percentage_changed,
        "duplicate_rows_detected": duplicates_detail.get("duplicate_rows_found", 0),
        "duplicate_rows_removed": duplicates_detail.get("removed", 0),
        "invalid_values_detected": _invalid_values_detected(quality_after),
    }

    score_before = quality_before.get("score")
    score_after = quality_after.get("score")
    grade_before = quality_before.get("grade")
    grade_after = quality_after.get("grade")

    lines = [
        "DATA CLEANING SUMMARY",
        "",
        f"Rows processed: {rows_in}",
        f"Columns processed: {column_count}",
        f"Missing values standardized: {counts['missing_values_standardized']}",
        f"Column names standardized: {counts['column_names_standardized']}",
        f"Gender values normalized: {counts['gender_values_normalized']}",
        f"Country values normalized: {counts['country_values_normalized']}",
        f"Boolean values normalized: {counts['boolean_values_normalized']}",
        f"Dates standardized: {counts['dates_standardized']}",
        f"Phone numbers normalized: {counts['phone_numbers_normalized']}",
        f"Currency formatting cleaned: {counts['currency_formatting_cleaned']}",
        f"Percentage values normalized: {counts['percentage_values_normalized']}",
        f"Duplicate rows detected: {counts['duplicate_rows_detected']}",
        f"Invalid values detected: {counts['invalid_values_detected']}",
    ]
    if score_before is not None and score_after is not None:
        delta = score_after - score_before
        sign = "+" if delta >= 0 else ""
        before_label = f"{score_before}% ({grade_before})" if grade_before else f"{score_before}%"
        after_label = f"{score_after}% ({grade_after})" if grade_after else f"{score_after}%"
        lines += ["", f"Quality score: {before_label} -> {after_label} ({sign}{delta})"]

    comparison = build_quality_comparison(quality_before, quality_after, imputed)
    ov = comparison["overall"]
    lines += ["", f"Verdict: {ov['verdict']}"]
    if comparison["dimensions"]:
        lines.append("Dimension scores (before -> after):")
        for d, v in comparison["dimensions"].items():
            lines.append(f"  {d.capitalize()}: {v['before']} -> {v['after']}")
    audit_summary = audit.summary() if audit is not None else None
    if audit_summary:
        lines += ["",
                  f"Changes recorded: {audit_summary['total_changes']} "
                  f"({audit_summary['cells_changed']} cells, {audit_summary['rows_removed']} rows removed)",
                  f"Reversibility: {audit_summary['reversibility']}"]
    for note in comparison["notes"]:
        lines.append(f"Note: {note}")

    return {
        "quality_comparison": comparison,
        "audit_summary": audit_summary,
        "counts": counts,
        "columns_affected": _columns_affected(details),
        "quality_score_before": score_before,
        "quality_score_after": score_after,
        "quality_grade_before": grade_before,
        "quality_grade_after": grade_after,
        "text": "\n".join(lines),
    }
