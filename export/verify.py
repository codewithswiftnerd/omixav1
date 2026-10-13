"""Post-export verification. READ-ONLY: re-opens the file that was just written and checks it, so a report can
never claim "cleaned" about a file that does not actually contain the cleaned data.

Checks (each reported as passed/failed with a count, nothing is raised):
  readable        the file opens and has the expected number of rows and columns
  header          column names match (after the formula-injection guard on header cells)
  cells_match     every exported cell equals the cleaned dataset (numbers compared as numbers)
  changes_applied every value change in the audit ledger is really in the file
  untouched       cells the pipeline did NOT change still read exactly as uploaded (CSV: byte-for-byte text),
                  which is what catches "45" -> "45.0", dropped leading zeros, re-formatted dates
  revalidation    re-reading the exported file and validating it again gives the same problem counts
The formula-injection guard (a leading ' on text cells that start with = + - @) is the one intentional
difference between dataset and file; it is counted and reported, not hidden.
"""

from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from export.exporter import _defuse_cell

MAX_SAMPLES = 10
VERIFY_MAX_CELLS = 1_500_000


def _jsonable(v):
    return v.item() if hasattr(v, "item") else v


def _canon(v) -> str:
    if v is None or v is pd.NA or (isinstance(v, float) and v != v):
        return ""
    if isinstance(v, pd.Timestamp):
        return v.strftime("%Y-%m-%d") if v == v.normalize() else v.strftime("%Y-%m-%d %H:%M:%S")
    if hasattr(v, "item") and not isinstance(v, (str, bytes)):
        try:
            v = v.item()
        except Exception:
            pass
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v == int(v) and abs(v) < 1e15 else repr(v)
    return str(v)


def _same(a, b, *, text_exact: bool = False) -> bool:
    ca, cb = _canon(a), _canon(b)
    if ca == cb:
        return True
    if text_exact:
        return False
    try:
        return math.isclose(float(ca), float(cb), rel_tol=1e-9, abs_tol=1e-12)
    except (TypeError, ValueError):
        return False


def _check(name: str, passed: Optional[bool], detail: str, count: int = 0, samples=None) -> dict:
    return {"check": name, "passed": passed, "detail": detail, "count": count, "samples": (samples or [])[:MAX_SAMPLES]}


