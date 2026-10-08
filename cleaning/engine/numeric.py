"""Numeric and currency rules. Malformed values are left untouched and flagged."""

from __future__ import annotations

import re

import pandas as pd

from cleaning import model as M
from cleaning.engine.base import (BaseCleaningRule, Flag, ParamSpec, RuleContext, RuleResult, as_object,
                                  blank_mask, map_unique, str_mask)

# symbol -> currencies it can legitimately mean. "$" is deliberately not tied to one country:
# it is accepted for USD only and flagged as a mismatch for anything else.
SYMBOL_CURRENCY = {
    "₦": "NGN", "₵": "GHS", "GH₵": "GHS", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR",
    "$": "USD", "US$": "USD", "KSh": "KES", "Ksh": "KES", "N": "NGN",
}
ISO_CODES = {"NGN", "GHS", "KES", "ZAR", "USD", "EUR", "GBP", "INR", "JPY", "CAD", "AUD", "XOF", "XAF",
             "EGP", "TZS", "UGX", "RWF", "MAD", "CNY", "AED", "SAR", "BRL"}

_SEPARATORS = {"comma": ",", "dot": ".", "space": " ", "none": ""}

# optional sign, optional currency token (symbol or ISO code) in front or behind
_TOKEN = r"(?:GH₵|US\$|KSh|Ksh|[₦₵€£¥₹$]|[A-Z]{3})"
_MONEY_RE = re.compile(rf"^\s*(?P<sign>[+-]?)\s*(?P<pre>{_TOKEN})?\s*(?P<sign2>[+-]?)\s*(?P<num>[\d][\d.,\s\u00a0\u202f]*)\s*(?P<post>{_TOKEN})?\s*$")


def _plain_number_regex(thousands: str, decimal: str) -> re.Pattern:
    t = re.escape(thousands) if thousands else ""
    d = re.escape(decimal)
    if thousands:
        return re.compile(rf"^(?:\d{{1,3}}(?:{t}\d{{3}})+|\d+)(?:{d}\d+)?$")
    return re.compile(rf"^\d+(?:{d}\d+)?$")


def _to_number(txt: str):
    f = float(txt)
    return int(f) if f.is_integer() and "." not in txt else f


class BasicNumericCleanupRule(BaseCleaningRule):
    type = "basic_numeric_cleanup"
    title = "Basic numeric cleanup"
    description = ("Strips spaces around numbers and removes comma thousands separators from values that are "
                   "unmistakably numbers (\"1,200\" -> 1200). Anything odd is left as it was.")
    capability = "basic_numeric_cleanup"
    phase = 30
    applies_to = ("integer", "decimal", "currency")
    _RE = re.compile(r"^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")

    def apply(self, series, ctx):
        out = as_object(series).copy()
        m = str_mask(out)
        flags, changed = [], 0
        if m.any():
            stripped = out.loc[m].str.strip()
            ok = stripped.str.match(self._RE)
            sel = ok[ok].index
            if len(sel):
                vals = stripped.loc[sel].str.replace(",", "", regex=False)
                out.loc[sel] = vals.map(_to_number)
                changed = len(sel)
        return RuleResult(out, metrics={"converted": changed})


class RemoveCurrencySymbolsRule(BaseCleaningRule):
    type = "remove_currency_symbols"
    title = "Remove currency symbols"
    description = ("Removes currency symbols and codes (₦, $, €, NGN...) from amounts stored as text. Does not "
                   "interpret or convert the amount: use Currency normalization for that.")
    capability = "remove_currency_symbols"
    phase = 25
    applies_to = ("currency", "text")

    def apply(self, series, ctx):
        out = as_object(series).copy()
        m = str_mask(out)
        n = 0
        if m.any():
            cur = out.loc[m]
            hit = cur.str.match(_MONEY_RE) & cur.str.contains(_TOKEN, regex=True)
            sel = hit[hit].index
            if len(sel):
                cleaned = cur.loc[sel].str.replace(_TOKEN, "", regex=True).str.strip()
                good = cleaned.str.match(r"^[+-]?\d[\d.,\s]*$")
                out.loc[good[good].index] = cleaned.loc[good]
                n = int(good.sum())
        return RuleResult(out, metrics={"symbols_removed": n})


class NormalizeCurrencyRule(BaseCleaningRule):
    type = "normalize_currency"
    title = "Currency / amount normalization"
    description = ("Converts amounts such as \"₦25,000\" or \"25 000\" into numbers, using the currency and "
                   "separators YOU configure. A symbol that contradicts the configured currency, a separator "
                   "that does not match, or a malformed number is flagged and left untouched.")
    capability = "normalize_currency"
    phase = 30
    applies_to = ("currency", "decimal", "integer")
    params = {
        "currency": ParamSpec("choice", required=True, choices=tuple(sorted(ISO_CODES)),
                              description="ISO code the column is denominated in."),
        "thousands_separator": ParamSpec("choice", default="comma", choices=tuple(_SEPARATORS),
                                         description="How thousands are grouped in this column."),
        "decimal_separator": ParamSpec("choice", default="dot", choices=("dot", "comma")),
    }

    def check_config(self):
        from cleaning.engine.base import RuleConfigError
        if _SEPARATORS[self.p["thousands_separator"]] == {"dot": ".", "comma": ","}[self.p["decimal_separator"]]:
            raise RuleConfigError("The thousands and decimal separators must differ.", rule=self.type)

    def apply(self, series, ctx):
        cur = self.p["currency"]
        thousands = _SEPARATORS[self.p["thousands_separator"]]
        decimal = {"dot": ".", "comma": ","}[self.p["decimal_separator"]]
        plain = _plain_number_regex(thousands, decimal)

        def decide(raw):
            if not isinstance(raw, str):
                return None            # already a number (or a date/bool): not text, not our business
            val, flag = self._parse(raw, cur, thousands, decimal, plain)
            if flag is not None:
                return ("flag", flag[0], flag[1], flag[2])
            return ("set", val) if val is not None else None

        out, flags, changed = map_unique(series, decide)
        return RuleResult(out, flags, metrics={"converted": changed, "currency": cur})

    @staticmethod
    def _parse(raw, cur, thousands, decimal, plain):
        txt = raw.replace("\u00a0", " ").replace("\u202f", " ").strip()
        if txt == "":
            return None, None
        mt = _MONEY_RE.match(txt)
        if not mt:
            return None, ("invalid_amount", "This is not a recognisable amount.", None)
        token = mt.group("pre") or mt.group("post")
        if mt.group("pre") and mt.group("post"):
            return None, ("invalid_amount", "Two currency markers found.", None)
        if token:
            implied = SYMBOL_CURRENCY.get(token) or (token if token in ISO_CODES else None)
            if implied is None:
                return None, ("unknown_currency", f"'{token}' is not a currency Omixa recognises.", None)
            if implied != cur:
                return None, ("currency_mismatch",
                              f"The value is marked {implied} but this column is configured as {cur}.", None)
        sign = (mt.group("sign") or "") + (mt.group("sign2") or "")
        if len(sign) > 1:
            return None, ("invalid_amount", "More than one sign.", None)
        num = mt.group("num").strip()
        if " " in num and thousands != " ":
            return None, ("separator_mismatch",
                          "Contains spaces but the column's thousands separator is not 'space'.", None)
        if not plain.match(num):
            return None, ("invalid_amount", "The digits do not match the configured separators.", None)
        norm = num.replace(thousands, "") if thousands else num
        if decimal != ".":
            norm = norm.replace(decimal, ".")
        return _to_number((sign + norm)), None
