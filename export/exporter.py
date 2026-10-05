from __future__ import annotations

import re
from typing import List, Optional
import pandas as pd

from cleaning.formatting import extract_column_style_profile, DEFAULT_FONT_NAME, DEFAULT_FONT_SIZE

# Characters that Excel/Sheets/LibreOffice treat as "this cell is a
# formula" if they're the FIRST character of a cell's text. A CSV or
# XLSX round-tripped through this tool carries whatever the original
# uploader typed, including, potentially, a malicious formula like
# `=cmd|'/c calc'!A1` or `=HYPERLINK("http://evil","click")`, and
# since export is the last thing this app does with the data before
# handing it back to (possibly a different) user to open in a
# spreadsheet app, this is the point where that has to be neutralized.
# See OWASP's CSV Injection guidance.
_FORMULA_TRIGGER_CHARS = ("=", "+", "-", "@", "\t", "\r")


# A leading + or - followed only by digits (optionally one decimal point) is a
# plain signed number or an E.164-style phone number, not a formula: there is no
# function call, reference, operator or separator an attacker could use. Prefixing
# these with an apostrophe silently corrupts legitimate data ("-5", "+2348061234567")
# and an apostrophe is literally shown in CSV viewers, so they are exempt.
_PLAIN_SIGNED_NUMBER_RE = re.compile(r"^[+-]\d+(?:\.\d+)?$")


def _defuse_cell(value):
    """Prefixes a leading formula-trigger character with a single
    quote, which every major spreadsheet app renders as "force this
    cell to be treated as plain text", the standard mitigation.
    Leaves non-strings (numbers, booleans, NaN) untouched; only text
    cells can carry a formula payload."""
    if isinstance(value, str) and value[:1] in _FORMULA_TRIGGER_CHARS:
        if _PLAIN_SIGNED_NUMBER_RE.match(value):
            return value
        return "'" + value
    return value


def sanitize_formula_injection(df: pd.DataFrame) -> pd.DataFrame:
    """Applies _defuse_cell to every text column. Numeric/boolean/
    datetime columns are skipped entirely, a formula payload can
    only live in a text cell, and this avoids ever touching a
    genuinely numeric value (e.g. a real -5)."""
    text_columns = df.select_dtypes(include=["object", "string"]).columns
    if len(text_columns) == 0:
        return df
    df = df.copy()
    for col in text_columns:
        df[col] = df[col].map(_defuse_cell)
    return df


def export_dataframe(
    df: pd.DataFrame,
    out_path: str,
    ext: str,
    source_path: str | None = None,
    source_sheet_name: str | None = None,
    has_header: bool = True,
    source_positions: Optional[List[int]] = None,
) -> None:
    """Writes the cleaned data to out_path.

    Formatting (see the 16-17.09.2026 testing/survey reports): when
    the source is an .xlsx, each output column is styled to match the
    MAJORITY font/size/bold/italic and a content-fitted width already
    found in that same column of the original file - see
    cleaning/formatting.py. This is deliberately not "preserve every
    cell's exact original style" (that would just re-import the
    inconsistency the user uploaded the file to fix) and not "force
    one sheet-wide default" (the old MVP behaviour the reports
    flagged, e.g. every cell forced to Calibri 11) - it's per-column
    standardization, the middle ground both reports asked for. A
    column with no resolvable original style (e.g. it's new, or the
    source wasn't an .xlsx) falls back to the library default.

    `has_header` and `source_positions` are only used for this
    styling step. `source_positions` is the list, positionally
    aligned with `df.columns`, of each surviving column's ORIGINAL
    position (0-indexed) in the source sheet - see
    processing/pipeline.py, which is what lets a column dropped by a
    resolution, or renamed by the column_names rule, still be traced
    back to the right original column's style. Omitting it (or
    passing a source that isn't an .xlsx) just means no style profile
    is available, so columns fall back to the default.

    What's also handled: if the upload was itself an .xlsx with more
    than one sheet, every sheet OTHER than the one Omixa actually
    read and cleaned is carried through to the output completely
    untouched, byte-for-byte as it was in the source file, instead of
    silently disappearing (see source_path/source_sheet_name below).
    """
    df = sanitize_formula_injection(df)
    # Header cells are written as text too, and openpyxl turns any string starting with "="
    # into a live formula, so a malicious column name must be defused like a cell value.
    df = df.copy()
    df.columns = [_defuse_cell(str(c)) for c in df.columns]

    if ext == "csv":
        df.to_csv(out_path, index=False)
        return

    if source_path and source_path.rsplit(".", 1)[-1].lower() == "xlsx":
        if _export_excel_preserving_other_sheets(
            df, out_path, source_path, source_sheet_name, has_header, source_positions
        ):
            return

    df.to_excel(out_path, index=False)


