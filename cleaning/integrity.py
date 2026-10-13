"""Honest cleaning outcome report.

Everything here is derived from two independent validations of the ACTUAL datasets (the file as uploaded and
the dataset that was exported) plus the audit ledger and the export verification. A rule having run is never
treated as evidence that a problem is fixed: a problem counts as resolved only when it is no longer found in
the final dataset.

Per column and overall, exactly one of:
  successfully_cleaned     nothing left that needs a person (invalid / suspicious / unresolved all zero)
  partially_cleaned        something was fixed or normalised, but values still need review
  requires_manual_review   nothing could be fixed safely; every flagged value was preserved as uploaded
Missing values are reported separately and are not an error by themselves.
"""

from __future__ import annotations

from typing import Optional

SUCCESS, PARTIAL, MANUAL = "successfully_cleaned", "partially_cleaned", "requires_manual_review"
_ISSUE_KEYS = ("invalid", "suspicious", "unresolved")
_CATEGORY_ISSUES = {"inconsistent_categories", "inconsistent_casing", "possible_misspelling", "label_variants"}


def _by_column(validation: dict, label_of=lambda c: c) -> dict:
    return {label_of(c["column"]): c for c in validation.get("columns", [])}


def _finding_total(report: Optional[dict], issues: set) -> int:
    return sum(int(f.get("affected_count") or 0) for f in (report or {}).get("findings", []) if f.get("issue") in issues)


def _sum(counts: dict, keys=_ISSUE_KEYS) -> int:
    return sum(int(counts.get(k, 0)) for k in keys)


def issues_removed_with_rows(flagged_before: dict, kept_labels) -> dict:
    """{uploaded column: number of flagged cells that sat in rows which are no longer in the cleaned data}.
    An issue that vanished because its whole row was removed (an exact duplicate) was not corrected."""
    kept = set(kept_labels or [])
    return {col: sum(1 for lab in cells if lab not in kept) for col, cells in (flagged_before or {}).items()
            if any(lab not in kept for lab in cells)}


