"""
Built-in rule definitions.

Two kinds of entries, one `RuleSpec` shape:

  * issue rules   - a detector that finds a defect (issue key = the finding key)
  * fixer rules   - issue key "fix:<rule_name>", documenting a cleaning rule from
                    cleaning/rules.py (what kind of change it makes, how sure we are,
                    whether it can be undone). The audit log reads these.

To add a rule: write a check in checks.py (or anywhere), add a RuleSpec below (or call
register_rule from your own module), done.
"""

from __future__ import annotations

from cleaning import checks as C
from cleaning import model as M
from cleaning.rule_registry import RuleSpec, register_rule

R = register_rule

# =========================================================================
# ISSUE RULES (detectors)
# =========================================================================

# ---- completeness -------------------------------------------------------
R(RuleSpec(
    id="OMX-CMP-001", issue="missing_values", title="Missing values", dimension=M.COMPLETENESS,
    criticality=0.6, confidence=0.99, max_level=M.HIGH,
    remediation=M.REQUIRES_REVIEW, operation=M.IMPUTATION, resolver="missing_values",
    reversibility=M.REVERSIBLE, fix_confidence=0.55,
    explanation="Empty cells (including placeholders like 'N/A' or '-') mean the record says nothing about "
                "that field, so counts, averages and joins on it are based on fewer rows than they appear to be.",
    recommendation="Prefer sourcing the real values. If a stand-in is acceptable, imputation fills a typical "
                   "value but it is an estimate, not the true value.",
    detector=C.check_missing,
))
R(RuleSpec(
    id="OMX-CMP-002", issue="high_missingness", title="Mostly-empty column", dimension=M.COMPLETENESS,
    criticality=0.8, confidence=0.99, min_level=M.HIGH,
    remediation=M.REQUIRES_REVIEW, operation=M.DELETION, resolver="high_missingness",
    reversibility=M.REVERSIBLE, fix_confidence=0.4,
    explanation="More than 70% of this column is empty. Filling that much would mostly be inventing data, and "
                "any analysis that uses it covers only a small minority of rows.",
    recommendation="Decide whether the column is worth keeping. OMIXA will not auto-fill it.",
))

# ---- uniqueness ---------------------------------------------------------
R(RuleSpec(
    id="OMX-UNQ-001", issue="duplicate_rows", title="Exact duplicate rows", dimension=M.UNIQUENESS,
    criticality=0.7, confidence=0.97,
    remediation=M.SAFE_AUTO_FIX, operation=M.DELETION, resolver="duplicates", reversibility=M.REVERSIBLE,
    fix_confidence=0.95, unit="row",
    explanation="Rows that are identical in every column are almost always the same record entered twice. "
                "They inflate counts and totals.",
    recommendation="Remove the repeats and keep the first occurrence. Removed rows are kept in the change "
                   "ledger so the step can be undone.",
    detector=C.check_duplicate_rows,
))
R(RuleSpec(
    id="OMX-UNQ-002", issue="possible_duplicate_records", title="Possible duplicate records",
    dimension=M.UNIQUENESS, criticality=0.5, confidence=0.55, max_level=M.HIGH,
    remediation=M.REQUIRES_REVIEW, operation=M.DELETION, resolver="possible_duplicate_records",
    reversibility=M.REVERSIBLE, fix_confidence=0.5, unit="row",
    explanation="These rows match another row on every column except one, which usually means the same "
                "real-world record was entered twice with a typo or an update. It can equally be two "
                "genuinely different records that share most attributes.",
    recommendation="Review the pairs and decide whether to merge or remove them.",
    detector=C.check_possible_duplicate_records,
))
R(RuleSpec(
    id="OMX-UNQ-003", issue="duplicate_identifier_values", title="Reused identifier", dimension=M.UNIQUENESS,
    criticality=0.9, confidence=0.85, min_level=M.MEDIUM,
    remediation=M.DO_NOT_MODIFY, operation=M.NONE, resolver=None, reversibility=M.NOT_APPLICABLE, unit="row",
    explanation="Two different records carry the same identifier. Joins and lookups on this key will silently "
                "pick the wrong record or multiply rows.",
    recommendation="Decide which record owns the identifier, or reissue it. This cannot be decided from the "
                   "data alone.",
    detector=C.check_duplicate_identifiers,
))

