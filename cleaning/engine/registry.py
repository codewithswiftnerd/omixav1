"""Rule-type registry: the single list of executable rule classes. Rules register themselves by
class; plan access lives in access.py (kept separate on purpose)."""

from __future__ import annotations

from cleaning.engine import access
from cleaning.engine.base import BaseCleaningRule
from cleaning.engine.categories import DuplicateDetectionRule, StandardizeCategoriesRule
from cleaning.engine.dates import DateDetectionRule, NormalizeDateRule, ValidateDateRule
from cleaning.engine.email_rules import ValidateEmailRule
from cleaning.engine.numeric import BasicNumericCleanupRule, NormalizeCurrencyRule, RemoveCurrencySymbolsRule
from cleaning.engine.phone import NormalizePhoneRule, ValidatePhoneRule
from cleaning.engine.text import (CustomReplacementRule, MissingValueRule, NormalizeCaseRule,
                                  NormalizeWhitespaceRule, TrimWhitespaceRule)

_CLASSES = [
    TrimWhitespaceRule, NormalizeWhitespaceRule, BasicNumericCleanupRule, RemoveCurrencySymbolsRule,
    MissingValueRule, DateDetectionRule, DuplicateDetectionRule,
    NormalizeCaseRule, NormalizePhoneRule, ValidatePhoneRule, NormalizeDateRule, ValidateDateRule,
    ValidateEmailRule, NormalizeCurrencyRule, StandardizeCategoriesRule, CustomReplacementRule,
]
RULE_TYPES: dict[str, type[BaseCleaningRule]] = {c.type: c for c in _CLASSES}

# Every executable rule must be classified in access.py, and vice versa: a rule nobody
# classified would otherwise be silently denied (or worse, silently allowed).
assert set(RULE_TYPES) == set(access.FREE_RULES | access.PRO_RULES), \
    set(RULE_TYPES) ^ set(access.FREE_RULES | access.PRO_RULES)
for _c in _CLASSES:
    assert (_c.capability or _c.type) == _c.type


def catalog(is_pro: bool) -> list[dict]:
    """What the UI may show. `available` reflects the server-derived plan; the backend still
    re-checks on every submission."""
    out = []
    for t, cls in RULE_TYPES.items():
        d = cls.describe()
        d["required_plan"] = access.required_plan(cls.type)
        d["available"] = d["required_plan"] == access.FREE or is_pro
        out.append(d)
    out.sort(key=lambda d: (d["phase"], d["type"]))
    return out
