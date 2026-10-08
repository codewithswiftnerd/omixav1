"""
Country-aware phone rules.

Nothing here is Nigeria-specific: the per-country facts (dial code, trunk prefix, national length)
come from cleaning/phone_formats.COUNTRIES, and COUNTRY_PATTERNS adds an optional stricter
"valid national number" pattern per country. Adding Ghana/Kenya/... = adding a pattern row.
"""

from __future__ import annotations

import re

import pandas as pd

from cleaning import model as M
from cleaning.engine.base import (BaseCleaningRule, Flag, ParamSpec, RuleContext, RuleResult, as_object,
                                  map_unique)
from cleaning.phone_formats import COUNTRIES

# Optional per-country regex for the national significant number (digits after dial code / trunk
# prefix). Only the countries listed are validated beyond length; others use length alone.
COUNTRY_PATTERNS: dict[str, re.Pattern] = {
    "NG": re.compile(r"^(?:70\d|80\d|81\d|90\d|91\d)\d{7}$"),     # Nigerian mobile ranges
    "GH": re.compile(r"^[235]\d{8}$"),
    "KE": re.compile(r"^[17]\d{8}$"),
    "ZA": re.compile(r"^[6-8]\d{8}$"),
}

_FORMATTING = re.compile(r"[\s().\-/\u00a0]")
_DIAL_CODES = sorted({(c["dial_code"], iso) for iso, c in COUNTRIES.items()}, key=lambda t: -len(t[0]))
SUPPORTED_COUNTRIES = tuple(sorted(COUNTRIES))


def _other_country(digits: str, home: str):
    for dial, iso in _DIAL_CODES:
        if iso != home and digits.startswith(dial):
            c = COUNTRIES[iso]
            lo, hi = c["national_digits"]
            if lo <= len(digits) - len(dial) <= hi:
                return iso
    return None


def classify_phone(raw, country: str, assume_missing_trunk: bool = False):
    """-> (national_digits | None, reason_code | None, message).
    A value is only accepted when it carries evidence for the configured country."""
    c = COUNTRIES[country]
    dial, trunk = c["dial_code"], c["trunk_prefix"]
    lo, hi = c["national_digits"]
    if isinstance(raw, float):
        if raw != raw:
            return None, None, ""
        raw = str(int(raw)) if raw.is_integer() else str(raw)
    elif isinstance(raw, int) and not isinstance(raw, bool):
        raw = str(raw)
    elif not isinstance(raw, str):
        return None, "invalid_phone", "Not a text or number value."
    txt = raw.strip()
    if txt == "":
        return None, None, ""
    if re.search(r"[A-Za-z]", txt):
        return None, "invalid_phone", "Contains letters."
    explicit_intl = txt.startswith("+") or txt.startswith("00")
    body = _FORMATTING.sub("", txt)
    if body.startswith("+"):
        body = body[1:]
    elif body.startswith("00"):
        body = body[2:]
    if not body.isdigit():
        return None, "invalid_phone", "Contains characters that are not part of a phone number."
    ok_pattern = COUNTRY_PATTERNS.get(country)

    def finish(national: str):
        if not (lo <= len(national) <= hi):
            return None, "invalid_length", f"Has {len(national)} digits after the country/trunk prefix; {country} numbers have {lo}" + (f"-{hi}" if hi != lo else "") + "."
        if ok_pattern and not ok_pattern.match(national):
            return None, "invalid_phone", f"Not a recognised {c['name']} number range."
        return national, None, ""

    if body.startswith(dial) and lo <= len(body) - len(dial) <= hi:
        return finish(body[len(dial):])
    other = _other_country(body, country) if (explicit_intl or len(body) > hi + len(trunk or "")) else None
    if other:
        return None, "country_mismatch", f"Looks like a {COUNTRIES[other]['name']} (+{COUNTRIES[other]['dial_code']}) number, not {c['name']}."
    if explicit_intl:
        return None, "country_mismatch", f"International number without the +{dial} country code."
    if trunk and body.startswith(trunk) and lo <= len(body) - len(trunk) <= hi:
        return finish(body[len(trunk):])
    if assume_missing_trunk and lo <= len(body) <= hi and not (trunk and body.startswith(trunk)):
        return finish(body)
    if trunk and len(body) == lo and not body.startswith(trunk):
        return None, "invalid_phone", f"Missing the leading {trunk} (or +{dial}); not changed because that would be a guess."
    return None, "invalid_length" if body.isdigit() else "invalid_phone", "Does not match the length of a number for this country."


_OUTPUTS = ("international", "e164", "national")


def format_phone(national: str, country: str, output_format: str) -> str:
    c = COUNTRIES[country]
    if output_format == "national":
        return (c["trunk_prefix"] or "") + national
    return f"+{c['dial_code']}{national}"


class _PhoneBase(BaseCleaningRule):
    applies_to = ("phone",)

    def _run(self, series, ctx, *, rewrite: bool):
        country = self.p["country"]
        trunk = self.p.get("assume_missing_trunk_prefix", False)

        def decide(raw):
            if isinstance(raw, str) and raw.strip() == "":
                return None
            nat, reason, msg = classify_phone(raw, country, trunk)
            if reason:
                return ("flag", reason, msg, None)
            return ("set", format_phone(nat, country, self.p["output_format"])) if rewrite else ("valid",)

        out, flags, changed = map_unique(series, decide)
        checked = int((~out.isna() & ~out.map(lambda v: isinstance(v, str) and v.strip() == "")).sum())
        return RuleResult(out, flags, metrics={"valid": checked - len(flags), "invalid_or_flagged": len(flags),
                                               "checked": checked, "country": country})


class NormalizePhoneRule(_PhoneBase):
    type = "normalize_phone"
    title = "Phone normalization"
    description = ("Recognises local (08031234567), +234 and 234 forms of the same number for the configured "
                   "country and writes them in one format. Values that are not valid for that country are "
                   "flagged and left exactly as they were.")
    capability = "normalize_phone"
    phase = 30
    params = {
        "country": ParamSpec("choice", required=True, choices=SUPPORTED_COUNTRIES, description="ISO country code, e.g. NG."),
        "output_format": ParamSpec("choice", default="international", choices=_OUTPUTS,
                                   description="international/e164 = +2348031234567, national = 08031234567."),
        "assume_missing_trunk_prefix": ParamSpec("bool", default=False,
                                                 description="Treat an N-digit number without the leading 0 as a local number (common after Excel drops the 0)."),
    }

    def apply(self, series, ctx):
        return self._run(series, ctx, rewrite=True)


class ValidatePhoneRule(_PhoneBase):
    type = "validate_phone"
    title = "Phone validation"
    description = "Checks phone numbers for the configured country and flags invalid ones. Changes nothing."
    capability = "validate_phone"
    operation = M.NONE
    mutates = False
    phase = 60
    params = {
        "country": ParamSpec("choice", required=True, choices=SUPPORTED_COUNTRIES),
        "assume_missing_trunk_prefix": ParamSpec("bool", default=False),
    }

    def apply(self, series, ctx):
        return self._run(series, ctx, rewrite=False)
