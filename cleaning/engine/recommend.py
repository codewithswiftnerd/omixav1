"""
Per-column type detection (with confidence) and cleaning recommendations. Read-only.

Recommendations are SUGGESTIONS: nothing here changes data, and risky or under-determined choices
(which country? which currency?) are marked `needs_choice` instead of being filled in by a guess.
Detection is heuristic; the confidence is shown so nobody mistakes it for certainty.
"""

from __future__ import annotations

import pandas as pd

from cleaning import profiling
from cleaning.engine.base import as_object, blank_mask, inconsistency_pct, str_mask
from cleaning.engine.numeric import SYMBOL_CURRENCY
from cleaning.engine.phone import SUPPORTED_COUNTRIES, classify_phone
from cleaning.engine.text import title_case

SAMPLE_ROWS = 20000


def _rec(rule: dict, reason: str, risk: str = "safe") -> dict:
    return {"rule": rule, "reason": reason, "risk": risk, "auto_apply": False}


def _whitespace_issues(s: pd.Series) -> tuple[int, int]:
    m = str_mask(s)
    if not m.any():
        return 0, 0
    t = s[m]
    return int((t != t.str.strip()).sum()), int(t.str.contains(r"\s{2,}", regex=True).sum())


def _guess_phone_country(s: pd.Series):
    vals = pd.unique(s[~blank_mask(s)])[:300]
    best, best_n = None, 0
    for c in SUPPORTED_COUNTRIES:
        n = sum(1 for v in vals if classify_phone(v, c)[1] is None)
        if n > best_n:
            best, best_n = c, n
    return (best, best_n / len(vals)) if len(vals) else (None, 0.0)


def _guess_currency(s: pd.Series):
    m = str_mask(s)
    if not m.any():
        return None
    found = {}
    for tok, cur in SYMBOL_CURRENCY.items():
        if tok == "N":
            continue
        n = int(s[m].str.contains(tok, regex=False).sum())
        if n:
            found[cur] = found.get(cur, 0) + n
    return max(found, key=found.get) if len(found) == 1 else None


def recommend_for_column(name: str, series: pd.Series, total: int) -> dict:
    s = as_object(series.head(SAMPLE_ROWS))
    prof = profiling.profile_column(str(name), s, len(s))
    sem = prof.semantic_type
    out = {"detected": {"type": sem, "confidence": round(float(prof.confidence), 2)},
           "recommendations": []}
    recs = out["recommendations"]
    nonblank = s[~blank_mask(s)]
    if nonblank.empty:
        return out

    edge, runs = _whitespace_issues(s)
    if edge:
        recs.append(_rec({"type": "trim_whitespace"}, f"{edge} value(s) have leading or trailing spaces."))
    if runs:
        recs.append(_rec({"type": "normalize_whitespace"}, f"{runs} value(s) contain repeated spaces."))

    confident = prof.confidence >= profiling.CONFIDENT
    if sem == profiling.PERSON_NAME and confident:
        txt = nonblank[str_mask(nonblank)]
        off = int((txt != txt.map(title_case)).sum()) if len(txt) else 0
        if off:
            recs.append(_rec({"type": "normalize_case", "mode": "title"},
                             f"Mixed capitalisation in {off} value(s)."))
    elif sem == profiling.PHONE and confident:
        country, share = _guess_phone_country(s)
        incons = inconsistency_pct(s)
        if country and share >= 0.6:
            if incons:
                recs.append(_rec({"type": "normalize_phone", "country": country, "output_format": "international"},
                                 f"{incons}% of numbers use a different layout; most look like {country} numbers."))
            recs.append(_rec({"type": "validate_phone", "country": country}, "Flag numbers that are not valid for this country."))
        else:
            recs.append(_rec({"type": "normalize_phone", "output_format": "international"},
                             "Phone numbers detected but no single country fits most of them: pick the country.", "needs_choice"))
    elif sem in (profiling.DATE, profiling.DATETIME) and confident:
        incons = inconsistency_pct(s)
        if incons:
            recs.append(_rec({"type": "normalize_date", "output_format": "YYYY-MM-DD"},
                             f"Several date layouts in use ({incons}% differ from the most common). "
                             "Ambiguous values like 01/02/2026 will be flagged, not guessed."))
        else:
            recs.append(_rec({"type": "validate_date"}, "Flag invalid or ambiguous dates."))
    elif sem == profiling.EMAIL and confident:
        txt = nonblank[str_mask(nonblank)]
        if len(txt) and (txt != txt.str.lower()).any():
            recs.append(_rec({"type": "normalize_case", "mode": "lower"}, "Some addresses contain capital letters."))
        recs.append(_rec({"type": "validate_email"}, "Flag malformed, placeholder or suspicious addresses."))
    elif sem == profiling.CURRENCY and confident:
        cur = _guess_currency(s)
        if cur:
            recs.append(_rec({"type": "normalize_currency", "currency": cur},
                             f"Amounts are stored as text with {cur} symbols."))
        elif str_mask(s).any():
            recs.append(_rec({"type": "normalize_currency"}, "Amounts are stored as text: pick the currency.", "needs_choice"))
    elif sem in (profiling.CATEGORICAL, profiling.BOOLEAN, profiling.GEOGRAPHIC):
        txt = nonblank[str_mask(nonblank)]
        if len(txt):
            key = txt.str.strip().str.casefold()
            variants = int(txt.nunique() - key.nunique())
            if variants > 0:
                recs.append(_rec({"type": "standardize_categories"},
                                 f"{variants} spelling/capitalisation variant(s) of the same value."))
                recs.append(_rec({"type": "normalize_case", "mode": "title"}, "Use one consistent capitalisation."))
    return out


def recommend_dataset(df: pd.DataFrame) -> dict:
    cols = {str(c): recommend_for_column(str(c), df[c], len(df)) for c in df.columns}
    suggested = {c: {"rules": [dict(r["rule"]) for r in v["recommendations"] if r["risk"] == "safe"]}
                 for c, v in cols.items() if any(r["risk"] == "safe" for r in v["recommendations"])}
    return {
        "columns": cols,
        "suggested_profile": {"name": "Suggested cleaning", "columns": suggested} if suggested else None,
        "note": "Suggestions only. Nothing is applied until you submit a cleaning profile. "
                "Detection is heuristic; check the confidence shown.",
    }
