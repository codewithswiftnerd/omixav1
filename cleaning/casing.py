"""Capitalisation consistency.

Why this exists: `categorical_standardization` only merges spellings that differ by case and
keeps whichever variant is most common. If most of a column is `sales` or `LAGOS`, the merged
result is still `sales` / `LAGOS`, and a column whose labels are all *different* words
(`active`, `INACTIVE`, `On hold`) is never touched at all, so mixed capitalisation survived
cleaning. Person names and ID prefixes were not looked at either.

Principles (same as the rest of the engine: fix only what is unambiguous):
  * Only values written in ONE uniform case (all lower or all UPPER) are rewritten. Anything
    already mixed-case (McDonald, O'Brien, van der Merwe, iPhone) is left exactly as typed.
  * Short all-caps tokens (HR, IT, USD, NGN) are treated as acronyms and kept.
  * A column that is consistently lower or consistently upper on purpose is left alone; the
    rule only acts when a column MIXES styles.
  * Every function here is pure: it returns a {old value: new value} plan, nothing is mutated,
    so the same plan drives both the fixer and the quality-report detector.
"""

from __future__ import annotations

import re
from typing import Optional

import pandas as pd

from cleaning import profiling

ACRONYM_MAX_LETTERS = 4
_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)
_ID_SHAPE = re.compile(r"^[A-Za-z]{1,8}[-_ ]?\d{2,}$")
_NAMEISH = re.compile(r"^[^\W\d_][^\W\d_'’.\- ]*(?:[ '’.\-][^\W\d_]+)*\.?$", re.UNICODE)

# Columns that hold free-form commentary, never a closed set of labels.
_FREE_TEXT_NAME_TOKENS = {"note", "notes", "comment", "comments", "remark", "remarks", "description",
                          "message", "feedback", "address", "bio", "summary", "review", "reason"}
_PLAIN_LABEL = re.compile(r"^[^\W\d_][^\W\d_ /&.'’\-]*(?:[ /&.'’\-]+[^\W\d_]+)*\.?$", re.UNICODE)
_MAX_LABEL_WORDS = 4
_MAX_LABEL_LEN = 40


# ---------------------------------------------------------------- primitives
def letters(value: str) -> str:
    return "".join(_LETTER.findall(value))


def style_of(value: str) -> Optional[str]:
    """'lower' | 'upper' | 'mixed' | None (no letters at all)."""
    ls = letters(value)
    if not ls:
        return None
    if ls.islower():
        return "lower"
    if ls.isupper():
        return "upper"
    return "mixed"


def is_acronym(value: str) -> bool:
    """HR, IT, I.T., USD, NGN/USD: one all-caps token (or tokens joined by / & ,) whose every
    part is short is an abbreviation. Words separated by spaces ("JOHN EZE") never are."""
    v = value.strip()
    if style_of(v) != "upper" or re.search(r"\s", v):
        return False
    parts = [p for p in re.split(r"[/&,]+", v) if letters(p)]
    return bool(parts) and all(len(letters(p)) <= ACRONYM_MAX_LETTERS for p in parts)


def variant_key(value: str) -> str:
    """Spellings that differ only by case, spacing, hyphens or dots share a key
    ("Port-Harcourt" == "Port Harcourt", "I.T." == "IT", "Web site" == "Website")."""
    key = re.sub(r"[\W_]+", "", value.lower())
    return key if len(key) >= 2 else " ".join(value.split()).lower()


def variant_rank(value: str) -> int:
    """Which spelling of the same word should represent the group. Lower is better:
    proper case (Pro) > short acronym (OK, HR) > all lower (pro) > shouting (PROFESSIONAL).
    Used before "most common" so a lowercase or ALL-CAPS majority can't win."""
    st = style_of(value)
    if st == "mixed":
        return 0
    if st == "upper" and is_acronym(value):
        return 1
    if st == "lower":
        return 2
    return 3


def canonical_variant(variants, counts) -> str:
    """Pick the representative of a group of case/spacing/punctuation variants."""
    # dotted abbreviations lose to the plain one regardless of count (I.T. -> IT); other punctuation
    # (the hyphen in Walk-in) is only a tie-break.
    return sorted(variants, key=lambda v: (variant_rank(v), punctuation_count(v) if variant_rank(v) == 1 else 0,
                                           -counts[v], punctuation_count(v), v))[0]


def punctuation_count(value: str) -> int:
    """Tie-break helper: between equally common variants prefer the one with less punctuation."""
    return len(re.findall(r"[^\w\s]", value))


def title_case(value: str) -> str:
    """Predictable title case: capital after spaces and hyphens; after an apostrophe only for a
    one-letter prefix (O'Brien, D'Angelo) so "john's" stays "John's"."""
    def word(w: str) -> str:
        res = []
        for part in w.split("-"):
            if "'" in part or "\u2019" in part:
                sep = "'" if "'" in part else "\u2019"
                head, _, tail = part.partition(sep)
                tail_fmt = tail.capitalize() if len(head) == 1 and tail else tail.lower()
                res.append(head.capitalize() + sep + tail_fmt)
            else:
                res.append(part.capitalize())
        return "-".join(res)
    return " ".join(word(w) for w in value.split(" "))


def sentence_case(value: str) -> str:
    """First letter capital, the rest lower (\"on hold\" -> \"On hold\")."""
    v = value.strip()
    return v[:1].upper() + v[1:].lower() if v else v


def _is_label(value: str) -> bool:
    v = value.strip()
    return bool(v) and len(v) <= _MAX_LABEL_LEN and len(v.split()) <= _MAX_LABEL_WORDS


