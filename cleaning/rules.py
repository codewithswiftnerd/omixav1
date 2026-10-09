"""
Cleaning Rules

Started from the V1 locked spec (missing values, exact duplicates,
formatting/whitespace) and grown with a second wave of rules that
follow the same philosophy: auto-fix only when the correction is
unambiguous, otherwise leave the data untouched and let
cleaning/quality_report.py flag it for a human to look at. Every
rule keeps the signature the pipeline already expects:

    (df: pd.DataFrame) -> (pd.DataFrame, int)   # cleaned df, count changed

so the pipeline can log exactly what happened for the summary the
frontend shows the user ("Removed 40 duplicate rows", etc).

Anything the spec says should be "flagged for review" instead of
auto-fixed is deliberately left untouched in the data (so it's still
visible/inspectable) and reported separately via each rule's
`details` dict rather than silently changed, see each function's
docstring.
"""

from __future__ import annotations
from typing import Optional, List, Tuple
import re
import pandas as pd

from cleaning import casing, currencies, detectors, profiling
from cleaning import model as M
from cleaning.audit import AuditLog
from cleaning.rule_registry import spec_for_fixer

# Order matters. Formatting runs first so that " Delhi" and "Delhi"
# are the same string before anything else compares them. Column
# names are cleaned up front since nothing downstream depends on the
# original header spelling. Missing-token normalization and numeric/
# gender/country/boolean/categorical/email/phone/date standardization
# all run *before* missing_values, so the fill step sees the real
# shape of the data (e.g. a numeric-looking text column becomes
# numeric before deciding whether to median-impute or
# "Unknown"-impute it). gender_standardization and
# country_standardization run BEFORE categorical_standardization on
# purpose: they pick a specific, spec-defined canonical spelling
# ("male", "Nigeria") for a fixed vocabulary, whereas
# categorical_standardization just merges same-value spelling
# variants toward whichever variant is already most common, running
# it first could canonicalize a gender/country column toward
# "MALE" or "NG" instead of the specific form the spec requires.
# duplicates runs last so standardized values are compared, not raw
# ones, otherwise "Male"/"MALE" or " x " vs "x" would hide real
# duplicate rows.
# "column_names" is deliberately NOT a default: renaming headers changes how the user's document
# reads ("Customer Name" -> "customer_name"). It runs only when the user explicitly selects
# "Standardize column names" (rules=[..., "column_names"]).
DEFAULT_RULES = [
    "formatting",
    "missing_token_normalization",
    "numeric_text_cleaning",
    "gender_standardization",
    "country_standardization",
    "boolean_standardization",
    # case_standardization must run BEFORE categorical_standardization: that rule keeps the most common
    # spelling, which would otherwise preserve a lowercase/UPPERCASE majority ("sales", "LAGOS").
    "currency_label_standardization",
    "case_standardization",
    "categorical_standardization",
    "email_cleaning",
    "phone_cleaning",
    "date_standardization",
    # "missing_values" is deliberately NOT a default: imputation is an estimate, not a
    # fix. It still runs when explicitly requested (rules=[..., "missing_values"]).
    "duplicates",
]

# High-missingness threshold (MV-004): above this %, a column is
# flagged for manual review instead of auto-imputed.
HIGH_MISSING_THRESHOLD = 70