# ---- consistency --------------------------------------------------------
R(RuleSpec(
    id="OMX-CON-001", issue="whitespace", title="Stray whitespace", dimension=M.CONSISTENCY,
    criticality=0.25, confidence=0.99, max_level=M.LOW,
    remediation=M.SAFE_AUTO_FIX, operation=M.NORMALIZATION, resolver="formatting", reversibility=M.REVERSIBLE,
    fix_confidence=0.99,
    explanation="Leading, trailing or repeated spaces make identical values compare as different, which breaks "
                "grouping, lookups and de-duplication.",
    recommendation="Trim and collapse the spaces.",
    detector=C.check_whitespace,
))
R(RuleSpec(
    id="OMX-CON-002", issue="inconsistent_categories", title="Inconsistent category spellings",
    dimension=M.CONSISTENCY, criticality=0.5, confidence=0.9, max_level=M.HIGH,
    remediation=M.SAFE_AUTO_FIX, operation=M.NORMALIZATION, resolver="categorical_standardization",
    reversibility=M.REVERSIBLE, fix_confidence=0.9,
    explanation="The same category written with different case or spacing splits one group into several, so "
                "per-category counts and filters are wrong.",
    recommendation="Merge variants that differ only by case or spacing into the spelling already used most "
                   "often. Nothing new is invented.",
    detector=C.check_inconsistent_categories,
))
R(RuleSpec(
    id="OMX-CON-003", issue="mixed_data_types", title="Mixed text and numbers", dimension=M.CONSISTENCY,
    criticality=0.6, confidence=0.7, max_level=M.HIGH,
    remediation=M.REQUIRES_REVIEW, operation=M.DELETION, resolver="mixed_data_types",
    reversibility=M.REVERSIBLE, fix_confidence=0.5,
    explanation="A column that is partly numbers and partly text cannot be summed or sorted reliably; forcing "
                "it to numeric would blank the text entries.",
    recommendation="Confirm what the column is meant to hold before converting; converting blanks the "
                   "non-numeric values.",
    detector=C.check_mixed_types,
))

