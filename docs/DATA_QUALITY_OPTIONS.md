# Data-quality options

All options go in the request's `options` object, are validated by `cleaning/options.py`, and apply to that job only.

| Option | Values | What it does |
|---|---|---|
| `missing_markers` | list of words | Extra words to treat as blank (in numeric columns; in text columns they are reported for review). |
| `not_missing` | list of words | Words that are REAL values in your data (e.g. `none`, `n/a`). Never blanked, never counted as missing. Wins over the built-in markers and `missing_markers`. |
| `duplicate_keys` | list of column names | Rows sharing these columns are reported as possible duplicates. Nothing is removed by this alone. |
| `duplicate_removal` | `report` (default), `keep_first`, `keep_last` | Opt-in removal of rows that repeat `duplicate_keys`. Needs `duplicate_keys`. A key column that cannot be found fails the job with nothing changed. Rows with an empty key are never duplicates. Every removal is logged (rule `OMX-FIX-019`) with the row it matched. |
| `consistency` | `{enabled, tolerance, ignore_columns}` | Related-column checks (quantity x price = total, subtotal + tax = total, start before end, minimum below maximum). `enabled: false` switches them off; `tolerance` is the relative difference allowed (0 to 0.05, default 0.005); `ignore_columns` excludes columns. |

## Related-column checks never change data
They report the row, the values found and the value expected. Which value is wrong is never decided.
A relationship is believed only when most rows follow it and it beats coincidence (see `cleaning/consistency.py`).

## Report fields added
- `quality_report.relationships`: every relationship the engine inferred, including ones that hold on every row.
- Findings `calculation_mismatch`, `contradictory_ordering`, `out_of_range_percentage` (all `requires_review`).
- `change_log.removed_rows[*].duplicate_of` / `duplicate_of_source_row`: the kept row each removed row matched.
- Row numbers in findings (`record`, `source_row`) refer to the rows of the file as uploaded, in both the before and after reports.