def handle_missing_values(df: pd.DataFrame, details: dict) -> tuple[pd.DataFrame, int]:
    """
    Rules MV-001 -> MV-004.

      - Numeric column, missing %  <= 70  -> fill with column median
        (robust to outliers, unlike mean).
      - Text/categorical column, missing % <= 70 -> fill with "Unknown"
        (keeps the fact that the value was absent, doesn't guess).
      - Any column with missing % > 70 -> left untouched and flagged
        for review; auto-filling that much of a column would mostly
        be inventing data.

    Returns the number of individual cells that were actually filled.
    """
    changed = 0
    per_column = {}
    flagged = []
    held_back = []
    total_rows = len(df)

    for col in df.columns:
        missing_count = int(df[col].isna().sum())
        if missing_count == 0:
            continue

        missing_pct = round((missing_count / total_rows) * 100, 2) if total_rows else 0.0

        if missing_pct > HIGH_MISSING_THRESHOLD:
            flagged.append({"column": col, "missing_percentage": missing_pct})
            per_column[col] = {"action": "review_required", "missing_percentage": missing_pct}
            continue

        # Semantic guard: a made-up phone number, email, identifier or date is worse than a
        # gap. These columns are left blank and reported, never filled with a median or
        # the string "Unknown". (profiling.never_impute holds back on modest confidence
        # because being wrong here is much costlier than leaving a cell empty.)
        col_profile = profiling.profile_column(str(col), df[col], total_rows)
        if profiling.never_impute(col_profile):
            held_back.append({"column": col, "semantic_type": col_profile.semantic_type,
                              "missing_percentage": missing_pct})
            per_column[col] = {
                "action": "not_imputable", "missing_percentage": missing_pct,
                "reason": f"{col_profile.semantic_type} values cannot be guessed",
            }
            continue

        if pd.api.types.is_bool_dtype(df[col]):
            # Booleans (e.g. from the boolean-standardization rule)
            # aren't well served by either median or "Unknown", fill
            # with whichever value (True/False) is more common.
            mode = df[col].mode(dropna=True)
            fill_value = bool(mode.iloc[0]) if not mode.empty else False
            action = "mode_imputation"
        elif pd.api.types.is_numeric_dtype(df[col]):
            action = "median_imputation"
            fill_value = None
        else:
            fill_value = "Unknown"
            action = "unknown_category"

        # Cells the user deliberately emptied through a resolution stay empty.
        orig_name = (details.get("_column_labels") or {}).get(col, col)
        protected = (details.get("_protected_blank") or {}).get(orig_name) or set()
        protected_missing = int(df[col].isna()[df.index.isin(list(protected))].sum()) if protected else 0
        if protected_missing:
            keep_blank = df[col].isna() & df.index.isin(list(protected))
            saved = df.loc[keep_blank, col].copy()
        else:
            keep_blank = None

        if action == "median_imputation":
            df[col], _ = detectors.fill_with_median(df[col])
        else:
            df[col] = df[col].fillna(fill_value)

        if keep_blank is not None:
            df[col] = df[col].astype(object).where(~keep_blank, None) if not pd.api.types.is_numeric_dtype(df[col]) \
                else df[col].mask(keep_blank)
            missing_count -= protected_missing
            per_column.setdefault(col, {})
        if missing_count <= 0:
            per_column[col] = {"action": "left_blank_by_user", "missing_percentage": missing_pct}
            continue
        changed += missing_count
        per_column[col] = {
            "action": action,
            "missing_percentage": missing_pct,
            "filled": missing_count,
        }

    details["missing_values"] = {
        "per_column": per_column,
        "flagged_for_review": flagged,
        "not_imputable": held_back,
        # provenance: how many cells in each column now hold an ESTIMATE, not an observation
        "imputed": {c: v["filled"] for c, v in per_column.items() if v.get("filled")},
    }
    return df, changed


def handle_duplicates(df: pd.DataFrame, details: dict) -> tuple[pd.DataFrame, int]:
    """
    Rules DP-001 / DP-002 (exact duplicates only for V1).

    A row is only treated as a duplicate if every column matches
    another row exactly. The first occurrence is kept, later ones are
    removed. Same-key-different-data cases (e.g. same customer_id,
    different city) are NOT touched here, that's a review case, not
    an auto-delete, so it's out of scope for this default rule.
    """
    before = len(df)
    duplicate_count = int(df.duplicated(keep="first").sum())
    # The original index labels are kept on purpose: they are the audit trail's row identity
    # ("row 17" means row 17 of the uploaded file). The pipeline resets the index just
    # before export.
    df = df.drop_duplicates(keep="first")
    removed = before - len(df)

    details["duplicates"] = {
        "duplicate_rows_found": duplicate_count,
        "removed": removed,
        "kept": "first",
    }
    return df, removed


def handle_formatting(df: pd.DataFrame, details: dict) -> tuple[pd.DataFrame, int]:
    """
    Rules FM-001 / FM-002 (the safe, always-on defaults).

      - Strip leading/trailing whitespace on every text column.
      - Collapse repeated internal whitespace to a single space.

    Case normalization (FM-003/004) and category/date standardization
    (FM-005/006) are deliberately NOT auto-applied here, the spec
    marks those "configurable"/"review", since blindly lowercasing a
    name column or remapping an unrecognized category can silently
    corrupt data. Those belong behind an explicit column-level config
    once that's defined, not in the default V1 pipeline.
    """
    changed = 0
    per_column = {}
    text_columns = df.select_dtypes(include=["object", "string"]).columns

    for col in text_columns:
        original = df[col].astype("string")
        cleaned = original.str.strip().str.replace(r"\s+", " ", regex=True)
        diff = int(((original != cleaned) & original.notna()).sum())
        if diff:
            df[col] = cleaned
            changed += diff
            per_column[col] = diff

    details["formatting"] = {"per_column_cells_changed": per_column}
    return df, changed


