"""
Pro analysis: turns the engine's quality reports + audit into the figures Pro users see
(headline, per-column scores, dimension breakdown, before/after comparison, "What changed"),
and into the compact record stored in processing history.

Nothing here keeps dataset values: sample values and change examples are stripped before a
session is stored.
"""

from __future__ import annotations

import time
from typing import Optional

SEV_LABEL = {"critical": "critical", "warning": "warning", "info": "information"}
STRUCTURAL_ISSUES = {"column_name_formatting", "constant_column", "high_missingness"}
CATEGORY_ISSUES = {"inconsistent_categories", "inconsistent_casing", "currency_label_variants", "gender_variants", "country_variants", "boolean_variants"}
DATE_INVALID_ISSUES = {"impossible_date"}

_CHANGE_TEXT = {
    "formatting": ("Trimmed whitespace from {n} cells", "cells"),
    "column_names": ("Tidied {n} column names", "cells"),
    "missing_token_normalization": ("Recognised {n} blank-style values (N/A, null, -) as missing", "cells"),
    "numeric_text_cleaning": ("Converted {n} formatted numbers (currency, commas, %) to plain numbers", "cells"),
    "gender_standardization": ("Standardised {n} gender values", "cells"),
    "country_standardization": ("Standardised {n} country values", "cells"),
    "boolean_standardization": ("Standardised {n} yes/no values", "cells"),
    "currency_label_standardization": ("Standardised {n} currency labels", "cells"),
    "case_standardization": ("Fixed capitalisation on {n} values", "cells"),
    "categorical_standardization": ("Standardised {n} inconsistent category values", "cells"),
    "email_cleaning": ("Fixed {n} email formatting problems", "cells"),
    "phone_cleaning": ("Fixed {n} phone number formats", "cells"),
    "date_standardization": ("Standardised {n} dates to YYYY-MM-DD", "cells"),
    "missing_values": ("Filled {n} missing values with estimates", "cells"),
    "duplicates": ("Removed {n} exact duplicate rows", "rows"),
}


def _count(report: dict, issues: set[str]) -> int:
    return sum(f.get("affected_count", 0) for f in report.get("findings", []) if f.get("issue") in issues)


def headline(report: dict) -> dict:
    structural_critical = sum(1 for f in report.get("findings", []) if f.get("issue") in STRUCTURAL_ISSUES and f.get("level") == "critical")
    return {
        "score": report.get("score"),
        "grade": report.get("grade"),
        "records": report.get("row_count"),
        "columns": report.get("column_count"),
        "duplicate_records": _count(report, {"duplicate_rows"}),
        "missing_values": _count(report, {"missing_values"}),
        "invalid_dates": _count(report, DATE_INVALID_ISSUES),
        "inconsistent_category_values": _count(report, CATEGORY_ISSUES),
        "critical_structural_errors": structural_critical,
        "issue_count": len(report.get("findings", [])),
    }


def dimension_breakdown(report: dict) -> dict:
    """Completeness, Consistency, Validity, Duplicates (uniqueness) from the engine; Structural derived."""
    dims = report.get("dimensions", {})
    out = {}
    for key, label in (("completeness", "Completeness"), ("consistency", "Consistency"), ("validity", "Validity"), ("uniqueness", "Duplicates")):
        d = dims.get(key) or {}
        out[key] = {"label": label, "score": d.get("score"), "issues": d.get("issue_count", 0), "applicable": d.get("applicable", False)}
    struct = [f for f in report.get("findings", []) if f.get("issue") in STRUCTURAL_ISSUES]
    loss = min(1.0, sum(f.get("impact", 0) for f in struct))
    out["structural"] = {"label": "Structural", "score": round(100 * (1 - loss)), "issues": len(struct), "applicable": True}
    return out


def severity_counts(report: dict) -> dict:
    c = {"critical": 0, "warning": 0, "information": 0}
    for f in report.get("findings", []):
        c[SEV_LABEL.get(f.get("severity"), "information")] += 1
    return c


def column_scores(report: dict) -> list[dict]:
    per: dict[str, list[dict]] = {}
    for f in report.get("findings", []):
        for c in (f.get("columns") or ([f["column"]] if f.get("column") else [])):
            per.setdefault(c, []).append(f)
    types = report.get("column_types", {}) or {}
    rows = []
    for col in list(types.keys()) or list(per.keys()):
        fs = per.get(col, [])
        loss = min(1.0, sum(f.get("impact", 0) for f in fs) * 2)  # column view is stricter than the dataset average
        rows.append({
            "column": col,
            "type": (types.get(col) or {}).get("semantic_type") or (types.get(col) or {}).get("inferred_type"),
            "score": round(100 * (1 - loss)),
            "issues": [{"title": f.get("title"), "severity": SEV_LABEL.get(f.get("severity"), "information"),
                        "count": f.get("affected_count", 0)} for f in sorted(fs, key=lambda x: -x.get("impact", 0))],
        })
    return sorted(rows, key=lambda r: (r["score"], r["column"]))


def _snapshot(report: dict, rows: int, columns: int) -> dict:
    h = headline(report)
    failures = sum(f.get("affected_count", 0) for f in report.get("findings", []) if f.get("dimension") in ("validity", "accuracy"))
    return {"score": h["score"], "grade": h["grade"], "rows": rows, "columns": columns, "issues": h["issue_count"],
            "duplicates": h["duplicate_records"], "missing_values": h["missing_values"], "validation_failures": failures}