def verify_export(
    *, out_path: str, ext: str, cleaned_df: pd.DataFrame, kept_index: list, column_labels: dict,
    source_text_df: Optional[pd.DataFrame], source_positions: list, ledger: dict,
    validation_after_totals: Optional[dict], sheet_name: Optional[str] = None, validation_config: Optional[dict] = None,
) -> dict:
    """cleaned_df has a 0..n-1 index; kept_index[i] is the ORIGINAL row label of cleaned row i;
    column_labels maps cleaned column name -> name in the uploaded file."""
    checks: list = []
    try:
        if ext == "csv":
            got = pd.read_csv(out_path, dtype=str, keep_default_na=False, na_values=[""], encoding="utf-8-sig")
        else:
            got = pd.read_excel(out_path, sheet_name=sheet_name or 0, dtype=object, keep_default_na=False, na_values=[""])
    except Exception as exc:
        return {"verified": False, "checks": [_check("readable", False, f"The exported file could not be read back ({type(exc).__name__}).")],
                "notes": []}

    exp_rows, exp_cols = cleaned_df.shape
    shape_ok = got.shape == (exp_rows, exp_cols)
    checks.append(_check("readable", shape_ok,
                         f"Exported file has {got.shape[0]} rows x {got.shape[1]} columns; dataset has {exp_rows} x {exp_cols}."
                         if not shape_ok else f"Exported file re-read OK: {exp_rows} rows x {exp_cols} columns."))
    if not shape_ok:
        return {"verified": False, "checks": checks, "notes": []}

    exp_header = [_defuse_cell(str(c)) for c in cleaned_df.columns]
    header_ok = [str(c) for c in got.columns] == exp_header
    checks.append(_check("header", header_ok, "Column names match." if header_ok else "Exported column names differ from the dataset."))

    too_big = exp_rows * exp_cols > VERIFY_MAX_CELLS
    pos_of_row = {lab: i for i, lab in enumerate(kept_index)}
    defused = 0
    if too_big:
        checks.append(_check("cells_match", None, f"Skipped: {exp_rows * exp_cols:,} cells is above the {VERIFY_MAX_CELLS:,}-cell verification limit."))
    else:
        total_mism, mism = 0, []
        for j, col in enumerate(cleaned_df.columns):
            src = cleaned_df.iloc[:, j]
            is_txt = src.map(lambda v: isinstance(v, str))
            guarded = src.map(lambda v: _defuse_cell(v) if isinstance(v, str) else v)
            defused += int((is_txt & (guarded.map(_canon) != src.map(_canon))).sum())
            exp, out = guarded.map(_canon), got.iloc[:, j].map(_canon)
            for i in np.flatnonzero((exp.values != out.values)):
                if _same(exp.iat[i], out.iat[i]):
                    continue
                total_mism += 1
                if len(mism) < MAX_SAMPLES:
                    mism.append({"row": _jsonable(kept_index[i]) if i < len(kept_index) else int(i),
                                 "column": column_labels.get(col, str(col)), "dataset": exp.iat[i][:40], "file": out.iat[i][:40]})
        checks.append(_check("cells_match", total_mism == 0,
                             "Every exported cell equals the cleaned dataset." if not total_mism
                             else f"{total_mism} exported cell(s) differ from the cleaned dataset.", total_mism, mism))

    # every change in the audit ledger is in the file
    col_of_label = {}
    for j, c in enumerate(cleaned_df.columns):
        col_of_label.setdefault(column_labels.get(c, str(c)), j)
    cells = ledger.get("cell_changes") or []
    applied = missing = 0
    miss_samples: list = []
    for ch in cells:
        i, j = pos_of_row.get(ch.get("row")), col_of_label.get(ch.get("column"))
        if i is None or j is None:
            continue   # row removed later / column renamed or dropped by a later step
        if _same(ch.get("new"), got.iat[i, j], text_exact=False) or _canon(ch.get("new")) == "" and _canon(got.iat[i, j]) == "":
            applied += 1
        else:
            # a later rule may legitimately have changed the same cell again: compare with the final dataset
            if _same(cleaned_df.iat[i, j], got.iat[i, j]):
                applied += 1
                continue
            missing += 1
            if len(miss_samples) < MAX_SAMPLES:
                miss_samples.append({"row": ch.get("row"), "column": ch.get("column"), "expected": _canon(ch.get("new"))[:40],
                                     "file": _canon(got.iat[i, j])[:40]})
    truncated = bool(ledger.get("truncated"))
    checks.append(_check("changes_applied", missing == 0,
                         f"{applied} ledger change(s) confirmed in the exported file" + (" (ledger was truncated: only the retained entries were checked)" if truncated else "")
                         if not missing else f"{missing} change(s) in the audit ledger are NOT in the exported file.", missing, miss_samples))

    # untouched cells read exactly as uploaded
    if source_text_df is None:
        checks.append(_check("untouched", None, "Skipped: the uploaded text was not available for comparison."))
    elif truncated:
        checks.append(_check("untouched", None, "Skipped: the audit ledger was truncated, so changed cells cannot be told apart from untouched ones."))
    elif too_big:
        checks.append(_check("untouched", None, f"Skipped: above the {VERIFY_MAX_CELLS:,}-cell verification limit."))
    else:
        changed_by_col: dict = {}
        for c in cells:
            changed_by_col.setdefault(c.get("column"), set()).add(c.get("row"))
        bad, n_checked, bad_samples = 0, 0, []
        for j, col in enumerate(cleaned_df.columns):
            sp = source_positions[j] if j < len(source_positions) else None
            if sp is None or sp >= source_text_df.shape[1]:
                continue
            label = column_labels.get(col, str(col))
            skip = changed_by_col.get(label, set())
            keep = [i for i, lab in enumerate(kept_index) if lab not in skip and lab in source_text_df.index]
            if not keep:
                continue
            orig = source_text_df.iloc[:, sp].reindex([kept_index[i] for i in keep]).map(_canon).values
            out = got.iloc[keep, j].map(_canon).values
            n_checked += len(keep)
            for k in np.flatnonzero(orig != out):
                o, f = orig[k], out[k]
                if ext != "csv" and _same(o, f):
                    continue
                if _defuse_cell(o) == f:       # formula-injection guard, reported separately
                    continue
                bad += 1
                if len(bad_samples) < MAX_SAMPLES:
                    bad_samples.append({"row": _jsonable(kept_index[keep[k]]), "column": label, "uploaded": o[:40], "file": f[:40]})
        checks.append(_check("untouched", bad == 0,
                             f"{n_checked} cell(s) the pipeline did not change still read exactly as uploaded." if not bad
                             else f"{bad} cell(s) with no recorded change differ from the upload (export re-formatted them).", bad, bad_samples))

    # revalidation of the file itself
    if validation_after_totals is not None:
        try:
            from processing.pipeline import read_source
            from cleaning.validation import validate_dataframe
            re_totals = validate_dataframe(read_source(out_path, has_header=True), validation_config, include_examples=False)["totals"]
            keys = ("invalid", "suspicious", "unresolved", "missing")
            diff = {k: (validation_after_totals.get(k, 0), re_totals.get(k, 0)) for k in keys
                    if validation_after_totals.get(k, 0) != re_totals.get(k, 0)}
            checks.append(_check("revalidation", not diff,
                                 "Re-reading and validating the exported file gives the same problem counts as the cleaned dataset." if not diff
                                 else "Re-reading the exported file classifies some values differently from the in-memory dataset "
                                      "(dataset, file): " + ", ".join(f"{k} {a}->{b}" for k, (a, b) in diff.items()), len(diff)))
        except Exception as exc:
            checks.append(_check("revalidation", None, f"Skipped: the exported file could not be re-validated ({type(exc).__name__})."))

    notes = []
    if defused:
        notes.append(f"{defused} text cell(s) start with = + - or @ and were prefixed with an apostrophe so spreadsheet apps "
                     "cannot run them as formulas. This is the only intended difference between the dataset and the file.")
    failed = [c for c in checks if c["passed"] is False]
    return {"verified": not failed, "checks": checks, "notes": notes,
            "skipped": [c["check"] for c in checks if c["passed"] is None]}
