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
