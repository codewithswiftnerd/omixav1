"""
Shared vocabulary for the OMIXA quality model.

Everything that talks about *what kind of problem* something is, *how bad* it is,
and *what may be done about it* uses the constants below, so the detectors, the
scoring engine, the recommendations and the audit log can never drift apart.
"""

from __future__ import annotations

# --- Quality dimensions ------------------------------------------------------
COMPLETENESS = "completeness"
VALIDITY = "validity"
UNIQUENESS = "uniqueness"
CONSISTENCY = "consistency"
ACCURACY = "accuracy"      # only where objectively measurable (impossible values)
TIMELINESS = "timeliness"  # only where a date column gives it meaning

DIMENSIONS = (COMPLETENESS, VALIDITY, UNIQUENESS, CONSISTENCY, ACCURACY, TIMELINESS)

# Relative weight of each dimension in the overall score. Dimensions that are not
# applicable to a dataset are dropped and the remainder re-normalised, so a file
# with no date columns is never penalised (or rewarded) for timeliness.
DIMENSION_WEIGHTS = {
    COMPLETENESS: 0.25,
    VALIDITY: 0.25,
    UNIQUENESS: 0.15,
    CONSISTENCY: 0.15,
    ACCURACY: 0.15,
    TIMELINESS: 0.05,
}

# --- Severity ----------------------------------------------------------------
CRITICAL, HIGH, MEDIUM, LOW = "critical", "high", "medium", "low"
SEVERITY_LEVELS = (CRITICAL, HIGH, MEDIUM, LOW)  # most -> least severe
_SEVERITY_RANK = {CRITICAL: 3, HIGH: 2, MEDIUM: 1, LOW: 0}

# The older UI/API speaks critical / warning / info. It is now *derived* from the
# impact-based level (single source of truth), never computed separately.
LEGACY_SEVERITY = {CRITICAL: "critical", HIGH: "warning", MEDIUM: "warning", LOW: "info"}

# impact = adjusted affected share x rule criticality x field importance
SEVERITY_THRESHOLDS = ((CRITICAL, 0.30), (HIGH, 0.12), (MEDIUM, 0.05))

# Score ceilings: a dataset with an unresolved critical/high defect cannot score
# like a clean one just because the defect sits in a small share of the cells.
SCORE_CAPS = {CRITICAL: 79, HIGH: 89}


def severity_rank(level: str) -> int:
    return _SEVERITY_RANK[level]


def max_severity(a: str, b: str) -> str:
    return a if _SEVERITY_RANK[a] >= _SEVERITY_RANK[b] else b


def min_severity(a: str, b: str) -> str:
    return a if _SEVERITY_RANK[a] <= _SEVERITY_RANK[b] else b


# --- Remediation classes -----------------------------------------------------
SAFE_AUTO_FIX = "safe_auto_fix"        # deterministic, low-risk
REQUIRES_REVIEW = "requires_review"    # ambiguous / inferential / destructive
DO_NOT_MODIFY = "do_not_modify"        # correctness cannot be established

REMEDIATION_LABELS = {
    SAFE_AUTO_FIX: "Safe to auto-fix",
    REQUIRES_REVIEW: "Requires user review",
    DO_NOT_MODIFY: "Do not auto-modify",
}

# --- What kind of change an operation is -------------------------------------
NORMALIZATION = "normalization"  # same value, canonical form (trim, case, ISO date)
IMPUTATION = "imputation"        # a value that was never in the file is added
CORRECTION = "correction"        # a value is replaced with a different one
INFERENCE = "inference"          # a value is derived from an assumption
DELETION = "deletion"            # a value, row or column is removed / blanked
NONE = "none"                    # diagnostic only

OPERATION_KINDS = (NORMALIZATION, IMPUTATION, CORRECTION, INFERENCE, DELETION, NONE)

# --- Reversibility -----------------------------------------------------------
REVERSIBLE = "reversible"                  # original value is kept in the change ledger
PARTIALLY_REVERSIBLE = "partially_reversible"  # ledger was truncated for size
IRREVERSIBLE = "irreversible"              # cannot be reconstructed from the ledger
NOT_APPLICABLE = "not_applicable"          # nothing is changed

# --- Approval ----------------------------------------------------------------
APPROVAL_AUTOMATIC = "automatic"          # ran because the default rule set ran
APPROVAL_USER_SELECTED = "user_selected"  # the user ticked this rule
APPROVAL_USER_APPROVED = "user_approved"  # the user picked a resolution choice


def grade_for(score: float) -> str:
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 60:
        return "D"
    return "F"
