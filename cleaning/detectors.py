"""
Shared, read-only heuristics.

Both cleaning/rules.py (auto-fix) and cleaning/quality_report.py
(detect-only diagnostics) need to answer the same questions, "is
this an email column?", "is this a boolean-looking column?", "is
this string actually a placeholder for missing data?", and they
need to answer them the *same way*, or the report would end up
promising a fix that the rules don't actually perform (or vice
versa). This module is the single source of truth for those
questions. Nothing in here mutates a dataframe.
"""

from __future__ import annotations
import re
from typing import Optional
import pandas as pd

from cleaning.phone_formats import COUNTRIES as _COUNTRIES, COUNTRY_ALIASES as _COUNTRY_ALIASES
from cleaning import currencies as _currencies

# Country reference is built from cleaning/phone_formats.COUNTRIES.
# The same curated code/name list already used for phone-number
# resolution, rather than a second hardcoded list, so adding a
# country in one place (phone_formats.py) covers both features.
_COUNTRY_CODE_TO_NAME: dict[str, str] = {code: c["name"] for code, c in _COUNTRIES.items()}
_COUNTRY_NAME_TO_CANONICAL: dict[str, str] = {c["name"].lower(): c["name"] for c in _COUNTRIES.values()}

# Common placeholder strings people use for "no value" that pandas'
# own NA detection doesn't catch (those are usually only caught when
# the *whole* column is empty, not a single stray cell). Matched
# case-insensitively against the trimmed cell value. Deliberately
# does NOT include ambiguous words like "missing", that can be a
# legitimate category label (e.g. a survey answer), so it's left
# alone rather than guessed at.
#
# "unknown" IS included, unlike the original V1 rationale: the
# improvement spec explicitly lists "Unknown"/"unknown" as a
# placeholder Omixa must recognize as missing (spec section 2), so
# this is a deliberate, spec-driven change from V1's more cautious
# default. It's still a *safe* one, normalizing "Unknown" to a real
# missing value, then (if the column's missing % is low enough)
# refilling text columns with the literal string "Unknown" via
# handle_missing_values, round-trips a pure placeholder back to
# exactly what it was. The only actual behavior change is for
# columns where "unknown" was previously left as its own distinct
# category value; it's now folded into the single missing
# representation like every other placeholder.
MISSING_TOKENS = {
    "", "na", "n/a", "n.a.", "n\\a", "null", "none", "nan",
    "-", "--", "?", "#n/a", "nil", "unknown",
    # punctuation-only placeholders: "???", "—" (em dash), "–" (en dash), "---"
    "??", "???", "????", "---", "\u2014", "\u2013",
    # worded placeholders
    "not known", "not available", "not applicable", "not specified", "not provided", "unspecified",
    "no data", "missing", "tbd", "n.k.", "none given",
}

# Restricted to unambiguous words on purpose. "1"/"0" are excluded
# from the base set: plenty of real columns use 1/0 as numeric codes
# rather than booleans, and we'd rather leave those as numbers than
# guess. EXTENDED_* adds "1"/"0" back in, but only for columns whose
# NAME signals a boolean flag (is_active, status, ...), see
# is_boolean_flag_column below and its use in
# cleaning/rules.handle_boolean_standardization. A generically-named
# numeric-looking column never gets this treatment.
TRUE_WORDS = {"yes", "y", "true", "t"}
FALSE_WORDS = {"no", "n", "false", "f"}
BOOLEAN_WORDS = TRUE_WORDS | FALSE_WORDS

EXTENDED_TRUE_WORDS = TRUE_WORDS | {"1"}
EXTENDED_FALSE_WORDS = FALSE_WORDS | {"0"}
EXTENDED_BOOLEAN_WORDS = EXTENDED_TRUE_WORDS | EXTENDED_FALSE_WORDS

BOOLEAN_FLAG_NAME_KEYWORDS = (
    "is_active", "active", "status", "enabled", "is_verified",
    "verified", "subscribed", "confirmed", "is_",
)

# Fixed, unambiguous gender vocabulary. Deliberately does NOT include
# anything else ("other", "non-binary", "prefer not to say", ...).
    # Those are left completely untouched, never remapped or guessed at.
