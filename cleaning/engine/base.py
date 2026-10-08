"""
Building blocks of the column-aware cleaning engine.

A cleaning rule is a small, declarative, side-effect-free transformation of ONE column:

    BaseCleaningRule.apply(series, ctx) -> RuleResult(values, flags, metrics)

Rules never see the user's plan (that is decided once, centrally, in `access.py`), never
touch other columns and never execute user-supplied code: their parameters are validated
against a fixed `ParamSpec` schema before a rule is even constructed.

Anything uncertain is FLAGGED (original preserved, reason + suggestion attached) instead of
changed. The executor turns flags into review items.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Optional

import pandas as pd

from cleaning import model as M

MAX_STR_PARAM = 200
MAX_LIST_PARAM = 200


class RuleConfigError(ValueError):
    """A rule's configuration is invalid. The message is safe to show the user."""

    def __init__(self, message: str, *, rule: Optional[str] = None, column: Optional[str] = None,
                 code: str = "INVALID_RULE_CONFIG"):
        super().__init__(message)
        self.rule, self.column, self.code = rule, column, code

    def to_dict(self) -> dict:
        d = {"code": self.code, "error": str(self)}
        if self.rule:
            d["rule"] = self.rule
        if self.column:
            d["column"] = self.column
        return d


# --------------------------------------------------------------------------- parameters

@dataclass(frozen=True)
class ParamSpec:
    kind: str                          # str | bool | int | float | choice | list_str | map_str | records
    default: Any = None
    required: bool = False
    choices: tuple = ()
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    description: str = ""
    nullable: bool = False
    fields: Optional[dict] = None      # for kind == "records": {field: ParamSpec}

    def describe(self) -> dict:
        d = {"type": self.kind, "required": self.required, "default": self.default, "description": self.description}
        if self.choices:
            d["choices"] = list(self.choices)
        if self.minimum is not None:
            d["minimum"] = self.minimum
        if self.maximum is not None:
            d["maximum"] = self.maximum
        if self.fields:
            d["fields"] = {k: v.describe() for k, v in self.fields.items()}
        return d


def _check_text(v: Any, name: str, rule: str, *, allow_empty: bool = True) -> str:
    if not isinstance(v, str):
        raise RuleConfigError(f"'{name}' must be text.", rule=rule)
    if len(v) > MAX_STR_PARAM:
        raise RuleConfigError(f"'{name}' is too long (max {MAX_STR_PARAM} characters).", rule=rule)
    if not allow_empty and not v.strip():
        raise RuleConfigError(f"'{name}' cannot be empty.", rule=rule)
    return v


def validate_params(schema: dict, raw: Any, rule: str) -> dict:
    """Strict: unknown keys, wrong types and out-of-range values are rejected, never coerced."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise RuleConfigError("Rule parameters must be an object.", rule=rule)
    unknown = sorted(set(raw) - set(schema))
    if unknown:
        raise RuleConfigError(f"Unknown parameter(s) for '{rule}': {', '.join(map(str, unknown))}.", rule=rule)
    out: dict = {}
    for name, spec in schema.items():
        if name not in raw or raw[name] is None and not spec.nullable and not spec.required:
            if spec.required and name not in raw:
                raise RuleConfigError(f"'{rule}' needs the parameter '{name}'.", rule=rule)
            out[name] = spec.default
            continue
        v = raw[name]
        if v is None:
            if spec.nullable:
                out[name] = None
                continue
            raise RuleConfigError(f"'{name}' cannot be empty.", rule=rule)
        k = spec.kind
        if k == "bool":
            if not isinstance(v, bool):
                raise RuleConfigError(f"'{name}' must be true or false.", rule=rule)
        elif k == "int":
            if isinstance(v, bool) or not isinstance(v, int):
                raise RuleConfigError(f"'{name}' must be a whole number.", rule=rule)
        elif k == "float":
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
                raise RuleConfigError(f"'{name}' must be a number.", rule=rule)
            v = float(v)
        elif k == "str":
            _check_text(v, name, rule)
        elif k == "choice":
            if not isinstance(v, (str, bool)) or v not in spec.choices:
                raise RuleConfigError(
                    f"'{name}' must be one of: {', '.join(str(c) for c in spec.choices)}.", rule=rule)
        elif k == "list_str":
            if not isinstance(v, list) or len(v) > MAX_LIST_PARAM:
                raise RuleConfigError(f"'{name}' must be a list of up to {MAX_LIST_PARAM} text values.", rule=rule)
            v = [_check_text(x, name, rule, allow_empty=False) for x in v]
        elif k == "map_str":
            if not isinstance(v, dict) or len(v) > MAX_LIST_PARAM:
                raise RuleConfigError(f"'{name}' must be an object of up to {MAX_LIST_PARAM} text pairs.", rule=rule)
            v = {_check_text(a, name, rule, allow_empty=False): _check_text(b, name, rule) for a, b in v.items()}
        elif k == "records":
            if not isinstance(v, list) or len(v) > MAX_LIST_PARAM:
                raise RuleConfigError(f"'{name}' must be a list of up to {MAX_LIST_PARAM} objects.", rule=rule)
            v = [validate_params(spec.fields, rec, rule) for rec in v]
        else:  # pragma: no cover - programmer error
            raise AssertionError(k)
        if spec.minimum is not None and isinstance(v, (int, float)) and v < spec.minimum:
            raise RuleConfigError(f"'{name}' must be at least {spec.minimum:g}.", rule=rule)
        if spec.maximum is not None and isinstance(v, (int, float)) and v > spec.maximum:
            raise RuleConfigError(f"'{name}' must be at most {spec.maximum:g}.", rule=rule)
        out[name] = v
    return out


# --------------------------------------------------------------------------- results

@dataclass
class Flag:
    """A value a rule refused to change silently."""
    row: Any
    original: Any
    reason: str                       # stable machine code, e.g. "ambiguous_date"
    message: str                      # human explanation
    suggestion: Any = None            # what the rule would propose, if anything

    def to_dict(self) -> dict:
        return {"row": _jsonable(self.row), "original": _jsonable(self.original), "reason": self.reason,
                "message": self.message, "suggestion": _jsonable(self.suggestion)}


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (str, bool, int, float)):
        return None if isinstance(v, float) and v != v else v
    if hasattr(v, "item"):
        try:
            return _jsonable(v.item())
        except Exception:
            pass
    return str(v)


@dataclass
class RuleResult:
    values: pd.Series
    flags: list[Flag] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass
class RuleContext:
    column: str                       # the column's name in the uploaded file
    row_count: int
    # decisions the user already made for this column: {original text: replacement}. Rules do
    # not read these; the executor applies them to flagged cells afterwards.


# --------------------------------------------------------------------------- helpers

def str_mask(series: pd.Series) -> pd.Series:
    """True where the cell is a Python/pandas string."""
    return series.map(lambda v: isinstance(v, str)).astype(bool)


def as_object(series: pd.Series) -> pd.Series:
    return series.astype(object) if series.dtype != object else series


def blank_mask(series: pd.Series) -> pd.Series:
    """Null or whitespace-only text."""
    isna = series.isna()
    s = str_mask(series)
    out = isna.copy()
    if s.any():
        out.loc[s] = series.loc[s].str.strip().eq("")
    return out.astype(bool)


def shape_signature(series: pd.Series) -> pd.Series:
    """Collapses each value to its 'shape' (digits -> 9, letters -> a/A) so formatting
    inconsistency can be measured without caring about the actual digits."""
    s = series.dropna().astype(str)
    return (s.str.replace(r"[A-Z]", "A", regex=True).str.replace(r"[a-z]", "a", regex=True)
             .str.replace(r"\d", "9", regex=True))


def inconsistency_pct(series: pd.Series) -> float:
    """% of non-empty values whose shape differs from the column's most common shape."""
    s = series[~blank_mask(series)]
    if s.empty:
        return 0.0
    sig = shape_signature(s)
    top = int(sig.value_counts().iloc[0])
    return round(100.0 * (len(sig) - top) / len(sig), 1)


