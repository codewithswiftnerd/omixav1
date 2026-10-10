"""
Column-level validation.

Answers, per column and per cell: "does this value satisfy what this column is supposed to hold?"
It is READ-ONLY. It never edits, deletes or converts a value; cleaning (cleaning/rules.py,
cleaning/resolutions.py) is a separate step the user controls.

Every cell lands in exactly ONE category, so the counts of a column always add up to its row count:

  missing     The cell is empty, or holds a recognised missing-value marker ("N/A", "null", and, in
              typed columns, "unknown", "--" ...). Not an error, reported separately from invalid.
  invalid     The value breaks the column's confirmed (or confidently detected) data type, or a rule
              that was configured: "forty" in a numeric column, "person@@example.com", 31/02/2026,
              a value outside a configured range.
  suspicious  The value is well-formed but may be wrong and needs human judgment: an age of 150, a
              probable e-mail domain typo, a label that differs only by case from the usual one.
  unresolved  Omixa cannot determine the right interpretation, so it neither accepts nor rejects it:
              "free" in an amount column (0? missing? a label?), "yesterday" with no reference date,
              05/06/2026 (day-first or month-first?), a phone number with no country, a placeholder
              word in a notes column, or ANY non-conforming value in a column whose type is unconfirmed.
  valid       Satisfies the column's type and rules. "Valid" means it conforms to what was checked; it
              does not mean the value is true.

Column type: a type the user selects is CONFIRMED. Otherwise the type is detected from the values (the
column name is a supporting hint only: it can raise confidence when values already point the same way,
it can never decide the type alone). Below CONFIDENT_TYPE the type is "needs_confirmation" and
non-conforming values are reported as UNRESOLVED, not INVALID, because we do not yet know the rule.

Performance: classification runs once per DISTINCT value and is then counted, so a million-row column
with few distinct values costs about the same as a small one.
"""

from __future__ import annotations

import difflib
import re
from datetime import date, datetime
from typing import Any, Optional

import pandas as pd

from cleaning import detectors
from cleaning import currencies as _currencies

VALID, INVALID, SUSPICIOUS, MISSING, UNRESOLVED = "valid", "invalid", "suspicious", "missing", "unresolved"
CATEGORIES = (VALID, INVALID, SUSPICIOUS, MISSING, UNRESOLVED)

NUMERIC, INTEGER, CURRENCY, DATE, EMAIL, PHONE, CATEGORICAL, IDENTIFIER, TEXT = (
    "numeric", "integer", "currency", "date", "email", "phone", "categorical", "identifier", "text")
TYPES = (NUMERIC, INTEGER, CURRENCY, DATE, EMAIL, PHONE, CATEGORICAL, IDENTIFIER, TEXT)

CONFIDENT_TYPE = 0.75          # detected-type confidence at or above which rules are applied as INVALID
MIN_SHAPE_SHARE = 0.30         # a name hint is ignored unless at least this share of values fit the type
MAX_EXAMPLES = 5

CLASSIFICATION_RULES = {
    MISSING: "Empty, or a recognised missing-value marker. Reported separately from invalid values.",
    INVALID: "Breaks the column's confirmed or confidently detected data type, or a rule you configured.",
    SUSPICIOUS: "Well-formed but may be wrong; needs human judgment (e.g. an age of 150).",
    UNRESOLVED: "Omixa cannot tell what the right interpretation is, so it neither accepts nor rejects the value.",
    VALID: "Conforms to the checks that were run. It does not prove the value is true.",
}

_EMAIL_STRICT = re.compile(
    r"^(?!\.)(?!.*\.\.)[A-Za-z0-9._%+\-]+(?<!\.)@[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)*\.[A-Za-z]{2,}$")
_EMAIL_DOMAIN_TYPOS = {
    "gmial.com", "gmail.con", "gmai.com", "gmail.co", "gnail.com", "gmaill.com", "yahooo.com", "yaho.com",
    "yahoo.con", "hotmial.com", "hotmai.com", "hotmail.con", "outlok.com", "outlook.con",
}
_RELATIVE_DATE = re.compile(
    r"^(today|tomorrow|yesterday|now|tonight|(last|next|this)\s+(week|month|year|monday|tuesday|wednesday|thursday|"
    r"friday|saturday|sunday)|\d+\s+(days?|weeks?|months?|years?)\s+(ago|from now)|day before yesterday)$", re.I)
_SLASH_DATE = re.compile(r"^(\d{1,4})([/.\-])(\d{1,2})\2(\d{1,4})$")
_NAMED_FORMATS = ("%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y", "%b %d %Y", "%B %d %Y", "%d-%b-%Y",
                  "%d-%B-%Y", "%d %b, %Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M")
