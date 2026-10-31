"""
Orchestrates: Read -> Clean -> Export

This file doesn't know the details of any single cleaning rule, that lives in cleaning/rules.py, which is Misumi's spec turned into
code. This file just wires the steps together in order.
"""

import logging
import os
import re
from typing import Optional, List
import pandas as pd

from config import Config
from utils.file_handler import find_source_file, cleaned_file_path
from cleaning import model as M
from cleaning.audit import AuditLog
from cleaning.rules import apply_rules
from cleaning.resolutions import apply_resolutions
from cleaning.quality_report import generate_report
from cleaning.summary import build_cleaning_summary
from export.exporter import export_dataframe

logger = logging.getLogger("omixa.pipeline")

# The audit returned to the browser is capped so a pathological file cannot produce a
# multi-megabyte response; the totals in audit["summary"] stay exact.
MAX_AUDIT_ENTRIES_RETURNED = 500


class DatasetTooLargeError(ValueError):
    """Raised with a message that is safe to show the user (it contains no file paths)."""


# pandas' default na_values silently turns "N/A", "NULL", "NA", "None", "nan" ... into NaN BEFORE any Omixa
# rule runs. That (a) erased which marker the user had (no audit entry, "missing before" under-reported what
# was converted) and (b) turned whole integer columns into float64, so valid values were exported as "45.0".
# Only a truly empty cell is missing at read time; every marker reaches the missing-token rule intact.
_NA_KWARGS = {"keep_default_na": False, "na_values": [""]}


def _read_csv(path: str, **kwargs) -> pd.DataFrame:
    """Uploads are accepted as UTF-8 or Latin-1 text (see utils.file_handler), so reading
    must tolerate both: a legacy Excel "CSV" saved in Windows-1252 used to pass upload
    validation and then crash here with UnicodeDecodeError."""
    try:
        return pd.read_csv(path, encoding="utf-8-sig", **_NA_KWARGS, **kwargs)
    except UnicodeDecodeError:
        return pd.read_csv(path, encoding="latin-1", **_NA_KWARGS, **kwargs)


def _sheet_extent(path: str, ext: str, ncols_hint: int, max_rows: int, cell_cap: int = 0):
    """(rows, cols) of the FIRST sheet without loading it into pandas, or None if unknown.

    Memory guard: pd.read_excel materialises the whole sheet before any size check can run, so a
    big workbook used to be fully loaded and only then rejected. This answers "is it too big?"
    from the sheet's declared dimension (cheap), and, when a writer omitted it, by streaming rows
    with a hard stop at the limit (constant memory). A lying dimension is bounded by the xlsx
    decompression guard and, in workers, by the job's address-space cap."""
    try:
        if ext == "xlsx":
            import openpyxl
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            try:
                ws = wb.worksheets[0]
                rows, cols = ws.max_row, ws.max_column
                if rows and cols:
                    return int(rows), int(cols)
                count, widest = 0, 0
                for row in ws.iter_rows(values_only=True):
                    count += 1
                    widest = max(widest, len(row))
                    if count > max_rows + 1 or count * max(widest, ncols_hint, 1) > (cell_cap or Config.MAX_CELLS) + max(widest, 1):
                        break
                return count, widest
            finally:
                wb.close()
        if ext == "xls":
            import xlrd
            book = xlrd.open_workbook(path, on_demand=True)
            try:
                sh = book.sheet_by_index(0)
                return int(sh.nrows), int(sh.ncols)
            finally:
                book.release_resources()
    except Exception:
        return None  # unknown: fall through to the post-load check
    return None


