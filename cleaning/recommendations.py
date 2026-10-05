"""
Recommendations

Turns the read-only quality report into three decisions, mirroring how OMIXA treats data:

    SAFE TO AUTO-FIX     deterministic, low-risk, reversible transformations. Backed by a
                         specific cleaning rule; both the finding and the fix must be high
                         confidence (see quality_report.enrich_finding).
    REQUIRES USER REVIEW ambiguous values, uncertain inference, imputation, or anything
                         destructive. OMIXA offers choices but never picks one for you.
    DO NOT AUTO-MODIFY   correctness cannot reasonably be established from the data
                         (lost digits, reused identifiers, possible non-binary gender
                         answers, stale records...). Reported, never touched.

Nothing here mutates a dataframe and nothing here decides which rule exists or what it
does: each finding already carries its class from the rule registry. This module groups
them for the frontend.

Backwards-compatible shape:
    safe       -> [{rule, reason, issue_count, findings}]   (findings classed SAFE)
    ambiguous  -> [finding, ...]                            (REVIEW + DO_NOT_MODIFY)
    recommended_rules -> rules to pre-select (the SAFE ones only)
New:
    review, do_not_modify -> the two halves of `ambiguous`
    summary               -> counts per class
    operation_legend      -> what normalization / imputation / correction / inference / deletion mean
"""

from __future__ import annotations

from cleaning import model as M
from cleaning.resolutions import RESOLUTION_OPTIONS
from cleaning.rule_registry import spec_for_fixer

OPERATION_LEGEND = {
    M.NORMALIZATION: "Same value in a canonical form (trim, case, ISO date). No information added or lost.",
    M.IMPUTATION: "A value that was never in the file is added. It is an estimate, not the real value.",
    M.CORRECTION: "A value is replaced by a different one.",
    M.INFERENCE: "A value is derived from an assumption (e.g. which country a phone number belongs to).",
    M.DELETION: "A value, row or column is removed or blanked.",
}


def _safe_reason(rule: str) -> str:
    spec = spec_for_fixer(rule)
    return spec.explanation if spec else ""


def generate_recommendations(report: dict) -> dict:
    findings = report.get("findings", [])

    safe_by_rule: dict[str, list[dict]] = {}
    review: list[dict] = []
    protected: list[dict] = []

    for finding in findings:
        cls = finding.get("remediation", M.REQUIRES_REVIEW)
        rule = finding.get("resolver")
        if cls == M.SAFE_AUTO_FIX and rule:
            safe_by_rule.setdefault(rule, []).append(finding)
            continue
        # Attach the available resolution choices right onto the finding, so the frontend
        # can render "day-first / month-first"-style buttons without a second lookup. A
        # DO_NOT_MODIFY finding never offers any: there is nothing safe to pick.
        options = RESOLUTION_OPTIONS.get(finding.get("issue"), []) if cls != M.DO_NOT_MODIFY else []
        enriched = {**finding, "resolution_options": options}
        (protected if cls == M.DO_NOT_MODIFY else review).append(enriched)

    safe = []
    for rule, matched in safe_by_rule.items():
        spec = spec_for_fixer(rule)
        safe.append({
            "rule": rule,
            "rule_id": spec.id if spec else None,
            "reason": _safe_reason(rule),
            "operation": spec.operation if spec else M.NORMALIZATION,
            "confidence": min((f["fix_confidence"] or 1.0) for f in matched),
            "reversibility": spec.reversibility if spec else M.REVERSIBLE,
            "issue_count": len(matched),
            "affected_count": sum(f["affected_count"] for f in matched),
            "findings": matched,
        })

    ambiguous = review + protected
    return {
        "safe": safe,
        "ambiguous": ambiguous,
        "review": review,
        "do_not_modify": protected,
        "recommended_rules": list(safe_by_rule.keys()),
        "summary": {
            M.SAFE_AUTO_FIX: sum(len(v) for v in safe_by_rule.values()),
            M.REQUIRES_REVIEW: len(review),
            M.DO_NOT_MODIFY: len(protected),
        },
        "operation_legend": OPERATION_LEGEND,
    }
