"""
Quality Profiles: a saved, reusable data standard, and the engine that checks a dataset against it.

A profile is plain JSON (validated by normalize_profile). evaluate_profile() returns one
PASS / WARNING / FAIL result per rule plus an overall compliance %. Column names are compared
in a normalised form (case, spaces and underscores ignored) because Omixa's own
column-name cleaning would otherwise make "Signup Date" and "signup_date" look different.
"""

from __future__ import annotations

import re
from typing import Any

import pandas as pd

from cleaning import detectors

PASS, WARNING, FAIL = "PASS", "WARNING", "FAIL"
TYPES = ("text", "integer", "number", "date", "email", "boolean")
_BOOL_WORDS = {"true", "false", "yes", "no", "y", "n", "t", "f", "0", "1"}
_INT_RE = re.compile(r"^[+-]?\d+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")

MAX_COLUMNS = 200
MAX_ALLOWED_VALUES = 500

DEFAULTS = {
    "description": "",
    "required_columns": [],
    "expected_types": {},
    "required_fields": [],
    "max_missing_pct": 5.0,
    "column_missing_pct": {},
    "max_duplicate_pct": 0.0,
    "duplicate_key_columns": [],
    "date_columns": [],
    "date_min": None,
    "date_max": None,
    "email_columns": [],
    "allowed_values": {},
    "no_edge_whitespace": True,
    "min_quality_score": 80,
}


def norm(name: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")


class ProfileError(ValueError):
    pass


def _str_list(value, field) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ProfileError(f"'{field}' must be a list of text values.")
    out = [v.strip() for v in value if v.strip()]
    if len(out) > MAX_COLUMNS:
        raise ProfileError(f"'{field}' has too many entries.")
    return list(dict.fromkeys(out))


def _pct(value, field, default):
    if value in (None, ""):
        return default
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ProfileError(f"'{field}' must be a number between 0 and 100.")
    if not 0 <= v <= 100:
        raise ProfileError(f"'{field}' must be between 0 and 100.")
    return v


def _iso(value, field):
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) or pd.isna(pd.to_datetime(value, errors="coerce")):
        raise ProfileError(f"'{field}' must be a date like 2024-01-31.")
    return value


def normalize_profile(raw: Any) -> dict:
    """Validates user input and fills defaults. Raises ProfileError with a friendly message."""
    if not isinstance(raw, dict):
        raise ProfileError("Profile must be an object.")
    name = raw.get("name")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise ProfileError("Give the profile a name (up to 80 characters).")
    p = dict(DEFAULTS)
    p["name"] = name.strip()
    desc = raw.get("description", "")
    if not isinstance(desc, str) or len(desc) > 400:
        raise ProfileError("Description must be text up to 400 characters.")
    p["description"] = desc.strip()
    for f in ("required_columns", "required_fields", "date_columns", "email_columns", "duplicate_key_columns"):
        p[f] = _str_list(raw.get(f), f)

    types = raw.get("expected_types") or {}
    if not isinstance(types, dict) or len(types) > MAX_COLUMNS:
        raise ProfileError("'expected_types' must map column names to types.")
    for col, t in types.items():
        if not isinstance(col, str) or t not in TYPES:
            raise ProfileError(f"Type for '{col}' must be one of: {', '.join(TYPES)}.")
    p["expected_types"] = {c.strip(): t for c, t in types.items() if c.strip()}

    allowed = raw.get("allowed_values") or {}
    if not isinstance(allowed, dict) or len(allowed) > MAX_COLUMNS:
        raise ProfileError("'allowed_values' must map column names to lists of values.")
    clean_allowed = {}
    for col, vals in allowed.items():
        if not isinstance(col, str) or not isinstance(vals, list) or not all(isinstance(v, (str, int, float)) for v in vals):
            raise ProfileError(f"Allowed values for '{col}' must be a list.")
        if len(vals) > MAX_ALLOWED_VALUES:
            raise ProfileError(f"Too many allowed values for '{col}'.")
        vs = [str(v).strip() for v in vals if str(v).strip()]
        if vs:
            clean_allowed[col.strip()] = list(dict.fromkeys(vs))
    p["allowed_values"] = clean_allowed

    p["max_missing_pct"] = _pct(raw.get("max_missing_pct"), "max_missing_pct", DEFAULTS["max_missing_pct"])
    p["max_duplicate_pct"] = _pct(raw.get("max_duplicate_pct"), "max_duplicate_pct", DEFAULTS["max_duplicate_pct"])
    cm = raw.get("column_missing_pct") or {}
    if not isinstance(cm, dict):
        raise ProfileError("'column_missing_pct' must map column names to percentages.")
    p["column_missing_pct"] = {c.strip(): _pct(v, f"column_missing_pct.{c}", 0.0) for c, v in cm.items() if isinstance(c, str) and c.strip()}
    p["date_min"] = _iso(raw.get("date_min"), "date_min")
    p["date_max"] = _iso(raw.get("date_max"), "date_max")
    if p["date_min"] and p["date_max"] and p["date_min"] > p["date_max"]:
        raise ProfileError("'date_min' cannot be after 'date_max'.")
    p["no_edge_whitespace"] = bool(raw.get("no_edge_whitespace", True))
    ms = raw.get("min_quality_score", DEFAULTS["min_quality_score"])
    p["min_quality_score"] = int(_pct(ms, "min_quality_score", 80))
    return p


