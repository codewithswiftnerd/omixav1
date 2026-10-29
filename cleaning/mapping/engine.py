"""Propose -> preview -> apply. Pure: building a plan never touches the data, so a suggestion can never
change a value (or a quality score). `apply_plan` is the only function that produces new values, and it
produces them only for resolutions the mapper marked safe (or the user wrote themselves)."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd

from cleaning.mapping.base import Mapper
from cleaning.mapping.boolean import BooleanMapper
from cleaning.mapping.category import CategoryMapper
from cleaning.mapping.country import CountryMapper
from cleaning.mapping.currency import CurrencyMapper
from cleaning.mapping.model import (AMBIGUOUS, APPLIED_STATUSES, CANONICAL, NOT_APPLICABLE, REVIEW, SAFE, SCOPE_FILE,
                                    UNKNOWN, USER_DEFINED, ColumnAnalysis, MappingRule, Resolution, is_blank)
from cleaning.mapping.normalize import fold

MAPPERS: dict[str, Mapper] = {m.category: m for m in (CountryMapper(), BooleanMapper(), CurrencyMapper(), CategoryMapper())}
MAX_PREVIEW_ITEMS = 200


def _ckey(text: str) -> str:
    return fold(text) or str(text).strip()          # "$" folds to nothing: fall back to the symbol itself


def validate_custom_mappings(mappings: Optional[dict]) -> dict:
    """{source: target} as the user wrote it. Rejects empty targets (that would silently delete data) and
    two sources that mean the same text but map to different targets."""
    out, seen = {}, {}
    for src, dst in (mappings or {}).items():
        if not isinstance(src, str) or not isinstance(dst, str):
            raise ValueError("Mappings must be text to text.")
        if not src.strip():
            raise ValueError("A mapping needs a source value.")
        if not dst.strip():
            raise ValueError(f"'{src}' maps to an empty value. Use Leave unchanged instead of mapping to nothing.")
        k = _ckey(src)
        if k in seen and seen[k] != dst:
            raise ValueError(f"'{src}' is mapped to two different values.")
        seen[k] = dst
        out[src] = dst
    return out


@dataclass
class PlanItem:
    original: Any
    count: int
    status: str
    target: Optional[str]
    rendered: Optional[str]
    rule: Optional[MappingRule]
    reason: str
    confidence: float
    candidates: list
    origin: str                                       # reference | custom_file
    rows: list = field(default_factory=list)

    @property
    def will_change(self) -> bool:
        return self.status in APPLIED_STATUSES and self.rendered is not None

    def to_dict(self) -> dict:
        return {"original": _plain(self.original), "proposed": self.rendered, "count": self.count,
                "status": self.status, "applied_automatically": self.will_change,
                "needs_approval": self.status == REVIEW, "reason": self.reason,
                "confidence": round(self.confidence, 2), "candidates": list(self.candidates),
                "rule_id": self.rule.rule_id if self.rule else None, "origin": self.origin}


@dataclass
class MappingPlan:
    column: str
    category: str
    output_format: str
    analysis: ColumnAnalysis
    items: list
    params: dict

    def summary(self) -> dict:
        s = defaultdict(int)
        for it in self.items:
            s[it.status] += it.count
        total = sum(s.values())
        return {"cells_checked": total, "will_apply": s[SAFE] + s[USER_DEFINED], "needs_approval": s[REVIEW],
                "ambiguous": s[AMBIGUOUS], "unrecognised": s[UNKNOWN], "already_canonical": s[CANONICAL],
                "not_applicable": s[NOT_APPLICABLE], "distinct_values": len(self.items)}

    def to_dict(self, limit: int = 50) -> dict:
        order = {REVIEW: 0, AMBIGUOUS: 1, SAFE: 2, USER_DEFINED: 2, UNKNOWN: 3, CANONICAL: 4, NOT_APPLICABLE: 5}
        shown = sorted((i for i in self.items if i.status != CANONICAL), key=lambda i: (order.get(i.status, 9), -i.count))
        return {"column": self.column, "category": self.category, "output_format": self.output_format,
                "column_fit": {"applicable": self.analysis.applicable, "strength": self.analysis.strength,
                               "evidence": round(self.analysis.evidence, 2), "reason": self.analysis.reason,
                               **{k: v for k, v in self.analysis.details.items() if k not in ("plan",)}},
                "summary": self.summary(), "items": [i.to_dict() for i in shown[:min(limit, MAX_PREVIEW_ITEMS)]],
                "items_truncated": len(shown) > min(limit, MAX_PREVIEW_ITEMS)}


@dataclass
class ApplyResult:
    values: pd.Series
    flags: list
    provenance: dict
    stats: dict


def _plain(v):
    if hasattr(v, "item") and not isinstance(v, str):
        try:
            return v.item()
        except Exception:
            pass
    return v if isinstance(v, (str, int, float, bool)) or v is None else str(v)


def _distinct(series: pd.Series) -> dict:
    """{(type, value): [raw, [row labels]]}: 1 and True stay distinct."""
    out: dict = {}
    for idx, v in series.items():
        if is_blank(v):
            continue
        k = (type(v).__name__, v if isinstance(v, (str, int, float, bool)) else str(v))
        ent = out.get(k)
        if ent is None:
            out[k] = [v, [idx]]
        else:
            ent[1].append(idx)
    return out


def build_plan(series: pd.Series, column: str, category: str, *, output_format: Optional[str] = None,
               mappings: Optional[dict] = None, params: Optional[dict] = None) -> MappingPlan:
    if category not in MAPPERS:
        raise ValueError(f"Unknown mapping category '{category}'.")
    mapper = MAPPERS[category]
    fmt = output_format or mapper.default_output
    if fmt not in mapper.output_formats:
        raise ValueError(f"'{fmt}' is not an output format for {category}. Choose one of: {', '.join(mapper.output_formats)}.")
    params = dict(params or {})
    custom = validate_custom_mappings(mappings)
    params["mappings"] = custom
    custom_by_key = {_ckey(s): (s, d) for s, d in custom.items()}

    distinct = _distinct(series)
    counts: dict = {}
    for raw, rows in distinct.values():
        counts[raw] = counts.get(raw, 0) + len(rows)
    analysis = mapper.analyze(counts, column, params)

    items = []
    for raw, rows in distinct.values():
        n = len(rows)
        origin = "reference"
        k = _ckey(raw) if isinstance(raw, str) else None
        if k is not None and k in custom_by_key:
            src, dst = custom_by_key[k]
            res = Resolution(USER_DEFINED, dst, MappingRule(category, src, dst, "fold", "user-mapping", "1", 1.0, True, (),
                                                           "user-mapping:file", SCOPE_FILE),
                             "Your mapping for this file.", 1.0, literal=dst)
            origin = "custom_file"
        elif not analysis.applicable:
            res = Resolution(NOT_APPLICABLE, None, None, analysis.reason)
        else:
            res = mapper.resolve(raw, analysis, params)
        rendered = res.literal if res.literal is not None else (mapper.render(res.target, fmt) if res.target else None)
        status = res.status
        if rendered is not None and isinstance(raw, str) and raw == rendered and status in (SAFE, USER_DEFINED):
            status = CANONICAL                               # already exactly the requested form
        items.append(PlanItem(raw, n, status, res.target, rendered, res.rule, res.reason, res.confidence,
                              [mapper.render(c, fmt) if c in _safe_targets(mapper) else c for c in res.candidates],
                              origin, rows))
    return MappingPlan(str(column), category, fmt, analysis, items, params)


def _safe_targets(mapper: Mapper) -> set:
    try:
        if mapper.category == "country":
            from cleaning.mapping.country import _index
            return set(_index()["rows"])
        if mapper.category == "currency":
            from cleaning.mapping.currency import _index as cx
            return set(cx()["rows"])
    except Exception:  # pragma: no cover
        pass
    return set()


def apply_plan(series: pd.Series, plan: MappingPlan):
    """Returns (new_series, flags, provenance, stats). Only applied statuses change values."""
    from cleaning.engine.base import Flag, as_object
    out = as_object(series).copy()
    flags, prov = [], {}
    stats = {"applied": 0, "already_canonical": 0, "needs_approval": 0, "ambiguous": 0, "unrecognised": 0,
             "not_applicable": 0, "custom_applied": 0}
    ref_version = ""
    for it in plan.items:
        if it.rule is not None and it.rule.reference_version:
            ref_version = it.rule.reference_version
        if it.will_change:
            for r in it.rows:
                out.at[r] = it.rendered
                prov[r] = {"category": plan.category, "rule_id": it.rule.rule_id if it.rule else None,
                           "origin": it.origin, "mode": "user_defined" if it.status == USER_DEFINED else "automatic_safe",
                           "reference": it.rule.reference_source if it.rule else None,
                           "reference_version": it.rule.reference_version if it.rule else None,
                           "confidence": round(it.confidence, 2), "output_format": plan.output_format,
                           "reason": it.reason}
            stats["applied"] += it.count
            if it.status == USER_DEFINED:
                stats["custom_applied"] += it.count
        elif it.status == CANONICAL:
            stats["already_canonical"] += it.count
        elif it.status == NOT_APPLICABLE:
            stats["not_applicable"] += it.count
        else:
            key = {REVIEW: "needs_approval", AMBIGUOUS: "ambiguous", UNKNOWN: "unrecognised"}[it.status]
            stats[key] += it.count
            code = {REVIEW: "mapping_needs_approval", AMBIGUOUS: "mapping_ambiguous", UNKNOWN: "mapping_unrecognised"}[it.status]
            msg = it.reason or "Left as typed."
            sugg = it.rendered if it.status == REVIEW else None
            meta = {"category": plan.category, "rule_id": it.rule.rule_id if it.rule else None, "origin": it.origin,
                    "reference": it.rule.reference_source if it.rule else None,
                    "reference_version": it.rule.reference_version if it.rule else None,
                    "confidence": round(it.confidence, 2), "output_format": plan.output_format,
                    "proposed": it.rendered}
            for r in it.rows:
                flags.append(Flag(r, it.original, code, msg, sugg, meta))
    stats["reference_version"] = ref_version
    return ApplyResult(out, flags, prov, stats)


def suggest_for_dataframe(df: pd.DataFrame, *, max_columns: int = 200, preview: int = 8) -> list[dict]:
    """Which columns could use which mapping, with a preview. Read-only. Strong evidence only:
    nothing is suggested for a column that does not look like the category."""
    out = []
    for col in list(df.columns)[:max_columns]:
        s = df[col]
        if not (s.dtype == object or pd.api.types.is_string_dtype(s) or pd.api.types.is_bool_dtype(s)
                or pd.api.types.is_numeric_dtype(s)):
            continue
        found = []
        for cat in ("country", "currency", "boolean", "category"):
            try:
                plan = build_plan(s, str(col), cat)
            except ValueError:
                continue
            if not plan.analysis.applicable:
                continue
            sm = plan.summary()
            actionable = sm["will_apply"] + sm["needs_approval"] + sm["ambiguous"]
            if cat == "category" and not (sm["will_apply"] + sm["needs_approval"]):
                continue
            if actionable == 0 and sm["unrecognised"] == 0:
                continue
            found.append((cat, plan))
        if not found:
            continue
        strong = [f for f in found if f[0] != "category" and f[1].analysis.strength in ("strong", "moderate")]
        for cat, plan in (strong or found):
            d = plan.to_dict(limit=preview)
            d["output_formats"] = dict(MAPPERS[cat].output_formats)
            d["default_output"] = MAPPERS[cat].default_output
            d["why"] = plan.analysis.reason
            out.append(d)
    return out
