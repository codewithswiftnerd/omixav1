"""
Runs a parsed CleaningProfile over a DataFrame, inside the worker's pipeline.

For every configured column the rules run in phase order (whitespace -> missing -> replacements ->
semantic normalisation -> case -> categories -> validation -> detection), each step is recorded in
the existing AuditLog (so the change ledger, `revert()` and the audit summary keep working), and
everything a rule refused to change becomes a review item with the original value preserved.
"""

from __future__ import annotations

import re
from typing import Optional

import pandas as pd

from cleaning import model as M
from cleaning import profiling
from cleaning.audit import AuditLog, changed_cells_mask
from cleaning.engine.base import RuleConfigError, RuleContext, as_object, blank_mask, inconsistency_pct
from cleaning.engine.profile import CleaningProfile

MAX_REVIEW_ITEMS_RETURNED = 500
MAX_CHANGE_SAMPLES = 25

# Which default (whole-dataset) rules a configured rule supersedes for that column, so the
# user's chosen output is never silently re-written by an automatic rule afterwards.
SUPERSEDES = {
    "normalize_phone": {"phone_cleaning"}, "validate_phone": set(),
    "normalize_date": {"date_standardization"}, "validate_date": set(), "basic_date_detection": set(),
    "normalize_currency": {"numeric_text_cleaning"}, "basic_numeric_cleanup": {"numeric_text_cleaning"},
    "remove_currency_symbols": {"numeric_text_cleaning"},
    "normalize_case": {"gender_standardization", "country_standardization", "boolean_standardization",
                       "categorical_standardization"},
    "standardize_categories": {"gender_standardization", "country_standardization",
                               "boolean_standardization", "categorical_standardization"},
    "custom_replacements": {"gender_standardization", "country_standardization", "boolean_standardization",
                            "categorical_standardization"},
    "basic_missing_value_handling": {"missing_token_normalization"},
    "validate_email": set(), "basic_duplicate_detection": set(),
}


def _norm(name) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")


def resolve_columns(df: pd.DataFrame, wanted) -> dict[str, str]:
    """profile column name -> real column name. Exact match first, then a case/space/underscore-
    insensitive match that must be unique. Anything else is an error (never a silent skip)."""
    exact = {str(c): c for c in df.columns}
    by_norm: dict[str, list] = {}
    for c in df.columns:
        by_norm.setdefault(_norm(c), []).append(c)
    out, missing = {}, []
    for w in wanted:
        if w in exact:
            out[w] = exact[w]
        elif len(by_norm.get(_norm(w), [])) == 1:
            out[w] = by_norm[_norm(w)][0]
        else:
            missing.append(w)
    if missing:
        raise RuleConfigError(
            "These columns are not in the uploaded file: " + ", ".join(f"'{m}'" for m in missing[:10]) +
            ("..." if len(missing) > 10 else "") + ".", column=missing[0], code="UNKNOWN_COLUMN")
    targets = list(out.values())
    if len(set(map(str, targets))) != len(targets):
        raise RuleConfigError("Two profile entries point at the same column.", code="DUPLICATE_COLUMN")
    return out


