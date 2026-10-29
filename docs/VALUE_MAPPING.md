# Value standardisation and mapping engine (+ baseline audit)

## 1. Baseline audit: what was verified

Method: read the pipeline (`processing/pipeline.py` -> `cleaning/*` -> `export/*`), then reproduced behaviour by
uploading messy files through the real API (upload -> process -> download) and comparing the file with the reports.

| # | Finding | Where | Status |
|---|---|---|---|
| 1 | The currency-label rule converted `$` to `USD` (and `dollar(s)` -> USD, `£`/`pound(s)` -> GBP): shared symbols were assumed to mean one currency. | `cleaning/rules.py` `_CURRENCY_LABELS` | **Fixed.** Only labels naming ONE currency are mapped. Test + doc updated. Behaviour change. |
| 2 | "Invalid values detected" showed **1** in `cleaning_summary` and **7** in `integrity_report` for the same file. Cause: the summary counted the number of *findings* in the post-clean report. | `cleaning/summary.py` `_invalid_values_detected` | **Fixed.** The integrity report is the single source; the summary is overwritten from it. Regression test added. |
| 3 | Review decisions could not name numeric cells (a bare `1` in a 0/1 column), and `unmatched_decisions` mis-reported them. | `cleaning/engine/executor.py` | **Fixed** (`_decision_keys`). |
| 4 | Country standardisation only knew ~150 phone-dialling countries; "Republic of Ghana" was not recognised; `England` -> United Kingdom was applied silently. | `cleaning/detectors.py`, `phone_formats.py` | Default rule left as is (not removed). The new `standardize_values` rule covers ISO 3166-1 and proposes lossy mappings for approval. |

Verified OK (no change needed): user-selected day-first reaches the transformation, report and export
(`04/12/2024` -> `2024-12-04`); ambiguous dates are left alone without a choice; row counts and duplicate removal agree
across summary, integrity report and file (7 -> 6, 1 exact duplicate, `unexplained = 0`); missing emails are counted as
missing, not invalid; leading zeros in IDs/phones survive CSV export; mixed-currency amount columns are left unconverted.

**Not audited in depth** (time): the scoring formula internals, Excel formatting fidelity beyond the existing tests,
imputation provenance beyond what `integrity_report.imputation` already reports. Existing tests for these pass.

## 2. Design

```
cleaning/mapping/
  reference/countries.json    ISO 3166-1 (249 codes), aliases, ambiguous names   <- data, not logic
  reference/currencies.json   ~50 ISO 4217 currencies + ambiguous symbols/words
  model.py       MappingRule (category, source, target, normalization, reference, version, confidence,
                              auto_safe, contexts, rule_id, scope, owner), Resolution, statuses
  normalize.py   lookup folding only (the original text is never altered in the audit)
  country.py boolean.py category.py currency.py   one Mapper each: analyze(column) + resolve(value) + render(format)
  engine.py      build_plan (pure, never mutates) -> apply_plan; suggest_for_dataframe; validate_custom_mappings
cleaning/engine/standardize.py   rule `standardize_values` (Pro), runs in the existing executor
routes/cleaning.py               POST /api/cleaning/mapping/<job_id>  (read-only preview, Pro, owner-scoped)
```
New category = new `Mapper` subclass in `engine.MAPPERS`. Phone formats, units, addresses are not built.

**Statuses.** `safe` (applied automatically) / `user_defined` (your mapping, this file) / `review` (proposed, needs approval) /
`ambiguous` (never applied) / `unknown` (left as typed) / `canonical` (already in the requested form) / `not_applicable`.

**Safety rules that are tested:** no fuzzy matching (Niger != Nigeria, typos are never "fixed"); codes that are also words
(`in`, `is`, `it`, `no`, `na`...) are never automatic in lower case, and `NA`/`NO` never (missing placeholder / answer);
Congo, Korea, Virgin Islands are ambiguous; England/Scotland/Wales/N. Ireland/Holland/Macedonia are proposed, not applied;
Kosovo (no ISO code) is not invented; a column is judged by its values, not its name; bare 0/1 is not Boolean unless yes/no
words sit beside it or the user confirms; `$`, `£`, `¥`, `kr`, "dollars", "pounds"... are ambiguous; amounts are never
touched by currency identification; no exchange-rate conversion exists anywhere; custom mappings cannot map to nothing.

**Review loop.** Flagged values go through the existing `decisions` mechanism: accept (use proposal), accept with `value`
(edit), reject (leave unchanged), or no decision (stays pending and is reported as pending).

**Audit.** Each changed cell carries `mapping`: category, rule_id, origin (`reference` | `custom_file` | `user_decision`),
mode (`automatic_safe` | `user_defined` | `user_approved` | `user_edited`), reference id + version, confidence, output format,
and for edits what Omixa proposed. Entries summarise it; `revert()` restores the originals (tested).

**Scoring.** Unchanged formula. Suggestions never touch the data, so they cannot change a score (tested). Applied mappings
change the data, so the existing consistency checks (e.g. `country_variants`) see the final file.

**Scopes.** Implemented: current file only (`mappings` on the rule). NOT implemented: saved-per-user and organisation scope
(needs persistent per-user storage; `MappingRule.scope/owner` are ready for it).

## 3. Limits to know
* Reference data was authored locally (no package install was possible). Before relying on edge cases, diff
  `countries.json` against the current ISO 3166 list; `_meta` records the version. Currencies are a curated subset: unlisted
  currencies are left unchanged.
* `standardize_values` is a Pro capability, like `standardize_categories`. If it should be Free, move it in `access.py`.
* The existing default rules (`country_standardization`, `boolean_standardization`, ...) still run for columns the new
  rule does not configure; for configured columns the new rule supersedes them (`SUPERSEDES`).
