"""
Safe, user-confirmed type normalization.

When the user CONFIRMS that a column is numeric / integer / currency, numeric text in it ("35", "1,200",
"3.50") is converted to a real number, one distinct value at a time. Everything else is left exactly as
uploaded and stays visible to validation, so:

  * "comma", "ERROR", "forty" are NEVER guessed, replaced with 0 or deleted; they are reported for review;
  * markers such as "N/A" / "UNKNOWN" are left for the missing-value rule (they are not converted here);
  * digit strings with a leading zero ("007", "0803...") are NOT converted: that would lose information;
  * a fractional number in an INTEGER column ("3.5") is not rounded: it is reported for review;
  * a column the user set to "Leave as is" is never touched.

Nothing here runs unless the column type was explicitly confirmed (see `column_types` in
processing/pipeline.run_pipeline). Without a confirmation the existing, stricter all-or-nothing rule in
cleaning/rules.handle_numeric_text_cleaning applies.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

import pandas as pd

from cleaning import detectors
from cleaning import model as M

CONVERTIBLE_TYPES = ("numeric", "integer", "currency")
# plain digits, optional sign, optional well-formed thousands separators, optional decimals
_PLAIN_NUMBER = re.compile(r"^[+-]?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?$")
_LEADING_ZERO = re.compile(r"^[+-]?0\d")
MAX_EXAMPLES = 5


def parse_text_number(text: str, integer_only: bool = False):
    """The number a text cell safely stands for, or None when converting could change its meaning."""
    t = text.strip()
    if not t or not _PLAIN_NUMBER.match(t) or t.startswith("+"):
        return None
    if _LEADING_ZERO.match(t) and not t.lstrip("-").startswith("0."):
        return None  # identifier-like ("007"): converting would silently drop the zeros
    clean = t.replace(",", "")
    try:
        f = float(clean)
    except ValueError:
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    if "." not in clean:
        return int(clean)
    if integer_only:
        return int(f) if f == int(f) else None
    return f


def _resolve_column(df: pd.DataFrame, name: str):
    cols = list(df.columns)
    if name in cols:
        return name
    key = re.sub(r"[\s_\-]+", "", str(name)).lower()
    hits = [c for c in cols if re.sub(r"[\s_\-]+", "", str(c)).lower() == key]
    return hits[0] if len(hits) == 1 else None


def normalize_confirmed_types(df: pd.DataFrame, column_types: Optional[dict], *, locked: Iterable = (),
                              audit=None, labels: Optional[dict] = None) -> tuple[pd.DataFrame, dict]:
    report = {"requested": dict(column_types or {}), "columns": {}, "unmatched": [], "skipped_locked": [],
              "converted_total": 0, "left_for_review_total": 0}
    if not column_types:
        return df, report
    locked = set(locked or ())
    before = df.copy() if audit is not None else None

    for name, ctype in column_types.items():
        if ctype not in CONVERTIBLE_TYPES:
            continue  # other confirmed types only affect validation; nothing is converted here
        col = _resolve_column(df, name)
        if col is None:
            report["unmatched"].append(name)
            continue
        if col in locked:
            report["skipped_locked"].append(str(col))
            continue
        series = df[col]
        if pd.api.types.is_numeric_dtype(series.dtype) or pd.api.types.is_bool_dtype(series.dtype):
            continue
        integer_only = ctype == "integer"
        out = series.astype(object).copy()
        converted, review, examples = 0, 0, []
        mapping: dict = {}
        for v in pd.unique(out[out.map(lambda x: isinstance(x, str))]):
            if v.strip() == "" or detectors.is_missing_token(v):
                continue
            num = parse_text_number(v, integer_only)
            if num is not None:
                mapping[v] = num
        if mapping:
            is_str = out.map(lambda x: isinstance(x, str))
            sel = out[is_str & out.isin(list(mapping))]
            out.loc[sel.index] = sel.map(mapping)
            converted = len(sel)
        # whatever still is text and is not blank / a missing marker needs a person to look at it
        rest = out[out.map(lambda x: isinstance(x, str))]
        for v, n in rest.value_counts().items():
            if v.strip() == "" or detectors.is_missing_token(v):
                continue
            review += int(n)
            if len(examples) < MAX_EXAMPLES:
                examples.append(v)
        if converted:
            df[col] = out
        report["columns"][str(col)] = {"type": ctype, "converted": int(converted), "left_for_review": int(review),
                                       "review_examples": examples}
        report["converted_total"] += int(converted)
        report["left_for_review_total"] += int(review)

    if audit is not None and report["converted_total"]:
        audit.record_step(
            "confirmed_type_normalization", before, df, rule_id=None, operation=M.NORMALIZATION,
            confidence=0.99, approval=M.APPROVAL_USER_APPROVED,
            reason="Numeric text converted to numbers in columns whose type you confirmed.",
            reversibility=M.REVERSIBLE, column_labels=labels)
    return df, report
