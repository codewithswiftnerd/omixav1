# Column-aware cleaning engine

Declarative, per-column cleaning that runs **inside the existing worker pipeline** (`run_pipeline`),
on top of the unchanged default rule set.

```
plan entitlement (server-side) -> access.py (capabilities) -> cleaning_profile -> column rules
   -> executor (cleaning/engine/executor.py) -> AuditLog + quality/review report -> cleaned file
```

## Where things live
| File | Role |
|---|---|
| `cleaning/engine/access.py` | The ONLY place that knows Free vs Pro (`FREE_RULES`, `PRO_RULES`, `PRO_FEATURES`, `authorize`) |
| `cleaning/engine/base.py` | `BaseCleaningRule`, strict `ParamSpec` validation, `Flag`, `map_unique` |
| `cleaning/engine/{text,numeric,phone,dates,email_rules,categories}.py` | Rules (no plan logic inside) |
| `cleaning/engine/registry.py` | rule type -> class; asserts every rule is classified in `access.py` |
| `cleaning/engine/profile.py` | parse/validate a profile (data only, fixed registry lookup) |
| `cleaning/engine/executor.py` | runs a profile, audit steps, review items, decisions, per-column metrics |
| `cleaning/engine/recommend.py` | column type + confidence, recommendations (Pro) |
| `routes/cleaning.py` | `GET /api/cleaning/rules`, `POST /api/cleaning/profile/validate` |

## API
`POST /api/process/<job_id>` accepts `cleaning_profile` (alongside `rules`, `resolutions`, `profile_id`):

```json
{"cleaning_profile": {
  "name": "Nigerian Customer Dataset",
  "columns": {
    "Name":  {"rules": [{"type": "trim_whitespace"}, {"type": "normalize_case", "mode": "title"}]},
    "Phone": {"rules": [{"type": "normalize_phone", "country": "NG", "output_format": "international"}]}
  },
  "decisions": [{"column": "Status", "original": "pendng", "action": "accept", "value": "Pending"}]
}}
```
* Free caller -> `402 {"code":"FEATURE_NOT_AVAILABLE","rule":...,"required_plan":"pro","upgrade_required":true}`
* Invalid profile -> `400 {"code":"INVALID_RULE_CONFIG"|"UNKNOWN_RULE",...}`; unknown column in the file -> `422 UNKNOWN_COLUMN` (queue mode: job fails with `CleaningConfigError`, nothing exported).
* Result: `summary.cleaning_engine` = per-column detected type/confidence, rule-level changed/flagged counts, before/after format inconsistency, review items (original preserved, reason, suggestion, status), unmatched decisions.
* Review loop: re-submit with `decisions` (accept/reject per flagged value).

## Rules
Free: trim_whitespace, normalize_whitespace, basic_numeric_cleanup, remove_currency_symbols,
basic_missing_value_handling, basic_duplicate_detection (report only), basic_date_detection (report only).
Pro: normalize_phone, validate_phone, normalize_date, validate_date, normalize_case, validate_email,
normalize_currency, standardize_categories, custom_replacements; features column_rules, cleaning_profiles,
recommendations, before_after_analysis, detailed_change_log.

Execution order is by phase (whitespace -> missing -> replacements -> semantic -> case -> categories ->
validation -> detection), stable within a phase. Columns a profile configures are held out from the default
rules that would re-write the result (`SUPERSEDES` in executor.py).

Adding a country: add its row to `cleaning/phone_formats.COUNTRIES` (and optionally `COUNTRY_PATTERNS`).
Adding a rule: write the class, add to `registry._CLASSES`, classify it in `access.py` (import-time assertion enforces it), add tests.


## Capitalisation, variants and text-numbers (default pipeline)

`case_standardization` (OMX-FIX-014) runs before `categorical_standardization` so the most common casing no
longer wins by default. It only rewrites values typed in ONE case (all lower or ALL CAPS):