def handle_column_names(df: pd.DataFrame, details: dict) -> tuple[pd.DataFrame, int]:
    """
    Cleans up column headers: strips stray whitespace, collapses
    runs of non-alphanumeric characters (spaces, dashes, dots, etc.)
    to a single underscore, and lowercases the result, so
    " First Name", "first-name" and "FirstName " all become the
    predictable "first_name". This only touches header labels, never
    the data itself, so there's no risk of corrupting a cell value.

    If cleaning would make two columns collide (e.g. "Name" and
    "name"), the later one gets a numeric suffix instead of silently
    overwriting the first, nothing is ever dropped.
    """
    seen: dict[str, int] = {}
    renamed = {}
    new_columns = []

    for col in df.columns:
        cleaned = re.sub(r"[^\w]+", "_", str(col).strip())
        cleaned = re.sub(r"_+", "_", cleaned).strip("_").lower()
        if not cleaned:
            cleaned = "column"

        if cleaned in seen:
            seen[cleaned] += 1
            unique = f"{cleaned}_{seen[cleaned]}"
        else:
            seen[cleaned] = 1
            unique = cleaned

        new_columns.append(unique)
        if unique != col:
            renamed[str(col)] = unique

    df.columns = new_columns
    details["column_names"] = {"renamed": renamed}
    return df, len(renamed)


def handle_missing_token_normalization(df: pd.DataFrame, details: dict) -> tuple[pd.DataFrame, int]:
    """
    Recognizes common stand-ins for "no value", blank strings,
    "N/A", "null", "None", "-", "?", etc., and converts them to a
    real missing value (NaN), so the 'Fill gaps' rule and the
    quality report both see them as missing instead of as a
    legitimate text category. Deliberately excludes ambiguous words
    like "unknown" or "missing", since those can be genuine survey/
    category answers rather than placeholders, those are left as-is.
    """
    changed = 0
    per_column = {}
    text_columns = df.select_dtypes(include=["object", "string"]).columns

    for col in text_columns:
        series = df[col]
        is_token = series.apply(lambda v: isinstance(v, str) and detectors.is_missing_token(v))
        count = int(is_token.sum())
        if count:
            df.loc[is_token, col] = pd.NA
            changed += count
            per_column[col] = count

    details["missing_token_normalization"] = {"per_column": per_column}
    return df, changed


def handle_numeric_text_cleaning(df: pd.DataFrame, details: dict) -> tuple[pd.DataFrame, int]:
    """
    Some numeric columns get read in as text because of formatting
    noise, currency symbols, thousands separators, percent signs,
    accounting-style parentheses for negatives ("$1,200.50", "(45)").
    This strips that noise and converts the column to a real numeric
    dtype, but only when EVERY non-null value in the column converts
    cleanly, a single genuinely non-numeric value (e.g. a real word)
    aborts the conversion for that column entirely, since a partial
    conversion could silently misrepresent mixed data. Columns that
    don't fully convert are left untouched (they may surface in the
    quality report as a mixed-type column instead).
    """
    changed = 0
    per_column = {}
    text_columns = df.select_dtypes(include=["object", "string"]).columns

    for col in text_columns:
        series = df[col]
        non_null = series.dropna().astype(str)
        if len(non_null) < 2:
            continue

        # Never coerce identifiers (phone numbers, account numbers,
        # postal codes, reference codes, ...) to a numeric dtype.
        # See detectors.is_identifier_column for why.
        if detectors.is_identifier_column(str(col), non_null):
            continue

        cleaned_strs = non_null.map(detectors.strip_numeric_noise)
        # "free" in an amount/price column means 0 (only there; elsewhere it is just a word)
        if set(profiling.name_tokens(str(col))) & detectors.MONEY_NAME_TOKENS:
            is_free = non_null.str.strip().str.lower().isin(detectors.ZERO_WORDS)
            cleaned_strs = cleaned_strs.where(~is_free, "0")
        parsed = cleaned_strs.map(detectors.try_parse_float)
        if parsed.isna().any():
            continue  # not every value converts cleanly -> leave column alone

        # If nothing needed noise stripped, the *values* are already
        # bare numbers, but the *column* may still be stuck in a
        # text dtype (e.g. missing_token_normalization just emptied
        # out "n/a"/"none"/"--" placeholders, which stops pandas from
        # ever re-inferring the dtype on its own). Only skip when the
        # column is already numeric; otherwise there IS something to
        # do, fix the dtype, even though no characters changed.
        diff_mask = non_null != cleaned_strs
        already_numeric = pd.api.types.is_numeric_dtype(df[col])
        if not diff_mask.any() and already_numeric:
            continue

        full_values = pd.Series(pd.NA, index=series.index, dtype="object")
        full_values.loc[non_null.index] = parsed.values
        numeric_col = pd.to_numeric(full_values, errors="coerce")

        # If every parsed value is a whole number (no decimal point in
        # the source, nothing fractional after stripping noise), keep
        # it looking like one. Plain float64 would round-trip through
        # export as "43.0" instead of "43", numerically identical but
        # a needless, confusing change to how the user's data reads.
        # pandas' nullable Int64 preserves both whole-number formatting
        # and NaN slots (plain int64 can't hold NaN at all).
        non_na = numeric_col.dropna()
        if not non_na.empty and (non_na % 1 == 0).all():
            df[col] = numeric_col.astype("Int64")
        else:
            df[col] = numeric_col

        count = int(diff_mask.sum())
        changed += count

        if count:
            # Classify what kind of noise was actually stripped,
            # purely for reporting (cleaning/summary.py splits this
            # into "Currency formatting cleaned" vs "Percentage
            # values normalized" per the spec), doesn't change the
            # cleaning behavior itself, a column can only be tagged
            # one way even if (rarely) it had both kinds of noise.
            has_percent = bool(non_null.str.contains("%", regex=False).any())
            has_currency = bool(non_null.map(currencies.has_currency).any())
            value_kind = "percentage" if has_percent else ("currency" if has_currency else "formatting")
            per_column[col] = {"changed": count, "type": value_kind}
        else:
            # No characters needed stripping, this is a dtype-only
            # fix (e.g. an age column left as text after missing
            # placeholders were normalized to NaN). No cells were
            # altered, so it's not counted in "changed", but it's
            # still reported so the summary reflects the dtype fix.
            per_column[col] = {"changed": 0, "type": "dtype_conversion"}

    details["numeric_text_cleaning"] = {"per_column": per_column}
    return df, changed


