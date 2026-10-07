"""
Semantic profiling.

Answers "what is this column *probably* for?" so that detection, scoring and
recommendations can use context (a 10-digit value in a phone column is not a
quantity; a median is a meaningless fill for an identifier).

Every inference carries a confidence and the evidence behind it. Callers that are
about to do something risky must check the confidence (see `is_confident`) rather
than trusting the label: a low-confidence guess may inform a *warning* but never
justifies changing data.

Nothing here mutates a dataframe.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Optional

import pandas as pd

from cleaning import detectors

# --- semantic type vocabulary -------------------------------------------------
IDENTIFIER = "identifier"
PERSON_NAME = "person_name"
EMAIL = "email"
PHONE = "phone"
DATE = "date"
DATETIME = "date_time"
CURRENCY = "currency"
GEOGRAPHIC = "geographic"
CATEGORICAL = "categorical"
NUMERIC_MEASUREMENT = "numeric_measurement"
BOOLEAN = "boolean"
FREE_TEXT = "free_text"
UNKNOWN = "unknown"

# Business importance of a field (0-1). Used to weight how much damage in a column
# costs the overall score. Callers can override per column (custom rules / future
# per-dataset configuration) via `field_importance`.
DEFAULT_IMPORTANCE = {
    IDENTIFIER: 1.0,
    CURRENCY: 0.9,
    EMAIL: 0.9,
    PHONE: 0.85,
    PERSON_NAME: 0.8,
    DATE: 0.8,
    DATETIME: 0.8,
    NUMERIC_MEASUREMENT: 0.7,
    GEOGRAPHIC: 0.6,
    CATEGORICAL: 0.6,
    BOOLEAN: 0.5,
    FREE_TEXT: 0.4,
    UNKNOWN: 0.5,
}

CONFIDENT = 0.75  # at or above this a semantic label may influence what we recommend

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_TIME_RE = re.compile(r"\d{1,2}:\d{2}")
_PHONE_VALUE_RE = re.compile(r"^[+(]\s*[\d\s().\-]{6,}$")
from cleaning import currencies as _currencies  # noqa: E402

_CURRENCY_TOKENS = {
    "price", "amount", "cost", "salary", "revenue", "fee", "fees", "balance", "total",
    "income", "payment", "fare", "wage", "budget", "sales", "profit", "charge", "tax",
}
_GEO_TOKENS = {
    "city", "town", "state", "province", "region", "county", "district", "address",
    "street", "location", "latitude", "longitude", "lat", "lng", "lon", "continent",
}
_NAME_TOKENS = {"name", "firstname", "lastname", "surname", "fullname", "forename", "givenname", "middlename"}
_NOT_PERSON_NAME = {
    "file", "user", "username", "product", "company", "business", "city", "country", "state",
    "column", "brand", "item", "school", "organisation", "organization", "department",
    "street", "project", "team", "device", "host", "table", "category", "campaign",
}
_ID_NAME_HINTS = {"id", "code", "ref", "reference", "number", "no", "num", "sku", "uuid", "guid"}


def name_tokens(name: str) -> list[str]:
    """'FirstName', 'first_name' and 'First Name' all -> ['first', 'name']."""
    spaced = _CAMEL.sub(" ", str(name))
    return [t for t in _TOKEN_SPLIT.split(spaced.lower()) if t]


@dataclass
class ColumnProfile:
    column: str
    semantic_type: str
    confidence: float
    evidence: list[str] = field(default_factory=list)
    subtype: Optional[str] = None
    importance: float = 0.5
    pandas_dtype: str = ""
    non_null: int = 0
    null_ratio: float = 0.0
    unique_ratio: float = 0.0
    is_identifier: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["confidence"] = round(self.confidence, 2)
        d["importance"] = round(self.importance, 2)
        d["null_ratio"] = round(self.null_ratio, 4)
        d["unique_ratio"] = round(self.unique_ratio, 4)
        return d


def _has_time_component(values: pd.Series) -> bool:
    sample = values.head(200)
    return bool(sample.str.contains(_TIME_RE).mean() >= 0.5) if len(sample) else False


def profile_column(name: str, series: pd.Series, total_rows: int) -> ColumnProfile:
    lname = str(name).lower()
    tokens = set(name_tokens(name))
    non_null_series = series.dropna()
    if not pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_datetime64_any_dtype(series):
        # "N/A", "-", "Unknown" are placeholders, not values: they must not make a numeric,
        # date or flag column look like free text.
        as_text = non_null_series.astype(str).str.strip()
        non_null_series = non_null_series[~as_text.str.lower().isin(detectors.MISSING_TOKENS)]
    non_null = int(len(non_null_series))
    unique_ratio = (non_null_series.nunique() / non_null) if non_null else 0.0
    base = dict(
        column=str(name),
        pandas_dtype=str(series.dtype),
        non_null=non_null,
        null_ratio=(1 - non_null / total_rows) if total_rows else 0.0,
        unique_ratio=unique_ratio,
    )

    def done(stype, conf, evidence, subtype=None, is_id=False):
        return ColumnProfile(
            semantic_type=stype, confidence=conf, evidence=evidence, subtype=subtype,
            importance=DEFAULT_IMPORTANCE[stype], is_identifier=is_id, **base,
        )

    if pd.api.types.is_bool_dtype(series):
        return done(BOOLEAN, 1.0, ["boolean dtype"])
    if non_null == 0:
        return done(UNKNOWN, 0.0, ["column has no values"])

    is_text = not pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_datetime64_any_dtype(series)
    text_vals = non_null_series.astype(str).str.strip() if is_text else None

    # --- email ---
    if tokens & {"email", "mail"} or "e-mail" in lname:
        return done(EMAIL, 0.95, ["column name says email"])
    if is_text and non_null >= 3 and text_vals.str.match(detectors.EMAIL_RE).mean() >= 0.8:
        return done(EMAIL, 0.9, ["≥80% of values have an email shape"])

    # --- phone ---
    if detectors.is_phone_column(str(name)):
        return done(PHONE, 0.9, ["column name says phone/mobile/tel"], is_id=True)
    if is_text and non_null >= 3 and text_vals.map(lambda v: bool(_PHONE_VALUE_RE.match(v))).mean() >= 0.8:
        return done(PHONE, 0.65, ["≥80% of values look like international phone numbers"], is_id=True)

    # --- datetime (dtype or name/values) ---
    if pd.api.types.is_datetime64_any_dtype(series):
        return done(DATE, 1.0, ["datetime dtype"])
    if is_text and detectors.is_probable_date_column(str(name), series):
        by_name = detectors.has_date_name(str(name))
        has_time = _has_time_component(text_vals)
        ev = ["column name says date"] if by_name else ["values are date-shaped"]
        return done(DATETIME if has_time else DATE, 0.9 if by_name else 0.8, ev)

    # --- identifiers (never numeric quantities) ---
    if detectors.is_identifier_name(str(name)):
        return done(IDENTIFIER, 0.9, ["column name matches an identifier pattern"], is_id=True)
    if is_text and text_vals.str.match(detectors._LEADING_ZERO_RE).any():
        return done(IDENTIFIER, 0.7, ["all-digit values with a meaningful leading zero"], is_id=True)
    if non_null >= 10 and unique_ratio >= 0.98 and (tokens & _ID_NAME_HINTS):
        return done(IDENTIFIER, 0.8, ["name hints at a code/number and every value is unique"], is_id=True)

    # --- age / gender / geographic / currency / person name ---
    if detectors.is_age_column(str(name)) and pd.api.types.is_numeric_dtype(series):
        return done(NUMERIC_MEASUREMENT, 0.9, ["column name says age"], subtype="age")
    if detectors.is_gender_column(str(name)):
        return done(CATEGORICAL, 0.9, ["column name says gender/sex"], subtype="gender")
    if detectors.is_country_column(str(name)):
        return done(GEOGRAPHIC, 0.9, ["column name says country"], subtype="country")
    if tokens & _GEO_TOKENS:
        return done(GEOGRAPHIC, 0.8, ["column name is a geographic term"])

    numeric_like = pd.api.types.is_numeric_dtype(series)
    if is_text and non_null >= 3:
        cleaned = text_vals.map(detectors.strip_numeric_noise).map(detectors.try_parse_float)
        numeric_like = bool(cleaned.notna().mean() >= 0.95)
        if text_vals.head(200).map(_currencies.has_currency).any() and cleaned.notna().mean() >= 0.8:
            return done(CURRENCY, 0.8, ["values carry currency symbols and parse as numbers"])
    if tokens & _CURRENCY_TOKENS and numeric_like:
        return done(CURRENCY, 0.75, ["column name is a money term and values are numeric"])

    if (tokens & _NAME_TOKENS) and not (tokens & _NOT_PERSON_NAME) and not numeric_like:
        conf = 0.8
        ev = ["column name says person name"]
        if is_text and text_vals.str.match(r"^[^\W\d_][\w'’.\- ]*$").mean() >= 0.9:
            conf, ev = 0.88, ev + ["values are alphabetic"]
        return done(PERSON_NAME, conf, ev)

    if numeric_like:
        sub = "percentage" if is_text and text_vals.str.contains("%", regex=False).mean() >= 0.5 else None
        return done(NUMERIC_MEASUREMENT, 0.7 if not is_text else 0.6,
                    ["numeric values"] if not is_text else ["≥95% of text values parse as numbers"], subtype=sub)

    # --- yes/no style flags ---
    if is_text and non_null >= 2:
        lowered = set(text_vals.str.lower().unique())
        if lowered and lowered <= detectors.BOOLEAN_WORDS | ({"1", "0"} if detectors.is_boolean_flag_column(str(name)) else set()):
            return done(BOOLEAN, 0.9, ["every value is a yes/no word"])

    # --- categorical vs free text vs unknown ---
    if is_text:
        nunique = non_null_series.nunique()
        if non_null >= 5 and nunique <= 50 and unique_ratio <= 0.5:
            return done(CATEGORICAL, min(0.9, 0.5 + (1 - unique_ratio) * 0.4),
                        [f"{nunique} distinct values repeated across {non_null} rows"])
        if text_vals.str.len().mean() > 40:
            return done(FREE_TEXT, 0.6, ["long text values"])
    return done(UNKNOWN, 0.3, ["no strong signal"])


def profile_dataset(df: pd.DataFrame, field_importance: Optional[dict] = None) -> dict:
    """
    Returns {"columns": {name: ColumnProfile}, "row_count": n, "column_count": m}.

    `field_importance` optionally overrides the default importance per column name
    ({"customer_email": 1.0}); the hook future per-dataset configuration and custom
    rules will use.
    """
    total = len(df)
    cols = {}
    for col in df.columns:
        p = profile_column(str(col), df[col], total)
        if field_importance and str(col) in field_importance:
            p.importance = float(min(1.0, max(0.0, field_importance[str(col)])))
            p.evidence.append("importance set by caller")
        cols[str(col)] = p
    return {"columns": cols, "row_count": total, "column_count": len(df.columns)}


def is_confident(profile: ColumnProfile, *types: str, threshold: float = CONFIDENT) -> bool:
    return profile.semantic_type in types and profile.confidence >= threshold


def never_impute(profile: ColumnProfile) -> bool:
    """Columns where inventing a value is never acceptable: a made-up phone number,
    email, identifier or date is worse than a gap. Even a modest-confidence label is
    enough to hold back here, because the cost of being wrong is asymmetric."""
    return profile.semantic_type in (IDENTIFIER, EMAIL, PHONE, DATE, DATETIME) and profile.confidence >= 0.6
