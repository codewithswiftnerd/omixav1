"""
Cell-based quality score.

The headline answers one question: how much of this file can you trust? It is measured from the
cells themselves, using the same per-cell classification the validation report shows
(cleaning/validation.py), so the score, the validation counts and the findings never disagree.

    1. Every cell earns credit:   1    filled in and valid
                                  0.5  filled in but untidy, odd, estimated, or needing a decision
                                  0    empty, a placeholder ("N/A", "-"), invalid, or an amount in a
                                       different currency from the rest of its column
    2. Column score  = 100 x (credits / cells in the column)
    3. Cell score    = weighted mean of the column scores. A column's weight is 0.5 + its importance
                       (0.5 .. 1.5): a bit more for identifier / email / phone / amount, less for free
                       text. Damage is NOT divided by the number of columns.
    4. Key-column rule: one unusable key column makes records unusable however tidy the rest is, so
                       score = cell score x (1 - 1.25 x share of the weakest key column that is
                       missing, invalid or mixed-currency)   (that term can remove at most 70% of the score)
    5. Duplicates:     score = score x (1 - share of rows that are exact repeats)

There is no ceiling on the number. An unresolved CRITICAL finding limits the grade LABEL only (it
cannot read better than "C") and says why; the number itself always moves with the data.

The four dimensions (completeness, validity, uniqueness, consistency; plus accuracy and timeliness
where they apply) are a BREAKDOWN of the same points: every point lost is assigned to exactly one
dimension, so 100 - sum(points lost per dimension) is the headline, to within rounding.

Severity of an individual finding is separate and unchanged:

    share        = affected / population                (how much of the column/table)
    share_adj    = share x population / (population+1) (small samples carry less evidence)
    impact       = share_adj x rule_criticality x field_factor
    field_factor = 0.5 + 0.5 x field_importance         (0.5 .. 1.0)

Performance: classification runs once per DISTINCT value (validation.classify_column) and masks are
vectorised numpy; there is no per-cell Python loop over the table.
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from cleaning import currencies
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


# --------------------------------------------------------------------------- cell ledger
# cause -> (credit lost per cell, dimension it is charged to, short human label)
CAUSES = {
    "missing":        (1.0, M.COMPLETENESS, "empty cells"),
    "placeholder":    (1.0, M.COMPLETENESS, "placeholder values (N/A, -, unknown)"),
    "invalid":        (1.0, M.VALIDITY, "invalid values"),
    "unresolved":     (1.0, M.VALIDITY, "values that cannot be interpreted"),
    "needs_decision": (M.HALF_CREDIT, M.VALIDITY, "values that need your decision (day/month order, phone country)"),
    "odd_label":      (M.HALF_CREDIT, M.CONSISTENCY, "labels written inconsistently"),
    "untidy":         (M.HALF_CREDIT, M.CONSISTENCY, "cells with untidy formatting"),
    "mixed_currency": (1.0, M.CONSISTENCY, "amounts in a different currency from the rest"),
    "odd_value":      (M.HALF_CREDIT, M.ACCURACY, "unusual values to check"),
    "estimated":      (M.HALF_CREDIT, M.ACCURACY, "values filled in by estimate"),
    "stale":          (M.HALF_CREDIT, M.TIMELINESS, "out-of-date records"),
    "duplicate_id":   (M.HALF_CREDIT, M.UNIQUENESS, "identifiers reused by a different record"),
}
# credit 0: what "missing or invalid" means for the key-column rule. Amounts in mixed currencies cannot be totalled or
# compared with each other, so they count too.
HARD_CAUSES = ("missing", "placeholder", "invalid", "unresolved", "mixed_currency")

# validation codes (see cleaning/validation.py) -> which cause a non-valid cell is
_PLACEHOLDER_UNRESOLVED = {"ambiguous_placeholder"}
_SOFT_UNRESOLVED = {"ambiguous_day_month", "needs_country", "ambiguous_number_format"}
_LABEL_CODES = {"inconsistent_label", "label_variant", "possible_misspelling", "padded_identifier",
                "inconsistent_identifier_format"}

# a type the validation report is confident about can promote a column to "key" even where the profiler's
# name-based guess disagrees (a currency column holding "abc" is still the amount column)
_TYPE_IMPORTANCE = {"identifier": 1.0, "currency": 0.9, "email": 0.9, "phone": 0.85}


def _cause_of(cat: str, code: str) -> Optional[str]:
    if cat == "valid":
        return None
    if cat == "missing":
        return "placeholder" if code == "missing_marker" else "missing"
    if cat == "invalid":
        return "invalid"
    if cat == "unresolved":
        if code in _PLACEHOLDER_UNRESOLVED:
            return "placeholder"
        return "needs_decision" if code in _SOFT_UNRESOLVED else "unresolved"
    if cat == "suspicious":
        return "odd_label" if code in _LABEL_CODES else "odd_value"
    return "invalid"


def _key_type_by_name(name: str) -> Optional[str]:
    """For a column with NO values at all there is nothing to detect a type from, so fall back to what the
    header says. A column named Email that is entirely empty is still an empty key column."""
    from cleaning import detectors, profiling
    tokens = set(profiling.name_tokens(name))
    if tokens & {"email", "mail"}:
        return "email"
    if detectors.is_phone_column(name):
        return "phone"
    if tokens & profiling._CURRENCY_TOKENS:
        return "currency"
    if detectors.is_identifier_name(name):
        return "identifier"
    return None


def _column_weight_and_key(col: str, eff_type: str, prof, caller_set: bool, empty: bool = False) -> tuple[float, bool, float]:
    importance = prof.importance if prof is not None else 0.5
    sem = prof.semantic_type if prof is not None else None
    if empty and eff_type not in M.KEY_TYPES and sem not in M.KEY_TYPES:
        eff_type = _key_type_by_name(col) or eff_type
    key = eff_type in M.KEY_TYPES or sem in M.KEY_TYPES
    if not caller_set:
        if eff_type in _TYPE_IMPORTANCE:
            importance = max(importance, _TYPE_IMPORTANCE[eff_type])
        if eff_type == "text" and sem in ("unknown", "free_text", None):
            importance = min(importance, 0.4)          # notes / comments: they matter least
    return M.WEIGHT_BASE + max(0.0, min(1.0, importance)), key, importance


def _valid_mask(series, valid_values: set, n: int) -> np.ndarray:
    if not valid_values:
        return np.zeros(n, dtype=bool)
    try:
        return series.isin(valid_values).to_numpy()
    except TypeError:                         # unhashable cells in an odd file: no half-credit probing
        return np.zeros(n, dtype=bool)


def build_ledger(ctx, findings: list[dict]) -> list[dict]:
    """One entry per column: how many of its cells fall in each cause, and its column score."""
    df, n = ctx.df, len(ctx.df)
    stale_by_col = {f["column"]: int(f["affected_count"]) for f in findings
                    if f["issue"] == "stale_data" and f.get("column") is not None}
    dup_id_by_col = {f["column"]: int(f["affected_count"]) for f in findings
                     if f["issue"] == "duplicate_identifier_values" and f.get("column") is not None}
    imputed = (ctx.provenance or {}).get("imputed") or {}
    out = []
    for col in df.columns:
        name = str(col)
        res = ctx.classified(col)
        prof = ctx.profile.get(name)
        eff = res["type_info"]["type"]
        counts = {c: 0 for c in CAUSES}
        valid_values = set()
        n_valid = 0
        for v, k in res["counts"].items():
            cat, code, _ = res["by_value"][v]
            cause = _cause_of(cat, code)
            if cause is None:
                n_valid += k
                if v is not None:
                    valid_values.add(v)
            else:
                counts[cause] += k

        # cells that ARE valid but not clean: untidy formatting, a minority currency (still one cell each)
        if n_valid:
            series = df[col]
            valid_cell = _valid_mask(series, valid_values, n)
            minority = np.zeros(n, dtype=bool)
            if eff in ("currency", "numeric", "integer"):
                bad_cur = currencies.minority_currency_values({v: res["counts"][v] for v in valid_values})
                if bad_cur:
                    minority = _valid_mask(series, bad_cur, n) & valid_cell
            untidy = ctx.untidy.get(name)
            untidy_only = (untidy & valid_cell & ~minority) if untidy is not None and len(untidy) == n else None
            counts["mixed_currency"] = int(minority.sum())
            counts["untidy"] = int(untidy_only.sum()) if untidy_only is not None else 0

        remaining = n_valid - counts["mixed_currency"] - counts["untidy"]
        for cause, cnt in (("estimated", int(imputed.get(name, 0) or 0)), ("stale", stale_by_col.get(name, 0)),
                           ("duplicate_id", dup_id_by_col.get(name, 0))):
            take = max(0, min(cnt, remaining))
            counts[cause] = take
            remaining -= take

        loss_cells = sum(counts[c] * CAUSES[c][0] for c in CAUSES)
        caller_set = bool(prof and "importance set by caller" in prof.evidence)
        empty = n_valid == 0 and counts["invalid"] == 0 and counts["unresolved"] == 0 and counts["needs_decision"] == 0 \
            and counts["odd_label"] == 0 and counts["odd_value"] == 0   # nothing but blanks / placeholders
        weight, key, importance = _column_weight_and_key(name, eff, prof, caller_set, empty)
        hard = sum(counts[c] for c in HARD_CAUSES)
        out.append({
            "column": name, "type": eff, "type_status": res["type_info"]["status"],
            "rows": n, "counts": {c: v for c, v in counts.items() if v},
            "valid_cells": n_valid, "clean_cells": max(0, remaining),
            "valid_pct": round(100.0 * n_valid / n, 1) if n else 100.0,
            "score": round(100.0 * (1.0 - loss_cells / n), 1) if n else 100.0,
            "bad_pct": round(100.0 * hard / n, 1) if n else 0.0,
            "weight": round(weight, 2), "importance": round(importance, 2), "key": key,
            "_loss_cells": loss_cells, "_hard_share": (hard / n) if n else 0.0,
        })
    return out


def _main_problem(entry: dict) -> str:
    worst, worst_loss = None, 0.0
    for cause, cnt in entry["counts"].items():
        loss = cnt * CAUSES[cause][0]
        if loss > worst_loss:
            worst, worst_loss = cause, loss
    if worst is None:
        return "None"
    return f"{entry['counts'][worst]} {CAUSES[worst][2]}"


def _dimension_applicable(dim: str, findings: list[dict], evaluated: set) -> bool:
    return dim in evaluated or any(f["dimension"] == dim for f in findings)


_ROW_CAUSE_LABELS = {"duplicate_rows": "duplicate rows (exact repeats of another row)"}


def _label(cause: str, col: Optional[str], count: int) -> str:
    what = CAUSES[cause][2] if cause in CAUSES else _ROW_CAUSE_LABELS[cause]
    return f"{col}: {count} {what}" if col else f"{count} {what}"


# --------------------------------------------------------------------------- readiness for analysis
READY, CAUTION, NOT_READY = "ready", "caution", "not_ready"
READINESS_TITLES = {READY: "Ready for analysis", CAUTION: "Usable with caution", NOT_READY: "Not ready for analysis"}
READY_MIN_SCORE, NOT_READY_BELOW = 85, 60
BLOCKER_COLUMN_BAD, CAUTION_COLUMN_BAD = 0.30, 0.10      # share of a column that is missing / invalid / mixed-currency


def readiness_of(overall: int, ledger: list, findings: list, dup_rows: int, n: int) -> dict:
    """Is this data fit to analyse? Deterministic rules over the same cells as the score. A verdict is
    "not_ready" if ANY blocker exists, "caution" if any caution exists or the score is under 85, else "ready"."""
    blockers, cautions = [], []

    def add(bucket, label, column=None, count=None):
        bucket.append({"label": label, "column": column, "count": count})

    if n == 0:
        return {"verdict": NOT_READY, "title": READINESS_TITLES[NOT_READY], "blockers": [{"label": "The file has no rows.", "column": None, "count": 0}],
                "cautions": [], "summary": "There is no data to analyse."}
    if overall < NOT_READY_BELOW:
        add(blockers, f"Only {overall}/100 of the data can be trusted (needs {NOT_READY_BELOW}+).")
    for e in ledger:
        share = e["_hard_share"]
        hard = sum(e["counts"].get(c, 0) for c in HARD_CAUSES)
        if share >= BLOCKER_COLUMN_BAD and (e["key"] or share >= 0.5):
            add(blockers, f"{e['column']} is {round(100 * share)}% missing or invalid, so results using it would be unreliable.", e["column"], hard)
        elif share >= CAUTION_COLUMN_BAD:
            add(cautions, f"{e['column']} is {round(100 * share)}% missing or invalid; analysis using it will be biased.", e["column"], hard)
        if e["counts"].get("mixed_currency") and share < BLOCKER_COLUMN_BAD:
            add(cautions, f"{e['column']} mixes currencies, so totals and averages will be wrong until it is standardised.", e["column"], e["counts"]["mixed_currency"])
        if e["counts"].get("needs_decision"):
            add(cautions, f"{e['column']} has {e['counts']['needs_decision']} value(s) that need your decision (for example day/month order or phone country).", e["column"], e["counts"]["needs_decision"])
        if e["counts"].get("estimated"):
            add(cautions, f"{e['column']} has {e['counts']['estimated']} estimated value(s) that are not real observations.", e["column"], e["counts"]["estimated"])
    crit = [f for f in findings if f["level"] == M.CRITICAL]
    for f in crit:
        add(blockers, f"Unresolved critical issue: {f['title']}" + (f" in '{f['column']}'" if f.get("column") else "") + ".", f.get("column"), f["affected_count"])
    if dup_rows:
        add(cautions, f"{dup_rows} duplicate row(s) would be counted twice.", None, dup_rows)
    if not blockers and overall < READY_MIN_SCORE and not any("score is" in c["label"] for c in cautions):
        add(cautions, f"The quality score is {overall}/100 (ready needs {READY_MIN_SCORE}+).")
    verdict = NOT_READY if blockers else CAUTION if cautions else READY
    summary = {READY: "No blocking problems found. The data can be analysed as it is.",
               CAUTION: "The data can be analysed, but the points below will affect the results.",
               NOT_READY: "Fix the blockers below before analysing, or the results will not be reliable."}[verdict]
    return {"verdict": verdict, "title": READINESS_TITLES[verdict], "blockers": blockers[:6], "cautions": cautions[:6], "summary": summary}


def score_dataset(findings: list[dict], profile: dict, *, total_rows: int, evaluated: set, ctx=None) -> dict:
    """
    findings: enriched findings (dimension, column, affected_count, level, issue, advisory ...).
    ctx:      the CheckContext of this report (dataframe, cached validation, untidy-cell masks, provenance).
    Returns {"overall": int, "grade": str, "dimensions": {...}, "model": {...}, "grade_limited_by": level|None, ...}.
    `uncapped` equals `overall` and `capped_by` is always None; both are kept so older clients keep working.
    """
    findings = [f for f in findings if not f.get("advisory")]   # offers are not defects
    n = int(total_rows)
    ledger = build_ledger(ctx, findings) if (ctx is not None and n and len(ctx.df.columns)) else []

    total_w = sum(e["weight"] for e in ledger) or 1.0
    points = {d: 0.0 for d in M.DIMENSIONS}                 # points lost per dimension (of 100)
    items = []                                              # (points, cause, column, count) for "what holds the score down"
    for e in ledger:
        for cause, cnt in e["counts"].items():
            loss, dim, _ = CAUSES[cause]
            pts = 100.0 * (e["weight"] / total_w) * (cnt * loss / n)
            points[dim] += pts
            items.append({"cause": cause, "column": e["column"], "count": cnt, "points": pts, "dimension": dim})
    cell_score = 100.0 - sum(points.values())

    # --- key-column rule: the weakest key column (missing or invalid share)
    keys = [e for e in ledger if e["key"] and e["_hard_share"] > 0]
    key_info = None
    key_pts = 0.0
    if keys:
        worst = max(keys, key=lambda e: (e["_hard_share"], e["column"]))
        penalty = min(M.KEY_COLUMN_MAX_PENALTY, M.KEY_COLUMN_FACTOR * worst["_hard_share"])
        key_pts = cell_score * penalty
        key_info = {"column": worst["column"], "bad_pct": round(100.0 * worst["_hard_share"], 1),
                    "factor": M.KEY_COLUMN_FACTOR, "penalty_pct": round(100.0 * penalty, 1),
                    "points": round(key_pts, 1)}
        # charge those points to the dimensions (and reasons) of the cells that caused them
        hard_items = [it for it in items if it["column"] == worst["column"] and it["cause"] in HARD_CAUSES]
        hard_total = sum(it["count"] for it in hard_items) or 1
        for it in hard_items:
            share = key_pts * it["count"] / hard_total
            points[it["dimension"]] += share
            it["points"] += share
    after_key = cell_score - key_pts

    # --- exact duplicate rows
    dup_rows = sum(int(f["affected_count"]) for f in findings if f["issue"] == "duplicate_rows")
    dup_share = min(1.0, dup_rows / n) if n else 0.0
    dup_pts = after_key * dup_share
    if dup_pts:
        points[M.UNIQUENESS] += dup_pts
        items.append({"cause": "duplicate_rows", "column": None, "count": dup_rows, "points": dup_pts,
                      "dimension": M.UNIQUENESS})
    final = max(0.0, after_key - dup_pts)

    overall = int(round(final))
    if final < 100.0 and overall >= 100:
        overall = 99            # 100 is reserved for "nothing flagged anywhere"
    overall = max(0, min(100, overall))

    dimensions: dict[str, dict] = {}
    for dim in M.DIMENSIONS:
        in_dim = [f for f in findings if f["dimension"] == dim]
        applicable = _dimension_applicable(dim, findings, evaluated) or points[dim] > 0
        if not applicable:
            dimensions[dim] = {"score": None, "applicable": False, "issue_count": 0, "loss": 0.0, "points_lost": 0.0}
            continue
        lost = min(100.0, points[dim])
        dimensions[dim] = {"score": int(round(100.0 - lost)), "applicable": True, "issue_count": len(in_dim),
                           "loss": round(lost / 100.0, 4), "points_lost": round(lost, 2)}

    # --- grade label: an unresolved critical finding limits the LABEL, never the number
    worst_level = None
    for f in findings:
        if f["level"] in M.GRADE_LIMITS and (worst_level is None or M.severity_rank(f["level"]) > M.severity_rank(worst_level)):
            worst_level = f["level"]
    grade_by_score = M.grade_for(overall)
    grade, limited = M.limit_grade(grade_by_score, worst_level)
    limit_note = None
    if limited:
        crit = [f for f in findings if f["level"] == worst_level]
        limit_note = (f"The number is {overall}, but the grade is held at {grade} while {len(crit)} critical "
                      f"issue(s) are unresolved (e.g. {crit[0]['title']}"
                      + (f" in '{crit[0]['column']}'" if crit[0].get("column") else "") + ").")

    # --- what is holding the score down: top reasons with counts, biggest first
    reasons = []
    for it in sorted(items, key=lambda i: (-i["points"], i["cause"], str(i["column"]))):
        if it["points"] < 0.05:
            continue
        reasons.append({"label": _label(it["cause"], it["column"], it["count"]), "column": it["column"],
                        "cause": it["cause"], "count": it["count"], "dimension": it["dimension"],
                        "pct_of_rows": round(100.0 * it["count"] / n, 1) if n else 0.0,
                        "key_column": bool(key_info and it["column"] == key_info["column"] and it["cause"] in HARD_CAUSES),
                        "points": round(it["points"], 1)})

    columns_out = []
    for e in ledger:
        d = {k: v for k, v in e.items() if not k.startswith("_")}
        d["main_problem"] = _main_problem(e)
        columns_out.append(d)

    model = {
        "version": 3,
        "formula": ("Score = the share of cells that are filled in and valid (cells that are untidy or need a check "
                    "count half), reduced further when a key column (ID, email, phone, amount) has many missing or "
                    "invalid cells, and for duplicate rows."),
        "cell_score": round(cell_score, 1),
        "key_column": key_info,
        "duplicates": {"rows": dup_rows, "share_pct": round(100.0 * dup_share, 1), "points": round(dup_pts, 1)},
        "points_lost": round(100.0 - final, 1),
        "columns": columns_out,
        "reasons": reasons[:8],
        "top_reasons": reasons[:3],
    }
    model["readiness"] = readiness_of(overall, ledger, findings, dup_rows, n)
    return {"overall": overall, "uncapped": overall, "grade": grade, "grade_by_score": grade_by_score,
            "grade_limited_by": worst_level if limited else None, "grade_note": limit_note,
            "dimensions": dimensions, "capped_by": None, "model": model, "unrounded": round(final, 3)}