def read_source(path: str, has_header: bool = True, max_cells: int = 0) -> pd.DataFrame:
    """
    Reads the uploaded file, and specifically protects
    identifier-looking columns (phone/account/IBAN/BVN/zip/etc., see detectors.is_identifier_name) from pandas' own dtype
    inference, which runs before any cleaning rule does and would
    otherwise silently drop a leading zero (e.g. a CSV column of
    phone numbers gets read as int64 by default, "0803317157"
    becomes 803317157, and there's no getting that zero back later).
    Reading those specific columns as raw strings up front is what
    lets the later identifier-aware cleaning rules do their job.

    has_header: whether the first row is a header row. Omixa never
    decides this on its own, it's whatever the caller (ultimately the
    user, see routes/process.py's "has_header") says it is. When
    False, the first row is treated as ordinary data and the columns
    are given generic placeholder names (column_1, column_2, ...).

    Raises DatasetTooLargeError (safe message) when the file exceeds the configured
    row/column ceilings.
    """
    from cleaning import detectors  # local import: avoids a cycle with rules.py at module load time

    ext = path.rsplit(".", 1)[1].lower()
    header_arg = 0 if has_header else None

    if ext == "csv":
        header = _read_csv(path, nrows=0, header=header_arg)
    else:
        header = pd.read_excel(path, nrows=0, header=header_arg)

    if len(header.columns) > Config.MAX_COLUMNS:
        raise DatasetTooLargeError(
            f"This file has more than {Config.MAX_COLUMNS} columns, which is more than OMIXA will process."
        )

    dtype_overrides = {
        col: str for col in header.columns if detectors.is_identifier_name(str(col))
    } or None

    # Rows x columns ceiling (a 500-column file must not be allowed 500k rows).
    cell_cap = min(Config.MAX_CELLS, max_cells) if max_cells else Config.MAX_CELLS
    ncols = max(1, len(header.columns))
    max_rows = min(Config.MAX_ROWS, max(1, cell_cap // ncols))
    cell_limited = max_rows < Config.MAX_ROWS

    def _too_large():
        if cell_limited:
            return DatasetTooLargeError(
                f"This file has more than {cell_cap:,} cells ({ncols} columns x more than "
                f"{max_rows:,} rows), which is more than OMIXA will process."
            )
        return DatasetTooLargeError(
            f"This file has more than {Config.MAX_ROWS:,} rows, which is more than OMIXA will process."
        )

    if ext == "csv":
        df = _read_csv(path, dtype=dtype_overrides, header=header_arg, nrows=max_rows + 1)
    else:
        extent = _sheet_extent(path, ext, ncols, max_rows, cell_cap)
        if extent is not None:
            data_rows = extent[0] - (1 if has_header else 0)
            if data_rows > max_rows:
                raise _too_large()  # rejected BEFORE pandas loads the sheet
        df = pd.read_excel(path, dtype=dtype_overrides, header=header_arg, **_NA_KWARGS)

    if len(df) > max_rows:
        raise _too_large()

    if not has_header:
        df.columns = [f"column_{i + 1}" for i in range(len(df.columns))]

    return df


def _first_sheet_name(path: str) -> Optional[str]:
    """Name of the sheet pd.read_excel(..., sheet_name=0) actually
    read, used so export.exporter can put the cleaned data back into
    that same sheet while leaving every other sheet in the workbook
    alone. Returns None for anything that isn't a readable .xlsx
    (e.g. a .csv, or a .xls openpyxl can't open), the exporter falls
    back to a plain export in that case."""
    if path.rsplit(".", 1)[-1].lower() != "xlsx":
        return None
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True)
        return wb.sheetnames[0]
    except Exception:
        return None


def _compact_validation(v: dict) -> dict:
    """Counts only (no example values): small enough to ship with every cleaning result."""
    return {"row_count": v["row_count"], "totals": v["totals"], "needs_attention": v["needs_attention"],
            "columns": [{"column": c["column"], "effective_type": c["effective_type"], "type_status": c["type_status"],
                         "counts": c["counts"], "fully_valid": c["fully_valid"]} for c in v["columns"]]}


def _missing_tracking(missing_before: dict, audit, *, labels_final: dict, cleaned_df, rows_in: int) -> dict:
    """Why a column's missing count moved. For every column of the uploaded file:
         originally_missing   cells already empty in the upload
         converted_to_missing cells that had a value and were emptied by a rule/resolution (with the rule)
         filled                cells that were empty and got a value (imputation: an ESTIMATE, not the original)
         missing_after        empty cells in the cleaned file
       A lower missing_after is NOT evidence of better data when `filled` is non-zero, and a higher one is
       explained by converted_to_missing, never unexplained."""
    after_by_orig = {}
    for cleaned_name in cleaned_df.columns:
        orig = labels_final.get(cleaned_name, cleaned_name)
        after_by_orig[str(orig)] = int(cleaned_df[cleaned_name].isna().sum())
    out = {}
    for col, before in missing_before.items():
        flow = audit.missing_flow.get(col, {})
        out[col] = {
            "originally_missing": before,
            "converted_to_missing": flow.get("converted_to_missing", 0),
            "filled": flow.get("filled_from_missing", 0),
            "missing_after": after_by_orig.get(col),
            "by_rule": flow.get("by_rule", {}),
            "rows_removed_note": audit.removed_rows_total or None,
        }
    return out


_PLAIN_NUMBER_TEXT = re.compile(r"^[+-]?\d+(?:\.\d+)?$")


def _render_floats_for_csv(cleaned_df, *, source_text_df, kept_index, cleaned_labels, source_positions, ledger):
    """A float64 column prints every whole number as "7.0" ("45" -> "45.0", the original reported bug) because
    one decimal elsewhere in the column forces the float dtype. Floats are therefore written with minimal
    formatting, and a cell the pipeline did NOT change gets its exact uploaded text back ("7", "7.0", "45.50"
    stay as typed). Only float columns are touched, and only the CSV text; the dataset itself is unchanged."""
    float_cols = [c for c in cleaned_df.columns if pd.api.types.is_float_dtype(cleaned_df[c])]
    if not float_cols:
        return cleaned_df
    changed = {(c.get("row"), c.get("column")) for c in ledger.get("cell_changes", [])}
    can_restore = source_text_df is not None and not ledger.get("truncated")
    out = cleaned_df.copy()
    for col in float_cols:
        j = list(cleaned_df.columns).index(col)
        sp = source_positions[j] if j < len(source_positions) else None
        label = cleaned_labels.get(col, str(col))
        orig = None
        if can_restore and sp is not None and sp < source_text_df.shape[1]:
            orig = source_text_df.iloc[:, sp].reindex(kept_index).tolist()
        rendered = []
        for i, v in enumerate(cleaned_df[col].tolist()):
            if v is None or v != v:
                rendered.append(None)
                continue
            text = f"{v:.15g}"
            if orig is not None and (kept_index[i], label) not in changed:
                o = orig[i]
                if isinstance(o, str) and _PLAIN_NUMBER_TEXT.match(o.strip()) and float(o) == v:
                    text = o.strip()
            rendered.append(text)
        out[col] = pd.Series(rendered, index=out.index, dtype="object")
    return out


def _localize_row_examples(report, *, kept_index, has_header: bool) -> None:
    """Row numbers in findings that quote rows (related-column contradictions). A report generated from the
    cleaned frame counts positions in the CLEANED data; map them back to the row numbers of the file the user
    uploaded (keeping the cleaned position as `cleaned_record`), so the before and after reports talk about
    the same rows even when duplicates were removed in between. `source_row` is the spreadsheet row number
    (counting the header row only when the file has one)."""
    off = 2 if has_header else 1
    for f in (report or {}).get("findings", []):
        for ex in f.get("examples") or []:
            rec = ex.get("record")
            if not isinstance(rec, int):
                continue
            if kept_index is not None and 0 < rec <= len(kept_index) and isinstance(kept_index[rec - 1], int):
                ex["cleaned_record"] = rec
                rec = kept_index[rec - 1] + 1
                ex["record"] = rec
            ex["source_row"] = rec - 1 + off


def _cleaned_keys(keys, cleaned_labels):
    """Duplicate key columns are named as in the uploaded file; the cleaned frame may have renamed them."""
    if not keys:
        return keys
    back = {}
    for cleaned, uploaded in cleaned_labels.items():
        back.setdefault(str(uploaded), cleaned)
    return [back.get(k, k) for k in keys]


def _resolve_locks(df, leave_as_is, resolutions, engine_profile):
    """Validates the user's "Leave as is" list against the real columns and reports conflicts.

    Returns (locked column names, report). Locking never depends on the frontend having sent a
    perfectly matching name: exact match first, then a case/space/underscore-insensitive match that must
    be unique. Anything else is reported as unmatched instead of being ignored."""
    report = {"requested": list(leave_as_is or []), "locked": [], "unmatched": [], "conflicts": []}
    if not leave_as_is:
        return set(), report
    from cleaning.engine.executor import _norm
    exact = {str(c): c for c in df.columns}
    by_norm = {}
    for c in df.columns:
        by_norm.setdefault(_norm(c), []).append(c)
    locked = set()
    for name in leave_as_is:
        if not isinstance(name, str):
            report["unmatched"].append(str(name))
        elif name in exact:
            locked.add(exact[name])
        elif len(by_norm.get(_norm(name), [])) == 1:
            locked.add(by_norm[_norm(name)][0])
        else:
            report["unmatched"].append(name)
    report["locked"] = sorted(map(str, locked))
    for r in resolutions or []:
        if isinstance(r, dict) and r.get("column") in locked and r.get("choice") not in (None, "skip"):
            report["conflicts"].append({
                "column": r["column"], "kind": "resolution_on_locked_column",
                "detail": f"'{r['column']}' is set to Leave as is, but you also chose '{r.get('choice')}' for it "
                          f"({r.get('issue')}). The explicit choice was applied; automatic rules were not."})
    if engine_profile is not None:
        from cleaning.engine.executor import resolve_columns
        for pcol in list(engine_profile.columns):
            try:
                real = resolve_columns(df, [pcol])[pcol]
            except Exception:
                continue
            if real in locked:
                del engine_profile.columns[pcol]
                report["conflicts"].append({
                    "column": str(real), "kind": "column_rules_skipped",
                    "detail": f"Column rules for '{real}' were not run because the column is set to Leave as is."})
    return locked, report


def _run_pipeline_impl(
    job_id: str,
    rules: Optional[List[str]] = None,
    resolutions: Optional[List[dict]] = None,
    has_header: bool = True,
    pro: bool = False,
    profile: Optional[dict] = None,
    cleaning_profile: Optional[dict] = None,
    max_cells: int = 0,
    leave_as_is: Optional[List[str]] = None,
    options: Optional[dict] = None,
) -> dict:
    """
    Returns a summary dict the frontend can display, e.g.:
        {
          "rows_in": 1000,
          "rows_out": 940,
          "rules_applied": ["missing_values", "duplicates"],
          "changes": {"duplicates_removed": 40, "missing_values_fixed": 20}
        }

    `resolutions` (optional) is a list of user-picked fixes for
    findings that no default rule can safely auto-apply on its own, e.g. "treat this date column as day-first". These are applied
    AFTER the standard rule set, and only ever touch the specific
    column/issue the user explicitly chose; see
    cleaning/resolutions.py. Omitting this argument (or passing an
    empty list) behaves exactly as before this existed.

    `has_header` (optional, default True) is whether the file's first
    row is a header row, see read_source()'s docstring, it's a caller
    choice, never an Omixa assumption.

    `leave_as_is` (optional) is a list of column names (as they appear in the uploaded file) the user
    chose to "Leave as is". Those columns are locked: no automatic rule, no column rule and no
    imputation may change a single value in them. Names that do not match a column are NOT ignored
    silently, they come back in result["column_options"]["unmatched"]. An explicit resolution the user
    picked for a locked column still runs (it is their own direct instruction) and is reported as a
    conflict so the contradiction is visible.

    `cleaning_profile` (optional) is a declarative per-column rule configuration, see
    cleaning/engine/. It is parsed and AUTHORIZED here as well as in the API (defence in depth:
    `pro` is the server-derived entitlement frozen into the job, never a client value). It runs
    after resolutions and before the default rule set; columns it configures are held out from the
    default rules that would otherwise re-write its output.
    """
    from cleaning.options import parse_options, validation_config
    options = parse_options(options)   # whitelist + bounds, again here (defence in depth: the queue payload is data)
    vconfig = validation_config(options)
    if options.get("number_format") in ("thousands_comma", "decimal_comma"):
        vconfig = {**(vconfig or {}), "defaults": {**((vconfig or {}).get("defaults") or {}), "number_format": options["number_format"]}}
    engine_profile = None
    if cleaning_profile:
        from cleaning.engine import authorize_profile, parse_profile
        engine_profile = parse_profile(cleaning_profile)
        authorize_profile(engine_profile, is_pro=bool(pro))

    source_path = find_source_file(job_id)
    if not source_path:
        raise FileNotFoundError("No source file found for this job")

    df = read_source(source_path, has_header=has_header, max_cells=max_cells)
    rows_in = len(df)
    columns_in = len(df.columns)
    # What the file said, for the post-export check "cells we did not change still read as uploaded".
    # CSV: the raw text (so "45" can never silently become "45.0"); Excel: the typed values as read.
    source_ext = source_path.rsplit(".", 1)[1].lower()
    try:
        source_text_df = (_read_csv(source_path, dtype=str, header=0 if has_header else None)
                          if source_ext == "csv" else df.copy())
        if source_ext == "csv" and (source_text_df.shape != df.shape):
            source_text_df = None
    except Exception:
        source_text_df = None
    from cleaning import detectors
    from cleaning.duplicates import analyze_duplicates
    # The number convention of each column as the UPLOADED data shows it. Once "3,45" has been normalised the
    # evidence is gone from the cleaned data, so the final validation is told what the file said.
    inferred_formats = {}
    for _c in df.columns:
        if not pd.api.types.is_numeric_dtype(df[_c]):
            _f = detectors.column_number_format([v for v in df[_c].dropna().tolist() if isinstance(v, str)])
            if _f != "unknown":
                inferred_formats[str(_c)] = _f
    duplicates_before = analyze_duplicates(df, options.get("duplicate_keys"), has_header=has_header)
    # Pro only: keep the uploaded data so a Quality Profile can be evaluated before AND after cleaning.
    original_df = df.copy() if (pro and profile) else None

    # Original (pre-resolution, pre-rename) column -> source-sheet
    # position, 0-indexed. Captured now because it's the only point
    # where df's column names are still exactly what read_source saw
    # in the file, column 0 really is source column 0, etc. Used
    # below to build source_positions for the exporter, see its
    # docstring for why this is needed for per-column style
    # preservation (cleaning/formatting.py).
    original_column_positions = {name: i for i, name in enumerate(df.columns)}

    # Read-only, and run BEFORE any rule touches df, so this is
    # guaranteed to reflect the file exactly as uploaded, the same
    # report /api/report/<job_id> would return for this file.
    quality_before = generate_report(df)
    _localize_row_examples(quality_before, kept_index=None, has_header=has_header)
    from cleaning.validation import validate_dataframe
    flagged_before: dict = {}   # {column: {row label: kind}}: lets the final report separate "fixed" from "row removed"
    validation_before = _compact_validation(validate_dataframe(df, vconfig, include_examples=False, collect_flagged=flagged_before))
    missing_before = {str(c): int(df[c].isna().sum()) for c in df.columns}

    audit = AuditLog()
    labels = {c: c for c in df.columns}  # current column name -> name in the uploaded file

    locked, column_options = _resolve_locks(df, leave_as_is, resolutions, engine_profile)

    # Resolutions are applied FIRST, against the exact column names
    # generate_report() (and therefore the frontend, and therefore
    # the caller's "resolutions" payload) used, e.g. "Signup Date",
    # not the rule-cleaned "signup_date". apply_rules' own
    # column_names step renames columns as one of its first actions,
    # so running resolutions after it would make every resolution's
    # "column" value stale and silently match nothing.
    df, resolution_log = apply_resolutions(df, resolutions, audit=audit, column_labels=labels)

    # df.columns here are still the ORIGINAL header names (minus anything a resolution
    # just dropped) in their original relative order, so this list is, positionally,
    # exactly which source column each surviving cleaned_df column will end up as,
    # which is what the exporter needs to look up "this output column's original
    # font/size/bold/italic/width" even after apply_rules renames every header.
    source_positions = [original_column_positions[name] for name in df.columns]
    columns_after_resolutions = list(df.columns)

    engine_report = None
    held_out = None
    if engine_profile is not None:
        from cleaning.engine import run_profile
        df, engine_report = run_profile(df, engine_profile, audit=audit, labels=labels)
        held_out = {c: set(v) for c, v in (engine_report.get("supersedes") or {}).items()} or None

    cleaned_df, change_log = apply_rules(
        df, rules=rules, audit=audit, column_labels=labels, protected_blank=resolution_log.get("blanked"),
        held_out=held_out, locked_columns=locked, options=options,
    )

    # Provenance: cells holding an ESTIMATE rather than an observation. Used so the
    # after-report credits completeness without pretending accuracy improved.
    imputed = dict(change_log.get("imputed") or {})
    if resolution_log.get("imputed") and len(columns_after_resolutions) == len(cleaned_df.columns):
        rename_map = dict(zip(columns_after_resolutions, cleaned_df.columns))
        for col, n in resolution_log["imputed"].items():
            tgt = rename_map.get(col)
            if tgt:
                imputed[tgt] = imputed.get(tgt, 0) + n

    # Row identity was kept as the ORIGINAL index through the whole run (for the audit);
    # export wants a plain 0..n-1 index.
    kept_index = [i.item() if hasattr(i, "item") else i for i in cleaned_df.index]
    cleaned_df = cleaned_df.reset_index(drop=True)
    rows_out = len(cleaned_df)
    cleaned_labels = dict(change_log.get("column_labels") or {})   # cleaned name -> name in the uploaded file

    ext = source_path.rsplit(".", 1)[1].lower()
    # .xls (legacy binary Excel) can be READ via xlrd, but pandas has
    # no maintained engine to WRITE it back out, the only library
    # that ever did (xlwt) has been unmaintained for years and isn't
    # a dependency here. So a .xls upload is still fully supported,
    # it just always comes back out as .xlsx, which every modern
    # spreadsheet app opens fine. It also means a .xls source can
    # never keep its other sheets (openpyxl can't open .xls at all,
    # see _first_sheet_name()), only a .xlsx source can.
    out_ext = "xlsx" if ext == "xls" else ext
    out_path = cleaned_file_path(job_id, out_ext)
    export_frame = cleaned_df
    if out_ext == "csv":
        export_frame = _render_floats_for_csv(
            cleaned_df, source_text_df=source_text_df, kept_index=kept_index, cleaned_labels=cleaned_labels,
            source_positions=source_positions, ledger=audit.change_log())
    export_dataframe(
        export_frame,
        out_path,
        ext=out_ext,
        source_path=source_path,
        source_sheet_name=_first_sheet_name(source_path),
        has_header=has_header,
        source_positions=source_positions,
    )

    # Check the file that was actually written, not just the dataframe that was meant to be written.
    from export.verify import verify_export
    vconfig_after = vconfig
    if inferred_formats:
        vconfig_after = {**(vconfig or {}), "rules": {**((vconfig or {}).get("rules") or {})}}
        for _c in cleaned_df.columns:
            _f = inferred_formats.get(str(cleaned_labels.get(_c, _c)))
            if _f:
                vconfig_after["rules"][str(_c)] = {**vconfig_after["rules"].get(str(_c), {}), "inferred_number_format": _f}
    validation_after_full = validate_dataframe(cleaned_df, vconfig_after, include_examples=False)
    try:
        export_verification = verify_export(
            out_path=out_path, ext=out_ext, cleaned_df=cleaned_df, kept_index=kept_index, column_labels=cleaned_labels,
            source_text_df=source_text_df, source_positions=source_positions, ledger=audit.change_log(),
            validation_after_totals=validation_after_full["totals"], validation_config=vconfig_after,
            sheet_name=_first_sheet_name(source_path) if out_ext == "xlsx" else None)
    except Exception:
        import logging
        logging.getLogger("omixa.export").exception("export verification crashed for job %s", job_id)
        export_verification = {"verified": False, "notes": [], "checks": [
            {"check": "verification", "passed": False, "detail": "The exported file could not be verified.", "count": 0, "samples": []}]}
    source_text_df = None

    # Re-run the same read-only quality checks against the cleaned data
    # so the frontend can show a before/after score, not just a list of
    # "N cells changed" counts.
    post_report = generate_report(cleaned_df, provenance={"imputed": imputed})
    _localize_row_examples(post_report, kept_index=kept_index, has_header=has_header)
    validation_after = _compact_validation(validation_after_full)

    cleaning_summary = build_cleaning_summary(
        rows_in=rows_in,
        rows_out=rows_out,
        column_count=len(cleaned_df.columns) or columns_in,
        change_log=change_log,
        quality_before=quality_before,
        quality_after=post_report,
        resolution_log=resolution_log,
        audit=audit,
        imputed=imputed,
    )

    audit_entries = audit.public_entries()

    pro_block = None
    if pro:
        from proquality.analysis import build_pro_block
        from proquality.profiles import evaluate_profile
        profile_before = evaluate_profile(original_df, profile, quality_before.get("score")) if profile else None
        profile_after = evaluate_profile(cleaned_df, profile, post_report.get("score")) if profile else None
        pro_block = build_pro_block(
            before=quality_before, after=post_report,
            summary_core={"rows_in": rows_in, "rows_out": rows_out, "rules_applied": change_log["rules_applied"]},
            audit_entries=audit_entries, profile=profile, profile_before=profile_before,
            profile_after=profile_after, cols_in=columns_in,
        )
    original_df = None

    rules_applied = list(change_log["rules_applied"]) + (["column_rules"] if engine_report else [])
    result = {
        "rows_in": rows_in,
        "rows_out": rows_out,
        "rules_applied": rules_applied,
        "changes": change_log["changes"],
        "details": change_log.get("details", {}),
        "resolutions_applied": resolution_log["applied"],
        "resolutions_skipped": resolution_log["skipped"],
        "quality_report": post_report,
        "quality_report_before": quality_before,
        "cleaning_summary": cleaning_summary,
        "quality_comparison": cleaning_summary.get("quality_comparison"),
        "audit": {
            "summary": audit.summary(),
            "entries": audit_entries[:MAX_AUDIT_ENTRIES_RETURNED],
            "entries_truncated": len(audit_entries) > MAX_AUDIT_ENTRIES_RETURNED,
        },
        "pro": pro_block,
    }
    if engine_report is not None:
        result["cleaning_engine"] = engine_report
    result["column_options"] = column_options
    # Proposed (never applied) mappings for review, e.g. "done" -> "Completed". The user approves or rejects each one
    # by re-submitting with options.value_mappings; a rejected pair stays visible as rejected, a distinct category.
    from cleaning.review_options import suggest_value_mappings
    _rejected = {(col, str(r).strip().casefold()) for col, m in (options.get("value_mappings") or {}).items()
                 for r in (m.get("reject") or [])}
    result["value_mapping_suggestions"] = [
        {**sg, "status": "rejected" if (sg["column"], sg["from"].strip().casefold()) in _rejected else sg["status"]}
        for sg in suggest_value_mappings(cleaned_df, cleaned_labels)]
    result["validation_before"] = validation_before
    result["validation_after"] = validation_after
    change_log_out = audit.change_log()
    row_offset = 2 if has_header else 1   # 0-based row label -> the row number the user sees in their spreadsheet
    for entry in change_log_out["cell_changes"]:
        if isinstance(entry.get("row"), int):
            entry["source_row"] = entry["row"] + row_offset
    for entry in change_log_out["removed_rows"]:
        if isinstance(entry.get("row"), int):
            entry["source_row"] = entry["row"] + row_offset
        if isinstance(entry.get("duplicate_of"), int):
            entry["duplicate_of_source_row"] = entry["duplicate_of"] + row_offset
    result["change_log"] = change_log_out
    result["missing_tracking"] = _missing_tracking(missing_before, audit, labels_final=cleaned_labels,
                                                   cleaned_df=cleaned_df, rows_in=rows_in)

    from cleaning.integrity import build_integrity_report, issues_removed_with_rows
    removed_with_rows = issues_removed_with_rows(flagged_before, kept_index)
    duplicates_after = analyze_duplicates(cleaned_df, _cleaned_keys(options.get("duplicate_keys"), cleaned_labels),
                                          has_header=has_header)
    # row labels of the cleaned frame are 0..n-1; show the position the user sees in the exported file
    result["export_verification"] = export_verification
    result["integrity_report"] = build_integrity_report(
        rows_in=rows_in, rows_out=rows_out, cols_in=columns_in, cols_out=len(cleaned_df.columns),
        validation_before=validation_before, validation_after=validation_after, cleaned_labels=cleaned_labels,
        missing_tracking=result["missing_tracking"], audit=audit, details=change_log["details"],
        rules_applied=change_log["rules_applied"], duplicates_before=duplicates_before, duplicates_after=duplicates_after,
        quality_before=quality_before, quality_after=post_report, export_verification=export_verification,
        removed_with_rows=removed_with_rows, resolution_imputed=resolution_log.get("imputed"))
    ir = result["integrity_report"]
    # Row-count reconciliation: every record that left the dataset must be explained by an audited,
    # rule-attributed removal. A mismatch is never accepted silently (see docs/CLEANING_ENGINE.md).
    removed_audited = int(audit.removed_rows_total or 0)
    unexplained = (rows_in - rows_out) - removed_audited
    ir["row_reconciliation"] = ({
        "retained": rows_out,
        "removed_audited": removed_audited,
        "unexplained": unexplained,
        "original": rows_in,
        "cells_modified": int(audit.cell_changes_total or 0),
    })
    if unexplained != 0:
        logger.error("row-count mismatch for job %s: in=%s out=%s audited_removed=%s",
                     job_id, rows_in, rows_out, removed_audited)
        ir["warnings"].append(
            f"Row count changed from {rows_in} to {rows_out} but only {removed_audited} removal(s) are recorded "
            f"in the change history ({abs(unexplained)} unexplained). Do not rely on this export until reviewed.")
        ir["status"] = "requires_manual_review"
        ir["fully_clean"] = False
        result["processing_warnings"] = list(result.get("processing_warnings") or []) + [
            {"code": "unexplained_row_count_change", "rows_in": rows_in, "rows_out": rows_out,
             "audited_removed": removed_audited, "unexplained": unexplained}]
    # ONE figure for "invalid values detected": the integrity report's (computed from validating the upload and the
    # final file). The summary used to count the number of FINDINGS in the cleaned report instead, so the two
    # screens disagreed for the same file (e.g. 1 vs 7).
    _iv = ir.get("invalid_values") or {}
    if isinstance(_iv.get("detected"), int):
        _old = cleaning_summary["counts"].get("invalid_values_detected")
        cleaning_summary["counts"]["invalid_values_detected"] = _iv["detected"]
        cleaning_summary["counts"]["invalid_values_remaining"] = int(_iv.get("remaining", 0) or 0) if "remaining" in _iv else (
            sum((_iv.get("by_kind_after") or {}).values()))
        if isinstance(cleaning_summary.get("text"), str):
            cleaning_summary["text"] = cleaning_summary["text"].replace(
                f"Invalid values detected: {_old}", f"Invalid values detected: {_iv['detected']}", 1)
    cleaning_summary["integrity"] = {"status": ir["status"], "fully_clean": ir["fully_clean"], "export_verified": ir["export_verified"]}
    # The result screen may only say "Your data is clean" when BOTH the quality findings and the integrity report agree.
    _review = (cleaning_summary.get("quality_comparison") or {}).get("review")
    if _review is not None and _review["clean"] and not ir["fully_clean"]:
        _notes = [str(w) for w in (ir.get("warnings") or [])] or ["The integrity report found something to confirm."]
        _review["clean"] = False
        _review["count"] = len(_notes)
        _review["items"] = [{"title": "Needs confirmation", "column": None, "count": 1, "unit": "file", "level": "medium",
                             "severity": "warning", "detail": w, "suggestion": "", "remediation": "requires_review",
                             "optional": False} for w in _notes]
        _review["headline"] = (f"Cleaned, {len(_notes)} item still needs your review" if len(_notes) == 1
                               else f"Cleaned, {len(_notes)} items still need your review")
    if isinstance(cleaning_summary.get("text"), str):
        label = {"successfully_cleaned": "Successfully cleaned", "partially_cleaned": "Partially cleaned",
                 "requires_manual_review": "Requires manual review"}[ir["status"]]
        extra = [f"\nOutcome: {label}" + (" (fully clean)" if ir["fully_clean"] else " (NOT fully clean)")]
        extra += [f"  - {w}" for w in ir["warnings"]]
        cleaning_summary["text"] += "\n".join(extra)
    return result


def run_pipeline(*args, **kwargs) -> dict:
    """Public entry point (signature and behaviour unchanged: see _run_pipeline_impl).

    Scopes the user's `not_missing` option ("None" is a real value here) to THIS job only. The setting lives in a
    ContextVar, so jobs running at the same time in other threads can never see it, and it is reset when the job
    ends, whether it succeeds or raises."""
    import inspect
    from cleaning import detectors
    from cleaning.options import parse_options
<<<<<<< HEAD
    protected = []
    try:
        raw = inspect.signature(_run_pipeline_impl).bind(*args, **kwargs).arguments.get("options")
        protected = parse_options(raw).get("not_missing", [])
    except Exception:
        protected = []          # a malformed option is rejected, with the proper message, by the implementation itself
    with detectors.keep_as_values(protected):
=======
    from cleaning import consistency
    protected, consistency_cfg = [], {}
    try:
        raw = inspect.signature(_run_pipeline_impl).bind(*args, **kwargs).arguments.get("options")
        parsed = parse_options(raw)
        protected, consistency_cfg = parsed.get("not_missing", []), parsed.get("consistency", {})
    except Exception:
        pass          # a malformed option is rejected, with the proper message, by the implementation itself
    with detectors.keep_as_values(protected), consistency.configured(consistency_cfg):
>>>>>>> 302fe70 (changes)
        return _run_pipeline_impl(*args, **kwargs)


run_pipeline.__doc__ = (run_pipeline.__doc__ or "") + "\n\n" + (_run_pipeline_impl.__doc__ or "")