def run_profile(df: pd.DataFrame, profile: CleaningProfile, *, audit: Optional[AuditLog] = None,
                labels: Optional[dict] = None, include_detail: bool = True) -> tuple[pd.DataFrame, dict]:
    """Returns (df, report). `df` is modified in place column by column (callers pass their own
    working frame); nothing outside the configured columns is touched."""
    mapping = resolve_columns(df, list(profile.columns))
    labels = labels or {c: c for c in df.columns}
    report_cols: dict[str, dict] = {}
    review: list[dict] = []
    totals = {"cells_changed": 0, "cells_flagged": 0, "rules_run": 0}
    superseded: dict[str, set] = {}
    next_id = 0

    for pcol, real in mapping.items():
        original = as_object(df[real])
        before_first = original
        n = len(original)
        try:
            ptype = profiling.profile_column(str(real), original, n)
            detected = {"type": ptype.semantic_type, "confidence": round(float(ptype.confidence), 2)}
        except Exception:                                    # detection is advisory only
            detected = {"type": "unknown", "confidence": 0.0}
        info = {
            "column": str(real), "detected": detected,
            "missing_before": int(blank_mask(original).sum()), "rules": [], "review_items": 0,
        }
        series = original
        active = [r for r in profile.columns[pcol] if r.enabled]
        for skipped in (r for r in profile.columns[pcol] if not r.enabled):
            info["rules"].append({"type": skipped.type, "status": "disabled"})
        active.sort(key=lambda r: (r.rule.phase, r.order))
        ctx = RuleContext(column=str(real), row_count=n)
        col_flags: list = []
        for inst in active:
            rule = inst.rule
            res = rule.apply(series, ctx)
            after = as_object(res.values)
            mask = changed_cells_mask(series, after) if rule.mutates else pd.Series(False, index=series.index)
            changed = int(mask.sum())
            entry = {
                "type": rule.type, "status": "ran", "changes_data": rule.mutates,
                "changed": changed, "flagged": len(res.flags), "metrics": res.metrics,
            }
            if res.notes:
                entry["notes"] = res.notes
            if rule.mutates and changed:
                entry["inconsistency_before_pct"] = inconsistency_pct(series)
                entry["inconsistency_after_pct"] = inconsistency_pct(after)
            if include_detail and rule.mutates and changed:
                idx = list(series.index[mask][:MAX_CHANGE_SAMPLES])
                entry["samples"] = [{"row": _row(i), "before": _show(series.at[i]), "after": _show(after.at[i])}
                                    for i in idx]
            info["rules"].append(entry)
            if audit is not None and changed:
                audit.record_step(
                    rule.type, pd.DataFrame({real: series}), pd.DataFrame({real: after}),
                    rule_id=f"OMX-ENG-{rule.type.upper().replace('_', '-')}", operation=rule.operation,
                    confidence=rule.fix_confidence, approval=M.APPROVAL_USER_SELECTED,
                    reason=rule.description, reversibility=M.REVERSIBLE, issue=None,
                    column_labels={real: labels.get(real, real)})
            for f in res.flags:
                next_id += 1
                col_flags.append((next_id, rule.type, f))
            totals["cells_changed"] += changed
            totals["cells_flagged"] += len(res.flags)
            totals["rules_run"] += 1
            superseded.setdefault(labels.get(real, real), set()).update(SUPERSEDES.get(rule.type, set()))
            series = after

        # ---- review decisions the user already made for this column
        df[real] = series
        series, applied = _apply_decisions(df, real, col_flags, profile, pcol, audit, labels, original)
        df[real] = series
        for fid, rtype, f in col_flags:
            st = applied.get(fid, "pending")
            review.append({"id": f"rev-{fid:05d}", "column": str(real), "rule": rtype, "row": _row(f.row),
                           "original": _show(_safe_at(original, f.row)), "value_when_flagged": _show(f.original), "reason": f.reason, "message": f.message,
                           "suggestion": _show(f.suggestion), "status": st})
        info["review_items"] = len(col_flags)
        info["review_pending"] = sum(1 for fid, _, _ in col_flags if applied.get(fid, "pending") == "pending")
        info["missing_after"] = int(blank_mask(series).sum())
        info["inconsistency_before_pct"] = inconsistency_pct(before_first)
        info["inconsistency_after_pct"] = inconsistency_pct(series)
        report_cols[str(real)] = info

    unmatched = _unmatched(profile, review, mapping)
    report = {
        "profile": profile.name,
        "columns": report_cols,
        "totals": {**totals, "review_pending": sum(1 for r in review if r["status"] == "pending"),
                   "review_total": len(review)},
        "review": {"items": review[:MAX_REVIEW_ITEMS_RETURNED], "truncated": len(review) > MAX_REVIEW_ITEMS_RETURNED},
        "unmatched_decisions": unmatched,
        "supersedes": {k: sorted(v) for k, v in superseded.items() if v},
    }
    return df, report


def _apply_decisions(df, real, col_flags, profile, pcol, audit, labels, uploaded):
    """Applies accept/reject answers to flagged cells. Returns (series, {flag_id: status})."""
    series = as_object(df[real])
    status: dict[int, str] = {}
    mine = [d for d in profile.decisions if d.column == pcol or _norm(d.column) == _norm(real)]
    if not mine or not col_flags:
        return series, status
    by_text: dict[str, list] = {}
    for fid, rtype, f in col_flags:
        # a decision may quote the value as uploaded OR as it looked when the rule flagged it
        for text in {f.original, _safe_at(uploaded, f.row)}:
            if isinstance(text, str):
                by_text.setdefault(text, []).append((fid, f))
    before = series.copy()
    for d in mine:
        hits = by_text.get(d.original, [])
        if not hits:
            continue
        if d.action == "reject":
            for fid, _ in hits:
                status[fid] = "rejected"
            continue
        value = d.value
        if value is None:
            sugg = hits[0][1].suggestion
            if isinstance(sugg, str) and " or " not in sugg:
                value = sugg
        if value is None:
            continue                                         # nothing to apply: stays pending
        for fid, f in hits:
            series.at[f.row] = value
            status[fid] = "accepted"
    if audit is not None and status:
        audit.record_step(
            "review_decision", pd.DataFrame({real: before}), pd.DataFrame({real: series}),
            rule_id="OMX-ENG-REVIEW", operation=M.CORRECTION, confidence=None, approval=M.APPROVAL_USER_APPROVED,
            reason="You accepted a flagged value during review.", reversibility=M.REVERSIBLE,
            column_labels={real: labels.get(real, real)})
    return series, status


def _safe_at(series, row):
    try:
        return series.at[row]
    except Exception:
        return None


def _unmatched(profile, review, mapping) -> list[dict]:
    seen = {(r["column"], r["original"]) for r in review} | {(r["column"], r["value_when_flagged"]) for r in review}
    out = []
    for d in profile.decisions:
        real = mapping.get(d.column)
        real_name = str(real) if real is not None else d.column
        if (real_name, d.original) not in seen:
            out.append({"column": d.column, "original": d.original, "reason": "no flagged value matched this decision"})
    return out


def _row(i):
    try:
        return int(i)
    except (TypeError, ValueError):
        return str(i)


def _show(v):
    if v is None or (isinstance(v, float) and v != v):
        return None
    if isinstance(v, (int, float, bool, str)):
        return v
    if hasattr(v, "item"):
        try:
            return v.item()
        except Exception:
            pass
    return str(v)
