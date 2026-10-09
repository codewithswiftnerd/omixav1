"""
Data Quality Report

Read-only diagnostics run on the uploaded file, before any cleaning rule touches it.
Nothing here mutates the dataframe.

Pipeline:  profile columns  ->  run every registered check  ->  enrich each raw
           finding from the rule registry (dimension, severity, confidence,
           remediation class, reversibility, explanation, ...)  ->  score.

The report answers: what is wrong, how serious, why, what can safely be fixed, what
needs a human, and (via `expected_after_safe_fixes`) roughly where the score lands if
only the safe fixes are applied.

Backwards compatibility: `score`, `grade`, `counts` (critical/warning/info),
`findings[*].severity` (critical/warning/info), `detail`, `suggestion`, `column_types`
keep their old shape. The richer fields are additive.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from cleaning import builtin_rules  # noqa: F401  (registers the built-in rules)
from cleaning import detectors, profiling, scoring
from cleaning import model as M
from cleaning.audit import changed_cells_mask
from cleaning.checks import CheckContext, HIGH_MISSING_THRESHOLD  # noqa: F401  (re-exported)
from cleaning.rule_registry import detector_specs, get_spec

QUALITY_MODEL_VERSION = 2
DRY_RUN_MAX_CELLS = 3_000_000  # skip the "would a fixer change this?" probes on very large frames

# Issues whose detection depends on a semantic guess about the column, so the finding's
# confidence is scaled by how sure the profiler is about that guess.
_SEMANTIC_ISSUES = {
    "invalid_email_format", "suspicious_phone_format", "ambiguous_date_format", "impossible_date",
    "impossible_age", "unrecognized_gender_value", "unrecognized_country_value", "mixed_data_types",
    "unrecoverable_scientific_notation", "stale_data",
}

# cleaning rule -> the (registry) issue it resolves when it would change something
_FORMAT_PROBES = [
    ("date_standardization", "inconsistent_date_format", "date value(s) in mixed formats"),
    ("numeric_text_cleaning", "numeric_formatting_noise", "number(s) stored with currency/percent/thousands formatting"),
    ("phone_cleaning", "phone_formatting", "phone number(s) with spaces/brackets/dashes"),
    ("email_cleaning", "email_formatting", "email(s) with mixed case or stray spaces"),
    ("gender_standardization", "gender_variants", "gender value(s) in non-canonical spelling"),
    ("country_standardization", "country_variants", "country value(s) in non-canonical spelling"),
    ("boolean_standardization", "boolean_variants", "yes/no value(s) in mixed form"),
    ("case_standardization", "inconsistent_casing", "value(s) with inconsistent capitalisation (names, IDs or labels)"),
]


def check_fixable_formats(ctx: CheckContext) -> list[dict]:
    """Finds formatting inconsistencies by *dry-running* the corresponding cleaning rule
    on a copy and counting what it would change. The finding and the fix therefore can
    never disagree about what is affected."""
    from cleaning import rules  # local import: rules imports detectors/audit, avoid import cycles

    df = ctx.df
    out: list[dict] = []
    if df.empty and not len(df.columns):
        return out
    if df.size > DRY_RUN_MAX_CELLS:
        return out

    base = df.copy()
    base, _ = rules.RULE_DISPATCH["missing_token_normalization"](base, {})

    for rule_name, issue, noun in _FORMAT_PROBES:
        work = base.copy()
        try:
            after, _ = rules.RULE_DISPATCH[rule_name](work, {})
        except Exception:  # a probe must never break reporting
            continue
        for col in base.columns:
            if col not in after.columns:
                continue
            n = int(changed_cells_mask(base[col], after[col]).sum())
            if not n:
                continue
            population = int(base[col].notna().sum()) or len(base)
            out.append({
                "column": col, "issue": issue, "affected": n, "population": population,
                "detail": f"{n} {noun}.",
                "suggestion": f"The '{rule_name}' rule fixes this automatically.",
            })

    # header tidiness (column-level)
    after, _ = rules.RULE_DISPATCH["column_names"](base.copy(), {})
    renamed = [(b, a) for b, a in zip(base.columns, after.columns) if b != a]
    if renamed:
        out.append({
            "column": None, "issue": "column_name_formatting", "affected": len(renamed),
            "population": len(base.columns), "unit": "column",
            "detail": f"{len(renamed)} column name(s) are not in tidy snake_case, e.g. "
                      f"'{renamed[0][0]}' → '{renamed[0][1]}'.",
            "suggestion": "Optional: select 'Standardize column names' to rename headers (off by default); data values are never touched.",
        })
    return out


_CHECKS = None


def _all_checks():
    return [s.detector for s in detector_specs()] + [check_fixable_formats]


# --------------------------------------------------------------------------- enrichment
def _confidence(spec, raw: dict, ctx: CheckContext) -> float:
    conf = spec.confidence
    col = raw.get("column")
    if spec.issue in _SEMANTIC_ISSUES and col in ctx.profile:
        conf *= 0.6 + 0.4 * ctx.profile[col].confidence
    population = raw.get("population", 0)
    if 0 < population < 10:
        conf *= 0.8 + 0.02 * population  # too few rows to be sure a pattern is a defect
    return round(max(0.05, min(1.0, conf)), 2)


def enrich_finding(raw: dict, ctx: CheckContext) -> dict:
    spec = get_spec(raw["issue"])
    if spec is None:  # a check emitted an issue nobody registered: keep it, but visibly unclassified
        from cleaning.rule_registry import RuleSpec
        spec = RuleSpec(id="OMX-UNREGISTERED", issue=raw["issue"], title=raw["issue"], dimension=M.VALIDITY,
                        explanation="", recommendation=raw.get("suggestion", ""), max_level=M.MEDIUM)

    col = raw.get("column")
    affected = int(raw.get("affected", 0))
    population = int(raw.get("population", 0)) or ctx.total_rows
    unit = raw.get("unit", spec.unit)
    # table-level findings (duplicate rows, header names) are not about one field, so they are
    # not discounted by a field's importance
    importance = ctx.profile[col].importance if col in ctx.profile else 1.0
    impact = scoring.impact_of(affected, population, spec.criticality, importance)
    level = scoring.level_for(
        impact, population=ctx.total_rows if unit != "column" else 99, min_level=spec.min_level,
        max_level=spec.max_level, floor=raw.get("severity_floor"),
    )
    confidence = _confidence(spec, raw, ctx)

    remediation = raw.get("remediation_override") or spec.remediation
    fix_conf = raw.get("fix_confidence", spec.fix_confidence)
    if remediation == M.SAFE_AUTO_FIX and (confidence < 0.6 or (fix_conf is not None and fix_conf < 0.8)):
        remediation = M.REQUIRES_REVIEW  # "safe" requires both a sure finding and a sure fix

    finding = {
        "rule_id": spec.id,
        "issue": spec.issue,
        "title": spec.title,
        "column": col,
        "columns": [col] if col else [],
        "dimension": spec.dimension,
        "level": level,
        "severity": M.LEGACY_SEVERITY[level],
        "affected_count": affected,
        "affected_unit": unit,
        "affected_pct": round(100.0 * affected / population, 2) if population else 0.0,
        "population": population,
        "impact": impact,
        "confidence": confidence,
        "explanation": spec.explanation,
        "detail": raw.get("detail", ""),
        "suggestion": raw.get("suggestion", ""),
        "recommended_action": spec.recommendation,
        "remediation": remediation,
        "remediation_label": M.REMEDIATION_LABELS[remediation],
        "operation": spec.operation,
        "reversibility": spec.reversibility if remediation != M.DO_NOT_MODIFY else M.NOT_APPLICABLE,
        "fix_confidence": fix_conf,
        "resolver": spec.resolver,
        "field_importance": round(importance, 2),
        "criticality": spec.criticality,
    }
    if col in ctx.profile:
        finding["semantic_type"] = ctx.profile[col].semantic_type
    if "sample_values" in raw:
        finding["sample_values"] = raw["sample_values"]
    return finding


def _legacy_inferred_type(col: str, series: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(series):
        return "boolean"
    if detectors.is_email_column(col, series):
        return "email"
    if detectors.is_phone_column(col):
        return "phone"
    if detectors.is_gender_column(col):
        return "gender"
    if detectors.is_country_column(col):
        return "country"
    if detectors.is_age_column(col) and pd.api.types.is_numeric_dtype(series):
        return "age"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "date"
    if detectors.is_probable_date_column(col, series):
        return "date"
    if pd.api.types.is_numeric_dtype(series):
        return "numeric"
    return "string"


def _column_types(df: pd.DataFrame, profile: dict) -> dict:
    out = {}
    for col in df.columns:
        p = profile[str(col)]
        out[str(col)] = {
            "pandas_dtype": str(df[col].dtype),
            "inferred_type": _legacy_inferred_type(str(col), df[col]),   # unchanged, for existing clients
            "semantic_type": p.semantic_type,
            "semantic_subtype": p.subtype,
            "semantic_confidence": round(p.confidence, 2),
            "evidence": p.evidence,
            "field_importance": round(p.importance, 2),
            "is_identifier": p.is_identifier,
        }
    return out


# --------------------------------------------------------------------------- scorecard
def _build_scorecard(findings: list[dict], scored: dict, total_rows: int, n_cols: int) -> dict:
    sev = {lv: 0 for lv in M.SEVERITY_LEVELS}
    for f in findings:
        sev[f["level"]] += 1
    by_class = {M.SAFE_AUTO_FIX: [], M.REQUIRES_REVIEW: [], M.DO_NOT_MODIFY: []}
    for f in findings:
        by_class[f["remediation"]].append(f)

    def brief(f):
        return {"rule_id": f["rule_id"], "issue": f["issue"], "column": f["column"], "level": f["level"],
                "affected_count": f["affected_count"], "affected_pct": f["affected_pct"],
                "title": f["title"]}

    cell_total = total_rows * n_cols
    cell_hits = sum(f["affected_count"] for f in findings if f["affected_unit"] == "cell")
    row_hits = sum(f["affected_count"] for f in findings if f["affected_unit"] == "row")
    return {
        "overall_score": scored["overall"],
        "score_before_cap": scored["uncapped"],
        "grade": scored["grade"],
        "score_capped_by": scored["capped_by"],
        "dimension_scores": {d: v["score"] for d, v in scored["dimensions"].items() if v["applicable"]},
        "dimensions_not_applicable": [d for d, v in scored["dimensions"].items() if not v["applicable"]],
        "issues_by_severity": sev,
        "affected": {
            "cells_flagged": cell_hits,
            "cells_flagged_pct": round(100.0 * min(cell_hits, cell_total) / cell_total, 2) if cell_total else 0.0,
            "rows_flagged_by_row_level_rules": row_hits,
            "rows_flagged_pct": round(100.0 * min(row_hits, total_rows) / total_rows, 2) if total_rows else 0.0,
            "note": "Upper bounds: one cell can trigger more than one finding.",
        },
        "safe_fixes_available": len(by_class[M.SAFE_AUTO_FIX]),
        "decisions_required": len(by_class[M.REQUIRES_REVIEW]),
        "do_not_modify": len(by_class[M.DO_NOT_MODIFY]),
        "safe_fixes": [brief(f) for f in by_class[M.SAFE_AUTO_FIX]],
        "decisions": [brief(f) for f in by_class[M.REQUIRES_REVIEW]],
        "protected": [brief(f) for f in by_class[M.DO_NOT_MODIFY]],
    }


def safe_rules_for(findings: list[dict]) -> list[str]:
    """Cleaning rules that would resolve the SAFE findings, in pipeline order, with the
    prerequisite placeholder-recognition step when numbers need cleaning."""
    from cleaning.rules import DEFAULT_RULES
    wanted = {f["resolver"] for f in findings if f["remediation"] == M.SAFE_AUTO_FIX and f["resolver"]}
    if "numeric_text_cleaning" in wanted:
        wanted.add("missing_token_normalization")
    return [r for r in DEFAULT_RULES if r in wanted]


def _project_after_safe_fixes(df: pd.DataFrame, findings: list[dict], field_importance) -> Optional[dict]:
    """Actually applies ONLY the safe rules to a copy and re-scores. A simulation, not a
    guess; and because imputation is never in the safe set, completeness cannot be
    'improved' by inventing values."""
    from cleaning.rules import apply_rules
    safe = safe_rules_for(findings)
    if not safe:
        return None
    try:
        fixed, _ = apply_rules(df.copy(), rules=safe)
        rep = generate_report(fixed, field_importance=field_importance, project=False)
    except Exception:
        return None
    return {
        "score": rep["score"], "grade": rep["grade"],
        "dimension_scores": rep["scorecard"]["dimension_scores"],
        "rules_assumed": safe,
        "note": "Assumes only the safe fixes are applied. Values are not invented, so completeness and "
                "accuracy are not credited for anything that needs a human decision.",
    }


# --------------------------------------------------------------------------- entry point
def generate_report(
    df: pd.DataFrame,
    *,
    provenance: Optional[dict] = None,
    field_importance: Optional[dict] = None,
    now: Optional[pd.Timestamp] = None,
    project: bool = False,
) -> dict:
    total_rows = len(df)
    prof = profiling.profile_dataset(df, field_importance)
    ctx = CheckContext(df=df, profile=prof["columns"], now=now or pd.Timestamp.now(),
                       provenance=provenance or {})
    if len(df.columns):
        ctx.evaluated.add(M.VALIDITY)

    raw: list[dict] = []
    for check in _all_checks():
        raw.extend(check(ctx))

    findings = [enrich_finding(r, ctx) for r in raw]
    findings.sort(key=lambda f: (-M.severity_rank(f["level"]), -f["impact"], f["issue"], str(f["column"])))

    scored = scoring.score_dataset(findings, ctx.profile, total_rows=total_rows, evaluated=ctx.evaluated)

    counts = {"critical": 0, "warning": 0, "info": 0}
    for f in findings:
        counts[f["severity"]] += 1
    severity_counts = {lv: 0 for lv in M.SEVERITY_LEVELS}
    for f in findings:
        severity_counts[f["level"]] += 1

    scorecard = _build_scorecard(findings, scored, total_rows, len(df.columns))
    report = {
        "quality_model_version": QUALITY_MODEL_VERSION,
        "score": scored["overall"],
        "overall_score": scored["overall"],
        "score_before_cap": scored["uncapped"],
        "score_capped_by": scored["capped_by"],
        "grade": scored["grade"],
        "row_count": total_rows,
        "column_count": len(df.columns),
        "counts": counts,
        "severity_counts": severity_counts,
        "dimension_scores": {d: v["score"] for d, v in scored["dimensions"].items()},
        "dimensions": scored["dimensions"],
        "findings": findings,
        "column_types": _column_types(df, ctx.profile),
        "scorecard": scorecard,
    }
    if project:
        report["expected_after_safe_fixes"] = _project_after_safe_fixes(df, findings, field_importance)
        if report["expected_after_safe_fixes"]:
            scorecard["expected_score_after_safe_fixes"] = report["expected_after_safe_fixes"]["score"]
    return report