# --------------------------------------------------------------------------- evaluation

def _colmap(df: pd.DataFrame) -> dict[str, str]:
    out: dict[str, str] = {}
    for c in df.columns:
        out.setdefault(norm(c), c)
    return out


def _strings(series: pd.Series) -> pd.Series:
    s = series.dropna()
    s = s[~s.astype(str).str.strip().eq("")]
    return s.astype(str)


def _blank_mask(series: pd.Series) -> pd.Series:
    return series.isna() | series.astype(str).str.strip().eq("")


def _grade(bad: int, total: int) -> str:
    if bad == 0:
        return PASS
    return WARNING if total and bad / total <= 0.01 else FAIL


def _conforms(series: pd.Series, t: str) -> pd.Series:
    s = _strings(series)
    if t == "text":
        return pd.Series(True, index=s.index)
    st = s.str.strip()
    if t == "integer":
        ok = st.str.fullmatch(_INT_RE.pattern) | st.map(lambda v: _is_whole_float(v))
    elif t == "number":
        ok = st.map(lambda v: _is_float(v))
    elif t == "date":
        ok = _parse_dates(st).notna()
    elif t == "email":
        ok = st.map(lambda v: bool(detectors.EMAIL_RE.match(v)))
    else:  # boolean
        ok = st.str.lower().isin(_BOOL_WORDS)
    return ok.astype(bool)


def _is_float(v: str) -> bool:
    try:
        float(v)
        return v.lower() not in ("nan", "inf", "-inf", "infinity")
    except ValueError:
        return False


def _is_whole_float(v: str) -> bool:
    return _is_float(v) and float(v) == int(float(v)) if _is_float(v) and abs(float(v)) < 1e18 else False


def _parse_dates(st: pd.Series) -> pd.Series:
    return pd.to_datetime(st, errors="coerce", format="mixed")


def _rule(key, label, status, detail, affected=0):
    return {"key": key, "label": label, "status": status, "detail": detail, "affected": int(affected)}