MALE_WORDS = {"male", "m"}
FEMALE_WORDS = {"female", "f"}
GENDER_WORDS = MALE_WORDS | FEMALE_WORDS

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Characters that are purely visual/formatting noise on an otherwise
# numeric value: thousands separators, percent signs, stray whitespace.
# Currency symbols/abbreviations for 140+ currencies are removed first by
# cleaning/currencies.py (strip_currency), which is why they aren't listed here.
_NUMERIC_NOISE_RE = re.compile(r"[,\s%]")
_PAREN_NEGATIVE_RE = re.compile(r"^\((.*)\)$")
_CURRENCY_SYMBOL_RE = re.compile(r"[$€£¥]")

_SCI_NOTATION_RE = re.compile(r"^-?\d+(\.\d+)?[eE][+-]?\d+$")
_PHONE_SAFE_CHARS_RE = re.compile(r"^[+()\-.\s\d]+$")


def is_missing_token(value: str) -> bool:
    return value.strip().lower() in MISSING_TOKENS


def is_email_column(name: str, series: pd.Series) -> bool:
    """Name hint first (cheap, explicit); falls back to checking
    whether most non-null values actually look like an email."""
    lname = name.lower()
    if "email" in lname or "e-mail" in lname or "e_mail" in lname:
        return True
    values = series.dropna().astype(str)
    if len(values) < 3:
        return False
    matches = values.str.match(EMAIL_RE)
    return bool(matches.mean() >= 0.8)


def is_phone_column(name: str) -> bool:
    lname = name.lower()
    return any(k in lname for k in (
        "phone", "mobile", "contact_no", "contact no", "contactnumber",
        "contact_number", "telephone", "tel_no", "whatsapp", "fax",
    )) or lname.strip() in ("tel", "cell")


def is_gender_column(name: str) -> bool:
    lname = name.lower().strip()
    return lname in ("gender", "sex") or "gender" in lname


def is_boolean_flag_column(name: str) -> bool:
    """Name-only signal that a column is meant to hold a yes/no-style
    flag (is_active, status, ...), used to decide whether "1"/"0" are
    safe to treat as True/False for THIS column. Never applied on its
    own, handle_boolean_standardization still requires every actual
    value in the column to be a recognized boolean word before
    converting anything."""
    lname = name.lower().strip()
    return any(k in lname for k in BOOLEAN_FLAG_NAME_KEYWORDS)


def is_age_column(name: str) -> bool:
    lname = name.lower().strip()
    return lname == "age" or lname.endswith("_age") or lname.startswith("age_")


# A birth date is the one date type where "in the future" is always
# impossible, regardless of dataset, unlike a generic "date" column
# (due_date, expiry_date, event_date, ...) where a future value is
# often completely legitimate. Kept deliberately narrower than
# is_probable_date_column's "date" substring match for that reason.
BIRTH_DATE_NAME_KEYWORDS = ("dob", "birth", "birthday")

# Earliest year treated as plausible for ANY date column. Chosen to
# be conservative (a genuine pre-1900 record is rare in the kind of
# spreadsheet Omixa targets, but not impossible), so this only
# catches values that are essentially always a data-entry slip
# (typo'd year, a "0002" from a broken date picker, etc.).
MIN_PLAUSIBLE_YEAR = 1900


def is_birth_date_column(name: str) -> bool:
    lname = name.lower().strip()
    return any(k in lname for k in BIRTH_DATE_NAME_KEYWORDS)


def is_country_column(name: str) -> bool:
    lname = name.lower().strip()
    return lname == "nation" or "country" in lname


# Column-name hints for values that are IDENTIFIERS rather than
# quantities, account numbers, IBANs, BVNs/NUBANs (Nigerian bank
# identifiers), postal codes, card numbers, etc. Nothing here should
# ever be converted to a numeric dtype: it's never added, averaged,
# or compared numerically, and doing so risks losing a leading zero
# (a real digit in a phone/account number, not padding) or having
# Excel silently render it in scientific notation on export.
ID_NAME_KEYWORDS = (
    "account_no", "account_num", "account_number", "account_id", "account_code",
    "acct_no", "acct_num", "acct_number", "acct_id", "iban", "swift", "bvn", "nuban", "sort_code",
    "zip", "zipcode", "postal", "postcode", "ssn", "passport",
    "national_id", "reg_no", "registration_no", "card_number",
    "pin_code", "routing_number", "vin", "imei",
)

_LEADING_ZERO_RE = re.compile(r"^0\d+$")


