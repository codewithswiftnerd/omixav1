"""
Date rules. Numeric day/month dates such as 01/02/2026 are only interpreted when the evidence
supports one reading: an explicit `day_first` setting, or other values in the SAME column that can
only be read one way (e.g. 31/12/2026). Otherwise the value is flagged and left unchanged.
"""

from __future__ import annotations

import re

import pandas as pd

from cleaning import model as M
from cleaning.engine.base import (BaseCleaningRule, Flag, ParamSpec, RuleContext, RuleResult, as_object,
                                  map_unique, str_mask)

OUTPUT_FORMATS = {
    "YYYY-MM-DD": "%Y-%m-%d", "DD/MM/YYYY": "%d/%m/%Y", "MM/DD/YYYY": "%m/%d/%Y",
    "DD-MM-YYYY": "%d-%m-%Y", "DD.MM.YYYY": "%d.%m.%Y", "D MMM YYYY": "%d %b %Y", "MMMM D, YYYY": "%B %d, %Y",
}
_MONTHS = {m.lower(): i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
_MONTHS.update({m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
     "November", "December"], 1)})
_MONTHS["sept"] = 9

_ISO = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$")
_NUM = re.compile(r"^(\d{1,2})([/\-.])(\d{1,2})\2(\d{4})$")
_NUM2Y = re.compile(r"^(\d{1,2})([/\-.])(\d{1,2})\2(\d{2})$")
_TEXT_DMY = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)?[\s\-/]+([A-Za-z]{3,9})\.?,?[\s\-/]+(\d{4})$")
_TEXT_MDY = re.compile(r"^([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})$")


def _valid(y, m, d) -> bool:
    try:
        pd.Timestamp(year=int(y), month=int(m), day=int(d))
        return 1000 <= int(y) <= 9999
    except (ValueError, OverflowError):
        return False


def _parse_one(txt: str):
    """-> ("iso", (y,m,d)) | ("dm", a, b, y) | ("text", (y,m,d)) | ("invalid", msg) | ("unsupported", msg)"""
    t = txt.strip()
    mt = _ISO.match(t)
    if mt:
        y, m, d = map(int, mt.groups())
        return ("iso", (y, m, d)) if _valid(y, m, d) else ("invalid", "Not a real calendar date.")
    mt = _NUM.match(t)
    if mt:
        a, _, b, y = mt.groups()
        return ("dm", int(a), int(b), int(y))
    if _NUM2Y.match(t):
        return ("two_digit_year", "Two-digit year: the century is ambiguous.")
    mt = _TEXT_DMY.match(t)
    if mt and mt.group(2).lower() in _MONTHS:
        d, mon, y = int(mt.group(1)), _MONTHS[mt.group(2).lower()], int(mt.group(3))
        return ("text", (y, mon, d)) if _valid(y, mon, d) else ("invalid", "Not a real calendar date.")
    mt = _TEXT_MDY.match(t)
    if mt and mt.group(1).lower() in _MONTHS:
        mon, d, y = _MONTHS[mt.group(1).lower()], int(mt.group(2)), int(mt.group(3))
        return ("text", (y, mon, d)) if _valid(y, mon, d) else ("invalid", "Not a real calendar date.")
    if re.search(r"\d{1,2}:\d{2}", t):
        return ("unsupported", "Contains a time of day; only dates are supported.")
    return ("unsupported", "Not a recognised date format.")


def build_decider(series: pd.Series, day_first, render):
    """Returns (decide, info). `decide(raw)` is a map_unique decision function.

    `render(y, m, d)` -> the new text, or None for validate-only rules (value is valid, unchanged).
    Column-level evidence (values that can only be read one way) is gathered over the DISTINCT
    values once, so the cost is per distinct value, not per row."""
    info = {"formats": {}, "day_first_used": None, "evidence": None}
    s = as_object(series)
    strs = [v for v in pd.unique(s[str_mask(s)]) if v.strip() != ""]
    parsed = {v: _parse_one(v) for v in strs}

    dayfirst_votes = sum(1 for p in parsed.values() if p[0] == "dm" and p[1] > 12 and p[2] <= 12)
    monthfirst_votes = sum(1 for p in parsed.values() if p[0] == "dm" and p[2] > 12 and p[1] <= 12)
    if day_first == "auto":
        if dayfirst_votes and not monthfirst_votes:
            resolved_df, info["evidence"] = True, f"{dayfirst_votes} value(s) can only be day-first"
        elif monthfirst_votes and not dayfirst_votes:
            resolved_df, info["evidence"] = False, f"{monthfirst_votes} value(s) can only be month-first"
        else:
            resolved_df = None
            if dayfirst_votes and monthfirst_votes:
                info["evidence"] = "conflicting: both day-first and month-first values exist"
    else:
        resolved_df, info["evidence"] = bool(day_first), "set explicitly in the rule"
    info["day_first_used"] = resolved_df
    for p in parsed.values():
        info["formats"][p[0]] = info["formats"].get(p[0], 0) + 1

    def done(y, m, d):
        text = render(y, m, d)
        return ("set", text) if text is not None else ("valid",)

    def decide(raw):
        if not isinstance(raw, str):
            if hasattr(raw, "strftime"):
                return done(raw.year, raw.month, raw.day)
            return None
        if raw.strip() == "":
            return None
        p = parsed.get(raw) or _parse_one(raw)
        kind = p[0]
        if kind in ("iso", "text"):
            return done(*p[1])
        if kind == "dm":
            a, b, y = p[1], p[2], p[3]
            if a > 12 and b <= 12:
                day, month = a, b
            elif b > 12 and a <= 12:
                day, month = b, a
            elif a == b:
                day, month = a, b
            elif a <= 12 and b <= 12:
                if resolved_df is None:
                    opts = [f"{y:04d}-{mm:02d}-{dd:02d}" for dd, mm in ((a, b), (b, a)) if _valid(y, mm, dd)]
                    return ("flag", "ambiguous_date",
                            "Could be day/month or month/day and nothing in the column settles it.",
                            " or ".join(opts) or None)
                day, month = (a, b) if resolved_df else (b, a)
            else:
                return ("flag", "invalid_date", "Neither number can be a month.", None)
            if _valid(y, month, day):
                return done(y, month, day)
            return ("flag", "invalid_date", "Not a real calendar date.", None)
        if kind == "two_digit_year":
            return ("flag", "ambiguous_date", p[1], None)
        return ("flag", "invalid_date" if kind == "invalid" else "unrecognised_date_format", p[1], None)

    return decide, info


