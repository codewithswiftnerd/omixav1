# Excel (.xlsx / .xls) fidelity

Omixa reads the first sheet into a table, cleans it, and **rebuilds that sheet** (`export/exporter.py`).
It is not an in-place editor, so it does not claim to preserve everything.

| Feature | Cleaned output |
|---|---|
| Cell values (cleaned) | yes |
| Other sheets, sheet order and names | kept as-is |
| Workbook properties (title, author) | kept |
| Column widths, per-column font / size / bold / italic | kept (majority style per column) |
| Header row | written bold in row 1 |
| Hidden columns | kept hidden |
| Frozen header row | kept |
| Formulas | **not kept** (cached values are read; formula-injection text is defused) |
| Merged cells | **not kept** |
| Hyperlinks, comments | **not kept** (text stays) |
| Conditional formatting, data validation, Excel tables | **not kept** |
| Row heights, hidden rows, fills, number formats | **not kept** |
| Macros (.xlsm, `vbaProject.bin`) | **rejected at upload** |
| `.xls` input | returned as `.xlsx` |

Why not more: cleaning removes duplicates/blank rows and can drop or rename columns, so any row- or
cell-addressed feature (merges, comments, hyperlinks, validation ranges, tables, row heights) would point at the
wrong cells. Re-mapping them is possible but is exactly the kind of risky hack we avoid; the UI/FAQ say so.
Regression tests: `tests/test_excel_fidelity.py`.
