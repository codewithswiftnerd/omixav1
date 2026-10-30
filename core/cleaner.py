"""
Omixa Core Cleaning Engine
Handles all data cleaning, standardization, and transformation logic.
"""

import pandas as pd
import numpy as np
import re
from datetime import datetime
from typing import Any


# ──────────────────────────────────────────────
# DOMAIN PROFILES
# ──────────────────────────────────────────────

DOMAIN_PROFILES = {
    "healthcare": {
        "date_columns": ["dob", "date_of_birth", "admission_date", "discharge_date",
                         "visit_date", "test_date", "sample_date"],
        "id_columns": ["patient_id", "mrn", "npi", "record_id"],
        "numeric_columns": ["age", "weight", "height", "bmi", "blood_pressure",
                            "heart_rate", "temperature", "dosage"],
        "categorical_columns": ["gender", "sex", "blood_type", "diagnosis", "icd_code",
                                "status", "department"],
        "phone_columns": ["phone", "contact", "emergency_contact"],
        "email_columns": ["email"],
        "outlier_sensitivity": "moderate",
    },
    "finance": {
        "date_columns": ["transaction_date", "date", "posting_date", "value_date",
                         "settlement_date", "invoice_date"],
        "id_columns": ["account_id", "transaction_id", "invoice_id", "customer_id"],
        "numeric_columns": ["amount", "balance", "credit", "debit", "fee",
                            "interest_rate", "quantity", "price", "tax"],
        "categorical_columns": ["currency", "status", "type", "category",
                                "payment_method", "account_type"],
        "phone_columns": ["phone", "contact"],
        "email_columns": ["email"],
        "outlier_sensitivity": "low",   # finance tolerates extreme values
    },
    "general": {
        "date_columns": ["date", "created_at", "updated_at", "timestamp"],
        "id_columns": ["id", "uuid", "record_id"],
        "numeric_columns": [],
        "categorical_columns": [],
        "phone_columns": ["phone", "mobile", "contact"],
        "email_columns": ["email"],
        "outlier_sensitivity": "moderate",
    },
}


# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────

def _col_matches(col: str, keywords: list[str]) -> bool:
    """True if column name contains any keyword (case-insensitive)."""
    col_lower = col.lower().replace(" ", "_")
    return any(kw in col_lower for kw in keywords)


def _is_numeric_col(series: pd.Series) -> bool:
    """Heuristic: can this column be coerced to numeric?"""
    coerced = pd.to_numeric(series.dropna(), errors='coerce')
    return coerced.notna().mean() > 0.7


def _is_date_col(series: pd.Series) -> bool:
    """Heuristic: does this column look like dates?"""
    sample = series.dropna().astype(str).head(50)
    date_pattern = re.compile(
        r'\d{1,4}[-/\.]\d{1,2}[-/\.]\d{1,4}|'
        r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}'
    )
    hits = sample.apply(lambda x: bool(date_pattern.search(x))).sum()
    return hits / max(len(sample), 1) > 0.6


# ──────────────────────────────────────────────
# STEP 1 – COLUMN NAMES
# ──────────────────────────────────────────────

