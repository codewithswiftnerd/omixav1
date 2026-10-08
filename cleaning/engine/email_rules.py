"""Email validation: flags, never rewrites."""

from __future__ import annotations

import re

import pandas as pd

from cleaning import model as M
from cleaning.engine.base import BaseCleaningRule, Flag, RuleResult, as_object, blank_mask

_LOCAL_OK = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+$")
_DOMAIN_OK = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24}$")
_PLACEHOLDER_LOCALS = {"test", "none", "na", "n/a", "null", "noemail", "no-email", "nobody", "abc", "xxx", "asdf", "email"}
_PLACEHOLDER_DOMAINS = {"example.com", "example.org", "test.com", "email.com", "domain.com", "none.com", "na.com"}


def classify_email(v):
    """-> (status, message): status in valid | malformed | suspicious"""
    if not isinstance(v, str):
        return "malformed", "Not text."
    t = v.strip()
    if t.count("@") != 1:
        return "malformed", "An email must contain exactly one @."
    local, domain = t.split("@")
    if not local or not domain:
        return "malformed", "Missing the part before or after @."
    if len(t) > 254 or len(local) > 64:
        return "malformed", "Too long to be a real address."
    if not _LOCAL_OK.match(local) or local.startswith(".") or local.endswith(".") or ".." in local:
        return "malformed", "The part before @ contains invalid characters or dots."
    if not _DOMAIN_OK.match(domain):
        return "malformed", "The domain is not valid (needs a name and an ending such as .com)."
    if local.lower() in _PLACEHOLDER_LOCALS or domain.lower() in _PLACEHOLDER_DOMAINS:
        return "suspicious", "Looks like a placeholder rather than a real address."
    if t != v:
        return "valid", ""
    return "valid", ""


class ValidateEmailRule(BaseCleaningRule):
    type = "validate_email"
    title = "Email validation"
    description = ("Classifies each address as valid, malformed, missing or suspicious. Malformed and suspicious "
                   "values are flagged for review; nothing is modified. (Combine with Text case = lower to "
                   "lowercase addresses.)")
    capability = "validate_email"
    operation = M.NONE
    mutates = False
    phase = 60
    applies_to = ("email", "text")

    def apply(self, series, ctx):
        out = as_object(series).copy()
        blank = blank_mask(out)
        flags, counts = [], {"valid": 0, "malformed": 0, "suspicious": 0, "missing": int(blank.sum())}
        vals = out[~blank]
        cache = {v: classify_email(v) for v in pd.unique(vals)}
        for idx, v in vals.items():
            status, msg = cache[v]
            counts[status] += 1
            if status != "valid":
                flags.append(Flag(idx, v, f"{status}_email", msg))
        return RuleResult(out, flags, metrics=counts)