# ---------------------------------------------------------------- column planners
def plan_person_names(values: pd.Series) -> dict:
    """{old: new} for names typed entirely in lower or UPPER case."""
    plan = {}
    for v in values.unique():
        if not isinstance(v, str) or not _NAMEISH.match(v.strip()):
            continue
        st = style_of(v)
        if st in ("lower", "upper") and not is_acronym(v):
            new = title_case(v)
            if new != v:
                plan[v] = new
    return plan


def looks_like_code_column(values: pd.Series) -> bool:
    vals = values.dropna().astype(str).str.strip()
    vals = vals[vals != ""]
    return len(vals) >= 3 and vals.map(lambda s: bool(_ID_SHAPE.match(s))).mean() >= 0.7


def plan_identifier_prefixes(values: pd.Series) -> dict:
    """cust-0011 -> CUST-0011 when the column's other IDs are upper-case (or the reverse).
    The majority style wins; values that are not letters+digits (CUST-X251) are untouched."""
    vals = values.dropna().astype(str)
    shaped = [v for v in vals.unique() if _ID_SHAPE.match(v.strip())]
    counts = vals.value_counts()
    up = sum(counts[v] for v in shaped if style_of(v) == "upper")
    lo = sum(counts[v] for v in shaped if style_of(v) == "lower")
    if not up or not lo:
        return {}
    plan = {}
    for v in shaped:
        st = style_of(v)
        new = v.upper() if up >= lo and st == "lower" else v.lower() if lo > up and st == "upper" else v
        if new != v:
            plan[v] = new
    return plan


def plan_category_labels(values: pd.Series) -> dict:
    """Make a label column use ONE casing style.

    Only acts when the column mixes styles. Lower-case and long UPPER-case labels are
    rewritten to the style the column's proper-case labels already use (Title Case, or
    Sentence case when its multi-word labels are sentence-cased)."""
    uniq = [v for v in values.unique() if isinstance(v, str) and _is_label(v)]
    if len(uniq) < 2:
        return {}
    by_key: dict[str, list[str]] = {}
    for v in uniq:
        by_key.setdefault(variant_key(v), []).append(v)

    def has_other_spelling(v):
        return len(by_key[variant_key(v)]) > 1

    styles = {v: style_of(v) for v in uniq}
    # "PRO" next to "Pro" is shouting, not an acronym; "HR" with no other spelling is an acronym.
    shouting = [v for v in uniq if styles[v] == "upper" and (not is_acronym(v) or has_other_spelling(v))]
    lower = [v for v in uniq if styles[v] == "lower"]
    mixed = [v for v in uniq if styles[v] == "mixed"]
    present = {s for s, group in (("upper", shouting), ("lower", lower), ("mixed", mixed)) if group}
    # an acronym that also appears in another case ("IT" and "it") makes the column mixed too
    acronym_variants = [v for v in uniq if styles[v] == "upper" and is_acronym(v) and has_other_spelling(v)]
    if len(present) < 2 and not acronym_variants:
        return {}

    multi = [v for v in mixed if len(v.split()) > 1]
    sentence_votes = sum(1 for v in multi if v == sentence_case(v))
    title_votes = sum(1 for v in multi if v == title_case(v))
    fmt = sentence_case if sentence_votes > title_votes else title_case

    plan = {}
    for v in shouting + lower:
        group = by_key[variant_key(v)]
        acronym = next((g for g in group if styles[g] == "upper" and is_acronym(g)), None)
        has_proper = any(styles[g] == "mixed" for g in group)
        if acronym and not has_proper and v != acronym:
            new = acronym                  # "it" -> "IT" when only IT / it exist
        elif is_acronym(v) and not has_proper and styles[v] == "upper":
            continue
        else:
            new = fmt(v)
        if new != v:
            plan[v] = new
    return plan


# ---------------------------------------------------------------- dataframe level
SKIP_TYPES = {profiling.EMAIL, profiling.PHONE, profiling.DATE, profiling.DATETIME,
              profiling.CURRENCY, profiling.NUMERIC_MEASUREMENT, profiling.FREE_TEXT,
              profiling.BOOLEAN}


def plan_column(name: str, series: pd.Series, total_rows: int,
                prof: Optional[profiling.ColumnProfile] = None) -> tuple[str, dict]:
    """(kind, {old: new}) for one text column; kind is 'name' | 'identifier' | 'category' | ''."""
    non_null = series.dropna()
    non_null = non_null[non_null.map(lambda v: isinstance(v, str))]
    if non_null.empty:
        return "", {}
    prof = prof or profiling.profile_column(str(name), series, total_rows)
    st, conf = prof.semantic_type, prof.confidence

    if st == profiling.PERSON_NAME and conf >= profiling.CONFIDENT:
        return "name", plan_person_names(non_null)
    if st in SKIP_TYPES:
        return "", {}
    if looks_like_code_column(non_null):
        return "identifier", plan_identifier_prefixes(non_null)

    if set(profiling.name_tokens(str(name))) & _FREE_TEXT_NAME_TOKENS:
        return "", {}
    # emoji, symbols and "!!!" notes mean this is commentary, not a label column
    plain = non_null.map(lambda v: bool(_PLAIN_LABEL.match(v.strip()))).mean()
    if plain < 0.8:
        return "", {}
    nunique = non_null.nunique()
    if st == profiling.IDENTIFIER or nunique < 2 or nunique > 50 or (total_rows and nunique > total_rows * 0.5):
        return "", {}
    return "category", plan_category_labels(non_null)


def plan_dataframe(df: pd.DataFrame) -> dict:
    """{column: (kind, {old: new})} for every column that has something to fix."""
    out = {}
    n = len(df)
    for col in df.select_dtypes(include=["object", "string"]).columns:
        kind, plan = plan_column(str(col), df[col], n)
        if plan:
            out[col] = (kind, plan)
    return out