def map_unique(series: pd.Series, decide) -> tuple:
    """Decides once per DISTINCT value, then maps the answer back over the column with C-level
    dict lookups (no per-row Python loop over the data).

    decide(raw) -> None                          leave as is
                 | ("set", new_value)            replace
                 | ("flag", reason, message, suggestion)
    Returns (new_series, flags, number_of_cells_changed)."""
    out = as_object(series).copy()
    nn = out[~out.isna()]
    if nn.empty:
        return out, [], 0
    decisions = {u: decide(u) for u in pd.unique(nn)}
    sets = {u: d[1] for u, d in decisions.items() if d and d[0] == "set" and not _same(d[1], u)}
    flagged = {u: d for u, d in decisions.items() if d and d[0] == "flag"}
    changed = 0
    if sets:
        sel = nn[nn.isin(list(sets))]
        out.loc[sel.index] = sel.map(sets)
        changed = len(sel)
    flags = []
    if flagged:
        sel = nn[nn.isin(list(flagged))]
        flags = [Flag(i, v, flagged[v][1], flagged[v][2], flagged[v][3] if len(flagged[v]) > 3 else None)
                 for i, v in sel.items()]
    return out, flags, changed


def _same(a, b) -> bool:
    return type(a) is type(b) and a == b


# --------------------------------------------------------------------------- base class

class BaseCleaningRule:
    type: ClassVar[str] = ""
    title: ClassVar[str] = ""
    description: ClassVar[str] = ""
    # which plan capability a user needs: looked up centrally in access.py, never here.
    capability: ClassVar[str] = ""
    operation: ClassVar[str] = M.NORMALIZATION
    # execution phase: lower runs first (whitespace -> semantic -> case -> categories -> checks)
    phase: ClassVar[int] = 30
    mutates: ClassVar[bool] = True     # False for validation / detection rules
    applies_to: ClassVar[tuple] = ("any",)
    params: ClassVar[dict] = {}
    fix_confidence: ClassVar[Optional[float]] = 0.95

    def __init__(self, raw_params: Any = None):
        self.p = validate_params(self.params, raw_params, self.type)
        self.check_config()

    def check_config(self) -> None:
        """Cross-parameter validation hook."""

    def apply(self, series: pd.Series, ctx: RuleContext) -> RuleResult:  # pragma: no cover
        raise NotImplementedError

    @classmethod
    def describe(cls) -> dict:
        return {
            "type": cls.type, "title": cls.title, "description": cls.description,
            "capability": cls.capability or cls.type, "changes_data": cls.mutates,
            "applies_to": list(cls.applies_to), "phase": cls.phase,
            "parameters": {k: v.describe() for k, v in cls.params.items()},
        }