- person names -> Title Case (mixed-case names such as McDonald / O'Brien are never touched)
- ID prefixes -> the column's majority (`cust-0011` -> `CUST-0011`)
- label columns that mix styles (`active` / `INACTIVE` / `On hold`) -> one style; short all-caps tokens
  (HR, IT, NGN) are kept as acronyms; notes / comment columns and anything with emoji are skipped

`categorical_standardization` now also merges spellings that differ only by hyphen, space or dot
(`Port-Harcourt` / `Port Harcourt`, `Web site` / `Website`, `I.T.` / `IT`).

`numeric_text_cleaning` additionally understands `44 yrs`, number words (`twenty`, `ten thousand naira`), a letter O
typed for zero (`1O,OOO`-style) and `free` = 0 in amount/price columns. A real word in a numeric column still blocks
conversion. The report adds `inconsistent_casing` and `negative_amount` (a repeated `-100` is flagged as a likely
placeholder, never silently changed). `account_status` / `account_type` are no longer treated as identifiers.

### Second round (found by running a messy file end to end)
- `PRO` next to `Pro` is shouting, not an acronym; between spellings of one word the representative is chosen by
  form first (Proper > short acronym > lower > SHOUTING) and only then by count, so `ok` can no longer beat `OK`.
  Dotted abbreviations resolve to the plain one (`I.T.` -> `IT`).
- Flag columns (`is_verified`, `active`...) with a stray value (`maybe`) still get their recognised yes/no spellings
  unified to `Yes` / `No`; the stray value is left as typed and reported. Generic columns are never touched.
- `currency_label_standardization` (OMX-FIX-015): columns named currency/ccy map naira, ₦, NGN -> NGN, £ -> GBP,
  `$` / dollars -> USD (assumption), € -> EUR. Combined values (`NGN/USD`) are left alone.
- Worded placeholders (`not known`, `TBD`, `not available`...) count as missing; `Naija` resolves to Nigeria.
- Mixed `dd/mm/yyyy` and `mm-dd-yyyy` dates are still NOT converted: they are genuinely ambiguous, so the engine
  reports the column instead of guessing.

## Data integrity: per-cell normalisation, honest reports, verified exports

**Reading.** CSV/Excel are read with `keep_default_na=False`: only a truly empty cell is missing at read time.
`N/A`, `NULL`, `NA`, `None` reach the missing-token rule, so each is converted *and* written to the audit ledger.
(pandas used to swallow them silently, which also turned integer columns into floats: `45` -> `45.0`.)

**Numbers (`numeric_text_cleaning`).** Every cell is judged on its own (`detectors.normalize_numeric_cell`):
`" 35 "` -> 35, `"₦2,500"` -> 2500, `"(45)"` -> -45. A cell that cannot be read safely (`comma`, `invalid`,
`ERROR`, an ambiguous `1,234`) is preserved byte-for-byte and flagged; it no longer blocks the rest of the column.
A column is only treated as numeric when >= 60% of its values read as numbers. A comma means thousands or a
decimal point only according to the *column* (`column_number_format`): evidence like `12,500.50` or `₦2,500`
means thousands; `3,45` means decimal comma, but a lone `1,234` in such a column stays ambiguous unless the user
sets `options.number_format`. Words (`forty-five`) are read only with `options.number_words` (recorded as
`user_selected`) or the per-column `words_to_numbers` resolution. Arbitrary text never becomes 0.

**Validation.** Plausibility checks (age < 0 or > 120, negative quantity, future signup date) run whatever the
column-type confidence, are warnings (`suspicious`) unless the user configured a hard range, and are reported as
`suspicious_values` (OMX-ACC-005). `allow_negative` / `allow_future_dates` / ranges are set through
`options.validation` (same shape as `/api/validate`). `possible_misspelling` (OMX-CON-020) suggests a correction
(`Lagoss` -> `Lagos`) and never applies it; a different real word (`Inactive` next to `Active`) is not a typo.

**Options** (`POST /api/process/<job>` body `options`, all optional, whitelisted in `cleaning/options.py`):
`number_format` (`auto|thousands_comma|decimal_comma`), `number_words` (bool), `missing_markers` (extra WEAK
markers such as `error`, blanked only where the column is clearly numeric), `duplicate_keys` (columns; detection
only), `validation` (`{column_types, rules, defaults}`). Every option extends a Free capability; Pro rules and
features still go through `cleaning/engine/access.authorize`.

**Duplicates** (`cleaning/duplicates.py`, read-only): *exact* rows (removed only by the `duplicates` rule, first
kept, every removal audited), *by key* (same key columns, other columns differ: review only), *potential* (same
e-mail/phone, not an exact copy: review only). A shared name is never an identity.

**Report** (`summary.integrity_report`): computed from re-validating the final dataset. Per column and overall:
`successfully_cleaned` | `partially_cleaned` | `requires_manual_review`; plus row/column counts, missing before/after,
invalid/suspicious/unresolved detected, resolved, needing review, duplicates, type/date/category inconsistencies,
changes per column, operations, warnings. `fully_clean` is true only when nothing needs a person and the export
verified. Audit entries carry `source_row` (the row number in the uploaded sheet).

**Export verification** (`summary.export_verification`, `export/verify.py`): the written file is re-read and
checked: shape, header, every cell vs the dataset, every ledger change present, cells nobody changed still read
exactly as uploaded (CSV: byte-for-byte), and re-validation gives the same problem counts. CSV floats are written
with minimal formatting and unchanged cells get their uploaded text back (`7` stays `7`, `45.50` stays `45.50`).
The formula-injection apostrophe is counted and reported as the only intended difference. Files above 1.5M cells
skip the per-cell checks and say so.


## Accuracy audit (October 2026)
- **Dates are judged per cell.** A column with an ambiguous value (03/04/2025) no longer blocks the valid ones: ISO dates and
  dates that can only be read one way (15/02/2024) are written as YYYY-MM-DD; ambiguous, impossible (32/13/2025) and
  placeholder cells stay exactly as uploaded and the column stays flagged. The convention is never guessed from other cells.
  Midnight timestamps (real Excel date cells) count as dates; a real time of day (14:30) still blocks rewriting.
- **Identifier formats** (`inconsistent_identifier_format`, OMX-CON-004): ID-001 next to a bare 3 is reported with the formats found
  and a recommendation to agree a column rule. Nothing is renumbered and no prefix is invented.
- **Ages**: negative = invalid; above 120 and exactly 0 = suspicious (0 is often a placeholder; `min: 0` declares it valid). Never defaulted.
- **Categories** are judged on distinct *labels*, not spellings, so small messy columns (active/Active/ACTIVE) are standardised.
  `casing.variant_key` ignores only case, spaces, dots and a hyphen between words: `AB+`/`AB-`, `Grade A+`/`A-`, `R&D`/`RD` are different.
- **Type detection**: a real number (or a column named amount/price/age) is never read as a phone number.
- **Integrity report**: `invalid_values.resolved` is computed per column and excludes issues that left only because a duplicate row was
  removed (`removed_with_duplicate_rows`); new `issue_summary` separates detected / automatically_corrected / remaining /
  requires_review / cannot_be_corrected_automatically / introduced_by_cleaning.
- A bare `-` or `+` is no longer prefixed with an apostrophe on export.
