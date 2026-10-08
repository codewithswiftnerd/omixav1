"""
Cleaning profile: the declarative, data-only description of what to do to which column.

    {
      "name": "Nigerian Customer Dataset",                       (optional label)
      "columns": {
        "Name":  {"rules": [{"type": "trim_whitespace"}, {"type": "normalize_case", "mode": "title"}]},
        "Phone": {"rules": [{"type": "normalize_phone", "country": "NG", "output_format": "international"}]}
      },
      "decisions": [                                              (optional, review answers)
        {"column": "Status", "original": "pendng", "action": "accept", "value": "Pending"}
      ]
    }

Parsing instantiates rule classes, which validates every parameter. Nothing in a profile is ever
evaluated, imported or executed: types are looked up in a fixed registry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from cleaning.engine import access
from cleaning.engine.base import BaseCleaningRule, RuleConfigError
from cleaning.engine.registry import RULE_TYPES

MAX_PROFILE_BYTES = 256 * 1024
MAX_COLUMNS = 200
MAX_RULES_PER_COLUMN = 20
MAX_DECISIONS = 2000


@dataclass
class RuleInstance:
    rule: BaseCleaningRule
    enabled: bool = True
    order: int = 0

    @property
    def type(self) -> str:
        return self.rule.type


@dataclass
class Decision:
    column: str
    original: str
    action: str
    value: Any = None


@dataclass
class CleaningProfile:
    name: str
    columns: dict[str, list[RuleInstance]]
    decisions: list[Decision] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def rule_types(self) -> set[str]:
        return {r.type for rules in self.columns.values() for r in rules if r.enabled}

    def required_capabilities(self) -> set[str]:
        """Everything the plan must allow for this profile to run. Disabled rules are not
        run, so they do not require anything."""
        return {"cleaning_profiles", "column_rules"} | self.rule_types()


def parse_profile(raw: Any) -> CleaningProfile:
    if not isinstance(raw, dict):
        raise RuleConfigError("'cleaning_profile' must be an object.")
    try:
        if len(json.dumps(raw, default=str)) > MAX_PROFILE_BYTES:
            raise RuleConfigError("The cleaning profile is too large.")
    except (TypeError, ValueError):
        raise RuleConfigError("The cleaning profile must be plain JSON data.")
    unknown = sorted(set(raw) - {"name", "columns", "decisions", "description"})
    if unknown:
        raise RuleConfigError(f"Unknown cleaning profile field(s): {', '.join(map(str, unknown))}.")
    name = raw.get("name", "Cleaning profile")
    if not isinstance(name, str) or len(name) > 80:
        raise RuleConfigError("'name' must be text up to 80 characters.")
    cols = raw.get("columns")
    if not isinstance(cols, dict) or not cols:
        raise RuleConfigError("'columns' must be an object mapping column names to rules.")
    if len(cols) > MAX_COLUMNS:
        raise RuleConfigError(f"A profile can configure at most {MAX_COLUMNS} columns.")

    parsed: dict[str, list[RuleInstance]] = {}
    for col, spec in cols.items():
        if not isinstance(col, str) or not col.strip() or len(col) > 200:
            raise RuleConfigError("Column names must be non-empty text.")
        if not isinstance(spec, dict) or set(spec) - {"rules"}:
            raise RuleConfigError(f"Column '{col}' must look like {{\"rules\": [...]}}.", column=col)
        rules = spec.get("rules")
        if not isinstance(rules, list) or not rules:
            raise RuleConfigError(f"Column '{col}' needs a non-empty list of rules.", column=col)
        if len(rules) > MAX_RULES_PER_COLUMN:
            raise RuleConfigError(f"Column '{col}' has too many rules.", column=col)
        seen, items = set(), []
        for i, item in enumerate(rules):
            if not isinstance(item, dict):
                raise RuleConfigError(f"Each rule for '{col}' must be an object.", column=col)
            rtype = item.get("type")
            if not isinstance(rtype, str) or rtype not in RULE_TYPES:
                shown = rtype if isinstance(rtype, str) and len(rtype) <= 60 else "?"
                raise RuleConfigError(f"Unknown rule type '{shown}' for column '{col}'.",
                                      rule=shown if shown != "?" else None, column=col, code="UNKNOWN_RULE")
            if rtype in seen:
                raise RuleConfigError(f"Rule '{rtype}' is listed twice for column '{col}'.", rule=rtype, column=col)
            seen.add(rtype)
            enabled = item.get("enabled", True)
            if not isinstance(enabled, bool):
                raise RuleConfigError("'enabled' must be true or false.", rule=rtype, column=col)
            params = {k: v for k, v in item.items() if k not in ("type", "enabled")}
            try:
                rule = RULE_TYPES[rtype](params)
            except RuleConfigError as exc:
                exc.column = exc.column or col
                raise
            items.append(RuleInstance(rule, enabled, i))
        parsed[col] = items

    decisions = []
    raw_dec = raw.get("decisions", [])
    if not isinstance(raw_dec, list) or len(raw_dec) > MAX_DECISIONS:
        raise RuleConfigError("'decisions' must be a list.")
    for d in raw_dec:
        if not isinstance(d, dict) or set(d) - {"column", "original", "action", "value"}:
            raise RuleConfigError("Each decision needs column, original, action (and value).")
        if not isinstance(d.get("column"), str) or not isinstance(d.get("original"), str):
            raise RuleConfigError("A decision's 'column' and 'original' must be text.")
        if d.get("action") not in ("accept", "reject"):
            raise RuleConfigError("A decision's 'action' must be 'accept' or 'reject'.")
        v = d.get("value")
        if v is not None and (isinstance(v, bool) or not isinstance(v, (str, int, float)) or
                              (isinstance(v, str) and len(v) > 200)):
            raise RuleConfigError("A decision's 'value' must be short text or a number.")
        decisions.append(Decision(d["column"], d["original"], d["action"], v))
    return CleaningProfile(name.strip() or "Cleaning profile", parsed, decisions, raw)


def authorize_profile(profile: CleaningProfile, *, is_pro: bool) -> None:
    """Raises access.FeatureNotAvailable. `is_pro` MUST be the server-derived entitlement."""
    access.authorize(profile.required_capabilities(), is_pro=is_pro)