# ---- validity -----------------------------------------------------------
R(RuleSpec(
    id="OMX-VAL-001", issue="invalid_email_format", title="Malformed email address", dimension=M.VALIDITY,
    criticality=0.7, confidence=0.9,
    remediation=M.REQUIRES_REVIEW, operation=M.DELETION, resolver="invalid_email_format",
    reversibility=M.REVERSIBLE, fix_confidence=0.6,
    explanation="These values do not have the shape of an email address, so mail to them will bounce. A typo "
                "could be anywhere in the address, so the correct one cannot be derived.",
    recommendation="Correct them from the source, or blank them so they stop being treated as contactable.",
    detector=C.check_invalid_email,
))
R(RuleSpec(
    id="OMX-VAL-002", issue="suspicious_phone_format", title="Implausible phone number", dimension=M.VALIDITY,
    criticality=0.5, confidence=0.7,
    remediation=M.REQUIRES_REVIEW, operation=M.INFERENCE, resolver="suspicious_phone_format",
    reversibility=M.REVERSIBLE, fix_confidence=0.7,
    explanation="The digit count is outside what a phone number can have, or the number lacks a country "
                "context.",
    recommendation="If you know the country, pick it to normalize to international format; numbers that do not "
                   "fit are left alone.",
    detector=C.check_suspicious_phone,
))
R(RuleSpec(
    id="OMX-VAL-003", issue="unrecoverable_scientific_notation", title="Digits lost to scientific notation",
    dimension=M.VALIDITY, criticality=1.0, confidence=0.95, min_level=M.CRITICAL,
    remediation=M.DO_NOT_MODIFY, operation=M.NONE, reversibility=M.NOT_APPLICABLE,
    explanation="A spreadsheet reformatted a long number (a phone or account number) as 1.343E+12, discarding "
                "digits. The original cannot be reconstructed from what is in the file.",
    recommendation="Re-export from the source system with the column stored as text.",
    detector=C.check_scientific_notation,
))
R(RuleSpec(
    id="OMX-VAL-004", issue="unrecognized_gender_value", title="Unrecognized gender value",
    dimension=M.VALIDITY, criticality=0.2, confidence=0.6, max_level=M.MEDIUM,
    remediation=M.DO_NOT_MODIFY, operation=M.NONE, reversibility=M.NOT_APPLICABLE,
    explanation="The value is not one of the recognised male/female spellings. It may be a valid answer "
                "('non-binary', 'other') or a typo; OMIXA never infers gender.",
    recommendation="Review manually.",
    detector=C.check_unrecognized_gender,
))
R(RuleSpec(
    id="OMX-VAL-005", issue="unrecognized_country_value", title="Unrecognized country", dimension=M.VALIDITY,
    criticality=0.35, confidence=0.7, max_level=M.MEDIUM,
    remediation=M.DO_NOT_MODIFY, operation=M.NONE, reversibility=M.NOT_APPLICABLE,
    explanation="The value is not in OMIXA's country reference. It may be a typo or simply a country the "
                "reference list does not include yet.",
    recommendation="Verify manually; no fuzzy matching is attempted.",
    detector=C.check_unrecognized_country,
))
R(RuleSpec(
    id="OMX-VAL-009", issue="invalid_values", title="Values that break the column's type or rules", dimension=M.VALIDITY,
    criticality=0.75, confidence=0.85,
    remediation=M.REQUIRES_REVIEW, operation=M.NONE,
    explanation="Words or malformed values in a column that is otherwise numeric, categorical or patterned, or values "
                "outside a configured range. They break calculations and are never deleted automatically.",
    recommendation="Review the affected rows and correct, replace, keep or remove each value.",
    detector=C.check_invalid_values,
))
R(RuleSpec(
    id="OMX-VAL-010", issue="unresolved_values", title="Values that cannot be interpreted safely", dimension=M.VALIDITY,
    criticality=0.6, confidence=0.7,
    remediation=M.REQUIRES_REVIEW, operation=M.NONE,
    explanation="Omixa cannot tell what these values mean (e.g. 'free' in an amount column, a placeholder word in a "
                "notes column), so they are preserved and counted as open issues instead of being guessed.",
    recommendation="Decide what the value means, or confirm the column's type so it can be judged.",
    detector=C.check_unresolved_values,
))
R(RuleSpec(
    id="OMX-VAL-006", issue="ambiguous_date_format", title="Ambiguous date format", dimension=M.VALIDITY,
    criticality=0.7, confidence=0.9,
    remediation=M.REQUIRES_REVIEW, operation=M.INFERENCE, resolver="ambiguous_date_format",
    reversibility=M.REVERSIBLE, fix_confidence=0.5,
    explanation="03/04/2024 is 3 April in day-first countries and 4 March in month-first ones. Picking wrong "
                "silently moves dates by up to 11 months. Two-digit years leave the century to a guess.",
    recommendation="Tell OMIXA which convention the file uses; it will then standardize to YYYY-MM-DD.",
    detector=C.check_ambiguous_dates,
))
R(RuleSpec(
    id="OMX-VAL-007", issue="potential_outliers", title="Statistical outliers", dimension=M.VALIDITY,
    criticality=0.3, confidence=0.5, max_level=M.MEDIUM,
    remediation=M.REQUIRES_REVIEW, operation=M.CORRECTION, resolver="potential_outliers",
    reversibility=M.REVERSIBLE, fix_confidence=0.4,
    explanation="These values sit far from the rest of the column. Unusual is not the same as wrong: they may "
                "be real extremes or entry errors.",
    recommendation="Look at them before trusting averages or totals; cap or remove only if you know they are errors.",
    detector=C.check_outliers,
))
R(RuleSpec(
    id="OMX-VAL-008", issue="constant_column", title="Constant column", dimension=M.VALIDITY,
    criticality=0.1, confidence=0.9, max_level=M.LOW,
    remediation=M.REQUIRES_REVIEW, operation=M.DELETION, resolver="constant_column",
    reversibility=M.REVERSIBLE, fix_confidence=0.7,
    explanation="Every value is the same, so the column cannot distinguish one record from another.",
    recommendation="Drop it if it adds nothing; keep it if it is a deliberate fixed attribute.",
    detector=C.check_constant_columns,
))