def comparison(before: dict, after: dict, rows_in: int, rows_out: int, cols_in: int, cols_out: int) -> dict:
    b, a = _snapshot(before, rows_in, cols_in), _snapshot(after, rows_out, cols_out)
    return {"before": b, "after": a,
            "delta": {k: (a[k] - b[k]) if isinstance(a[k], (int, float)) and isinstance(b[k], (int, float)) else None for k in a}}


def what_changed(audit_entries: list[dict], after_report: dict, rules_applied: list[str]) -> dict:
    totals: dict[str, int] = {}
    for e in audit_entries:
        key = e.get("rule")
        n = e.get("rows_removed", 0) if key == "duplicates" else e.get("cells_changed", 0)
        totals[key] = totals.get(key, 0) + (n or 0)
    lines = []
    for rule, (text, _unit) in _CHANGE_TEXT.items():
        n = totals.get(rule, 0)
        if n:
            lines.append({"status": "ok", "text": text.format(n=n)})
    for key, n in totals.items():  # resolutions or future rules not in the table
        if key not in _CHANGE_TEXT and n:
            lines.append({"status": "ok", "text": f"Applied '{key}' to {n} cells"})
    review = sum(f.get("affected_count", 0) for f in after_report.get("findings", [])
                 if f.get("remediation") in ("requires_review", "do_not_modify"))
    changed = sum(totals.values())
    if review:
        lines.append({"status": "review", "text": f"{review} values require manual review"})
    if not lines:
        lines.append({"status": "ok", "text": "No changes were needed"})
    return {"lines": lines, "changes_made": changed, "needs_review": review}


def review_items(after_report: dict) -> list[dict]:
    items = []
    for f in after_report.get("findings", []):
        if f.get("remediation") in ("requires_review", "do_not_modify"):
            items.append({"title": f.get("title"), "column": f.get("column"), "count": f.get("affected_count", 0),
                          "severity": SEV_LABEL.get(f.get("severity"), "information"),
                          "recommended_action": f.get("recommended_action")})
    return sorted(items, key=lambda i: {"critical": 0, "warning": 1, "information": 2}[i["severity"]])


def change_log(audit_entries: list[dict]) -> list[dict]:
    """One row per recorded change. Value examples are intentionally left out of stored data."""
    return [{
        "rule": e.get("rule"), "column": e.get("column"), "operation": e.get("operation"),
        "cells_changed": e.get("cells_changed", 0), "rows_removed": e.get("rows_removed", 0),
        "approval": e.get("approval"), "confidence": e.get("confidence"),
    } for e in audit_entries]


def build_pro_block(*, before: dict, after: dict, summary_core: dict, audit_entries: list[dict],
                    profile: Optional[dict], profile_before: Optional[dict], profile_after: Optional[dict],
                    cols_in: int) -> dict:
    rows_in, rows_out = summary_core["rows_in"], summary_core["rows_out"]
    block = {
        "headline": headline(after),
        "headline_before": headline(before),
        "dimensions": dimension_breakdown(after),
        "severity": severity_counts(after),
        "columns": column_scores(after),
        "comparison": comparison(before, after, rows_in, rows_out, cols_in, after.get("column_count", cols_in)),
        "what_changed": what_changed(audit_entries, after, summary_core.get("rules_applied", [])),
        "review_items": review_items(after),
        "change_log": change_log(audit_entries),
        "profile": None,
    }
    if profile and profile_after:
        # A profile's own date rule can catch invalid dates the general engine does not flag: report the larger.
        dates_rule = next((r for r in profile_after["results"] if r["key"] == "dates"), None)
        if dates_rule:
            block["headline"]["invalid_dates"] = max(block["headline"]["invalid_dates"], dates_rule["affected"])
        before_status = {r["key"]: r["status"] for r in (profile_before or {}).get("results", [])}
        block["profile"] = {
            "id": profile.get("id"), "name": profile["name"], "min_quality_score": profile.get("min_quality_score"),
            "compliance_before": (profile_before or {}).get("compliance"),
            "compliance_after": profile_after["compliance"],
            "results": [{**r, "before_status": before_status.get(r["key"])} for r in profile_after["results"]],
            "passed": profile_after["passed"], "warnings": profile_after["warnings"], "failed": profile_after["failed"],
        }
    return block


def session_record(*, dataset_name: str, ext: str, size_bytes: int, has_header: bool, rules_applied: list[str],
                   processing_ms: int, pro_block: dict) -> dict:
    """What is stored in history: figures and findings only, never rows or example values."""
    pro_block = dict(pro_block)  # Firestore documents are capped at 1 MB: keep the stored copy bounded
    pro_block["columns"] = pro_block.get("columns", [])[:300]
    pro_block["change_log"] = pro_block.get("change_log", [])[:300]
    comp = pro_block["comparison"]
    prof = pro_block.get("profile")
    return {
        "created_at": time.time(),
        "status": "completed",
        "dataset_name": dataset_name[:200],
        "file_type": ext,
        "file_size_bytes": size_bytes,
        "has_header": has_header,
        "rules_applied": rules_applied,
        "processing_ms": processing_ms,
        "score_before": comp["before"]["score"],
        "score_after": comp["after"]["score"],
        "issue_count": comp["after"]["issues"],
        "issue_count_before": comp["before"]["issues"],
        "profile_id": prof["id"] if prof else None,
        "profile_name": prof["name"] if prof else None,
        "compliance": prof["compliance_after"] if prof else None,
        "report": pro_block,
    }
