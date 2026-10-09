"""
Quality checks (detectors).

Each check is a function `check(ctx) -> list[dict]` returning RAW findings:

    {"column": str | None, "issue": key, "affected": int, "population": int,
     "detail": str, "suggestion": str, ...optional extras}

They only *measure*. Severity, dimension, confidence, remediation class and the rest of
the finding model are attached afterwards from the rule registry (see
quality_report.enrich), so a check never decides how serious something is.

Nothing here mutates a dataframe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from cleaning import casing, detectors, profiling
from cleaning import model as M

HIGH_MISSING_THRESHOLD = 70
CONSTANT_COLUMN_MAX_UNIQUE = 1
STALE_AFTER_DAYS = 365
_STALE_NAME_TOKENS = {"updated", "modified", "refreshed", "synced", "checked", "seen", "login", "activity"}


@dataclass
class CheckContext:
    df: pd.DataFrame
    profile: dict                       # profiling.profile_dataset(df)["columns"]
    now: pd.Timestamp
    provenance: dict = field(default_factory=dict)  # e.g. {"imputed": {"age": 3}}
    evaluated: set = field(default_factory=set)     # dimensions that had something to check

    @property
    def total_rows(self) -> int:
        return len(self.df)

    def text_columns(self):
        return self.df.select_dtypes(include=["object", "string"]).columns

    def prof(self, col) -> profiling.ColumnProfile:
        return self.profile[str(col)]


def real_values(series: pd.Series) -> pd.Series:
    """Non-null values with placeholders ("N/A", "-", "Unknown") removed. A placeholder is a
    *missing* value (the completeness check reports it); counting it again as an invalid
    email / unrecognised gender / odd phone would double-penalise one defect."""
    values = series.dropna().astype(str)
    return values[~values.str.strip().str.lower().isin(detectors.MISSING_TOKENS)]


def _pct(n: int, d: int) -> float:
    return round((n / d) * 100, 2) if d else 0.0


# ------------------------------------------------------------------ completeness
def check_missing(ctx: CheckContext) -> list[dict]:
    out = []
    df, n = ctx.df, ctx.total_rows
    if len(df.columns):
        ctx.evaluated.add(M.COMPLETENESS)
    for col in df.columns:
        series = df[col]
        na = series.isna()
        placeholders = 0
        if not pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series):
            as_text = series.astype("string").str.strip().str.lower()
            placeholders = int((as_text.isin(detectors.MISSING_TOKENS) & ~na).sum())
        missing = int(na.sum()) + placeholders
        if not missing:
            continue
        pct = _pct(missing, n)
        extra = f" ({placeholders} are placeholders such as 'N/A' or '-')" if placeholders else ""
        prof = ctx.prof(col)
        if pct > HIGH_MISSING_THRESHOLD:
            out.append({
                "column": col, "issue": "high_missingness", "affected": missing, "population": n,
                "detail": f"{pct}% missing ({missing} of {n} rows){extra}.",
                "suggestion": "Too sparse to fill reliably, this column is left untouched and "
                              "flagged for manual review rather than auto-filled.",
                "severity_floor": M.CRITICAL,
            })
        else:
            fill = "the column median" if pd.api.types.is_numeric_dtype(series) else '"Unknown"'
            held_back = profiling.never_impute(prof)
            out.append({
                "column": col, "issue": "missing_values", "affected": missing, "population": n,
                "detail": f"{pct}% missing ({missing} rows){extra}.",
                "suggestion": (
                    f"Leave blank: {prof.semantic_type} values cannot be guessed; source the real ones."
                    if held_back else f"The 'Fill gaps' rule would fill these with {fill}; that is "
                                      f"imputation, not recovery of the real value."
                ),
                "remediation_override": M.DO_NOT_MODIFY if held_back else None,
            })
    return out


# ------------------------------------------------------------------ uniqueness
def check_duplicate_rows(ctx: CheckContext) -> list[dict]:
    df, n = ctx.df, ctx.total_rows
    if n >= 2 and len(df.columns):
        ctx.evaluated.add(M.UNIQUENESS)
    dup = int(df.duplicated(keep="first").sum()) if n and len(df.columns) else 0
    if not dup:
        return []
    pct = _pct(dup, n)
    has_identifier = any(p.is_identifier for p in ctx.profile.values())
    # Identical rows are only *probably* the same record. With very few columns, or when
    # most of the file repeats, identical rows are often legitimate (two identical
    # purchases) so the decision moves to the user.
    confident_duplicates = (len(df.columns) >= 3 or has_identifier) and pct <= 50
    return [{
        "column": None, "issue": "duplicate_rows", "affected": dup, "population": n, "unit": "row",
        "detail": f"{dup} exact duplicate rows ({pct}% of the file).",
        "suggestion": "The 'Remove duplicates' rule drops these, keeping the first occurrence."
                      if confident_duplicates else
                      "Identical rows may be genuine repeats (few columns or a mostly repeated file); "
                      "confirm before removing.",
        "remediation_override": None if confident_duplicates else M.REQUIRES_REVIEW,
        "fix_confidence": 0.95 if confident_duplicates else 0.6,
    }]


def check_possible_duplicate_records(ctx: CheckContext) -> list[dict]:
    df, n = ctx.df, ctx.total_rows
    if n < 2 or len(df.columns) < 2 or len(df.columns) > 40 or n > 50000:
        return []
    exact = df.duplicated(keep=False)
    best_col, best = None, 0
    for col in df.columns:
        others = [c for c in df.columns if c != col]
        near = df.duplicated(subset=others, keep=False)
        cand = int((near & ~exact).sum())
        if cand > best:
            best_col, best = col, cand
    if not best:
        return []
    return [{
        "column": best_col, "issue": "possible_duplicate_records", "affected": best, "population": n, "unit": "row",
        "detail": f"{best} row(s) match another row on every column except '{best_col}'.",
        "suggestion": "Not auto-fixed, these look like possible duplicate records; review "
                      "before deciding whether to merge or remove them.",
    }]


def check_duplicate_identifiers(ctx: CheckContext) -> list[dict]:
    """A column that is *supposed* to identify a record (id, uuid, ...) repeating while
    the rows differ means two different records are claiming the same identity."""
    out = []
    df, n = ctx.df, ctx.total_rows
    if n < 2:
        return out
    exact = df.duplicated(keep=False)
    for col in df.columns:
        tokens = set(profiling.name_tokens(col))
        if not (tokens & {"id", "uuid", "guid", "key"}) or tokens & {"phone", "zip", "postal"}:
            continue
        prof = ctx.prof(col)
        if not (prof.is_identifier and prof.confidence >= profiling.CONFIDENT):
            continue
        s = df[col]
        dup = s.notna() & s.duplicated(keep="first")
        # rows that merely repeat an identical row are the exact-duplicate rule's business
        conflicting = int((dup & ~exact).sum())
        if conflicting:
            out.append({
                "column": col, "issue": "duplicate_identifier_values", "affected": conflicting,
                "population": int(s.notna().sum()), "unit": "row",
                "detail": f"{conflicting} row(s) reuse an identifier already held by a different record.",
                "suggestion": "Not auto-fixed: decide which record is right, or whether the identifier "
                              "should be reissued.",
                "sample_values": [str(v) for v in s[dup & ~exact].astype(str).unique()[:5]],
            })
    return out


# ------------------------------------------------------------------ consistency
def check_whitespace(ctx: CheckContext) -> list[dict]:
    out = []
    n = ctx.total_rows
    cols = ctx.text_columns()
    if len(cols):
        ctx.evaluated.add(M.CONSISTENCY)
    for col in cols:
        original = ctx.df[col].astype("string")
        cleaned = original.str.strip().str.replace(r"\s+", " ", regex=True)
        diff = int(((original != cleaned) & original.notna()).sum())
        if diff:
            out.append({
                "column": col, "issue": "whitespace", "affected": diff, "population": n,
                "detail": f"{diff} values have extra or trailing whitespace.",
                "suggestion": "The 'Fix formatting' rule trims and collapses this automatically.",
            })
    return out


def check_inconsistent_categories(ctx: CheckContext) -> list[dict]:
    out = []
    n = ctx.total_rows
    for col in ctx.text_columns():
        prof = ctx.prof(col)
        if profiling.never_impute(prof) or prof.semantic_type in (profiling.FREE_TEXT, profiling.PERSON_NAME):
            continue
        values = ctx.df[col].dropna().astype(str)
        nunique = values.nunique()
        if nunique < 2 or nunique > 50 or nunique > n * 0.5:
            continue
        counts = values.value_counts()
        groups: dict[str, list[str]] = {}
        for v in counts.index:
            groups.setdefault(casing.variant_key(v), []).append(v)
        variants = [g for g in groups.values() if len(g) > 1]
        if not variants:
            continue
        affected = 0
        for g in variants:
            canonical = casing.canonical_variant(g, counts)
            affected += int(sum(counts[v] for v in g if v != canonical))
        examples = ", ".join(sorted(variants[0])[:3])
        out.append({
            "column": col, "issue": "inconsistent_categories", "affected": affected, "population": n,
            "detail": f"{len(variants)} value(s) appear under multiple spellings/casings, e.g. {examples}.",
            "suggestion": "The 'Standardize categories' rule merges case/spacing variants into the "
                          "spelling already used most often. Different labels for the same thing "
                          "(NY vs New York) are not merged.",
            "sample_values": sorted(variants[0])[:5],
        })
    return out


def check_mixed_types(ctx: CheckContext) -> list[dict]:
    out = []
    for col in ctx.text_columns():
        prof = ctx.prof(col)
        # codes like "123" next to "A45" are normal for identifiers/phones/emails/dates
        if prof.semantic_type in (profiling.IDENTIFIER, profiling.PHONE, profiling.EMAIL,
                                  profiling.DATE, profiling.DATETIME) and prof.confidence >= 0.6:
            continue
        values = real_values(ctx.df[col])
        if len(values) < 10:
            continue
        parses = values.map(detectors.strip_numeric_noise).map(detectors.try_parse_float).notna()
        ratio = parses.mean()
        if 0.05 < ratio < 0.95:
            numeric_n = int(parses.sum())
            minority = min(numeric_n, len(values) - numeric_n)
            out.append({
                "column": col, "issue": "mixed_data_types", "affected": minority, "population": len(values),
                "detail": f"{numeric_n} of {len(values)} non-empty values look numeric; the rest look like text.",
                "suggestion": "Not auto-fixed, confirm whether this column should be numeric before "
                              "relying on it for calculations.",
            })
    return out


# ------------------------------------------------------------------ validity
def check_outliers(ctx: CheckContext) -> list[dict]:
    out = []
    for col in ctx.df.select_dtypes(include="number").columns:
        prof = ctx.prof(col)
        if prof.semantic_type in (profiling.IDENTIFIER, profiling.PHONE) or prof.subtype == "age":
            continue  # identifiers have no 'typical range'; ages have their own hard-limit check
        s = ctx.df[col].dropna()
        if len(s) < 10 or s.nunique() <= 2:
            continue
        q1, q3 = s.quantile(0.25), s.quantile(0.75)
        iqr = q3 - q1
        if iqr == 0:
            continue
        lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        n_out = int(((s < lo) | (s > hi)).sum())
        if n_out:
            out.append({
                "column": col, "issue": "potential_outliers", "affected": n_out, "population": int(len(s)),
                "detail": f"{n_out} values fall outside the typical range ({round(lo, 2)} to {round(hi, 2)}).",
                "suggestion": "Not auto-fixed, worth a manual look before trusting averages or totals "
                              "on this column. Unusual is not the same as wrong.",
            })
    return out


def check_constant_columns(ctx: CheckContext) -> list[dict]:
    out = []
    for col in ctx.df.columns:
        s = ctx.df[col].dropna()
        if len(s) and s.nunique() <= CONSTANT_COLUMN_MAX_UNIQUE:
            out.append({
                "column": col, "issue": "constant_column", "affected": int(len(s)), "population": ctx.total_rows,
                "detail": f'Every non-empty value is "{s.iloc[0]}".',
                "suggestion": "Not auto-fixed, this column may not be worth keeping.",
            })
    return out


def check_invalid_email(ctx: CheckContext) -> list[dict]:
    out = []
    for col in ctx.text_columns():
        if not detectors.is_email_column(str(col), ctx.df[col]):
            continue
        values = real_values(ctx.df[col])
        if values.empty:
            continue
        bad = values[~values.str.match(detectors.EMAIL_RE)]
        if len(bad):
            out.append({
                "column": col, "issue": "invalid_email_format", "affected": int(len(bad)), "population": int(len(values)),
                "detail": f"{len(bad)} value(s) don't look like valid email addresses.",
                "suggestion": "Not auto-fixed, verify and correct these manually.",
                "sample_values": sorted(bad.unique().tolist())[:5],
            })
    return out


def check_suspicious_phone(ctx: CheckContext) -> list[dict]:
    out = []
    for col in ctx.text_columns():
        if not detectors.is_phone_column(str(col)):
            continue
        values = real_values(ctx.df[col])
        if values.empty:
            continue
        digits = values.str.replace(r"\D", "", regex=True).str.len()
        bad = int(((digits < 7) | (digits > 15)).sum())
        if bad:
            out.append({
                "column": col, "issue": "suspicious_phone_format", "affected": bad, "population": int(len(values)),
                "detail": f"{bad} value(s) have an unusual number of digits for a phone number.",
                "suggestion": "Not auto-fixed, worth a manual check. If you know the country, the "
                              "resolution below can normalize to international format.",
            })
    return out


def check_scientific_notation(ctx: CheckContext) -> list[dict]:
    out = []
    for col in ctx.text_columns():
        if not detectors.is_phone_column(str(col)):
            continue
        values = real_values(ctx.df[col])
        sci = values[values.map(detectors.is_scientific_notation)]
        lost = [v for v in sci if detectors.try_recover_scientific_phone(v) is None]
        if lost:
            out.append({
                "column": col, "issue": "unrecoverable_scientific_notation", "affected": len(lost),
                "population": int(len(values)),
                "detail": f"{len(lost)} value(s) were mangled into scientific notation (likely a "
                          f"spreadsheet auto-formatting a long number) and can't be safely reconstructed.",
                "suggestion": "Not auto-fixed, the original digits are lost; re-enter these from the source data.",
                "sample_values": sorted(set(lost))[:5],
                "severity_floor": M.CRITICAL,
            })
    return out


def check_unrecognized_gender(ctx: CheckContext) -> list[dict]:
    out = []
    for col in ctx.text_columns():
        if not detectors.is_gender_column(str(col)):
            continue
        values = real_values(ctx.df[col]).str.strip()
        bad = values[~values.str.lower().isin(detectors.GENDER_WORDS)]
        if len(bad):
            out.append({
                "column": col, "issue": "unrecognized_gender_value", "affected": int(len(bad)),
                "population": int(len(values)),
                "detail": f"{len(bad)} value(s) aren't recognized male/female variants.",
                "suggestion": "Not auto-fixed, Omixa never guesses gender; review these manually. "
                              "'Other' or 'non-binary' may be perfectly valid answers.",
                "sample_values": sorted(bad.unique().tolist())[:5],
            })
    return out


def check_unrecognized_country(ctx: CheckContext) -> list[dict]:
    out = []
    for col in ctx.text_columns():
        if not detectors.is_country_column(str(col)):
            continue
        values = real_values(ctx.df[col]).str.strip()
        bad = values[values.map(detectors.standardize_country_value).isna()]
        if len(bad):
            out.append({
                "column": col, "issue": "unrecognized_country_value", "affected": int(len(bad)),
                "population": int(len(values)),
                "detail": f"{len(bad)} value(s) don't match a known country code or name.",
                "suggestion": "Not auto-fixed, verify and correct these manually, or add the country to "
                              "cleaning/phone_formats.COUNTRIES if it's just missing from the reference list.",
                "sample_values": sorted(bad.unique().tolist())[:5],
            })
    return out


def check_ambiguous_dates(ctx: CheckContext) -> list[dict]:
    out = []
    for col in ctx.text_columns():
        s = ctx.df[col]
        if not detectors.is_probable_date_column(str(col), s):
            continue
        stats = detectors.date_parse_stats(s)
        ambiguous_col = detectors.unambiguous_date_parse(s) is None
        if not ambiguous_col and not stats["two_digit_year"]:
            continue
        affected = max(stats["ambiguous"], stats["two_digit_year"]) if not ambiguous_col else \
            max(stats["ambiguous"], 1) + (stats["two_digit_year"] if not stats["ambiguous"] else 0)
        reason = ("day-first and month-first readings disagree" if ambiguous_col
                  else f"{stats['two_digit_year']} value(s) use a 2-digit year, so the century is a guess")
        out.append({
            "column": col, "issue": "ambiguous_date_format", "affected": min(affected, stats["total"] or affected),
            "population": stats["total"],
            "detail": "Values look like dates, but it's not possible to tell day-first from "
                      "month-first formatting without guessing." if ambiguous_col else
                      f"Values look like dates, but {reason}.",
            "suggestion": "Not auto-fixed, confirm the intended format before standardizing.",
            "sample_values": s.dropna().astype(str).unique()[:3].tolist(),
        })
    return out


# ------------------------------------------------------------------ accuracy (objective only)
def check_impossible_age(ctx: CheckContext) -> list[dict]:
    out = []
    for col in ctx.df.columns:
        if not detectors.is_age_column(str(col)) or not pd.api.types.is_numeric_dtype(ctx.df[col]):
            continue
        ctx.evaluated.add(M.ACCURACY)
        v = ctx.df[col].dropna()
        bad = v[(v < 0) | (v > 120)]
        if len(bad):
            out.append({
                "column": col, "issue": "impossible_age", "affected": int(len(bad)), "population": int(len(v)),
                "detail": f"{len(bad)} value(s) fall outside a plausible human age range (0-120).",
                "suggestion": "Not auto-fixed, these look like data-entry errors; review manually.",
                "sample_values": sorted(set(bad.tolist()))[:5],
            })
    return out


def check_negative_amounts(ctx: CheckContext) -> list[dict]:
    """A money column (amount_paid, price, fee...) holding negative numbers. When ONE negative value
    repeats many times (-100 on 43 rows) it is almost certainly a placeholder for "unknown", not a
    real refund; either way the value cannot be fixed from the data alone, so it is reported."""
    out = []
    for col in ctx.df.columns:
        if not (set(profiling.name_tokens(str(col))) & detectors.MONEY_NAME_TOKENS):
            continue
        if not pd.api.types.is_numeric_dtype(ctx.df[col]):
            continue
        ctx.evaluated.add(M.ACCURACY)
        v = ctx.df[col].dropna()
        neg = v[v < 0]
        if not len(neg):
            continue
        top_value, top_count = neg.value_counts().index[0], int(neg.value_counts().iloc[0])
        placeholder = top_count >= 3 and top_count >= 0.6 * len(neg)
        detail = (f"{top_count} rows hold the same negative value ({top_value:g}); that is typically a placeholder "
                  f"for 'unknown', not a real amount." if placeholder
                  else f"{len(neg)} negative value(s) in a column that should not be negative.")
        out.append({
            "column": col, "issue": "negative_amount", "affected": int(len(neg)), "population": int(len(v)),
            "detail": detail,
            "suggestion": "Not auto-fixed: confirm whether these are refunds or placeholders, then blank or correct "
                          "them. Averages and totals of this column are wrong until you do.",
            "sample_values": sorted(set(neg.tolist()))[:5],
        })
    return out


def check_impossible_dates(ctx: CheckContext) -> list[dict]:
    out = []
    for col in ctx.text_columns():
        s = ctx.df[col]
        if not detectors.is_probable_date_column(str(col), s):
            continue
        parsed = detectors.unambiguous_date_parse(s)
        if parsed is None:
            continue
        ctx.evaluated.add(M.ACCURACY)
        p = parsed.dropna()
        too_old = p[p.dt.year < detectors.MIN_PLAUSIBLE_YEAR]
        future = p[p > ctx.now] if detectors.is_past_event_date_column(str(col)) else p.iloc[0:0]
        idx = too_old.index.union(future.index)
        if len(idx):
            reasons = []
            if len(too_old):
                reasons.append(f"{len(too_old)} before {detectors.MIN_PLAUSIBLE_YEAR}")
            if len(future):
                reasons.append(f"{len(future)} in the future")
            out.append({
                "column": col, "issue": "impossible_date", "affected": int(len(idx)), "population": int(len(p)),
                "detail": f"{len(idx)} value(s) look impossible ({', '.join(reasons)}).",
                "suggestion": "Not auto-fixed, review manually, or choose to blank or remove these rows.",
                "sample_values": sorted(s.loc[idx].astype(str).unique().tolist())[:5],
            })
    return out


# ------------------------------------------------------------------ timeliness
def check_stale_data(ctx: CheckContext) -> list[dict]:
    out = []
    for col in ctx.text_columns():
        tokens = set(profiling.name_tokens(col))
        if not (tokens & _STALE_NAME_TOKENS):
            continue
        s = ctx.df[col]
        if not detectors.is_probable_date_column(str(col), s):
            continue
        parsed = detectors.unambiguous_date_parse(s)
        if parsed is None:
            continue
        p = parsed.dropna()
        if p.empty:
            continue
        ctx.evaluated.add(M.TIMELINESS)
        cutoff = ctx.now - pd.Timedelta(days=STALE_AFTER_DAYS)
        stale = int((p < cutoff).sum())
        if stale:
            newest = p.max().date().isoformat()
            out.append({
                "column": col, "issue": "stale_data", "affected": stale, "population": int(len(p)),
                "detail": f"{stale} of {len(p)} records in '{col}' are more than a year old "
                          f"(most recent: {newest}).",
                "suggestion": "Not auto-fixed: only the data owner can say whether old records are still current.",
            })
    return out


# ------------------------------------------------------------------ provenance
def check_unverified_imputation(ctx: CheckContext) -> list[dict]:
    """Values this tool filled in are complete but not *verified*; the cleaned file's
    accuracy must not be reported as if they were real observations."""
    out = []
    for col, count in (ctx.provenance.get("imputed") or {}).items():
        if col in ctx.df.columns and count:
            ctx.evaluated.add(M.ACCURACY)
            out.append({
                "column": col, "issue": "unverified_imputed_values", "affected": int(count),
                "population": ctx.total_rows,
                "detail": f"{count} value(s) in '{col}' were filled in by an imputation rule; they are "
                          f"estimates, not observations.",
                "suggestion": "Treat these as estimates in any analysis, or replace them with real values.",
            })
    return out