def handle_boolean_standardization(df: pd.DataFrame, details: dict) -> tuple[pd.DataFrame, int]:
    """
    Standardizes columns whose only non-null values are yes/no-style
    words (Yes/No, Y/N, True/False, T/F, in any casing) into real
    booleans. "1"/"0" are also accepted, but ONLY for columns whose
    NAME signals a boolean flag (is_active, active, status, enabled,
    verified, ..., see detectors.is_boolean_flag_column): plenty of
    generically-named columns use 1/0 as legitimate numeric codes,
    not booleans, so guessing there would be unsafe. Handles both
    text columns (e.g. "1"/"0" read as strings because the column was
    mixed with words elsewhere) and columns pandas already read as a
    clean numeric dtype (e.g. an is_active column that happened to be
    all-1s-and-0s and got read in as int64).
    """
    changed = 0
    per_column = {}

    text_columns = df.select_dtypes(include=["object", "string"]).columns
    for col in text_columns:
        series = df[col]
        non_null = series.dropna().astype(str).str.strip()
        if non_null.empty:
            continue
        lowered = non_null.str.lower()
        unique_vals = set(lowered.unique())

        name_is_flag = detectors.is_boolean_flag_column(str(col))
        allowed_words = detectors.EXTENDED_BOOLEAN_WORDS if name_is_flag else detectors.BOOLEAN_WORDS
        true_words = detectors.EXTENDED_TRUE_WORDS if name_is_flag else detectors.TRUE_WORDS
        false_words = detectors.EXTENDED_FALSE_WORDS if name_is_flag else detectors.FALSE_WORDS

        if not unique_vals:
            continue
        if not unique_vals.issubset(allowed_words):
            # A flag column (is_verified, active...) with one or two stray values ("maybe") still has
            # ten different spellings of yes/no. Unify the recognised ones to Yes / No text and leave the
            # stray values exactly as typed (they are reported, not guessed). Never for generically named columns.
            recognised = lowered.isin(allowed_words)
            if not name_is_flag or recognised.mean() < 0.9 or recognised.all():
                continue
            yes_no = lowered[recognised].map(lambda v: "Yes" if v in true_words else "No")
            df.loc[yes_no.index, col] = yes_no
            changed += int(len(yes_no))
            per_column[col] = {"changed": int(len(yes_no)), "partial": True,
                               "left_as_is": sorted(unique_vals - allowed_words)[:5]}
            continue

        mapped = lowered.map(lambda v: True if v in true_words else False)
        new_series = pd.Series(pd.NA, index=series.index, dtype="object")
        new_series.loc[non_null.index] = mapped.values
        df[col] = new_series.astype("boolean")

        count = len(non_null)
        changed += count
        per_column[col] = {
            "changed": count,
            "true_words_seen": sorted(unique_vals & true_words),
            "false_words_seen": sorted(unique_vals & false_words),
        }

    # Numeric columns: only ever touched when the NAME says "this is
    # a flag" AND every non-null value is exactly 0 or 1, a
    # generic numeric column is never reinterpreted as a boolean.
    numeric_columns = df.select_dtypes(include=["number"]).columns
    for col in numeric_columns:
        if not detectors.is_boolean_flag_column(str(col)):
            continue
        series = df[col]
        non_null = series.dropna()
        if non_null.empty or not non_null.isin([0, 1]).all():
            continue

        df[col] = series.map(lambda v: None if pd.isna(v) else bool(int(v))).astype("boolean")
        count = int(len(non_null))
        changed += count
        per_column[col] = {"changed": count, "true_words_seen": ["1"], "false_words_seen": ["0"]}

    details["boolean_standardization"] = {"per_column": per_column}
    return df, changed