def is_identifier_name(name: str) -> bool:
    """Name-only half of the identifier check, usable before any
    data has been read (e.g. to decide read-time dtype), unlike
    is_identifier_column which also needs the values."""
    if is_phone_column(name):
        return True
    lname = re.sub(r"[\s\-]+", "_", name.strip().lower())
    # "account_status" / "account_type" are labels, not identifiers: only a bare "account" or an
    # account/acct column that says it is a number/id/code counts.
    if lname in ("account", "acct") or any(k in lname for k in ID_NAME_KEYWORDS):
        return True
    return lname.strip() == "id" or lname.endswith("_id") or lname.endswith(" id")


def is_identifier_column(name: str, series: pd.Series) -> bool:
    """
    True for columns that should never be coerced to a numeric dtype,
    even if every value happens to parse as a number.

    Two ways a column earns this: its name matches a known
    identifier pattern (phone, account, IBAN, postal code, ...), or
    its values do. The value-based check catches identifier columns
    with generic names ("Number", "Ref", "ID") by looking for a
    leading zero on an otherwise all-digit value (e.g. "0803317157",
    "0042"), real quantities essentially never carry a meaningful
    leading zero, but phone numbers, account numbers, and reference
    codes very often do, and converting them to a numeric dtype
    would silently drop that leading digit.
    """
    if is_identifier_name(name):
        return True

    values = series.dropna().astype(str).str.strip()
    if values.empty:
        return False
    return bool(values.str.match(_LEADING_ZERO_RE).any())


_DATE_NAME_TOKENS = {
    "date", "dob", "birthday", "birthdate", "datetime", "timestamp",
    # only counted as date names because is_probable_date_column also requires the VALUES to parse
    "created", "updated", "modified", "expiry", "expiration",
}
# compound words that really are dates: signupdate, startdate, createddate, ...
_DATE_SUFFIX_RE = re.compile(
    r"(?:start|end|due|birth|created|updated|modified|signup|join|joined|order|ship|shipped|hire|hired|"
    r"expiry|expire|expiration|issue|issued|delivery|payment|release|registration|enrol|enroll|admission|"
    r"graduation|purchase|transaction|invoice|event|visit|appointment)date$"
)


def has_date_name(name: str) -> bool:
    """True when the column NAME says it holds a date.

    Token-based on purpose: a plain substring test ("date" in name) also matches
    candidate_name, validated_by, update_note and mandate, which then got reported as
    'ambiguous date columns' and fed into the date-standardisation rule."""
    import re as _re
    spaced = _re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(name))
    tokens = [t for t in _re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t]
    if any(t in _DATE_NAME_TOKENS for t in tokens):
        return True
    return any(_DATE_SUFFIX_RE.search(t) for t in tokens)


# Share of non-empty values that must parse as a date before a column NAMED like a
# date is actually treated as one. Stops a text column that merely has 'date' in
# its name from being reported as ambiguous when nothing in it parses.
MIN_DATE_PARSE_SHARE = 0.5


def is_probable_date_column(name: str, series: pd.Series) -> bool:
    """Name hint (token-based, see has_date_name) backed by the values, or a sample
    of values that mostly parse as dates AND mostly contain date-shaped separators
    (so a plain numeric ID column is not flagged as a date just because pandas can
    technically parse '20230101' as one)."""
    values = series.dropna().astype(str)
    if values.empty:
        return False

    if has_date_name(name):
        sample = values.head(500)
        parsed = pd.to_datetime(sample, errors="coerce", format="mixed")
        return bool(parsed.notna().mean() >= MIN_DATE_PARSE_SHARE)

    if len(values) < 3:
        return False
    shaped = values.str.match(r"^\d{1,4}[/\-.]\d{1,2}[/\-.]\d{1,4}$")
    if shaped.mean() < 0.8:
        return False
    parsed = pd.to_datetime(values, errors="coerce", format="mixed")
    return bool(parsed.notna().mean() >= 0.8)



_YEAR_FIRST_RE = r"^\s*\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:[ T].*)?$"


def parse_dates(values: pd.Series, dayfirst: bool) -> pd.Series:
    """Parse date-like strings, applying day-first/month-first ONLY where it can matter.

    pandas applies dayfirst=True to year-first strings as well, so "1990-01-05" came back as
    1 May 1990. A year-first date (2024-03-04) has one conventional reading (Y-M-D), so it is
    parsed that way whatever `dayfirst` says; only the remaining D/M/Y-style values follow
    the flag. Without this, pure-ISO columns looked "ambiguous" and a user choosing
    day-first silently swapped day and month in every ISO value."""
    text = values.astype("string")
    year_first = text.str.match(_YEAR_FIRST_RE).fillna(False).astype(bool)
    rest = ~year_first & values.notna()
    pieces = []
    if year_first.any():
        pieces.append(pd.to_datetime(text[year_first], errors="coerce", dayfirst=False, format="mixed"))
    if rest.any():
        pieces.append(pd.to_datetime(text[rest], errors="coerce", dayfirst=dayfirst, format="mixed"))
    if not pieces:
        return pd.Series(pd.to_datetime([pd.NaT] * len(values)), index=values.index)
    return pd.concat(pieces).reindex(values.index)