# ---- accuracy (objectively measurable only) -----------------------------
R(RuleSpec(
    id="OMX-ACC-001", issue="impossible_age", title="Impossible age", dimension=M.ACCURACY,
    criticality=0.85, confidence=0.97, min_level=M.MEDIUM,
    remediation=M.REQUIRES_REVIEW, operation=M.DELETION, resolver="impossible_age",
    reversibility=M.REVERSIBLE, fix_confidence=0.5,
    explanation="No person is below 0 or above 120. The value is wrong, but the true value cannot be derived.",
    recommendation="Correct from the source; otherwise blank the value or drop the row.",
    detector=C.check_impossible_age,
))
R(RuleSpec(
    id="OMX-ACC-004", issue="negative_amount", title="Negative or placeholder amounts", dimension=M.ACCURACY,
    criticality=0.8, confidence=0.9, min_level=M.MEDIUM,
    remediation=M.DO_NOT_MODIFY, operation=M.NONE, resolver=None, reversibility=M.NOT_APPLICABLE,
    explanation="A paid/price/fee column contains negative numbers. When one value such as -100 repeats on many "
                "rows it is a placeholder for 'unknown', and it silently drags totals and averages down.",
    recommendation="Confirm whether these are refunds or placeholders, then blank or correct them at the source.",
    detector=C.check_negative_amounts,
))
R(RuleSpec(
    id="OMX-ACC-002", issue="impossible_date", title="Impossible date", dimension=M.ACCURACY,
    criticality=0.85, confidence=0.9, min_level=M.MEDIUM,
    remediation=M.REQUIRES_REVIEW, operation=M.DELETION, resolver="impossible_date",
    reversibility=M.REVERSIBLE, fix_confidence=0.5,
    explanation="The date is before 1900, or lies in the future for something that must already have happened "
                "(a birth, signup or payment).",
    recommendation="Correct from the source; otherwise blank the value or drop the row.",
    detector=C.check_impossible_dates,
))
R(RuleSpec(
    id="OMX-ACC-003", issue="unverified_imputed_values", title="Imputed (estimated) values",
    dimension=M.ACCURACY, criticality=0.3, confidence=1.0, max_level=M.MEDIUM,
    remediation=M.DO_NOT_MODIFY, operation=M.NONE, reversibility=M.NOT_APPLICABLE,
    explanation="These cells were filled by an imputation rule. They remove the gap but are estimates, so "
                "filling them improves completeness without improving accuracy.",
    recommendation="Treat as estimates in analysis, or replace with real values.",
    detector=C.check_unverified_imputation,
))

# ---- timeliness ---------------------------------------------------------
R(RuleSpec(
    id="OMX-TIM-001", issue="stale_data", title="Stale records", dimension=M.TIMELINESS,
    criticality=0.4, confidence=0.6, max_level=M.MEDIUM,
    remediation=M.DO_NOT_MODIFY, operation=M.NONE, reversibility=M.NOT_APPLICABLE,
    explanation="A 'last updated'-style column shows records that have not been touched for over a year, so "
                "they may no longer reflect reality.",
    recommendation="Only the data owner can say whether they are still current.",
    detector=C.check_stale_data,
))

# ---- format inconsistencies found by safely dry-running the fixers --------
# (no detector: quality_report derives these from the dry run so that the finding and the
#  fix can never disagree about what would change)
def _fmt(id_, issue, title, resolver, why, what, crit=0.35, conf=0.9, fixconf=0.9, op=M.NORMALIZATION,
         max_level=M.LOW):
    R(RuleSpec(
        id=id_, issue=issue, title=title, dimension=M.CONSISTENCY, criticality=crit, confidence=conf,
        max_level=max_level, remediation=M.SAFE_AUTO_FIX, operation=op, resolver=resolver,
        reversibility=M.REVERSIBLE, fix_confidence=fixconf, explanation=why, recommendation=what,
    ))

_fmt("OMX-CON-010", "inconsistent_date_format", "Dates in mixed formats", "date_standardization",
     "The column mixes date formats, so it sorts as text and cannot be filtered by period.",
     "Rewrite unambiguous dates as YYYY-MM-DD. Only done when day-first and month-first readings agree.",
     crit=0.5, fixconf=0.95, max_level=M.MEDIUM)