_CURRENCY_LABELS = {
    "ngn": "NGN", "naira": "NGN", "\u20a6": "NGN", "nigerian naira": "NGN",
    "gbp": "GBP", "\u00a3": "GBP", "pound": "GBP", "pounds": "GBP", "sterling": "GBP", "pound sterling": "GBP",
    "usd": "USD", "$": "USD", "dollar": "USD", "dollars": "USD", "us dollar": "USD", "us dollars": "USD",
    "eur": "EUR", "\u20ac": "EUR", "euro": "EUR", "euros": "EUR",
}
_CURRENCY_COLUMN_TOKENS = {"currency", "ccy", "curr"}


def handle_currency_label_standardization(df: pd.DataFrame, details: dict) -> tuple[pd.DataFrame, int]:
    """
    A column that NAMES the currency ("currency", "currency_label", "ccy") but spells it five ways
    (naira / \u20a6 / NGN, \u00a3 / GBP) is brought to ISO codes. Only unambiguous names and symbols are mapped
    ("$" is read as USD); combined values such as "NGN/USD" and anything unknown are left as typed.
    """
    changed = 0
    per_column = {}
    for col in df.select_dtypes(include=["object", "string"]).columns:
        if not (set(profiling.name_tokens(str(col))) & _CURRENCY_COLUMN_TOKENS):
            continue
        series = df[col]
        mask = series.map(lambda v: isinstance(v, str) and v.strip().lower() in _CURRENCY_LABELS
                          and _CURRENCY_LABELS[v.strip().lower()] != v)
        count = int(mask.sum())
        if not count:
            continue
        df.loc[mask, col] = series[mask].map(lambda v: _CURRENCY_LABELS[v.strip().lower()])
        changed += count
        per_column[col] = {"changed": count}
    details["currency_label_standardization"] = {"per_column": per_column}
    return df, changed


def handle_case_standardization(df: pd.DataFrame, details: dict) -> tuple[pd.DataFrame, int]:
    """
    Capitalisation consistency (see cleaning/casing.py for the principles).

      - Person names typed entirely in lower or UPPER case become Title Case
        ("DAVID ADEYEMI" -> "David Adeyemi"). Mixed-case names (McDonald, O'Brien) are never touched.
      - ID prefixes follow the column's majority ("cust-0011" -> "CUST-0011").
      - A label column that mixes styles ("active", "INACTIVE", "On hold") is brought to one style.
        Short all-caps tokens (HR, IT, NGN) are kept as acronyms.
    """
    changed = 0
    per_column = {}
    for col, (kind, plan) in casing.plan_dataframe(df).items():
        series = df[col]
        mask = series.map(lambda v: isinstance(v, str) and v in plan)
        count = int(mask.sum())
        if not count:
            continue
        df.loc[mask, col] = series[mask].map(plan)
        changed += count
        per_column[col] = {"changed": count, "kind": kind, "values_fixed": len(plan)}
    details["case_standardization"] = {"per_column": per_column}
    return df, changed


def handle_categorical_standardization(df: pd.DataFrame, details: dict) -> tuple[pd.DataFrame, int]:
    """
    Merges values that are the SAME category spelled differently only
    by case, spacing, hyphens or dots, "Male" / "MALE" / "male" and "Walk in" / "Walk-in" /
    "Web site" / "Website" all become one
    value. The canonical spelling chosen is whichever variant already
    appears most often in the data (ties broken alphabetically), so
    nothing is invented, it's just consolidation. Only considered for
    columns that look categorical (a small, repeated set of values,
    not free text or near-unique IDs), so it never touches something
    like a names or addresses column. Genuinely different labels for
    the same idea (e.g. "NY" vs "New York") are NOT merged, that
    requires judgement a rule shouldn't make, and is left for the
    quality report's "inconsistent categories" finding instead.
    """
    changed = 0
    per_column = {}
    total_rows = len(df)
    text_columns = df.select_dtypes(include=["object", "string"]).columns

    for col in text_columns:
        series = df[col]
        non_null = series.dropna().astype(str)
        nunique = non_null.nunique()
        if nunique < 2 or nunique > 50 or (total_rows and nunique > total_rows * 0.5):
            continue

        groups: dict[str, list[str]] = {}
        for v in non_null.unique():
            groups.setdefault(casing.variant_key(v), []).append(v)

        variant_groups = {k: v for k, v in groups.items() if len(v) > 1}
        if not variant_groups:
            continue

        value_counts = non_null.value_counts()
        remap = {}
        for variants in variant_groups.values():
            canonical = casing.canonical_variant(variants, value_counts)
            for v in variants:
                if v != canonical:
                    remap[v] = canonical

        if not remap:
            continue

        mask = non_null.isin(remap.keys())
        df.loc[non_null.index[mask], col] = non_null[mask].map(remap)
        count = int(mask.sum())
        changed += count
        per_column[col] = {"changed": count, "groups_merged": len(variant_groups)}

    details["categorical_standardization"] = {"per_column": per_column}
    return df, changed