def _count(flags, reason):
    return sum(f.reason == reason for f in flags)


class NormalizeDateRule(BaseCleaningRule):
    type = "normalize_date"
    title = "Date normalization"
    description = ("Reads common date layouts and rewrites them in the output format you pick. Ambiguous values "
                   "(01/02/2026), invalid dates and unrecognised text are flagged and left unchanged.")
    capability = "normalize_date"
    phase = 30
    applies_to = ("date", "datetime")
    params = {
        "output_format": ParamSpec("choice", default="YYYY-MM-DD", choices=tuple(OUTPUT_FORMATS)),
        "day_first": ParamSpec("choice", default="auto", choices=("auto", True, False),
                               description="auto = decide from unambiguous values in the column, else flag."),
    }

    def apply(self, series, ctx):
        fmt = OUTPUT_FORMATS[self.p["output_format"]]
        if pd.api.types.is_datetime64_any_dtype(series):
            out = series.dt.strftime(fmt).astype(object)
            out[series.isna()] = None
            return RuleResult(out, metrics={"converted": int(series.notna().sum()), "ambiguous": 0, "invalid": 0})
        decide, info = build_decider(series, self.p["day_first"],
                                     lambda y, m, d: pd.Timestamp(year=y, month=m, day=d).strftime(fmt))
        out, flags, changed = map_unique(series, decide)
        return RuleResult(out, flags, metrics={
            "converted": changed, "ambiguous": _count(flags, "ambiguous_date"), "invalid": _count(flags, "invalid_date"),
            "unrecognised": _count(flags, "unrecognised_date_format"), "day_first_used": info["day_first_used"],
            "evidence": info["evidence"], "formats_seen": info["formats"]})


class ValidateDateRule(BaseCleaningRule):
    type = "validate_date"
    title = "Date validation"
    description = "Flags invalid, ambiguous and unrecognised dates. Changes nothing."
    capability = "validate_date"
    operation = M.NONE
    mutates = False
    phase = 60
    applies_to = ("date", "datetime")
    params = {"day_first": ParamSpec("choice", default="auto", choices=("auto", True, False))}

    def apply(self, series, ctx):
        if pd.api.types.is_datetime64_any_dtype(series):
            return RuleResult(as_object(series).copy(), metrics={"valid": int(series.notna().sum()), "flagged": 0})
        decide, info = build_decider(series, self.p["day_first"], lambda y, m, d: None)
        out, flags, _ = map_unique(series, decide)
        checked = int((~out.isna() & ~out.map(lambda v: isinstance(v, str) and v.strip() == "")).sum())
        return RuleResult(out, flags, metrics={"valid": checked - len(flags), "flagged": len(flags),
                                               "formats_seen": info["formats"]})


class DateDetectionRule(BaseCleaningRule):
    type = "basic_date_detection"
    title = "Date detection"
    description = "Reports which date layouts a column uses and how many values are ambiguous. Changes nothing."
    capability = "basic_date_detection"
    operation = M.NONE
    mutates = False
    phase = 70
    applies_to = ("date", "datetime")
    params = {}

    def apply(self, series, ctx):
        if pd.api.types.is_datetime64_any_dtype(series):
            return RuleResult(as_object(series).copy(), metrics={"parsed": int(series.notna().sum()), "ambiguous": 0, "invalid": 0})
        decide, info = build_decider(series, "auto", lambda y, m, d: None)
        out, flags, _ = map_unique(series, decide)
        checked = int((~out.isna() & ~out.map(lambda v: isinstance(v, str) and v.strip() == "")).sum())
        return RuleResult(out, [], metrics={
            "parsed": checked - len(flags), "ambiguous": _count(flags, "ambiguous_date"),
            "invalid": _count(flags, "invalid_date"), "formats_seen": info["formats"]})
