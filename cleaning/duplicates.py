"""Duplicate analysis. READ-ONLY: it never removes or edits anything.

Three different things are kept apart because they deserve different treatment:

  exact      Every column is identical (after the cleaning rules ran). The only class the default
             `duplicates` rule removes, first occurrence kept, every removal written to the audit ledger.
  by_key     Rows that share the SAME values in the user's chosen key columns (e.g. email) but differ in
             other columns. Reported for review; never removed or merged automatically.
  potential  Rows that share a STRONG identifier (e-mail or phone) but are not exact copies, so they may be
             one person recorded twice. Review only.

Two people with the same name are NOT duplicates: a name is never an identifier. Same name + different e-mail
is deliberately reported as nothing at all.
"""

from __future__ import annotations

from typing import Iterable, Optional

import pandas as pd

from cleaning import profiling

_STRONG_TOKENS = {"email", "mail", "phone", "mobile", "tel", "telephone", "gsm", "cell"}
MAX_GROUPS_RETURNED = 20


def _norm(v):
    if v is None or v is pd.NA or (isinstance(v, float) and v != v):
        return None
    t = " ".join(str(v).split()).casefold()
    return t or None


def _key_frame(df: pd.DataFrame, cols: list) -> pd.DataFrame:
    return pd.DataFrame({c: df[c].map(_norm) for c in cols})


def _groups(df: pd.DataFrame, cols: list, exclude_exact: pd.Series | None = None) -> list[list]:
    """Row-label groups that agree on `cols` (blank keys never group: two empty e-mails are not a match)."""
    keys = _key_frame(df, cols)
    usable = keys.notna().all(axis=1)
    if exclude_exact is not None:
        usable &= ~exclude_exact
    sub = keys[usable]
    out = []
    if sub.empty:
        return out
    dup = sub.duplicated(keep=False)
    for _, g in sub[dup].groupby(list(sub.columns), sort=False):
        out.append(list(g.index))
    return out


def _row_numbers(labels: Iterable, has_header: bool) -> list[int]:
    off = 2 if has_header else 1   # spreadsheet row of 0-based label 0
    return [int(l) + off if isinstance(l, (int,)) or hasattr(l, "item") else l for l in labels]


def analyze_duplicates(df: pd.DataFrame, key_columns: Optional[list] = None, *, has_header: bool = True) -> dict:
    n = len(df)
    exact_mask = df.duplicated(keep=False) if n else pd.Series(dtype=bool)
    exact_extra = int(df.duplicated(keep="first").sum()) if n else 0
    result = {
        "rows": n,
        "exact": {"rows_in_groups": int(exact_mask.sum()), "extra_copies": exact_extra},
        "by_key": {"key_columns": [], "unmatched_columns": [], "groups": 0, "rows": 0, "samples": []},
        "potential": {"identifier_columns": [], "groups": 0, "rows": 0, "samples": []},
    }
    if n < 2:
        return result
    by_col = {str(c): c for c in df.columns}

    if key_columns:
        cols = [by_col[k] for k in key_columns if k in by_col]
        result["by_key"]["key_columns"] = [str(c) for c in cols]
        result["by_key"]["unmatched_columns"] = [k for k in key_columns if k not in by_col]
        if cols:
            # rows that are exact copies are the exact class's business, only differing rows are "by key"
            grp = _groups(df, cols, exclude_exact=exact_mask)
            result["by_key"].update(groups=len(grp), rows=sum(len(g) for g in grp),
                                    samples=[_row_numbers(g, has_header) for g in grp[:MAX_GROUPS_RETURNED]])

    strong = [c for c in df.columns if set(profiling.name_tokens(str(c))) & _STRONG_TOKENS]
    result["potential"]["identifier_columns"] = [str(c) for c in strong]
    seen_rows: set = set()
    pot_groups = []
    for c in strong:
        for g in _groups(df, [c], exclude_exact=exact_mask):
            if not set(g) <= seen_rows:
                pot_groups.append(g)
                seen_rows |= set(g)
    result["potential"].update(groups=len(pot_groups), rows=sum(len(g) for g in pot_groups),
                               samples=[_row_numbers(g, has_header) for g in pot_groups[:MAX_GROUPS_RETURNED]])
    return result