def clean_column_names(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    original = list(df.columns)
    df.columns = (
        df.columns
        .str.strip()
        .str.lower()
        .str.replace(r'[\s\-]+', '_', regex=True)
        .str.replace(r'[^\w]', '', regex=True)
        .str.replace(r'_+', '_', regex=True)
        .str.strip('_')
    )
    renamed = [f"'{o}' → '{n}'" for o, n in zip(original, df.columns) if o != n]
    return df, renamed


# ──────────────────────────────────────────────
# STEP 2 – DUPLICATES
# ──────────────────────────────────────────────

def remove_duplicates(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    before = len(df)
    df = df.drop_duplicates()
    return df, before - len(df)


# ──────────────────────────────────────────────
# STEP 3 – MISSING VALUES
# ──────────────────────────────────────────────

def handle_missing_values(df: pd.DataFrame, strategy: str = "auto") -> tuple[pd.DataFrame, dict]:
    report = {}
    for col in df.columns:
        missing_count = df[col].isna().sum()
        if missing_count == 0:
            continue

        pct = missing_count / len(df)
        report[col] = {"missing": int(missing_count), "pct": round(pct * 100, 2)}

        if pct > 0.6:
            # Drop column if more than 60% missing
            df = df.drop(columns=[col])
            report[col]["action"] = "column_dropped"
            continue

        if strategy == "auto":
            if pd.api.types.is_numeric_dtype(df[col]):
                fill_val = df[col].median()
                df[col] = df[col].fillna(fill_val)
                report[col]["action"] = f"filled_with_median ({round(fill_val, 4)})"
            else:
                fill_val = df[col].mode().iloc[0] if not df[col].mode().empty else "Unknown"
                df[col] = df[col].fillna(fill_val)
                report[col]["action"] = f"filled_with_mode ({fill_val})"
        elif strategy == "drop_rows":
            df = df.dropna(subset=[col])
            report[col]["action"] = "rows_dropped"
        elif strategy == "fill_zero":
            df[col] = df[col].fillna(0)
            report[col]["action"] = "filled_with_zero"

    return df, report


# ──────────────────────────────────────────────
# STEP 4 – DATA TYPES
# ──────────────────────────────────────────────

def fix_data_types(df: pd.DataFrame, profile: dict) -> tuple[pd.DataFrame, dict]:
    report = {}

    for col in df.columns:
        original_dtype = str(df[col].dtype)

        # Date columns
        if _col_matches(col, profile["date_columns"]) or _is_date_col(df[col]):
            try:
                df[col] = pd.to_datetime(df[col], infer_datetime_format=True, errors='coerce')
                report[col] = f"{original_dtype} → datetime"
            except Exception:
                pass

        # Numeric columns
        elif _col_matches(col, profile["numeric_columns"]) or _is_numeric_col(df[col]):
            if not pd.api.types.is_numeric_dtype(df[col]):
                # Strip currency symbols, commas, percent signs
                cleaned = (
                    df[col].astype(str)
                    .str.replace(r'[£$€₦,\s%]', '', regex=True)
                    .str.replace(r'\((\d+\.?\d*)\)', r'-\1', regex=True)  # (123) → -123
                )
                coerced = pd.to_numeric(cleaned, errors='coerce')
                if coerced.notna().mean() > 0.7:
                    df[col] = coerced
                    report[col] = f"{original_dtype} → numeric"

        # Categorical
        elif _col_matches(col, profile["categorical_columns"]):
            if df[col].nunique() / max(len(df), 1) < 0.2:
                df[col] = df[col].astype('category')
                report[col] = f"{original_dtype} → category"

    return df, report


# ──────────────────────────────────────────────
# STEP 5 – STRING STANDARDIZATION
# ──────────────────────────────────────────────

def standardize_strings(df: pd.DataFrame, profile: dict) -> tuple[pd.DataFrame, dict]:
    report = {}

    for col in df.select_dtypes(include=['object', 'string']).columns:
        changes = []

        # Strip whitespace
        df[col] = df[col].astype(str).str.strip()

        # Emails
        if _col_matches(col, profile["email_columns"]):
            df[col] = df[col].str.lower().str.strip()
            invalid = ~df[col].str.match(r'^[\w\.\+\-]+@[\w\-]+\.[a-z]{2,}$', na=False)
            if invalid.sum():
                df.loc[invalid, col] = np.nan
                changes.append(f"{invalid.sum()} invalid emails nulled")

        # Phones
        elif _col_matches(col, profile["phone_columns"]):
            df[col] = (
                df[col].astype(str)
                .str.replace(r'[\s\-\(\)\.]', '', regex=True)
                .str.replace(r'^00', '+', regex=True)
            )
            changes.append("phone normalized")

        # General text
        else:
            # Title-case short categoricals
            if df[col].nunique() < 50:
                df[col] = df[col].str.title()
                changes.append("title-cased")

            # Remove special chars
            df[col] = df[col].str.replace(r'[^\w\s\-\.\,\(\)\@\/\+]', '', regex=True)

        if changes:
            report[col] = changes

    return df, report


# ──────────────────────────────────────────────
# STEP 6 – OUTLIER DETECTION
# ──────────────────────────────────────────────

def detect_and_handle_outliers(
    df: pd.DataFrame,
    sensitivity: str = "moderate",
    action: str = "flag"
) -> tuple[pd.DataFrame, dict]:
    """
    sensitivity: 'low' (z>4), 'moderate' (z>3), 'high' (z>2.5)
    action: 'flag' (add boolean col) | 'cap' (winsorize) | 'remove'
    """
    thresholds = {"low": 4.0, "moderate": 3.0, "high": 2.5}
    z_thresh = thresholds.get(sensitivity, 3.0)
    report = {}

    for col in df.select_dtypes(include=[np.number]).columns:
        series = df[col].dropna()
        if len(series) < 10:
            continue

        mean, std = series.mean(), series.std()
        if std == 0:
            continue

        z_scores = ((df[col] - mean) / std).abs()
        outlier_mask = z_scores > z_thresh
        n_outliers = outlier_mask.sum()

        if n_outliers == 0:
            continue

        if action == "flag":
            df[f"{col}_outlier_flag"] = outlier_mask
        elif action == "cap":
            lower = mean - z_thresh * std
            upper = mean + z_thresh * std
            df[col] = df[col].clip(lower=lower, upper=upper)
        elif action == "remove":
            df = df[~outlier_mask]

        report[col] = {"outliers": int(n_outliers), "action": action}

    return df, report


# ──────────────────────────────────────────────
# STEP 7 – DOMAIN-SPECIFIC RULES
# ──────────────────────────────────────────────

def apply_domain_rules(df: pd.DataFrame, domain: str) -> tuple[pd.DataFrame, list[str]]:
    notes = []

    if domain == "healthcare":
        # Age sanity check
        for col in df.columns:
            if "age" in col and pd.api.types.is_numeric_dtype(df[col]):
                invalid = (df[col] < 0) | (df[col] > 130)
                if invalid.sum():
                    df.loc[invalid, col] = np.nan
                    notes.append(f"Nulled {invalid.sum()} impossible ages in '{col}'")

        # BMI sanity check
        for col in df.columns:
            if "bmi" in col and pd.api.types.is_numeric_dtype(df[col]):
                invalid = (df[col] < 10) | (df[col] > 80)
                if invalid.sum():
                    df.loc[invalid, col] = np.nan
                    notes.append(f"Nulled {invalid.sum()} impossible BMI values in '{col}'")

        # Gender standardization
        for col in df.columns:
            if col in ["gender", "sex"]:
                mapping = {
                    "m": "Male", "male": "Male", "man": "Male", "1": "Male",
                    "f": "Female", "female": "Female", "woman": "Female", "2": "Female",
                    "o": "Other", "other": "Other", "non-binary": "Other",
                }
                df[col] = df[col].astype(str).str.lower().str.strip().map(mapping).fillna(df[col])
                notes.append(f"Standardized gender values in '{col}'")

    elif domain == "finance":
        # Ensure amounts are positive where expected
        for col in df.columns:
            if any(k in col for k in ["amount", "price", "fee", "balance"]):
                if pd.api.types.is_numeric_dtype(df[col]):
                    if df[col].lt(0).sum() > 0:
                        notes.append(
                            f"Warning: '{col}' contains {df[col].lt(0).sum()} negative values"
                        )

        # Currency standardization
        for col in df.columns:
            if "currency" in col:
                df[col] = df[col].astype(str).str.upper().str.strip()
                notes.append(f"Uppercased currency codes in '{col}'")

    return df, notes


# ──────────────────────────────────────────────
# MASTER CLEAN FUNCTION
# ──────────────────────────────────────────────

def clean_dataset(
    df: pd.DataFrame,
    domain: str = "general",
    missing_strategy: str = "auto",
    outlier_action: str = "flag",
) -> dict:
    """
    Full cleaning pipeline. Returns cleaned df + detailed report.
    """
    profile = DOMAIN_PROFILES.get(domain, DOMAIN_PROFILES["general"])
    report = {
        "domain": domain,
        "original_shape": df.shape,
        "steps": {},
        "warnings": [],
    }
    start = datetime.utcnow()

    # 1. Column names
    df, renamed = clean_column_names(df)
    report["steps"]["column_names"] = renamed or ["No changes needed"]

    # 2. Duplicates
    df, dupes_removed = remove_duplicates(df)
    report["steps"]["duplicates"] = {"removed": dupes_removed}

    # 3. Missing values
    df, missing_report = handle_missing_values(df, strategy=missing_strategy)
    report["steps"]["missing_values"] = missing_report

    # 4. Data types
    df, dtype_report = fix_data_types(df, profile)
    report["steps"]["data_types"] = dtype_report

    # 5. String standardization
    df, string_report = standardize_strings(df, profile)
    report["steps"]["strings"] = string_report

    # 6. Outliers
    df, outlier_report = detect_and_handle_outliers(
        df,
        sensitivity=profile["outlier_sensitivity"],
        action=outlier_action,
    )
    report["steps"]["outliers"] = outlier_report

    # 7. Domain rules
    df, domain_notes = apply_domain_rules(df, domain)
    report["steps"]["domain_rules"] = domain_notes

    # Final stats
    report["final_shape"] = df.shape
    report["rows_removed"] = report["original_shape"][0] - df.shape[0]
    report["cols_removed"] = report["original_shape"][1] - df.shape[1]
    report["duration_ms"] = round((datetime.utcnow() - start).total_seconds() * 1000, 2)

    return {"dataframe": df, "report": report}


# ──────────────────────────────────────────────
# PROFILING (pre-clean analysis)
# ──────────────────────────────────────────────

def profile_dataset(df: pd.DataFrame) -> dict:
    """Generate a data quality profile before cleaning."""
    total_rows, total_cols = df.shape
    total_cells = total_rows * total_cols

    col_profiles = {}
    for col in df.columns:
        series = df[col]
        missing = int(series.isna().sum())
        col_info = {
            "dtype": str(series.dtype),
            "missing": missing,
            "missing_pct": round(missing / total_rows * 100, 2),
            "unique": int(series.nunique()),
            "sample": series.dropna().head(5).tolist(),
        }

        if pd.api.types.is_numeric_dtype(series):
            col_info.update({
                "min": series.min(),
                "max": series.max(),
                "mean": round(series.mean(), 4),
                "std": round(series.std(), 4),
            })

        col_profiles[col] = col_info

    return {
        "rows": total_rows,
        "cols": total_cols,
        "total_cells": total_cells,
        "total_missing": int(df.isna().sum().sum()),
        "missing_pct": round(df.isna().sum().sum() / total_cells * 100, 2),
        "duplicate_rows": int(df.duplicated().sum()),
        "columns": col_profiles,
    }
