"""
Impact-based scoring.

The old model was `100 - (15 per critical + 6 per warning + 2 per info)`: one stray
space in one cell cost as much as the same space in every cell, and a column that was
99% corrupt cost the same as one that was 1% corrupt.

Here every finding gets an *impact* that depends on how much of the data it touches,
how bad that kind of defect is, and how much the damaged field matters:

    share        = affected / population                (how much of the column/table)
    share_adj    = share x population / (population+1) (small samples carry less evidence)
    impact       = share_adj x rule_criticality x field_factor
    field_factor = 0.5 + 0.5 x field_importance         (0.5 .. 1.0)

Severity is a function of impact (bounded by what the rule type allows), so the same
defect is `low` at 1% and `critical` when it wrecks a key field.

Dimension score = 100 x (1 - sum of that dimension's losses), where a column-level
loss is scaled by the column's share of total field importance (damage to a key column
costs more than the same damage to an unimportant one) and a table-level loss (duplicate
rows) is taken as-is. The overall score is the weighted mean of the dimensions that
actually apply to the dataset, then capped when an unresolved critical or high issue
exists, so a large clean dataset cannot hide one serious defect behind a good average.
"""

from __future__ import annotations

from typing import Optional

from cleaning import model as M

SMALL_SAMPLE_ROWS = 5  # below this, severity is capped at `medium` unless the rule demands more


def field_factor(importance: float) -> float:
    return 0.5 + 0.5 * max(0.0, min(1.0, importance))


def adjusted_share(affected: int, population: int) -> float:
    if population <= 0 or affected <= 0:
        return 0.0
    share = min(1.0, affected / population)
    return share * (population / (population + 1.0))


def impact_of(affected: int, population: int, criticality: float, importance: float) -> float:
    return round(adjusted_share(affected, population) * criticality * field_factor(importance), 4)


def level_for(impact: float, *, population: int, min_level: str, max_level: str,
              floor: Optional[str] = None) -> str:
    level = M.LOW
    for name, threshold in M.SEVERITY_THRESHOLDS:
        if impact >= threshold:
            level = name
            break
    if population < SMALL_SAMPLE_ROWS:
        level = M.min_severity(level, M.MEDIUM)
    level = M.max_severity(level, min_level)
    level = M.min_severity(level, max_level)
    if floor:
        level = M.max_severity(level, floor)
    return level


def _dimension_applicable(dim: str, findings: list[dict], evaluated: set) -> bool:
    return dim in evaluated or any(f["dimension"] == dim for f in findings)


def score_dataset(findings: list[dict], profile: dict, *, total_rows: int, evaluated: set) -> dict:
    """
    findings: enriched findings (each has dimension, impact, column, unit,
    affected_count, population, criticality, level).
    Returns {"overall": int, "grade": str, "dimensions": {...}, "capped_by": level|None}.
    """
    importances = [p.importance for p in profile.values()]
    total_importance = sum(importances) or 1.0

    dimensions: dict[str, dict] = {}
    for dim in M.DIMENSIONS:
        in_dim = [f for f in findings if f["dimension"] == dim]
        if not _dimension_applicable(dim, findings, evaluated):
            dimensions[dim] = {"score": None, "applicable": False, "issue_count": 0, "loss": 0.0}
            continue
        loss = 0.0
        for f in in_dim:
            share = adjusted_share(f["affected_count"], f["population"])
            base = share * f["criticality"]
            col = f.get("column")
            if col is not None and col in profile and f.get("unit") != "column":
                loss += base * (profile[col].importance / total_importance)
            else:
                loss += base
        loss = min(1.0, loss)
        dimensions[dim] = {
            "score": int(round(100 * (1 - loss))),
            "applicable": True,
            "issue_count": len(in_dim),
            "loss": round(loss, 4),
        }

    weights = {d: M.DIMENSION_WEIGHTS[d] for d, v in dimensions.items() if v["applicable"]}
    if not weights:
        overall, capped_by, uncapped = 100, None, 100
    else:
        total_w = sum(weights.values())
        overall = sum(dimensions[d]["score"] * w for d, w in weights.items()) / total_w
        uncapped = int(round(overall))
        capped_by = None
        worst = None
        for f in findings:
            if f["level"] in M.SCORE_CAPS and (worst is None or M.severity_rank(f["level"]) > M.severity_rank(worst)):
                worst = f["level"]
        if worst is not None:
            cap = M.SCORE_CAPS[worst]
            if overall > cap:
                overall, capped_by = cap, worst
        overall = int(round(overall))

    return {"overall": overall, "uncapped": uncapped, "grade": M.grade_for(overall),
            "dimensions": dimensions, "capped_by": capped_by}