_fmt("OMX-CON-011", "numeric_formatting_noise", "Numbers stored as formatted text", "numeric_text_cleaning",
     "Currency symbols, thousands separators or % signs keep these numbers stored as text, so they cannot be "
     "summed or compared.",
     "Strip the formatting and store real numbers. Only when every value in the column converts cleanly.",
     crit=0.5, fixconf=0.9, max_level=M.MEDIUM)
_fmt("OMX-CON-012", "phone_formatting", "Inconsistent phone formatting", "phone_cleaning",
     "Spaces, brackets and dashes make the same number look different, which breaks matching.",
     "Remove punctuation, keeping every digit and a leading '+'. No digit or country code is added.",
     crit=0.3, fixconf=0.95)
_fmt("OMX-CON-013", "email_formatting", "Email case/spacing", "email_cleaning",
     "Mixed case or stray spaces make one address look like several.",
     "Trim and lowercase. Lowercasing is standard for emails but is an assumption about the mail host.",
     crit=0.3, fixconf=0.9)
_fmt("OMX-CON-014", "gender_variants", "Gender spelled several ways", "gender_standardization",
     "M / Male / male are one value written three ways.",
     "Map recognised male/female spellings to one form; anything else is left untouched.",
     crit=0.3, fixconf=0.95)
_fmt("OMX-CON-015", "country_variants", "Country written several ways", "country_standardization",
     "NG / nigeria / Nigeria are one country written three ways.",
     "Map recognised codes and names to one canonical name; unknown values are left untouched.",
     crit=0.3, fixconf=0.92)
_fmt("OMX-CON-016", "boolean_variants", "Yes/no flags in mixed form", "boolean_standardization",
     "Y / yes / TRUE / 1 mean the same thing but are different values.",
     "Convert to true/false, only when every value in the column is a recognised yes/no word.",
     crit=0.3, fixconf=0.9)
_fmt("OMX-CON-019", "currency_label_variants", "Currency written inconsistently", "currency_label_standardization",
     "The same currency appears as naira, \u20a6 and NGN (or \u00a3 and GBP), so totals by currency split into groups.",
     "Bring the labels to ISO codes. Combined values such as NGN/USD are left for you to review.",
     crit=0.35, fixconf=0.9, max_level=M.MEDIUM)
_fmt("OMX-CON-018", "inconsistent_casing", "Inconsistent capitalisation", "case_standardization",
     "Names in ALL CAPS next to Title Case, IDs like cust-0011 next to CUST-0012, or labels such as "
     "active / INACTIVE / On hold make the same kind of value look different and break grouping and sorting.",
     "Bring the column to one style: Title Case for names, the majority prefix for IDs, one label style for "
     "categories. Mixed-case values and short acronyms (HR, IT) are left alone.",
     crit=0.35, fixconf=0.93, max_level=M.MEDIUM)
_fmt("OMX-CON-017", "column_name_formatting", "Untidy column names", "column_names",
     "Spaces, capitals and punctuation in headers cause trouble in code and databases.",
     "Rename to lowercase_snake_case. Only labels change, never data.", crit=0.1, fixconf=0.95)

# =========================================================================
# FIXER RULES (documentation of each cleaning rule, read by the audit log)
# =========================================================================
def _fixer(name, id_, op, fixconf, why, reversibility=M.REVERSIBLE, remediation=M.SAFE_AUTO_FIX):
    R(RuleSpec(
        id=id_, issue=f"fix:{name}", title=name.replace("_", " "), dimension=M.CONSISTENCY,
        criticality=0.0, confidence=fixconf, fix_confidence=fixconf, remediation=remediation,
        operation=op, resolver=name, reversibility=reversibility, explanation=why,
        recommendation="", min_level=M.LOW, max_level=M.LOW,
    ))