def unambiguous_date_parse(values: pd.Series) -> Optional[pd.Series]:
    """
    Tries to parse a column of date-like strings safely, judging
    each value on its own rather than as an all-or-nothing block.

    For every non-null value, both day-first and month-first parsing
    are tried:
      - if only one convention produces a valid date (e.g. any day-
        of-month >12 makes month-first impossible), that value is
        unambiguous by elimination;
      - if both produce the SAME date (e.g. already-ISO values, or a
        day/month that happen to coincide), it's unambiguous;
      - if both produce valid but DIFFERENT dates (e.g. "03/04/2024"
        could be 3 April or March 4th), that single value is
        genuinely ambiguous;
      - if neither convention can parse it (a typo, a placeholder
        like "Unknown"), it's simply invalid, not ambiguous.

    A handful of invalid or missing values no longer block
    standardizing the rest of an otherwise-clean column, each row
    is judged independently, so 14 good dates and 1 typo now
    standardize the 14 and leave the typo exactly as it was (see
    handle_date_standardization), instead of the whole column being
    skipped.

    Returns None only when the column contains at least one
    genuinely ambiguous value (day-first vs month-first disagree, that calls for a human to confirm the intended convention, since
    it should apply consistently to every row in the column) or when
    nothing in the column could be parsed at all. Otherwise returns a
    Series aligned to `values`' index, with NaT for any value that
    couldn't be parsed under either convention.
    """
    non_null = values.dropna().astype(str)
    if non_null.empty:
        return None

    day_first = parse_dates(non_null, dayfirst=True)
    month_first = parse_dates(non_null, dayfirst=False)

    day_ok = day_first.notna()
    month_ok = month_first.notna()
    both_ok = day_ok & month_ok
    disagree = both_ok & (day_first != month_first)

    if disagree.any():
        return None  # genuine day-first vs month-first ambiguity somewhere in the column

    # Where only one convention parsed, use it; where both parsed,
    # they already agree (the disagreement case returned above), so
    # either works, day-first is the arbitrary tie-breaker.
    resolved = day_first.where(day_ok, month_first)

    if resolved.isna().all():
        return None  # nothing in the column could be parsed at all

    # reindex (not .loc assignment into a preallocated ns-typed Series): newer pandas parses to
    # microsecond resolution and refuses that assignment with an internal AssertionError.
    return resolved.reindex(values.index)


# A comma is a thousands separator only when it sits in proper groups of three
# ("1,200", "12,345,678.50"). A comma followed by one or two digits at the end
# ("3,45") is a decimal comma. Anything else with a comma is ambiguous.
_THOUSANDS_RE = re.compile(r"^[^\d]*-?\d{1,3}(,\d{3})+(\.\d+)?[^\d]*$")
_DECIMAL_COMMA_RE = re.compile(r"^(-?\d+),(\d{1,2})$")


# --- text that is really a number -------------------------------------------------------------
# "44 yrs" -> 44, "twenty" -> 20, "1O,OOO" (letter O typed for zero) -> 1,000 style digits.
_UNIT_SUFFIX_RE = re.compile(r"^(-?\d+(?:\.\d+)?)\s*(?:yrs?|years?|y/o|yo)\.?$", re.I)
_OCR_ZERO_RE = re.compile(r"^[\dOo][\dOo,.\s]*$")
_UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
          "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
          "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
         "ninety": 90}
ZERO_WORDS = {"free", "gratis", "complimentary", "no charge"}
MONEY_NAME_TOKENS = {"amount", "paid", "price", "cost", "fee", "fees", "total", "revenue", "payment",
                     "salary", "balance", "charge", "spend"}


def parse_number_words(text: str) -> Optional[int]:
    """\"twenty\" -> 20, \"twenty-five\" -> 25, \"ten thousand\" -> 10000. None if it is not purely
    number words (so ordinary words are never read as numbers)."""
    words = re.split(r"[\s\-]+", text.strip().lower().replace(" and ", " "))
    if not words or words == [""]:
        return None
    total = current = 0
    seen = False
    for w in words:
        if w in _UNITS:
            current += _UNITS[w]
        elif w in _TENS:
            current += _TENS[w]
        elif w == "hundred" and current:
            current *= 100
        elif w == "thousand" and (current or total):
            total += (current or 1) * 1000
            current = 0
        else:
            return None
        seen = True
    return total + current if seen else None


