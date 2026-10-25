"""User-directed cleaning steps: nothing here runs unless the request's `options` asks for it.

* phone_country            country-aware phone normalisation (never assumed)
* missing_values           per-column action for genuinely empty cells: leave | fill | flag | remove_rows
* value_mappings           mappings the USER approved (or rejected) for categories / cities / any text column
* suggest_value_mappings   read-only: proposes mappings for review; it never changes a cell
"""

from __future__ import annotations

import re
from typing import Any, Optional

import pandas as pd

from cleaning import detectors


def blank_mask(series: pd.Series) -> pd.Series:
    """Null or whitespace-only text: a genuinely empty cell (not the words N/A, None or Unknown)."""
    out = series.isna()
    is_str = series.map(lambda v: isinstance(v, str))
    if is_str.any():
        out = out | (is_str & series.map(lambda v: isinstance(v, str) and v.strip() == ""))
    return out.astype(bool)


def _label(details: dict, col) -> str:
    return str((details.get("_column_labels") or {}).get(col, col))


def _norm_key(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v)).strip().casefold()


USER_DIRECTED_RULES = ("approved_value_mappings", "missing_value_handling", "approved_value_actions")


# --------------------------------------------------------------------------- phone: country-aware
def phone_country_for(details: dict, col) -> Optional[str]:
    pc = (details.get("_options") or {}).get("phone_country")
    if isinstance(pc, dict):
        return pc.get(_label(details, col))
    return pc


def apply_phone_country(df: pd.DataFrame, details: dict, per_column: dict) -> int:
    """Normalises phone cells to the country's standard form, ONLY where the value itself carries the evidence
    (the country's dial code or trunk prefix, an explicit +/00 prefix). Anything else is left exactly as uploaded
    (not even its punctuation is touched) and listed with the reason, never guessed at."""
    from cleaning.phone_formats import normalize_phone
    changed, left = 0, {}
    for col in df.select_dtypes(include=["object", "string"]).columns:
        country = phone_country_for(details, col)
        if not country or not detectors.is_phone_column(str(col)):
            continue
        out = df[col].astype("object").copy()
        col_changed, unresolved = 0, []
        for idx, val in out.items():
            if not isinstance(val, str) or not val.strip():
                continue
            norm = normalize_phone(val, country)
            if norm is not None and norm != val:
                out.at[idx] = norm
                col_changed += 1
            elif norm is None and len(unresolved) < 10:
                unresolved.append(val)
        if col_changed:
            df[col] = out
            changed += col_changed
        entry = per_column.setdefault(col, {"changed": 0, "scientific_notation_recovered": 0})
        entry.update(country=country, country_normalised=col_changed)
        if unresolved:
            left[col] = unresolved
    details["phone_country"] = {"left_unnormalised_samples": left}
    return changed


# --------------------------------------------------------------------------- missing values
def _rule_for(opts: dict, label: str) -> Optional[dict]:
    mv = opts.get("missing_values") or {}
    return (mv.get("columns") or {}).get(label) or mv.get("default")


def handle_missing_value_handling(df: pd.DataFrame, details: dict) -> tuple[pd.DataFrame, int]:
    """Per column: leave | fill with the user's own value | flag for review | remove rows (only on request).
    Only GENUINELY empty cells (null or whitespace-only) are touched: "N/A", "None", "Unknown" are text until the
    placeholder rule or the user's `missing_markers` treat them as missing."""
    opts = details.get("_options") or {}
    per_column, drop_mask, changed = {}, pd.Series(False, index=df.index), 0
    for col in list(df.columns):
        rule = _rule_for(opts, _label(details, col))
        if not rule or rule["action"] == "leave":
            continue
        empty = blank_mask(df[col])
        n = int(empty.sum())
        if not n:
            continue
        action = rule["action"]
        if action == "fill":
            s = df[col].astype("object").copy()
            s.loc[empty] = rule["value"]
            df[col] = s
            changed += n
        elif action == "remove_rows":
            drop_mask |= empty
        per_column[col] = {"action": action, "cells": n, **({"value": rule["value"]} if action == "fill" else {})}
    removed = int(drop_mask.sum())
    if removed:
        df = df.loc[~drop_mask]
        changed += removed
    details["missing_value_handling"] = {
        "per_column": per_column, "rows_removed": removed,
        "flagged_for_review": {c: v["cells"] for c, v in per_column.items() if v["action"] == "flag"},
    }
    return df, changed


# --------------------------------------------------------------------------- approved mappings
def handle_approved_value_mappings(df: pd.DataFrame, details: dict) -> tuple[pd.DataFrame, int]:
    """Applies mappings the user approved ({"PH": "Port Harcourt"}), matched ignoring case and extra spaces, to the
    whole column so the result is consistent. Rejected sources are never touched. A source that is not present is
    reported, never invented."""
    maps = (details.get("_options") or {}).get("value_mappings") or {}
    changed, per_column = 0, {}
    for col in df.select_dtypes(include=["object", "string"]).columns:
        spec = maps.get(_label(details, col))
        if not spec:
            continue
        rejected = {_norm_key(r) for r in spec.get("reject") or []}
        approved = {_norm_key(a): b for a, b in (spec.get("approve") or {}).items() if _norm_key(a) not in rejected}
        s = df[col].astype("object").copy()
        keys = s.map(lambda v: _norm_key(v) if isinstance(v, str) else None)
        hit = keys.isin(list(approved)) & s.notna()
        new = keys[hit].map(approved)
        differs = new.index[new.values != s[hit].values]
        if len(differs):
            s.loc[differs] = new.loc[differs]
            df[col] = s
        n = int(len(differs))
        changed += n
        per_column[col] = {"changed": n, "approved": len(approved), "rejected": len(rejected),
                           "unmatched": sorted(a for a in approved if a not in set(keys.dropna()))[:20]}
    details["approved_value_mappings"] = {"per_column": per_column}
    return df, changed