_fixer("column_names", "OMX-FIX-001", M.NORMALIZATION, 0.95, "Header labels tidied; no data value changes.")
_fixer("formatting", "OMX-FIX-002", M.NORMALIZATION, 0.99, "Whitespace trimmed and collapsed.")
_fixer("missing_token_normalization", "OMX-FIX-003", M.NORMALIZATION, 0.9,
       "Placeholders such as 'N/A', 'null' or '-' recognised as a missing value.")
_fixer("numeric_text_cleaning", "OMX-FIX-004", M.NORMALIZATION, 0.9,
       "Currency/percent/thousands formatting removed; value unchanged.")
_fixer("gender_standardization", "OMX-FIX-005", M.NORMALIZATION, 0.95, "Recognised gender spellings unified.")
_fixer("country_standardization", "OMX-FIX-006", M.NORMALIZATION, 0.92, "Recognised country codes/names unified.")
_fixer("boolean_standardization", "OMX-FIX-007", M.NORMALIZATION, 0.9, "Yes/no words converted to true/false.")
_fixer("categorical_standardization", "OMX-FIX-008", M.NORMALIZATION, 0.9,
       "Case/spacing variants merged into the most common spelling.")
_fixer("currency_label_standardization", "OMX-FIX-015", M.NORMALIZATION, 0.9,
       "Currency columns that spell the same currency several ways (naira, \u20a6, NGN; \u00a3, GBP) brought to ISO codes. "
       "Combined or unknown values are left as typed.")
_fixer("case_standardization", "OMX-FIX-014", M.NORMALIZATION, 0.93,
       "Names, ID prefixes and label columns that mixed lower/UPPER/Title case brought to one style. "
       "Mixed-case values and short acronyms are left as typed.")
_fixer("email_cleaning", "OMX-FIX-009", M.NORMALIZATION, 0.9, "Emails trimmed and lowercased.")
_fixer("phone_cleaning", "OMX-FIX-010", M.NORMALIZATION, 0.95,
       "Phone punctuation removed; digits preserved. Scientific-notation numbers restored only when lossless.")
_fixer("date_standardization", "OMX-FIX-011", M.NORMALIZATION, 0.95,
       "Unambiguous dates rewritten as YYYY-MM-DD.")
_fixer("missing_values", "OMX-FIX-012", M.IMPUTATION, 0.55,
       "Gaps filled with a column median (numbers), the most common value (flags) or the literal 'Unknown' (text). "
       "These are estimates, not recovered values.", remediation=M.REQUIRES_REVIEW)
_fixer("duplicates", "OMX-FIX-013", M.DELETION, 0.95,
       "Exact duplicate rows removed, first occurrence kept.")

# user-chosen resolutions (cleaning/resolutions.py): documented so the audit has an id for them
_RESOLUTION_OPS = {
    "ambiguous_date_format": (M.INFERENCE, "User chose the day/month convention; dates rewritten accordingly."),
    "high_missingness": (M.IMPUTATION, "User chose to fill (imputation) or drop a mostly-empty column."),
    "potential_outliers": (M.CORRECTION, "User chose to cap or remove outlier values."),
    "constant_column": (M.DELETION, "User chose to drop a constant column."),
    "mixed_data_types": (M.DELETION, "User chose to convert to numeric, blanking non-numeric entries."),
    "invalid_email_format": (M.DELETION, "User chose to blank invalid email addresses."),
    "impossible_age": (M.DELETION, "User chose to blank or remove impossible ages."),
    "impossible_date": (M.DELETION, "User chose to blank or remove impossible dates."),
    "possible_duplicate_records": (M.DELETION, "User chose to remove near-duplicate rows."),
    "suspicious_phone_format": (M.INFERENCE, "User asserted a country; numbers fitting it were converted to +country form."),
}
RESOLUTION_OPERATIONS = _RESOLUTION_OPS
# (issue, choice) -> (operation, reason) where one issue offers choices that do different things, so the
# audit trail describes what the user actually chose rather than the issue's default.
RESOLUTION_CHOICE_OPERATIONS = {
    ("mixed_data_types", "words_to_numbers"):
        (M.INFERENCE, "User chose to read number words as numbers (e.g. \"forty\" -> 40); other values untouched."),
    ("mixed_data_types", "free_as_zero"):
        (M.INFERENCE, "User chose to treat the word \"free\" (and equivalents) as 0; other values untouched."),
}