_PHONE_CHARS = re.compile(r"^[+\d\s().\-]+$")
_AGE_DEFAULT_SOFT_MAX = 120


# --------------------------------------------------------------------------- configuration
def _norm_rules(raw: Optional[dict]) -> dict:
    """Keeps only recognised keys with sane types; unknown keys are ignored (never trusted)."""
    out: dict = {}
    raw = raw if isinstance(raw, dict) else {}
    for k in ("min", "max", "soft_min", "soft_max"):
        v = raw.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = float(v)
    for k in ("allow_negative", "unique"):
        if isinstance(raw.get(k), bool):
            out[k] = raw[k]
    if isinstance(raw.get("allowed_values"), list):
        out["allowed_values"] = [str(v) for v in raw["allowed_values"][:5000]]
    if isinstance(raw.get("pattern"), str) and raw["pattern"] and len(raw["pattern"]) <= 300:
        try:
            re.compile(raw["pattern"])
            out["pattern"] = raw["pattern"]
        except re.error:
            out["pattern_error"] = True
    for k in ("min_date", "max_date", "reference_date"):
        d = _parse_iso(raw.get(k))
        if d:
            out[k] = d
    if raw.get("date_convention") in ("day_first", "month_first"):
        out["date_convention"] = raw["date_convention"]
    if isinstance(raw.get("country"), str) and raw["country"].strip():
        out["country"] = raw["country"].strip().upper()
    vm = raw.get("value_meanings")
    if isinstance(vm, dict):   # dataset-defined meaning of a word, e.g. {"free": 0}; makes it a defined value
        out["value_meanings"] = {str(k).strip().lower(): v for k, v in list(vm.items())[:200]
                                 if isinstance(v, (int, float)) and not isinstance(v, bool)}
    return out


