"""
Central plan -> capability mapping. THE ONLY place that knows what Free and Pro may run.

Rules never check the plan themselves. Callers (the API, and again the worker as defence in
depth) ask `authorize()` with the plan the SERVER derived from the subscription record
(accounts.entitlements.effective). Nothing a client sends can influence it.
"""

from __future__ import annotations

from typing import Iterable

FREE, PRO = "free", "pro"

# Executable rule types a plan may run
FREE_RULES = frozenset({
    "trim_whitespace", "normalize_whitespace", "basic_numeric_cleanup", "remove_currency_symbols",
    "basic_duplicate_detection", "basic_missing_value_handling", "basic_date_detection",
})
PRO_RULES = frozenset({
    "normalize_phone", "validate_phone", "normalize_date", "validate_date", "normalize_case",
    "validate_email", "normalize_currency", "standardize_categories", "custom_replacements",
})
# Product features that are not themselves a rule
PRO_FEATURES = frozenset({
    "column_rules", "cleaning_profiles", "recommendations", "before_after_analysis", "detailed_change_log",
})
PRO_CAPABILITIES = PRO_RULES | PRO_FEATURES
ALL_CAPABILITIES = FREE_RULES | PRO_CAPABILITIES

PLAN_CAPABILITIES = {FREE: FREE_RULES, PRO: FREE_RULES | PRO_CAPABILITIES}


class FeatureNotAvailable(Exception):
    """Raised when the plan lacks a capability. `to_response()` is the HTTP 402 body."""

    def __init__(self, capability: str):
        super().__init__(f"'{capability}' is available with Omixa Pro.")
        self.capability = capability

    def to_response(self) -> dict:
        return {
            "code": "FEATURE_NOT_AVAILABLE",
            "rule": self.capability,
            "required_plan": PRO,
            "error": f"'{self.capability}' is available with Omixa Pro.",
            "upgrade_required": True,        # the flag the existing frontend already understands
        }


def plan_for(is_pro: bool) -> str:
    return PRO if is_pro else FREE


def required_plan(capability: str) -> str:
    return PRO if capability in PRO_CAPABILITIES else FREE


def authorize(capabilities: Iterable[str], *, is_pro: bool) -> None:
    """Raises FeatureNotAvailable for the first capability the plan lacks.
    `is_pro` must come from the server-side entitlement, never from the request."""
    allowed = PLAN_CAPABILITIES[plan_for(is_pro)]
    for cap in sorted(set(capabilities), key=lambda c: (c not in PRO_FEATURES, c)):
        if cap not in ALL_CAPABILITIES:
            raise FeatureNotAvailable(cap)       # unknown => deny, never allow by default
        if cap not in allowed:
            raise FeatureNotAvailable(cap)