def evaluate_profile(df: pd.DataFrame, profile: dict, quality_score: int | None = None) -> dict:
    """Runs every rule the profile contains. Read-only: never modifies df."""
    cmap = _colmap(df)
    rows = len(df)
    results: list[dict] = []

    def col(name):
        real = cmap.get(norm(name))
        return real

    # 1. required columns
    if profile["required_columns"]:
        missing = [c for c in profile["required_columns"] if col(c) is None]
        results.append(_rule("required_columns", "Required columns",
                             FAIL if missing else PASS,
                             ("Missing: " + ", ".join(missing)) if missing else f"All {len(profile['required_columns'])} required columns present",
                             len(missing)))

    # 2. data types
    if profile["expected_types"]:
        worst, bad_cols, checked = PASS, [], 0
        for name, t in profile["expected_types"].items():
            real = col(name)
            if real is None:
                continue
            checked += 1
            s = _strings(df[real])
            bad = int((~_conforms(df[real], t)).sum()) if len(s) else 0
            status = _grade(bad, len(s))
            if status != PASS:
                bad_cols.append(f"{name} ({bad} not {t})")
                worst = FAIL if (status == FAIL or worst == FAIL) else WARNING
        results.append(_rule("data_types", "Data types", worst,
                             ("; ".join(bad_cols[:5]) + ("…" if len(bad_cols) > 5 else "")) if bad_cols else f"{checked} columns match their expected type",
                             len(bad_cols)))

    # 3. required fields
    if profile["required_fields"]:
        gaps = []
        for name in profile["required_fields"]:
            real = col(name)
            if real is None:
                gaps.append(f"{name} (column missing)")
                continue
            n = int(_blank_mask(df[real]).sum())
            if n:
                gaps.append(f"{name} ({n} blank)")
        results.append(_rule("required_fields", "Required fields filled", FAIL if gaps else PASS,
                             ("; ".join(gaps[:5]) + ("…" if len(gaps) > 5 else "")) if gaps else "No blanks in required fields", len(gaps)))

    # 4. missing-value thresholds
    over, near = [], []
    scope = [c for c in df.columns]
    for real in scope:
        limit = profile["column_missing_pct"].get(next((k for k in profile["column_missing_pct"] if norm(k) == norm(real)), ""), profile["max_missing_pct"])
        pct = round(100 * float(_blank_mask(df[real]).sum()) / rows, 2) if rows else 0.0
        if pct > limit * 1.5 and pct > limit:
            over.append((real, pct, limit))
        elif pct > limit:
            near.append((real, pct, limit))
    status = FAIL if over else WARNING if near else PASS
    bad = over + near
    results.append(_rule("missing_values", "Missing values", status,
                         "; ".join(f"{c}: {p}% (limit {l}%)" for c, p, l in bad[:5]) if bad else f"All columns within the {profile['max_missing_pct']:g}% limit",
                         len(bad)))

    # 5. duplicates
    key_cols = [col(c) for c in profile["duplicate_key_columns"] if col(c)]
    subset = key_cols or None
    dups = int(df.duplicated(subset=subset).sum()) if rows else 0
    dup_pct = round(100 * dups / rows, 2) if rows else 0.0
    limit = profile["max_duplicate_pct"]
    if dup_pct <= limit:
        st = PASS
    elif limit > 0 and dup_pct <= limit * 1.5:
        st = WARNING
    else:
        st = FAIL
    results.append(_rule("duplicates", "Duplicate threshold", st,
                         f"{dups} duplicate {'key matches' if subset else 'rows'} ({dup_pct}%), limit {limit:g}%", dups))

    # 6. dates
    if profile["date_columns"]:
        bad_total, rng_total, n_total, notes = 0, 0, 0, []
        for name in profile["date_columns"]:
            real = col(name)
            if real is None:
                notes.append(f"{name} (column missing)")
                bad_total += 1
                continue
            s = _strings(df[real]).str.strip()
            parsed = _parse_dates(s)
            invalid = int(parsed.isna().sum())
            n_total += len(s)
            bad_total += invalid
            if invalid:
                notes.append(f"{name}: {invalid} invalid")
            ok = parsed.dropna()
            out = 0
            if profile["date_min"]:
                out += int((ok < pd.Timestamp(profile["date_min"])).sum())
            if profile["date_max"]:
                out += int((ok > pd.Timestamp(profile["date_max"])).sum())
            if out:
                rng_total += out
                notes.append(f"{name}: {out} outside allowed range")
        st = _grade(bad_total + rng_total, max(n_total, 1))
        results.append(_rule("dates", "Valid dates", st, "; ".join(notes[:5]) if notes else "All dates valid and in range", bad_total + rng_total))

    # 7. emails
    if profile["email_columns"]:
        bad_total, n_total, notes = 0, 0, []
        for name in profile["email_columns"]:
            real = col(name)
            if real is None:
                notes.append(f"{name} (column missing)")
                bad_total += 1
                continue
            s = _strings(df[real]).str.strip()
            invalid = int((~s.map(lambda v: bool(detectors.EMAIL_RE.match(v)))).sum())
            n_total += len(s)
            bad_total += invalid
            if invalid:
                notes.append(f"{name}: {invalid} invalid")
        results.append(_rule("emails", "Valid emails", _grade(bad_total, max(n_total, 1)), "; ".join(notes[:5]) if notes else "All emails valid", bad_total))

    # 8. allowed values
    if profile["allowed_values"]:
        bad_total, n_total, notes = 0, 0, []
        for name, allowed in profile["allowed_values"].items():
            real = col(name)
            if real is None:
                notes.append(f"{name} (column missing)")
                bad_total += 1
                continue
            allowed_set = {a.lower() for a in allowed}
            s = _strings(df[real]).str.strip()
            invalid = int((~s.str.lower().isin(allowed_set)).sum())
            n_total += len(s)
            bad_total += invalid
            if invalid:
                notes.append(f"{name}: {invalid} outside allowed values")
        results.append(_rule("allowed_values", "Allowed category values", _grade(bad_total, max(n_total, 1)),
                             "; ".join(notes[:5]) if notes else "All values within the allowed sets", bad_total))

    # 9. formatting
    if profile["no_edge_whitespace"]:
        n_bad, n_cells = 0, 0
        for c in df.columns:
            if df[c].dtype == object or str(df[c].dtype).startswith("str"):
                s = _strings(df[c])
                n_cells += len(s)
                n_bad += int((s != s.str.strip()).sum())
        results.append(_rule("formatting", "Clean formatting (no stray spaces)", _grade(n_bad, max(n_cells, 1)),
                             f"{n_bad} cells with leading/trailing spaces" if n_bad else "No stray whitespace", n_bad))

    # 10. minimum quality score
    if quality_score is not None and profile["min_quality_score"]:
        minimum = profile["min_quality_score"]
        st = PASS if quality_score >= minimum else WARNING if quality_score >= minimum - 5 else FAIL
        results.append(_rule("min_score", "Minimum quality score", st, f"Score {quality_score}, required {minimum}", 0))

    points = {PASS: 1.0, WARNING: 0.5, FAIL: 0.0}
    compliance = round(100 * sum(points[r["status"]] for r in results) / len(results)) if results else 100
    return {
        "results": results,
        "compliance": compliance,
        "passed": sum(r["status"] == PASS for r in results),
        "warnings": sum(r["status"] == WARNING for r in results),
        "failed": sum(r["status"] == FAIL for r in results),
    }