# --------------------------------------------------------------------------- review pop-up decisions
def handle_approved_value_actions(df: pd.DataFrame, details: dict) -> tuple[pd.DataFrame, int]:
    """Applies what the user decided in the review pop-up for specific flagged values: clear the cell (make it
    empty) or remove the rows holding it. Only values the user listed are touched, only in the column they were
    listed for, matched ignoring case and extra spaces. Anything not listed is "keep". A listed value that is not
    found is reported, never invented."""
    maps = (details.get("_options") or {}).get("value_actions") or {}
    changed, per_column, drop_mask = 0, {}, pd.Series(False, index=df.index)
    for col in list(df.columns):
        spec = maps.get(_label(details, col))
        if not spec:
            continue
        s = df[col].astype("object").copy()
        keys = s.map(lambda v: _norm_key(v) if isinstance(v, str) else None)
        clear = {_norm_key(a) for a in spec.get("clear") or []}
        remove = {_norm_key(a) for a in spec.get("remove_rows") or []}
        m_remove = keys.isin(list(remove)) & s.notna() if remove else pd.Series(False, index=df.index)
        m_clear = (keys.isin(list(clear)) & s.notna() & ~m_remove) if clear else pd.Series(False, index=df.index)
        if m_clear.any():
            s.loc[m_clear] = None
            df[col] = s
        drop_mask |= m_remove
        present = set(keys.dropna())
        per_column[col] = {"cleared": int(m_clear.sum()), "rows_removed": int(m_remove.sum()),
                           "not_found": sorted((clear | remove) - present)[:20]}
        changed += int(m_clear.sum())
    removed = int(drop_mask.sum())
    if removed:
        df = df.loc[~drop_mask]
        changed += removed
    details["approved_value_actions"] = {"per_column": per_column, "rows_removed": removed}
    return df, changed


# --------------------------------------------------------------------------- suggestions (read-only)
# Groups of words that OFTEN mean the same thing in status-like columns. They are only used to PROPOSE a mapping;
# nothing is applied until the user approves it, and the user can reject it to keep two distinct categories.
SYNONYM_GROUPS = (
    {"done", "complete", "completed", "finished", "fulfilled"},
    {"pending", "waiting", "awaiting", "in progress", "processing"},
    {"cancelled", "canceled", "void", "voided"},
    {"active", "enabled", "live"},
    {"inactive", "disabled", "dormant"},
)
MAX_DISTINCT_FOR_SUGGESTIONS = 40


def suggest_value_mappings(df: pd.DataFrame, labels: Optional[dict] = None) -> list[dict]:
    """[{column, from, to, count, reason, status: "suggested"}]. Only low-cardinality text columns are examined.
    Two values are suggested as equivalent only when they differ by a trailing -d/-ed/-s or belong to one synonym
    group; being merely similar is never enough, and unfamiliar abbreviations are never guessed."""
    labels = labels or {}
    out = []
    for col in df.select_dtypes(include=["object", "string"]).columns:
        s = df[col].dropna()
        s = s[s.map(lambda v: isinstance(v, str) and v.strip() != "")]
        if s.empty:
            continue
        counts = s.map(lambda v: re.sub(r"\s+", " ", v).strip()).value_counts()
        if len(counts) < 2 or len(counts) > MAX_DISTINCT_FOR_SUGGESTIONS or len(counts) > max(3, len(s) * 0.5):
            continue
        shown = {v: int(n) for v, n in counts.items()}
        keys = {v: _norm_key(v) for v in shown}
        seen = set()
        for a in shown:
            for b in shown:
                if a == b or keys[a] == keys[b] or (a, b) in seen:
                    continue
                reason = _equivalence(keys[a], keys[b])
                if not reason:
                    continue
                # keep the more common spelling (ties: the longer one) as the target
                src, dst = (a, b) if (shown[b], len(b)) > (shown[a], len(a)) else (b, a)
                if (src, dst) in seen:
                    continue
                seen.update({(a, b), (b, a)})
                out.append({"column": labels.get(col, str(col)), "from": src, "to": dst, "count": shown[src],
                            "reason": reason, "status": "suggested", "_to_count": shown[dst]})
    # one proposal per source value: when several targets qualify, offer the one the column already uses most
    best: dict = {}
    for sg in out:
        k = (sg["column"], sg["from"])
        if k not in best or sg["_to_count"] > best[k]["_to_count"]:
            best[k] = sg
    return [{k: v for k, v in sg.items() if k != "_to_count"} for sg in best.values()]


def _equivalence(a: str, b: str) -> Optional[str]:
    for g in SYNONYM_GROUPS:
        if a in g and b in g:
            return "These words often mean the same thing in a status column. Confirm that they do in your data."
    short, long_ = sorted((a, b), key=len)
    if long_.startswith(short) and len(short) >= 4 and 0 < len(long_) - len(short) <= 3 and re.fullmatch(r"(?:d|ed|s|es)", long_[len(short):]):
        return "Differ only by a word ending; they may be the same status written two ways."
    return None