def strip_numeric_noise(value: str) -> str:
    m = _UNIT_SUFFIX_RE.match(value.strip())
    if m:
        return m.group(1)
    spoken = re.sub(r"\s+(naira|dollars?|usd|ngn|pounds?|gbp|euros?)$", "", value.strip(), flags=re.I)
    words = parse_number_words(spoken) if re.fullmatch(r"[A-Za-z][A-Za-z\s\-]*", spoken) else None
    if words is not None:
        return str(words)
    v = _currencies.strip_currency(value)
    if _OCR_ZERO_RE.match(v.strip()) and re.search(r"\d", v) and re.search(r"[Oo]", v):
        v = re.sub(r"[Oo]", "0", v)
    if "," in v:
        core = _NUMERIC_NOISE_RE.sub("", v.replace(",", "")) if _THOUSANDS_RE.match(v) else None
        if core is None:
            m = _DECIMAL_COMMA_RE.match(_NUMERIC_NOISE_RE.sub("", v.replace(",", "|")).replace("|", ","))
            if m:
                # decimal comma ("3,45" -> "3.45"), never read as thousands (345)
                return f"{m.group(1)}.{m.group(2)}"
            # ambiguous comma placement: leave the comma in so the value does not
            # parse and the whole column is left untouched and surfaced for review
            return v
    s = _NUMERIC_NOISE_RE.sub("", v)
    m = _PAREN_NEGATIVE_RE.match(s)
    if m:
        s = "-" + m.group(1)
    return s


def try_parse_float(cleaned: str) -> Optional[float]:
    if cleaned in ("", "-", "."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def standardize_country_value(value: str) -> Optional[str]:
    """
    Maps a country CODE ("NG"), a common alternate name/abbreviation
    ("USA", "UK", see phone_formats.COUNTRY_ALIASES), or a
    differently-cased/spaced country NAME ("nigeria", " Nigeria ") to
    the single canonical name ("Nigeria") from
    cleaning/phone_formats.COUNTRIES. Returns None (meaning: leave
    the original value untouched) for anything not found in either
    reference, no fuzzy matching, so a typo or an unlisted country
    is left alone rather than guessed at. Extending country coverage
    only ever requires adding an entry to phone_formats.COUNTRIES or
    phone_formats.COUNTRY_ALIASES; nothing here needs to change.
    """
    if not isinstance(value, str):
        return None
    v = value.strip()
    if not v:
        return None
    code = v.upper()
    if code in _COUNTRY_CODE_TO_NAME:
        return _COUNTRY_CODE_TO_NAME[code]
    if code in _COUNTRY_ALIASES:
        return _COUNTRY_CODE_TO_NAME[_COUNTRY_ALIASES[code]]
    return _COUNTRY_NAME_TO_CANONICAL.get(v.lower())


def is_scientific_notation(value: object) -> bool:
    """True for strings shaped like a float in scientific notation
    (e.g. "1.343E+12"), the classic sign that a spreadsheet
    silently reformatted a long digit string (a phone number) into a
    number."""
    if not isinstance(value, str):
        return False
    return bool(_SCI_NOTATION_RE.match(value.strip()))


def try_recover_scientific_phone(value: str) -> Optional[str]:
    """
    Attempts to reconstruct the original all-digit string from a
    scientific-notation value, WITHOUT inventing any digit.

    A value like "1.343E+12" only carries as many significant digits
    as its mantissa shows ("1343"); expanding it to a 13-digit number
    would mean inventing 9 trailing zeros that were never actually
    there. This only returns a reconstructed value when the mantissa
    already carries enough digits after the decimal point to cover
    the exponent, i.e. the expansion is exact, not padded. That's
    rarely true for a real spreadsheet-mangled phone number (Excel
    usually keeps only a handful of significant digits), so this
    intentionally returns None, "flag it, don't guess", for the
    large majority of real cases; see cleaning/quality_report.py's
    unrecoverable_scientific_notation finding for what happens then.
    """
    if not is_scientific_notation(value):
        return None

    s = value.strip()
    sign = "-" if s.startswith("-") else ""
    s = s.lstrip("+-")
    mantissa, exponent_str = re.split(r"[eE]", s)
    exponent = int(exponent_str)
    if exponent < 0:
        return None  # a phone number can't have a fractional digit count

    if "." in mantissa:
        int_part, frac_part = mantissa.split(".", 1)
    else:
        int_part, frac_part = mantissa, ""

    if len(frac_part) < exponent:
        return None  # would require inventing trailing zeros -> unsafe, flag instead

    digits = (int_part + frac_part)[: len(int_part) + exponent]
    if not digits:
        return None
    return sign + digits


def normalize_phone_punctuation(value: str) -> Optional[str]:
    """
    Strips spaces, brackets, hyphens, and dots from a phone-like
    value while preserving every digit and a single leading '+' (if
    present), "+234 806-123-4567" -> "+2348061234567",
    "(234) 806 123 4567" -> "2348061234567". Returns None (leave
    untouched) if the value contains anything other than digits and
    that expected punctuation (e.g. an extension like "ext. 204", or
    free text), mixed content like that isn't safe to strip blindly,
    since punctuation might be meaningful there. Never adds, drops,
    reorders digits, or adds a country code that wasn't already
    present; the number stays a string throughout (never a numeric
    dtype). Country-aware reformatting (choosing which region a bare
    number belongs to) is a separate, user-confirmed step, see
    cleaning/phone_formats.normalize_phone.
    """
    s = value.strip()
    if not s or not _PHONE_SAFE_CHARS_RE.match(s):
        return None
    plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    cleaned = ("+" if plus else "") + digits
    return cleaned if cleaned != s else None


# --------------------------------------------------------------------------
# Date helpers used by the quality checks
# --------------------------------------------------------------------------

# Event-in-the-past columns: a value in the future is impossible, not just unusual
# (a signup, order, payment or birth that has not happened yet). Generic 'date',
# 'due_date', 'expiry_date', 'event_date' columns are deliberately NOT here, a future
# value is routine for those.
PAST_EVENT_NAME_TOKENS = {
    "dob", "birth", "birthday", "birthdate", "created", "signup", "registered", "registration",
    "joined", "hired", "purchase", "purchased", "transaction", "paid", "payment", "shipped", "delivered",
}


def is_past_event_date_column(name: str) -> bool:
    import re as _re
    spaced = _re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(name))
    tokens = {t for t in _re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t}
    return bool(tokens & PAST_EVENT_NAME_TOKENS) or is_birth_date_column(name)