def build_integrity_report(
    *, rows_in: int, rows_out: int, cols_in: int, cols_out: int, validation_before: dict, validation_after: dict,
    cleaned_labels: dict, missing_tracking: dict, audit, details: dict, rules_applied: list,
    duplicates_before: dict, duplicates_after: dict, quality_before: dict, quality_after: dict,
    export_verification: Optional[dict], removed_with_rows: Optional[dict] = None,
) -> dict:
    before = _by_column(validation_before)
    after = _by_column(validation_after, lambda c: cleaned_labels.get(c, c))

    cells_by_col: dict = {}
    ops: dict = {}
    for e in audit.public_entries():
        n = int(e.get("cells_changed") or 0)
        if n:
            cells_by_col[e.get("column")] = cells_by_col.get(e.get("column"), 0) + n
            ops[e["rule"]] = ops.get(e["rule"], 0) + n

    columns, unresolved = [], []
    tot = {"before": dict.fromkeys(_ISSUE_KEYS, 0), "after": dict.fromkeys(_ISSUE_KEYS, 0)}
    # Judged per column (never netted across columns) and net of issues that left only because their row was
    # removed: "corrected" = flagged in the upload, no longer flagged in the cleaned file, row still present.
    corrected_total = introduced_total = removed_total = 0
    for label, b in before.items():
        a = after.get(label)
        mt = missing_tracking.get(label, {})
        if a is None:   # column dropped by an explicit resolution
            columns.append({"column": label, "status": "removed", "note": "Column was removed by an explicit resolution."})
            continue
        for k in _ISSUE_KEYS:
            tot["before"][k] += int(b["counts"].get(k, 0))
            tot["after"][k] += int(a["counts"].get(k, 0))
        issues_b, issues_a = _sum(b["counts"]), _sum(a["counts"])
        gone_with_rows = min(issues_b, int((removed_with_rows or {}).get(label, 0)))
        comparable_before = issues_b - gone_with_rows
        resolved = max(0, comparable_before - issues_a)
        introduced = max(0, issues_a - comparable_before)
        corrected_total += resolved
        introduced_total += introduced
        removed_total += gone_with_rows
        changed = cells_by_col.get(label, 0)
        if issues_a == 0:
            status = SUCCESS
        elif resolved > 0 or changed > 0:
            status = PARTIAL
        else:
            status = MANUAL
        notes = []
        if mt.get("converted_to_missing"):
            notes.append(f"{mt['converted_to_missing']} placeholder value(s) were converted to missing.")
        if mt.get("filled"):
            notes.append(f"{mt['filled']} missing value(s) were filled with an estimate, not the original data.")
        if issues_a:
            unresolved.append({"column": label, "needs_review": issues_a,
                               **{k: int(a["counts"].get(k, 0)) for k in _ISSUE_KEYS}})
        columns.append({
            "column": label, "detected_type": a["effective_type"], "type_status": a["type_status"],
            "missing_before": mt.get("originally_missing", int(b["counts"].get("missing", 0))),
            "missing_after": int(a["counts"].get("missing", 0)),
            "invalid_before": issues_b, "invalid_resolved": resolved, "needs_review_after": issues_a,
            "invalid_removed_with_rows": gone_with_rows, "invalid_introduced": introduced,
            "counts_before": b["counts"], "counts_after": a["counts"],
            "cells_changed": changed, "status": status, "notes": notes,
        })

    statuses = [c["status"] for c in columns if c["status"] != "removed"]
    overall = MANUAL if MANUAL in statuses else PARTIAL if PARTIAL in statuses else SUCCESS
    # a column that only has issues and nothing was fixed anywhere means "manual"; a mix is "partial"
    if overall == MANUAL and any(s in (SUCCESS,) for s in statuses) and any(c.get("cells_changed") for c in columns):
        overall = PARTIAL

    exact_found = int(((details or {}).get("duplicates") or {}).get("duplicate_rows_found", duplicates_before["exact"]["extra_copies"]))
    exact_removed = int(((details or {}).get("duplicates") or {}).get("removed", 0))
    removal_authorized = "duplicates" in (rules_applied or [])

    warnings = []
    if tot["after"]["invalid"]:
        warnings.append(f"{tot['after']['invalid']} invalid value(s) remain in the cleaned data (kept exactly as uploaded).")
    if tot["after"]["unresolved"]:
        warnings.append(f"{tot['after']['unresolved']} value(s) cannot be interpreted safely and need a decision.")
    if tot["after"]["suspicious"]:
        warnings.append(f"{tot['after']['suspicious']} suspicious value(s) were flagged as warnings and left unchanged.")
    if exact_found and not exact_removed:
        warnings.append(f"{exact_found} exact duplicate row(s) were detected but removal was not selected, so they are still in the file.")
    if duplicates_after["by_key"]["groups"]:
        warnings.append(f"{duplicates_after['by_key']['rows']} row(s) share a key ({', '.join(duplicates_after['by_key']['key_columns'])}) "
                        "but differ elsewhere: review, nothing was merged.")
    if duplicates_after["potential"]["groups"]:
        warnings.append(f"{duplicates_after['potential']['rows']} row(s) share an e-mail or phone number with another row but are not "
                        "exact copies: possible duplicate people, nothing was merged or removed.")
    total_missing_after = sum(c.get("missing_after", 0) for c in columns if c["status"] != "removed")
    if total_missing_after:
        warnings.append(f"{total_missing_after} missing value(s) remain (never invented or filled automatically).")
    if export_verification is not None and not export_verification.get("verified"):
        failed = [c["detail"] for c in export_verification.get("checks", []) if c.get("passed") is False]
        warnings.append("Export check FAILED: " + " ".join(failed))
        overall = MANUAL

    fully_clean = (overall == SUCCESS and not unresolved and not duplicates_after["by_key"]["groups"]
                   and not duplicates_after["potential"]["groups"] and not (exact_found and not exact_removed)
                   and (export_verification is None or export_verification.get("verified")))
    return {
        "computed_from": "the final dataset (re-validated after cleaning, and the exported file re-read)",
        "status": overall, "fully_clean": bool(fully_clean),
        "rows": {"original": rows_in, "final": rows_out, "removed": max(0, rows_in - rows_out)},
        "columns_count": {"original": cols_in, "final": cols_out},
        "missing_values": {"before": sum(c.get("missing_before", 0) for c in columns if c["status"] != "removed"),
                           "after": total_missing_after,
                           "converted_from_placeholders": sum(int(v.get("converted_to_missing", 0)) for v in missing_tracking.values()),
                           "filled": sum(int(v.get("filled", 0)) for v in missing_tracking.values())},
        "invalid_values": {"detected": _sum(tot["before"]), "resolved": corrected_total,
                           "removed_with_duplicate_rows": removed_total, "introduced_by_cleaning": introduced_total,
                           "requiring_manual_review": _sum(tot["after"]),
                           "by_kind_before": tot["before"], "by_kind_after": tot["after"]},
        # The five things a reader needs kept apart (all counted on the data, none assumed from the rules that ran):
        "issue_summary": {
            "detected": _sum(tot["before"]),
            "automatically_corrected": corrected_total,
            "removed_with_duplicate_rows": removed_total,
            "introduced_by_cleaning": introduced_total,
            "remaining": _sum(tot["after"]),
            "requires_review": int(tot["after"]["suspicious"]) + int(tot["after"]["unresolved"]),
            "cannot_be_corrected_automatically": int(tot["after"]["invalid"]),
            "cells_normalised": sum(cells_by_col.values()),
            "definitions": {
                "detected": "Values in the uploaded file that were invalid, suspicious or could not be interpreted (missing values and valid-but-untidy formatting are not counted here).",
                "automatically_corrected": "Detected values that are no longer a problem in the cleaned file and whose row is still there.",
                "removed_with_duplicate_rows": "Detected values that left only because their whole row was an exact duplicate and was removed. They were not corrected.",
                "introduced_by_cleaning": "Values that are a problem in the cleaned file but were not in the upload.",
                "remaining": "Values that are still a problem in the cleaned file; every one was kept exactly as uploaded.",
                "requires_review": "Remaining values that are suspicious or ambiguous: a person has to confirm or correct them.",
                "cannot_be_corrected_automatically": "Remaining values that are definitely invalid (a negative age, an impossible date) and have no safe automatic correction.",
                "cells_normalised": "Cells whose text was rewritten to a consistent form (trimmed, ISO date, plain number, one capitalisation). Most were valid before and are not 'issues'.",
            },
        },
        "duplicates": {"exact_rows_detected": exact_found, "removal_authorized": removal_authorized, "exact_rows_removed": exact_removed,
                       "by_key": duplicates_after["by_key"], "potential": duplicates_after["potential"]},
        "data_type_inconsistencies": {"before": _finding_total(quality_before, {"mixed_data_types"}),
                                      "after": _finding_total(quality_after, {"mixed_data_types"})},
        "date_parsing_issues": {
            "before": sum(_sum(c["counts"], ("invalid", "unresolved")) for c in validation_before["columns"] if c["effective_type"] == "date"),
            "after": sum(_sum(c["counts"], ("invalid", "unresolved")) for c in validation_after["columns"] if c["effective_type"] == "date")},
        "categorical_inconsistencies": {"before": _finding_total(quality_before, _CATEGORY_ISSUES),
                                        "after": _finding_total(quality_after, _CATEGORY_ISSUES)},
        "columns": columns,
        "changes_by_column": {k: v for k, v in sorted(cells_by_col.items(), key=lambda kv: -kv[1])},
        "operations_performed": [{"operation": k, "cells_changed": v} for k, v in sorted(ops.items(), key=lambda kv: -kv[1])],
        "unresolved_issues": unresolved, "warnings": warnings,
        "export_verified": None if export_verification is None else bool(export_verification.get("verified")),
    }