def _export_excel_preserving_other_sheets(
    df: pd.DataFrame,
    out_path: str,
    source_path: str,
    source_sheet_name: str | None,
    has_header: bool,
    source_positions: Optional[List[int]],
) -> bool:
    """Rebuilds only the sheet Omixa cleaned, styled per-column from
    that same sheet's own original formatting (see
    cleaning/formatting.py); every other sheet in the source workbook
    is copied through as-is. Returns False (falls back to a plain
    single-sheet export with no styling) if the source can't be
    reopened with openpyxl for any reason, an unreadable source
    shouldn't turn into a hard failure on download."""
    try:
        import openpyxl
        from openpyxl.styles import Font
        from openpyxl.utils import get_column_letter
    except ImportError:
        return False

    try:
        wb = openpyxl.load_workbook(source_path)
    except Exception:
        return False

    target_name = source_sheet_name if source_sheet_name in wb.sheetnames else wb.sheetnames[0]
    target_index = wb.sheetnames.index(target_name)
    src_ws = wb[target_name]

    # Profile the ORIGINAL sheet's own formatting before it's deleted
    # below. source_positions maps each of df's columns back to that
    # column's position in THIS sheet (columns a resolution dropped
    # just aren't in the list); if it's missing (e.g. an older
    # caller), fall back to a plain identity mapping, which is only
    # wrong if a resolution dropped a column in between - a harmless
    # degradation, not a crash.
    n_source_cols = max(source_positions, default=-1) + 1 if source_positions else len(df.columns)
    style_profile = extract_column_style_profile(src_ws, n_source_cols, has_header)
    positions = source_positions if source_positions is not None else list(range(len(df.columns)))

    # Drop the old (uncleaned) version of the target sheet and
    # recreate it in the same tab position, so the other sheets keep
    # their original order relative to it.
    del wb[target_name]
    ws = wb.create_sheet(title=target_name, index=target_index)

    ws.append([str(c) for c in df.columns])
    for row in df.itertuples(index=False, name=None):
        ws.append([None if pd.isna(v) else v for v in row])

    for col_idx, source_pos in enumerate(positions):
        col_letter = get_column_letter(col_idx + 1)
        style = style_profile[source_pos] if 0 <= source_pos < len(style_profile) else None
        font_name = style["font_name"] if style else DEFAULT_FONT_NAME
        font_size = style["size"] if style else DEFAULT_FONT_SIZE
        data_italic = bool(style["italic"]) if style else False
        data_bold = bool(style["bold"]) if style else False

        # Technical header row (the column label Omixa always writes
        # first): bold, so it reads as a header regardless of
        # has_header, but still in the column's own majority
        # font/size/italic rather than a hardcoded default.
        header_cell = ws.cell(row=1, column=col_idx + 1)
        header_cell.font = Font(name=font_name, size=font_size, bold=True, italic=data_italic)

        # Data rows: standardized to the column's own majority style,
        # not a sheet-wide one.
        data_font = Font(name=font_name, size=font_size, bold=data_bold, italic=data_italic)
        for row_cells in ws.iter_rows(min_row=2, min_col=col_idx + 1, max_col=col_idx + 1):
            row_cells[0].font = data_font

        width = style["width"] if style else None
        if width:
            ws.column_dimensions[col_letter].width = width

    wb.save(out_path)
    return True
