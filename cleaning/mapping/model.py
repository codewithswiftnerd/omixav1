"""Data model shared by every mapper: one rule shape, one resolution shape."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# Resolution status: what the engine may do with a value.
SAFE = "safe"                  # reference data + context establish it: applied automatically
REVIEW = "review"              # plausible but not certain: proposed, applied only after approval
AMBIGUOUS = "ambiguous"        # several targets (or a lossy one): never applied automatically
UNKNOWN = "unknown"            # not recognised: left exactly as typed
CANONICAL = "canonical"        # already the requested representation: nothing to do
USER_DEFINED = "user_defined"  # the user's own mapping for this file: applied (they wrote it)
NOT_APPLICABLE = "not_applicable"  # e.g. an amount in a currency-label column: never touched

APPLIED_STATUSES = (SAFE, USER_DEFINED)

SCOPE_REFERENCE, SCOPE_FILE, SCOPE_USER = "reference", "file", "user"


@dataclass(frozen=True)
class MappingRule:
    """One alias -> canonical target rule, with everything needed to audit it later."""
    category: str                     # country | boolean | category | currency | ...
    source: str                       # the alias as written in the reference data / by the user
    target: str                       # canonical key (country alpha-2, "true", a user's label, ...)
    normalization: str = "fold"       # exact | fold (case/space/punctuation/diacritics)
    reference_source: str = ""        # e.g. "iso3166-1"
    reference_version: str = ""
    confidence: float = 1.0
    auto_safe: bool = True            # may it be applied without approval, in a suitable context?
    contexts: tuple = ()              # column types / name hints where it applies; () = any
    rule_id: str = ""
    scope: str = SCOPE_REFERENCE      # reference | file | user (user scope needs persistence: not built)
    owner: Optional[str] = None       # user id for scope == "user"

    def describe(self) -> dict:
        return {"id": self.rule_id, "category": self.category, "source": self.source, "target": self.target,
                "normalization": self.normalization, "reference": self.reference_source,
                "version": self.reference_version, "confidence": self.confidence, "auto_safe": self.auto_safe,
                "contexts": list(self.contexts), "scope": self.scope}


@dataclass
class Resolution:
    status: str
    target: Optional[str] = None      # canonical KEY (rendered later in the user's output format)
    rule: Optional[MappingRule] = None
    reason: str = ""
    confidence: float = 0.0
    candidates: list = field(default_factory=list)
    # Optional per-value override of the rendered text (user-defined mappings are written verbatim)
    literal: Optional[str] = None


@dataclass
class ColumnAnalysis:
    """What a mapper concluded about a column before looking at single values."""
    category: str
    applicable: bool                  # False => the column is not of this kind: nothing is changed
    strength: str                     # strong | moderate | weak
    evidence: float                   # 0..1 share of values that fit this category
    reason: str = ""
    details: dict = field(default_factory=dict)


def is_blank(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and v != v:
        return True
    try:
        import pandas as pd
        if v is pd.NA or v is pd.NaT:
            return True
    except Exception:  # pragma: no cover
        pass
    return isinstance(v, str) and not v.strip()
