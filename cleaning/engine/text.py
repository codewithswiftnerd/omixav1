"""Whitespace, case, missing-value and replacement rules."""

from __future__ import annotations

import re

import pandas as pd

from cleaning import model as M
from cleaning.engine.base import (BaseCleaningRule, ParamSpec, RuleContext, RuleResult, as_object,
                                  blank_mask, str_mask)

_WS_RUN = re.compile(r"[\s\u00a0\u200b\u2007\u202f]+")
_EDGE_WS = r"^[\s\u00a0\u200b\u2007\u202f]+|[\s\u00a0\u200b\u2007\u202f]+$"


def _map_strings(series: pd.Series, fn) -> pd.Series:
    """Applies a vectorised string transform to the string cells only; nulls and
    non-string values (numbers, dates, booleans) pass through untouched."""
    out = as_object(series).copy()
    m = str_mask(out)
    if m.any():
        out.loc[m] = fn(out.loc[m].str)
    return out


class TrimWhitespaceRule(BaseCleaningRule):
    type = "trim_whitespace"
    title = "Trim whitespace"
    description = "Removes leading and trailing whitespace."
    capability = "trim_whitespace"
    phase = 10

    def apply(self, series, ctx):
        return RuleResult(_map_strings(series, lambda s: s.replace(_EDGE_WS, "", regex=True)))


class NormalizeWhitespaceRule(BaseCleaningRule):
    type = "normalize_whitespace"
    title = "Normalize whitespace"
    description = "Trims the value and collapses repeated internal whitespace to a single space."
    capability = "normalize_whitespace"
    phase = 10

    def apply(self, series, ctx):
        return RuleResult(_map_strings(series, lambda s: s.replace(_WS_RUN.pattern, " ", regex=True)
                                                         .str.replace(_EDGE_WS, "", regex=True)))


# --------------------------------------------------------------------------- case

_SMALL_WORDS: set = set()  # deliberately empty: "Title Case" means every word, predictably


def title_case(value: str) -> str:
    """Predictable title case: capital after whitespace and hyphens; after an apostrophe only
    for a one-letter prefix (O'Brien, D'Angelo) so "john's" stays "John's"."""
    def word(w: str) -> str:
        parts = w.split("-")
        res = []
        for part in parts:
            if "'" in part or "\u2019" in part:
                sep = "'" if "'" in part else "\u2019"
                head, _, tail = part.partition(sep)
                tail_fmt = tail.capitalize() if len(head) == 1 and tail else tail.lower()
                res.append(head.capitalize() + sep + tail_fmt)
            else:
                res.append(part.capitalize())
        return "-".join(res)
    return " ".join(word(w) for w in value.split(" "))


class NormalizeCaseRule(BaseCleaningRule):
    type = "normalize_case"
    title = "Text case"
    description = "Applies lowercase, UPPERCASE or Title Case to this column only."
    capability = "normalize_case"
    phase = 40
    params = {"mode": ParamSpec("choice", required=True, choices=("preserve", "lower", "upper", "title"),
                                description="Target capitalisation.")}

    def apply(self, series, ctx):
        mode = self.p["mode"]
        if mode == "preserve":
            return RuleResult(as_object(series).copy())
        if mode == "lower":
            return RuleResult(_map_strings(series, lambda s: s.lower()))
        if mode == "upper":
            return RuleResult(_map_strings(series, lambda s: s.upper()))
        out = as_object(series).copy()
        m = str_mask(out)
        if m.any():
            out.loc[m] = out.loc[m].map(title_case)   # per value; word-aware, so not vectorisable
        return RuleResult(out)


# --------------------------------------------------------------------------- missing values

DEFAULT_MISSING_TOKENS = ("n/a", "na", "null", "none", "nil", "-", "--", "?", "unknown", "nan", "#n/a")


class MissingValueRule(BaseCleaningRule):
    type = "basic_missing_value_handling"
    title = "Missing values"
    description = ("Turns placeholder text (N/A, null, -) into real blanks and, only if you ask, "
                   "fills blanks with a constant you choose. Nothing is ever invented.")
    capability = "basic_missing_value_handling"
    operation = M.NORMALIZATION
    phase = 15
    params = {
        "tokens": ParamSpec("list_str", default=list(DEFAULT_MISSING_TOKENS),
                            description="Text values to treat as missing (case-insensitive)."),
        "fill_value": ParamSpec("str", default=None, nullable=True,
                                description="Optional constant to put in every blank cell."),
    }

    def apply(self, series, ctx):
        out = as_object(series).copy()
        tokens = {t.strip().lower() for t in self.p["tokens"]}
        m = str_mask(out)
        flagged = 0
        if m.any():
            is_token = out.loc[m].str.strip().str.lower().isin(tokens) | out.loc[m].str.strip().eq("")
            idx = is_token[is_token].index
            flagged = len(idx)
            out.loc[idx] = None
        filled = 0
        if self.p["fill_value"] is not None:
            blank = out.isna()
            filled = int(blank.sum())
            out.loc[blank] = self.p["fill_value"]
        return RuleResult(out, metrics={"placeholders_blanked": flagged, "blanks_filled": filled,
                                        "missing_after": int(out.isna().sum())})


# --------------------------------------------------------------------------- replacements

class CustomReplacementRule(BaseCleaningRule):
    type = "custom_replacements"
    title = "Custom replacements"
    description = "Literal find-and-replace pairs you define. No patterns or code: plain text only."
    capability = "custom_replacements"
    operation = M.CORRECTION
    phase = 20
    params = {
        "replacements": ParamSpec("records", required=True, fields={
            "from": ParamSpec("str", required=True),
            "to": ParamSpec("str", required=True),
            "match": ParamSpec("choice", default="exact", choices=("exact", "contains")),
            "case_sensitive": ParamSpec("bool", default=False),
        }, description="Ordered list of {from, to, match, case_sensitive}."),
    }

    def check_config(self):
        from cleaning.engine.base import RuleConfigError
        if not self.p["replacements"]:
            raise RuleConfigError("'replacements' needs at least one entry.", rule=self.type)
        for r in self.p["replacements"]:
            if r["from"] == "":
                raise RuleConfigError("A replacement's 'from' cannot be empty.", rule=self.type)

    def apply(self, series, ctx):
        out = as_object(series).copy()
        m = str_mask(out)
        counts = {}
        if m.any():
            for rep in self.p["replacements"]:
                src, dst, flags_cs = rep["from"], rep["to"], rep["case_sensitive"]
                cur = out.loc[m]
                if rep["match"] == "exact":
                    hit = cur.eq(src) if flags_cs else cur.str.lower().eq(src.lower())
                    n = int(hit.sum())
                    if n:
                        out.loc[hit[hit].index] = dst
                else:
                    pat = re.escape(src)
                    hit = cur.str.contains(pat, case=flags_cs, regex=True)
                    n = int(hit.sum())
                    if n:
                        sel = hit[hit].index
                        out.loc[sel] = out.loc[sel].str.replace(pat, lambda _m, d=dst: d, case=flags_cs, regex=True)
                counts[src] = n
        return RuleResult(out, metrics={"replacements_applied": counts})
