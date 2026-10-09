"""
Orchestrates: Read -> Clean -> Export

This file doesn't know the details of any single cleaning rule, that lives in cleaning/rules.py, which is Misumi's spec turned into
code. This file just wires the steps together in order.
"""

import os
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

# The audit returned to the browser is capped so a pathological file cannot produce a
# multi-megabyte response; the totals in audit["summary"] stay exact.
MAX_AUDIT_ENTRIES_RETURNED = 500


class DatasetTooLargeError(ValueError):
    """Raised with a message that is safe to show the user (it contains no file paths)."""


def _read_csv(path: str, **kwargs) -> pd.DataFrame:
    """Uploads are accepted as UTF-8 or Latin-1 text (see utils.file_handler), so reading
    must tolerate both: a legacy Excel "CSV" saved in Windows-1252 used to pass upload
    validation and then crash here with UnicodeDecodeError."""
    try:
        return pd.read_csv(path, encoding="utf-8-sig", **kwargs)
    except UnicodeDecodeError:
        return pd.read_csv(path, encoding="latin-1", **kwargs)


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
        df = pd.read_excel(path, dtype=dtype_overrides, header=header_arg)

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


def run_pipeline(
    job_id: str,
    rules: Optional[List[str]] = None,
    resolutions: Optional[List[dict]] = None,
    has_header: bool = True,
    pro: bool = False,
    profile: Optional[dict] = None,
    cleaning_profile: Optional[dict] = None,
    max_cells: int = 0,
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

    `cleaning_profile` (optional) is a declarative per-column rule configuration, see
    cleaning/engine/. It is parsed and AUTHORIZED here as well as in the API (defence in depth:
    `pro` is the server-derived entitlement frozen into the job, never a client value). It runs
    after resolutions and before the default rule set; columns it configures are held out from the
    default rules that would otherwise re-write its output.
    """
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

    audit = AuditLog()
    labels = {c: c for c in df.columns}  # current column name -> name in the uploaded file

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
        held_out=held_out,
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
    cleaned_df = cleaned_df.reset_index(drop=True)
    rows_out = len(cleaned_df)

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
    export_dataframe(
        cleaned_df,
        out_path,
        ext=out_ext,
        source_path=source_path,
        source_sheet_name=_first_sheet_name(source_path),
        has_header=has_header,
        source_positions=source_positions,
    )

    # Re-run the same read-only quality checks against the cleaned data
    # so the frontend can show a before/after score, not just a list of
    # "N cells changed" counts.
    post_report = generate_report(cleaned_df, provenance={"imputed": imputed})

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
    return result