def date_parse_stats(values: pd.Series) -> dict:
    """Per-value ambiguity census for a date-like text column.

    {"total": non-null count, "ambiguous": values whose day-first and month-first
    readings are both valid but differ, "unparseable": neither reading works,
    "two_digit_year": values written with a 2-digit year (century is a guess)}"""
    non_null = values.dropna().astype(str)
    if non_null.empty:
        return {"total": 0, "ambiguous": 0, "unparseable": 0, "two_digit_year": 0}
    day_first = parse_dates(non_null, dayfirst=True)
    month_first = parse_dates(non_null, dayfirst=False)
    both = day_first.notna() & month_first.notna()
    return {
        "total": int(len(non_null)),
        "ambiguous": int((both & (day_first != month_first)).sum()),
        "unparseable": int((day_first.isna() & month_first.isna()).sum()),
        "two_digit_year": int(non_null.str.match(r"^\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2}$").sum()),
    }


def has_time_component(values: pd.Series) -> bool:
    """True when any date-like text carries a time of day, so rewriting it as a bare
    YYYY-MM-DD would silently throw the time away."""
    non_null = values.dropna().astype(str)
    if non_null.empty:
        return False
    return bool(non_null.str.contains(r"\d{1,2}:\d{2}", regex=True).any())


def fill_with_median(series: pd.Series) -> tuple[pd.Series, float]:
    """Median imputation that cannot crash on integer columns.

    pandas' nullable Int64 (what the numeric-cleaning rule produces for whole-number
    columns) refuses a fractional fill value, so the median 59.5 of [44, 56, 56, 63, 71,
    82] used to raise TypeError and fail the whole job. A whole-number median keeps the
    column's integer type; a fractional one converts the column to float64 rather than
    silently rounding the estimate."""
    median = series.median()
    is_int_like = pd.api.types.is_integer_dtype(series)
    if is_int_like and pd.notna(median) and float(median) != int(median):
        series = series.astype("float64")
    elif is_int_like and pd.notna(median):
        median = int(median)
    return series.fillna(median), median