def _parse_iso(v) -> Optional[date]:
    if isinstance(v, date):          # already normalised (normalize_config is idempotent)
        return v
    if isinstance(v, str):
        try:
            return datetime.strptime(v.strip()[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def normalize_config(config: Optional[dict]) -> dict:
    """{column_types: {col: type}, rules: {col: {...}}, defaults: {...rules applied to every column...}}"""
    config = config if isinstance(config, dict) else {}
    types = {str(c): t for c, t in (config.get("column_types") or {}).items() if t in TYPES}
    rules = {str(c): _norm_rules(r) for c, r in (config.get("rules") or {}).items()}
    return {"column_types": types, "rules": rules, "defaults": _norm_rules(config.get("defaults"))}


# --------------------------------------------------------------------------- cell helpers
def _is_blank(v) -> bool:
    if v is None or v is pd.NA:
        return True
    if isinstance(v, float) and v != v:
        return True
    if isinstance(v, str) and v.strip() == "":
        return True
    try:
        return bool(pd.isna(v)) if not isinstance(v, (str, bytes, list, dict, tuple)) else False
    except (TypeError, ValueError):
        return False


def _text(v) -> str:
    return v.strip() if isinstance(v, str) else str(v).strip()


def _numeric_value(v) -> Optional[float]:
    """The number a cell holds, if it is a plain number or FORMATTED like one (symbols, thousands
    separators, %, parentheses). Words are never read as numbers here."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if hasattr(v, "item"):
        try:
            return float(v.item())
        except (TypeError, ValueError):
            return None
    return detectors.try_parse_float(detectors.strip_numeric_noise(_text(v)))


def _is_shaped(ctype: str, v) -> bool:
    """Loose, TYPE-DETECTION-ONLY shape test: 'looks like it is trying to be a …', so a malformed value
    ("person@@example.com", "31/02/2026") still counts as evidence for the type instead of hiding it."""
    t = _text(v)
    if ctype in (NUMERIC, INTEGER, CURRENCY):
        return _numeric_value(v) is not None
    if ctype == EMAIL:
        return "@" in t
    if ctype == PHONE:
        return bool(_PHONE_CHARS.match(t)) and sum(ch.isdigit() for ch in t) >= 6
    if ctype == DATE:
        return bool(_SLASH_DATE.match(t) or _RELATIVE_DATE.match(t) or _try_named(t) is not None
                    or re.match(r"^\d{4}-\d{1,2}-\d{1,2}", t))
    return False


# --------------------------------------------------------------------------- type detection
def detect_type(name: str, series: pd.Series, config: Optional[dict] = None) -> dict:
    """{'type', 'confidence', 'evidence': [...], 'status': 'confirmed'|'detected'|'needs_confirmation'}"""
    cfg = normalize_config(config)
    confirmed = cfg["column_types"].get(str(name))
    if confirmed:
        return {"type": confirmed, "confidence": 1.0, "evidence": ["selected by the user"], "status": "confirmed"}

    values = [v for v in series.dropna().tolist() if not _is_blank(v)]
    typed_vals = [v for v in values if not (isinstance(v, str) and detectors.is_missing_token(v))]
    n = len(typed_vals)
    if n == 0:
        return {"type": TEXT, "confidence": 0.0, "evidence": ["no values to inspect"], "status": "needs_confirmation"}

    from cleaning import profiling
    tokens = set(profiling.name_tokens(str(name)))
    dtype = series.dtype

    if pd.api.types.is_bool_dtype(dtype):
        return {"type": CATEGORICAL, "confidence": 1.0, "evidence": ["boolean column"], "status": "detected"}
    if pd.api.types.is_datetime64_any_dtype(dtype):
        return {"type": DATE, "confidence": 1.0, "evidence": ["date/time column"], "status": "detected"}

    def share(t):
        return sum(1 for v in typed_vals if _is_shaped(t, v)) / n

    def result(t, conf, ev):
        return {"type": t, "confidence": round(min(conf, 1.0), 2), "evidence": ev,
                "status": "detected" if conf >= CONFIDENT_TYPE else "needs_confirmation"}

    name_says = {
        EMAIL: bool(tokens & {"email", "mail"}), PHONE: bool(tokens & {"phone", "mobile", "tel", "telephone", "gsm", "cell"}),
        DATE: detectors.has_date_name(str(name)),
        CURRENCY: bool(tokens & profiling._CURRENCY_TOKENS),
        IDENTIFIER: detectors.is_identifier_name(str(name)),
    }
    str_vals = [v for v in typed_vals if isinstance(v, str)]

    # identifiers first: an all-digit value with a meaningful leading zero is never a quantity
    lead_zero = sum(1 for v in str_vals if detectors._LEADING_ZERO_RE.match(v.strip()))
    if lead_zero and (lead_zero / n >= 0.2 or name_says[IDENTIFIER]) and not name_says[PHONE]:
        return result(IDENTIFIER, 0.8, ["values have meaningful leading zeros, so they are codes, not quantities"])

    for t in (EMAIL, PHONE, DATE):
        s = share(t)
        if s >= 0.6:
            boosted = name_says[t]
            return result(t, s + (0.15 if boosted else 0), [f"{round(s * 100)}% of values look like {t}s"]
                          + (["the column name agrees"] if boosted else []))
        if name_says[t] and s >= MIN_SHAPE_SHARE:
            return result(t, s + 0.15, [f"{round(s * 100)}% of values look like {t}s and the column name agrees"])

    code_like = bool(str_vals) and sum(1 for v in str_vals if not re.search(r"\s", v.strip())) / len(str_vals) >= 0.8
    if name_says[IDENTIFIER] and code_like and not pd.api.types.is_numeric_dtype(dtype):
        return result(IDENTIFIER, 0.7, ["the column name looks like an identifier"])

    s_num = share(NUMERIC)
    money_symbols = any(_currencies.has_currency(v) for v in str_vals[:200])
    if s_num >= 0.6 or (name_says[CURRENCY] and s_num >= MIN_SHAPE_SHARE) or (age_like(name) and s_num >= MIN_SHAPE_SHARE):
        conf = s_num if s_num >= 0.6 else s_num + 0.15
        t = CURRENCY if (money_symbols or name_says[CURRENCY]) else NUMERIC
        if pd.api.types.is_numeric_dtype(dtype):
            conf = 1.0
        ev = [f"{round(s_num * 100)}% of values are numbers"] + (["the column name agrees"] if conf > s_num else [])
        return result(t, conf, ev)

    distinct = len({str(v).strip() for v in typed_vals})
    if distinct <= max(12, int(n * 0.2)) and n >= 5:
        return result(CATEGORICAL, 0.7, [f"{distinct} distinct values repeated across {n} rows"])
    # free text has no type rule a value could break, so there is nothing to confirm
    return {"type": TEXT, "confidence": 0.6, "evidence": ["free-form text"], "status": "detected"}


def age_like(name: str) -> bool:
    return detectors.is_age_column(str(name))


# --------------------------------------------------------------------------- date parsing
def _try_named(t: str) -> Optional[date]:
    for fmt in _NAMED_FORMATS:
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


def _build(y: int, m: int, d: int) -> Optional[date]:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_date_cell(v, convention: Optional[str] = None) -> tuple[str, Optional[date], str]:
    """-> (status, date|None, code) where status is 'ok' | 'impossible' | 'ambiguous' | 'relative' | 'unrecognised'."""
    if isinstance(v, (pd.Timestamp, datetime)):
        return "ok", pd.Timestamp(v).date(), "ok"
    if isinstance(v, date):
        return "ok", v, "ok"
    t = _text(v)
    if _RELATIVE_DATE.match(t):
        return "relative", None, "relative_date"
    named = _try_named(t)
    if named:
        return "ok", named, "ok"
    iso = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ T].*)?$", t)
    if iso:
        d = _build(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        return ("ok", d, "ok") if d else ("impossible", None, "impossible_date")
    m = _SLASH_DATE.match(t)
    if m and len(m.group(4)) in (2, 4):
        a, b, y = int(m.group(1)), int(m.group(3)), int(m.group(4))
        if len(m.group(4)) == 2:
            y += 2000 if y < 50 else 1900
        if len(m.group(1)) > 2:
            return "unrecognised", None, "unrecognised_date_format"
        if a > 12 and b > 12:
            return "impossible", None, "impossible_date"
        if a > 12:                       # can only be day-first
            if convention == "month_first":
                return "impossible", None, "impossible_date"
            d = _build(y, b, a)
            return ("ok", d, "ok") if d else ("impossible", None, "impossible_date")
        if b > 12:                       # can only be month-first
            if convention == "day_first":
                return "impossible", None, "impossible_date"
            d = _build(y, a, b)
            return ("ok", d, "ok") if d else ("impossible", None, "impossible_date")
        if a == b or convention is not None:
            day, month = (a, b) if convention != "month_first" else (b, a)
            d = _build(y, month, day)
            return ("ok", d, "ok") if d else ("impossible", None, "impossible_date")
        # both readings are real calendar dates and differ: do not guess
        if _build(y, b, a) is None and _build(y, a, b) is None:
            return "impossible", None, "impossible_date"
        if _build(y, b, a) is None:
            return "ok", _build(y, a, b), "ok"
        if _build(y, a, b) is None:
            return "ok", _build(y, b, a), "ok"
        return "ambiguous", None, "ambiguous_day_month"
    return "unrecognised", None, "unrecognised_date_format"


# --------------------------------------------------------------------------- phone
def _phone_digits(t: str) -> str:
    return re.sub(r"[\s().\-]", "", t)


def classify_phone(v, rules: dict) -> tuple[str, str, str]:
    t = _text(v)
    if not _PHONE_CHARS.match(t):
        return INVALID, "unexpected_characters", "Contains letters or symbols that are not part of a phone number."
    s = _phone_digits(t)
    if s.count("+") > 1 or ("+" in s and not s.startswith("+")):
        return INVALID, "unexpected_characters", "The + sign is only valid once, at the very start."
    digits = s.lstrip("+")
    if not digits.isdigit():
        return INVALID, "unexpected_characters", "Contains characters that are not digits."
    country = rules.get("country")
    if s.startswith("+"):
        if digits.startswith("234"):
            return _ng(digits[3:], "international +234 number")
        if 8 <= len(digits) <= 15:
            return VALID, "ok", "International format; length is plausible (country-specific rules not checked)."
        return INVALID, "invalid_length", f"International numbers have 8 to 15 digits; this has {len(digits)}."
    if digits.startswith("234") and len(digits) in (13,):
        return _ng(digits[3:], "number with 234 prefix")
    if digits.startswith("0") and len(digits) == 11 and digits[1] in "789":
        return VALID, "ok", "Nigerian national format (11 digits)."
    if digits.startswith("0") and country in (None, "NG") and digits[1:2] in ("7", "8", "9") and len(digits) in range(8, 15):
        return INVALID, "invalid_length_ng", f"Looks like a Nigerian number but has {len(digits)} digits; expected 11."
    if country == "NG":
        if len(digits) == 10 and digits[0] in "789":
            return VALID, "ok", "Nigerian number without the leading 0 (country set to NG)."
        return INVALID, "invalid_ng_number", "Does not match Nigerian numbering (0 + 10 digits, or +234 + 10 digits)."
    if country:
        from cleaning.phone_formats import normalize_phone
        ok = normalize_phone(digits, country)
        if ok:
            return VALID, "ok", f"Matches {country} numbering."
        return INVALID, "invalid_for_country", f"Does not match {country} numbering."
    if len(digits) < 7 or len(digits) > 15:
        return INVALID, "invalid_length", f"A phone number has 7 to 15 digits; this has {len(digits)}."
    return UNRESOLVED, "needs_country", "No country code or national prefix, so the country (and the right rules) is unknown."


def _ng(national: str, what: str) -> tuple[str, str, str]:
    if len(national) == 10 and national[0] in "789":
        return VALID, "ok", f"Valid Nigerian {what}."
    return INVALID, "invalid_length_ng", f"Nigerian {what} should have 10 digits after +234 (starting 7, 8 or 9); got {len(national)}."


# --------------------------------------------------------------------------- per-type classifiers
def _range_checks(num: float, rules: dict, default_age: bool, ctype: str) -> Optional[tuple[str, str, str]]:
    if "min" in rules and num < rules["min"]:
        return INVALID, "out_of_range", f"Below the minimum allowed value ({rules['min']:g})."
    if "max" in rules and num > rules["max"]:
        return INVALID, "out_of_range", f"Above the maximum allowed value ({rules['max']:g})."
    if num < 0 and rules.get("allow_negative") is False:
        return INVALID, "negative_not_allowed", "Negative values are not allowed for this field."
    if "soft_min" in rules and num < rules["soft_min"]:
        return SUSPICIOUS, "unusual_value", f"Below the usual range ({rules['soft_min']:g})."
    if "soft_max" in rules and num > rules["soft_max"]:
        return SUSPICIOUS, "unusual_value", f"Above the usual range ({rules['soft_max']:g})."
    if default_age:
        if num < 0 and "allow_negative" not in rules:
            return INVALID, "negative_not_allowed", "An age cannot be negative."
        if num > _AGE_DEFAULT_SOFT_MAX and "soft_max" not in rules and "max" not in rules:
            return SUSPICIOUS, "implausible_age", f"An age above {_AGE_DEFAULT_SOFT_MAX} is possible but unlikely; please check."
    elif ctype == CURRENCY and num < 0 and "allow_negative" not in rules:
        return SUSPICIOUS, "negative_amount", "Negative amount: could be a refund or an entry error."
    return None


def classify_numeric(v, rules: dict, ctype: str, strict: bool, default_age: bool = False) -> tuple[str, str, str]:
    bad = INVALID if strict else UNRESOLVED
    suffix = "" if strict else " (column type not confirmed)"
    num = _numeric_value(v)
    if num is None:
        t = _text(v)
        meanings = rules.get("value_meanings") or {}
        if t.lower() in meanings:
            return VALID, "defined_meaning", f"'{t}' is defined by this dataset's rules as {meanings[t.lower()]}."
        if detectors.is_zero_word(t):
            return UNRESOLVED, "undefined_word_meaning", (
                f"'{t}' might mean 0, missing, or a label. Omixa will not assume; define what it means or correct it.")
        if detectors.spoken_number(t) is not None:
            return bad, "number_in_words", f"A number written in words ({t!r}); numeric processing needs digits{suffix}."
        if re.search(r"\d", t) and re.search(r"[A-Za-z]", t):
            return bad, "mixed_text_and_digits", f"Mixes letters and digits{suffix}."
        if re.search(r"\d", t):
            return bad, "invalid_numeric_format", f"Not a valid number format{suffix}."
        return bad, "not_numeric", f"Text where a number is expected{suffix}."
    if ctype == INTEGER and num != int(num):
        return bad, "not_integer", f"A whole number is expected{suffix}."
    hit = _range_checks(num, rules, default_age, ctype) if strict else None
    if hit:
        return hit
    return VALID, "ok", "Valid number."


def classify_date(v, rules: dict, strict: bool, ref: Optional[date]) -> tuple[str, str, str]:
    bad = INVALID if strict else UNRESOLVED
    suffix = "" if strict else " (column type not confirmed)"
    status, d, code = parse_date_cell(v, rules.get("date_convention"))
    if status == "relative":
        why = ("A reference date was supplied, but Omixa does not rewrite relative dates."
               if rules.get("reference_date") else "There is no reference date, so a relative date cannot be turned into a calendar date.")
        return UNRESOLVED, "relative_date", why
    if status == "ambiguous":
        return UNRESOLVED, "ambiguous_day_month", "Could be day-first or month-first and both are real dates. Choose a convention."
    if status == "impossible":
        return bad, "impossible_date", f"Not a real calendar date{suffix}."
    if status == "unrecognised":
        return bad, "unrecognised_date_format", f"Not a recognised date format{suffix}."
    if "min_date" in rules and d < rules["min_date"]:
        return bad, "out_of_range", f"Earlier than the allowed range ({rules['min_date']})."
    if "max_date" in rules and d > rules["max_date"]:
        return bad, "out_of_range", f"Later than the allowed range ({rules['max_date']})."
    if d.year < detectors.MIN_PLAUSIBLE_YEAR or d.year > 2100:
        return SUSPICIOUS, "implausible_year", f"Year {d.year} is outside the plausible range."
    return VALID, "ok", "Valid calendar date."


def classify_email(v, rules: dict, strict: bool) -> tuple[str, str, str]:
    bad = INVALID if strict else UNRESOLVED
    t = _text(v)
    if " " in t:
        return bad, "invalid_email_syntax", "Contains spaces."
    if t.count("@") != 1:
        return bad, "invalid_email_syntax", "An e-mail address has exactly one @ sign." if "@" in t else "Missing the @ sign."
    if not _EMAIL_STRICT.match(t):
        return bad, "invalid_email_syntax", "Not a valid e-mail address (check the part before @, the domain and the ending)."
    if t.rsplit("@", 1)[1].lower() in _EMAIL_DOMAIN_TYPOS:
        return SUSPICIOUS, "possible_domain_typo", "Valid shape, but the domain looks like a common misspelling."
    return VALID, "ok", "Valid e-mail syntax (deliverability is not checked)."


def classify_identifier(v, rules: dict) -> tuple[str, str, str]:
    t = v if isinstance(v, str) else str(v)
    if "pattern" in rules and not re.fullmatch(rules["pattern"], t.strip()):
        return INVALID, "pattern_mismatch", "Does not match the identifier format defined for this column."
    if t != t.strip():
        return SUSPICIOUS, "padded_identifier", "Has leading or trailing spaces, which can stop identifiers from matching."
    return VALID, "ok", "Identifier kept exactly as written."


_SYMBOL_ONLY = re.compile(r"^[^\w]+$", re.UNICODE)


def classify_text(v) -> tuple[str, str, str]:
    t = _text(v)
    if _SYMBOL_ONLY.match(t):
        return UNRESOLVED, "symbol_only", "Only symbols/emoji: may be a note, a placeholder or noise. Kept as written."
    return VALID, "ok", "Text kept as written."


def classify_categorical(uniques: dict, rules: dict, strict: bool) -> dict:
    """uniques: {value: count}. Returns {value: (category, code, reason)}; context-dependent (needs the
    other values), so it works on the whole set of distinct values at once."""
    bad = INVALID if strict else UNRESOLVED
    out = {}
    allowed = rules.get("allowed_values")
    if allowed:
        by_exact = {a: a for a in allowed}
        by_key = {}
        for a in allowed:
            by_key.setdefault(_key(a), []).append(a)
        for v in uniques:
            t = _text(v)
            if t in by_exact:
                out[v] = (VALID, "ok", "In the allowed list.")
            elif len(by_key.get(_key(t), [])) == 1:
                out[v] = (SUSPICIOUS, "label_variant", f"Differs from allowed value {by_key[_key(t)][0]!r} only by case/spacing.")
            else:
                close = difflib.get_close_matches(t.lower(), [a.lower() for a in allowed], n=3, cutoff=0.8)
                if len(close) == 1:
                    out[v] = (SUSPICIOUS, "possible_misspelling", f"Close to allowed value {close[0]!r}; may be a misspelling.")
                elif len(close) > 1:
                    out[v] = (UNRESOLVED, "ambiguous_category_mapping", "Close to several allowed values; Omixa cannot choose safely.")
                else:
                    out[v] = (bad, "not_in_allowed_list", "Not one of the allowed values.")
        return out
    # no allowed list: judge consistency against the column's own dominant spellings
    groups: dict = {}
    for v, c in uniques.items():
        groups.setdefault(_key(_text(v)), []).append((c, v))
    for key, members in groups.items():
        members.sort(key=lambda x: -x[0])
        top = members[0][1]
        for c, v in members:
            out[v] = (VALID, "ok", "Consistent label.") if v == top else (
                SUSPICIOUS, "inconsistent_label", f"Same label as {top!r} written differently (case/spacing/punctuation).")
    keys = sorted(groups, key=lambda k: -sum(c for c, _ in groups[k]))
    freq = {k: sum(c for c, _ in groups[k]) for k in keys}
    for k in keys:
        if freq[k] > 1:
            continue
        for k2 in keys:
            if freq[k2] >= 3 * freq[k] and freq[k2] >= 3 and difflib.SequenceMatcher(None, k, k2).ratio() >= 0.85 and k != k2:
                v = groups[k][0][1]
                out[v] = (SUSPICIOUS, "possible_misspelling", f"Rare spelling very close to {groups[k2][0][1]!r}; may be a typo.")
                break
    return out


def _key(s: str) -> str:
    return re.sub(r"[^\w]", "", s.lower())


# --------------------------------------------------------------------------- column validation
def _marker_category(v, ctype: str) -> Optional[tuple[str, str, str]]:
    """Missing / placeholder handling shared by every type. None = not a blank or marker."""
    if _is_blank(v):
        return MISSING, "empty", "Empty cell."
    if isinstance(v, str):
        low = v.strip().lower()
        if low in detectors.STRONG_MISSING_TOKENS:
            return MISSING, "missing_marker", f"{v.strip()!r} is a recognised missing-value marker."
        if low in detectors.WEAK_MISSING_TOKENS:
            if ctype in (TEXT, CATEGORICAL, IDENTIFIER):
                return UNRESOLVED, "ambiguous_placeholder", (
                    f"{v.strip()!r} may mean 'no value' or may be a real answer/note. Kept as written; please decide.")
            return MISSING, "missing_marker", f"{v.strip()!r} is treated as a missing-value marker in this {ctype} column."
    return None


def _distinct_counts(series: pd.Series) -> dict:
    counts: dict = {}
    for v, c in series.value_counts(dropna=False, sort=False).items():
        counts[v if not _is_blank(v) else None] = counts.get(v if not _is_blank(v) else None, 0) + int(c)
    return counts


def classify_column(name: str, series: pd.Series, config: Optional[dict] = None) -> dict:
    """Returns {'type_info', 'rules', 'by_value': {value: (category, code, reason)}, 'counts': {value: n}}"""
    cfg = normalize_config(config)
    info = detect_type(name, series, config)
    ctype, strict = info["type"], info["status"] in ("confirmed", "detected")
    rules = {**cfg["defaults"], **cfg["rules"].get(str(name), {})}
    default_age = (ctype in (NUMERIC, INTEGER) and age_like(name))
    ref = rules.get("reference_date")

    # objects (not the raw cells) can be unhashable in odd files: key by value, fall back to repr
    work = series.astype(object)
    try:
        counts = {}
        for v, c in work.value_counts(dropna=False, sort=False).items():
            k = None if _is_blank(v) else v
            counts[k] = counts.get(k, 0) + int(c)
    except TypeError:
        work = work.map(lambda v: v if _is_blank(v) or isinstance(v, (str, int, float, bool)) else repr(v))
        counts = {}
        for v, c in work.value_counts(dropna=False, sort=False).items():
            k = None if _is_blank(v) else v
            counts[k] = counts.get(k, 0) + int(c)

    by_value: dict = {}
    pending: dict = {}
    for v, c in counts.items():
        marker = _marker_category(v, ctype)
        if marker:
            by_value[v] = marker
        else:
            pending[v] = c

    if ctype == CATEGORICAL:
        by_value.update(classify_categorical(pending, rules, strict))
    else:
        for v in pending:
            if ctype in (NUMERIC, INTEGER, CURRENCY):
                by_value[v] = classify_numeric(v, rules, ctype, strict, default_age)
            elif ctype == DATE:
                by_value[v] = classify_date(v, rules, strict, ref)
            elif ctype == EMAIL:
                by_value[v] = classify_email(v, rules, strict)
            elif ctype == PHONE:
                by_value[v] = classify_phone(v, rules) if strict else _soften(classify_phone(v, rules))
            elif ctype == IDENTIFIER:
                by_value[v] = classify_identifier(v, rules)
            else:
                by_value[v] = classify_text(v)

    if rules.get("unique") and ctype in (IDENTIFIER, TEXT, NUMERIC, INTEGER, EMAIL, PHONE):
        for v, c in pending.items():
            if c > 1 and by_value[v][0] == VALID:
                by_value[v] = (SUSPICIOUS, "duplicate_value", f"Appears {c} times in a column configured as unique.")
    return {"type_info": info, "rules": rules, "by_value": by_value, "counts": counts, "default_age": default_age}


def _soften(res: tuple[str, str, str]) -> tuple[str, str, str]:
    cat, code, why = res
    return (UNRESOLVED, code, why + " (column type not confirmed)") if cat == INVALID else res


def _suggested_actions(name: str, info: dict, counts: dict, issues: dict) -> list[dict]:
    acts: list[dict] = []
    if info["status"] == "needs_confirmation":
        acts.append({"action": "confirm_type", "text": "Confirm what this column should contain so values can be judged against the right rule."})
    if counts[INVALID]:
        acts.append({"action": "review_invalid", "text": f"Review {counts[INVALID]} invalid value(s): correct, replace, keep or remove each one. Nothing is deleted automatically."})
    if counts[UNRESOLVED]:
        extra = {
            "undefined_word_meaning": " Define what the word means (e.g. free = 0) or correct it.",
            "ambiguous_day_month": " Choose day-first or month-first.",
            "needs_country": " Tell Omixa the country these numbers belong to.",
            "relative_date": " Replace with a real date or supply a reference date.",
            "ambiguous_placeholder": " Decide whether it means 'no value' or keep it as a note.",
        }
        hint = "".join(v for k, v in extra.items() if k in issues)
        acts.append({"action": "resolve", "text": f"{counts[UNRESOLVED]} value(s) cannot be interpreted safely and were left as written.{hint}"})
    if counts[SUSPICIOUS]:
        acts.append({"action": "review_suspicious", "text": f"Check {counts[SUSPICIOUS]} suspicious value(s); they may be correct."})
    if counts[MISSING]:
        acts.append({"action": "review_missing", "text": f"{counts[MISSING]} value(s) are missing. Filling them would be an estimate, not the original value."})
    return acts


def validate_dataframe(df: pd.DataFrame, config: Optional[dict] = None, *, include_examples: bool = True) -> dict:
    cfg = normalize_config(config)
    n = len(df)
    columns = []
    totals = {c: 0 for c in CATEGORIES}
    for col in df.columns:
        res = classify_column(str(col), df[col], cfg)
        counts = {c: 0 for c in CATEGORIES}
        issues: dict = {}
        examples: dict = {c: [] for c in CATEGORIES if c != VALID}
        for v, k in res["counts"].items():
            cat, code, why = res["by_value"][v]
            counts[cat] += k
            if cat != VALID:
                issues[code] = issues.get(code, 0) + k
                if include_examples and len(examples[cat]) < MAX_EXAMPLES:
                    examples[cat].append({"value": None if v is None else _jsonable(v), "count": k, "code": code, "reason": why})
        assert sum(counts.values()) == n, f"column {col!r}: counts {counts} do not add up to {n} rows"
        for c in CATEGORIES:
            totals[c] += counts[c]
        info = res["type_info"]
        columns.append({
            "column": str(col),
            "detected_type": info["type"] if info["status"] != "confirmed" else None,
            "confirmed_type": info["type"] if info["status"] == "confirmed" else None,
            "effective_type": info["type"],
            "type_status": info["status"],
            "type_confidence": info["confidence"],
            "type_evidence": info["evidence"],
            "total": n,
            "counts": counts,
            "issues": issues,
            "examples": examples if include_examples else {},
            "rules": {k: (str(v) if isinstance(v, date) else v) for k, v in res["rules"].items()},
            "suggested_actions": _suggested_actions(str(col), info, counts, issues),
            "fully_valid": counts[VALID] == n and info["status"] != "needs_confirmation",
        })
    return {
        "version": 1, "row_count": n, "column_count": len(df.columns), "columns": columns, "totals": totals,
        "classification_rules": CLASSIFICATION_RULES,
        "needs_attention": [c["column"] for c in columns if not c["fully_valid"]],
    }


def _jsonable(v):
    if hasattr(v, "item"):
        try:
            return v.item()
        except Exception:
            pass
    if isinstance(v, (pd.Timestamp, datetime, date)):
        return str(v)
    return v if isinstance(v, (str, int, float, bool)) else str(v)


def rows_for(df: pd.DataFrame, column: str, category: str, config: Optional[dict] = None, *,
             code: Optional[str] = None, offset: int = 0, limit: int = 100, header_offset: int = 2) -> dict:
    """The affected rows behind one count in the report, with the ORIGINAL value of every cell.
    `row_index` is the 0-based data row; `sheet_row` is the row number a spreadsheet would show."""
    if category not in CATEGORIES:
        raise ValueError("unknown category")
    if column not in df.columns:
        raise KeyError(column)
    res = classify_column(column, df[column], config)
    hit = {v for v, (cat, cd, _) in res["by_value"].items() if cat == category and (code is None or cd == code)}
    series = df[column].astype(object)
    mask = series.map(lambda v: (None if _is_blank(v) else v) in hit) if hit else pd.Series(False, index=series.index)
    idx = list(series.index[mask.to_numpy()])
    limit = max(1, min(int(limit), 1000))
    page = idx[max(0, int(offset)):max(0, int(offset)) + limit]
    pos = {lab: i for i, lab in enumerate(df.index)}
    rows = []
    for lab in page:
        v = series.at[lab]
        key = None if _is_blank(v) else v
        _, cd, why = res["by_value"][key]
        rows.append({"row_index": pos[lab], "sheet_row": pos[lab] + header_offset, "value": None if _is_blank(v) else _jsonable(v),
                     "code": cd, "reason": why})
    return {"column": column, "category": category, "code": code, "total": len(idx), "offset": int(offset), "rows": rows}