def handle_email_cleaning(df: pd.DataFrame, details: dict) -> tuple[pd.DataFrame, int]:
    """
    For columns that look like email addresses (by column name, or
    because most of the values match an email shape), trims stray
    whitespace and lowercases the address. Lowercasing is a common,
    low-risk normalization for emails since domains are case-
    insensitive and almost all real-world mailboxes treat the local
    part the same way, but it IS an assumption, so it's scoped
    tightly to columns that are confidently email columns. Validity
    (is this actually a well-formed email?) is not judged here, see
    the quality report's email-validation finding for that.
    """
    changed = 0
    per_column = {}
    text_columns = df.select_dtypes(include=["object", "string"]).columns

    for col in text_columns:
        series = df[col]
        if not detectors.is_email_column(str(col), series):
            continue

        original = series.astype("string")
        cleaned = original.str.strip().str.lower()
        diff = (original != cleaned) & original.notna()
        count = int(diff.sum())
        if count:
            df[col] = cleaned
            changed += count
            per_column[col] = count

    details["email_cleaning"] = {"per_column": per_column}
    return df, changed


def handle_phone_cleaning(df: pd.DataFrame, details: dict) -> tuple[pd.DataFrame, int]:
    """
    For columns that look like phone numbers (by column name only, phone formats vary too much by country to detect reliably from
    values alone):

      - Strips spaces, brackets, hyphens, and dots down to digits
        plus a single leading "+" where present, e.g.
        "+234 806-123-4567" -> "+2348061234567",
        "(234) 806 123 4567" -> "2348061234567". Only applied when the
        value contains nothing but digits and that expected
        punctuation (see detectors.normalize_phone_punctuation), a
        value with an extension ("ext. 204") or other text is left
        completely untouched rather than risk mangling it.
      - Detects values a spreadsheet mangled into scientific notation
        (e.g. "1.343E+12") and recovers the original digits ONLY when
        that's provably lossless (no invented trailing zeros, see
        detectors.try_recover_scientific_phone). Otherwise the value
        is left exactly as-is and reported so the frontend can flag
        it, see cleaning/quality_report.py's
        unrecoverable_scientific_notation finding.

    Never converts the column to a numeric dtype, never re-groups
    digits, and never adds/removes a country code, that would
    require knowing the number's country; see
    cleaning/phone_formats.normalize_phone for the separate,
    user-confirmed way to do that.
    """
    changed = 0
    per_column = {}
    unrecoverable = {}
    text_columns = df.select_dtypes(include=["object", "string"]).columns

    for col in text_columns:
        if not detectors.is_phone_column(str(col)):
            continue

        original = df[col].astype("string")
        new_values = original.copy()
        col_changed = 0
        col_recovered = 0
        flagged_samples: list[str] = []

        for idx, val in original.items():
            if val is pd.NA or (isinstance(val, float) and pd.isna(val)):
                continue
            s = str(val)

            if detectors.is_scientific_notation(s):
                recovered = detectors.try_recover_scientific_phone(s)
                if recovered:
                    new_values.at[idx] = recovered
                    col_changed += 1
                    col_recovered += 1
                elif len(flagged_samples) < 5:
                    flagged_samples.append(s)
                continue

            cleaned = detectors.normalize_phone_punctuation(s)
            if cleaned is not None:
                new_values.at[idx] = cleaned
                col_changed += 1

        if col_changed:
            df[col] = new_values
            changed += col_changed
            per_column[col] = {"changed": col_changed, "scientific_notation_recovered": col_recovered}
        if flagged_samples:
            unrecoverable[col] = flagged_samples

    details["phone_cleaning"] = {"per_column": per_column, "scientific_notation_unrecoverable": unrecoverable}
    return df, changed


