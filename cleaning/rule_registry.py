"""
Rule registry.

One `RuleSpec` describes everything OMIXA knows about one kind of problem or fix:

    id            stable identifier ("OMX-CMP-001") that appears in findings and in
                  the audit log, so a finding and the change that resolved it can be
                  joined without guessing
    issue         the finding key detectors emit (e.g. "invalid_email_format")
    detector      callable(ctx) -> list[finding dict]; None for fix-only rules
    dimension     which quality dimension the problem damages
    criticality   0-1 weight of how much this *kind* of defect matters
    confidence    how sure the detector is that this really is a defect
    min/max level severity bounds (a cosmetic issue never exceeds `low`; an
                  unrecoverable loss of data never drops below `high`)
    remediation   safe_auto_fix | requires_review | do_not_modify
    operation     normalization | imputation | correction | inference | deletion | none
    resolver      the cleaning rule name (rules.RULE_DISPATCH) or resolution issue key
                  (resolutions._RESOLVERS) that fixes it
    reversibility whether the original value survives in the change ledger
    explanation   WHY it is a problem
    recommendation WHAT to do

Adding a rule = write a detector, build a RuleSpec, call `register_rule`. Nothing else
needs to change: scoring, recommendations, the scorecard and the audit all read the
registry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from cleaning import model as M


@dataclass(frozen=True)
class RuleSpec:
    id: str
    issue: str
    title: str
    dimension: str
    explanation: str
    recommendation: str
    criticality: float = 0.5
    confidence: float = 0.8
    min_level: str = M.LOW
    max_level: str = M.CRITICAL
    remediation: str = M.REQUIRES_REVIEW
    operation: str = M.NONE
    resolver: Optional[str] = None
    reversibility: str = M.NOT_APPLICABLE
    # confidence that the *fix* is right (distinct from detector confidence)
    fix_confidence: Optional[float] = None
    detector: Optional[Callable] = field(default=None, compare=False, repr=False)
    # "cell" -> a count of affected cells in a column, "row" -> whole rows, "column" -> structure
    unit: str = "cell"

    def __post_init__(self):
        assert self.dimension in M.DIMENSIONS, self.dimension
        assert self.remediation in M.REMEDIATION_LABELS, self.remediation
        assert self.operation in M.OPERATION_KINDS, self.operation
        assert 0.0 <= self.criticality <= 1.0 and 0.0 <= self.confidence <= 1.0


_BY_ISSUE: dict[str, RuleSpec] = {}
_BY_ID: dict[str, RuleSpec] = {}
_ORDER: list[str] = []  # issue keys in registration order = detector run order


def register_rule(spec: RuleSpec, *, replace: bool = False) -> RuleSpec:
    if not replace and (spec.issue in _BY_ISSUE or spec.id in _BY_ID):
        raise ValueError(f"rule already registered: {spec.id} / {spec.issue}")
    if spec.issue in _BY_ISSUE:
        _ORDER.remove(spec.issue)
        del _BY_ID[_BY_ISSUE[spec.issue].id]
    _BY_ISSUE[spec.issue] = spec
    _BY_ID[spec.id] = spec
    _ORDER.append(spec.issue)
    return spec


def unregister_rule(issue: str) -> None:
    """Mainly for tests and for custom-rule experiments."""
    spec = _BY_ISSUE.pop(issue, None)
    if spec:
        _BY_ID.pop(spec.id, None)
        _ORDER.remove(issue)


def get_spec(issue: str) -> Optional[RuleSpec]:
    return _BY_ISSUE.get(issue)


def get_spec_by_id(rule_id: str) -> Optional[RuleSpec]:
    return _BY_ID.get(rule_id)


def all_specs() -> list[RuleSpec]:
    return [_BY_ISSUE[i] for i in _ORDER]


def detector_specs() -> list[RuleSpec]:
    return [s for s in all_specs() if s.detector is not None]


def specs_for_resolver(resolver: str) -> list[RuleSpec]:
    return [s for s in all_specs() if s.resolver == resolver]


def spec_for_fixer(rule_name: str) -> Optional[RuleSpec]:
    """The spec documenting a cleaning rule (a rules.RULE_DISPATCH key). Fixer specs
    are registered with issue "fix:<rule_name>". The audit log uses this to attach
    rule id, operation kind, confidence and reversibility to every change."""
    return _BY_ISSUE.get(f"fix:{rule_name}")