def handle_gender_standardization(df: pd.DataFrame, details: dict) -> tuple[pd.DataFrame, int]:
    """
    For columns that look like a gender field (by column name), maps
    recognized male/female variants (MALE, Male, male, M / FEMALE,
    Female, female, F) to the canonical lowercase "male"/"female".
    Only values that already match this fixed vocabulary are touched, anything else (a genuine "Other"/"Non-binary"/"Prefer not to
    say" answer, or a typo) is left completely untouched, never
    guessed at. Never infers gender from a name or any other column.
    Missing-style values ("Unknown", "N/A", ...) are handled by
    missing_token_normalization, which runs before this rule.
    """
    changed = 0
    per_column = {}
    text_columns = df.select_dtypes(include=["object", "string"]).columns

    for col in text_columns:
        if not detectors.is_gender_column(str(col)):
            continue

        series = df[col]
        non_null = series.dropna().astype(str)
        stripped = non_null.str.strip()
        lowered = stripped.str.lower()

        recognized = lowered.isin(detectors.GENDER_WORDS)
        if not recognized.any():
            continue

        mapped = lowered[recognized].map(
            lambda v: "male" if v in detectors.MALE_WORDS else "female"
        )
        diff = stripped[recognized].values != mapped.values
        count = int(diff.sum())
        if not count:
            continue

        new_series = series.astype("object").copy()
        new_series.loc[mapped.index] = mapped.values
        df[col] = new_series

        changed += count
        per_column[col] = {"changed": count}

    details["gender_standardization"] = {"per_column": per_column}
    return df, changed


def handle_country_standardization(df: pd.DataFrame, details: dict) -> tuple[pd.DataFrame, int]:
    """
    For columns that look like a country field (by column name), maps
    a recognized country CODE ("NG") or differently-cased/spaced NAME
    ("nigeria") to the single canonical name ("Nigeria"), see
    cleaning/phone_formats.COUNTRIES, the same curated reference used
    for phone-number country resolution. A value that isn't in that
    reference (a typo, an unlisted country) is left completely
    untouched rather than fuzzy-matched, see
    detectors.standardize_country_value.
    """
    changed = 0
    per_column = {}
    text_columns = df.select_dtypes(include=["object", "string"]).columns

    for col in text_columns:
        if not detectors.is_country_column(str(col)):
            continue

        series = df[col]
        non_null = series.dropna().astype(str)
        stripped = non_null.str.strip()

        mapped = stripped.map(detectors.standardize_country_value)
        recognized = mapped.notna()
        if not recognized.any():
            continue

        diff = stripped[recognized].values != mapped[recognized].values
        count = int(diff.sum())
        if not count:
            continue

        new_series = series.astype("object").copy()
        idx_to_update = mapped.index[recognized]
        new_series.loc[idx_to_update] = mapped[recognized].values
        df[col] = new_series

        changed += count
        per_column[col] = {"changed": count}

    details["country_standardization"] = {"per_column": per_column}
    return df, changed


def handle_date_standardization(df: pd.DataFrame, details: dict) -> tuple[pd.DataFrame, int]:
    """
    For columns that look like dates, converts them to a single ISO
    format (YYYY-MM-DD), but ONLY when the format is unambiguous,
    i.e. day-first and month-first parsing agree on every value
    (which is always true for already-ISO dates, and also true
    whenever every day-of-month happens to be >12). If the column
    could reasonably mean two different dates depending on
    convention (e.g. "03/04/2024", 3rd April or March 4th?), it is
    left completely untouched; the quality report flags it for a
    human to confirm instead of guessing.
    """
    changed = 0
    per_column = {}
    skipped = []
    skipped_time = []
    text_columns = df.select_dtypes(include=["object", "string"]).columns

    for col in text_columns:
        series = df[col]
        if not detectors.is_probable_date_column(str(col), series):
            continue

        # A column that carries times of day would lose them if rewritten as a bare date, and
        # 2-digit years force a century guess ("68" -> 1968 or 2068?). Both are left to a
        # human rather than silently changed.
        if detectors.has_time_component(series):
            skipped_time.append(col)
            continue
        if detectors.date_parse_stats(series)["two_digit_year"]:
            skipped.append(col)
            continue

        parsed = detectors.unambiguous_date_parse(series)
        if parsed is None:
            skipped.append(col)
            continue

        formatted = parsed.dt.strftime("%Y-%m-%d")
        original = series.astype("string")
        new_values = pd.Series(pd.NA, index=series.index, dtype="string")
        has_value = parsed.notna()
        new_values.loc[has_value] = formatted[has_value]
        # Leave any value that failed to parse exactly as it was,
        # rather than blanking it out.
        new_values.loc[~has_value & original.notna()] = original[~has_value & original.notna()]

        diff = (original != new_values) & original.notna()
        count = int(diff.sum())
        if count:
            df[col] = new_values
            changed += count
            per_column[col] = {"changed": count, "format": "YYYY-MM-DD"}

    details["date_standardization"] = {
        "per_column": per_column, "skipped_ambiguous": skipped, "skipped_has_time_component": skipped_time,
    }
    return df, changed


RULE_DISPATCH = {
    "missing_values": handle_missing_values,
    "duplicates": handle_duplicates,
    "formatting": handle_formatting,
    "column_names": handle_column_names,
    "missing_token_normalization": handle_missing_token_normalization,
    "numeric_text_cleaning": handle_numeric_text_cleaning,
    "gender_standardization": handle_gender_standardization,
    "country_standardization": handle_country_standardization,
    "boolean_standardization": handle_boolean_standardization,
    "currency_label_standardization": handle_currency_label_standardization,
    "case_standardization": handle_case_standardization,
    "categorical_standardization": handle_categorical_standardization,
    "email_cleaning": handle_email_cleaning,
    "phone_cleaning": handle_phone_cleaning,
    "date_standardization": handle_date_standardization,
}


def _split_held(df, labels, held_out, rule_name):
    """Parks the columns a user-configured rule owns so `rule_name` cannot touch them."""
    if not held_out:
        return None
    cols = [c for c in df.columns if rule_name in held_out.get(labels.get(c, c), ())]
    if not cols:
        return None
    return df.drop(columns=cols), df[cols]


def _rejoin_held(df, parked, original_order):
    for c in parked.columns:
        df[c] = parked[c]
    return df[[c for c in original_order if c in df.columns] + [c for c in df.columns if c not in original_order]]


def _rename_labels(labels: dict, before_cols, after_cols) -> dict:
    """Keep a current-name -> name-in-the-uploaded-file map after a step renames headers
    (rules rename positionally and never reorder or drop columns)."""
    if list(before_cols) == list(after_cols):
        return labels
    if len(before_cols) == len(after_cols):
        return {a: labels.get(b, b) for b, a in zip(before_cols, after_cols)}
    return {c: labels.get(c, c) for c in after_cols}


def apply_rules(
    df: pd.DataFrame,
    rules: list[str] | None = None,
    audit: AuditLog | None = None,
    approval: str | None = None,
    column_labels: dict | None = None,
    protected_blank: dict | None = None,
    held_out: dict | None = None,
) -> tuple[pd.DataFrame, dict]:
    """
    rules=None            -> run DEFAULT_RULES (the normal "Clean my
                              data" button with nothing unchecked)
    rules=[]               -> run NOTHING (the user unchecked every
                              rule on purpose, respect that)
    rules=["some_rule"]   -> run only the rules explicitly listed

    audit (optional): an AuditLog. When given, every step is diffed (before vs after) and
    recorded: what changed, why, which rule id, examples, counts, confidence, and
    whether it was automatic (default set) or user-selected (explicit list). The counts
    come from comparing the data, not from what a rule says it did.

    held_out (optional): {column name in the uploaded file: {default rule names}}. A column the
    user configured explicitly (cleaning profile) is not touched by the listed automatic rules, so
    their chosen output format is never re-written. Whitespace formatting, duplicates and the other
    rules still see every column.
    """
    explicit = rules is not None
    selected = DEFAULT_RULES if rules is None else rules
    approval = approval or (M.APPROVAL_USER_SELECTED if explicit else M.APPROVAL_AUTOMATIC)
    changes = {}
    details: dict = {}  # fresh per call, never shared across jobs/requests
    labels = dict(column_labels) if column_labels else {c: c for c in df.columns}
    # cells a user deliberately emptied via a resolution: {name in uploaded file: {row labels}}
    details["_protected_blank"] = protected_blank or {}

    for rule_name in selected:
        fn = RULE_DISPATCH.get(rule_name)
        if not fn:
            continue
        before = df.copy() if audit is not None else None
        before_cols = list(df.columns)
        details["_column_labels"] = labels
        held = _split_held(df, labels, held_out, rule_name)
        if held is not None:
            df, parked = held
        df, count = fn(df, details)
        if held is not None:
            df = _rejoin_held(df, parked, before_cols)
        changes[f"{rule_name}_changed"] = count

        if audit is not None:
            spec = spec_for_fixer(rule_name)
            audit.record_step(
                rule_name, before, df,
                rule_id=spec.id if spec else None,
                operation=spec.operation if spec else M.NORMALIZATION,
                confidence=spec.fix_confidence if spec else None,
                approval=approval,
                reason=spec.explanation if spec else "",
                reversibility=spec.reversibility if spec else M.REVERSIBLE,
                column_labels=labels,
            )
        labels = _rename_labels(labels, before_cols, list(df.columns))

    details.pop("_protected_blank", None)
    details.pop("_column_labels", None)
    log = {"rules_applied": selected, "changes": changes, "details": details}
    log["imputed"] = dict((details.get("missing_values") or {}).get("imputed") or {})
    log["column_labels"] = labels
    return df, log
