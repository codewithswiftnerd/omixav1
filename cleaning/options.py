"""Request-level cleaning options (additive: omitting `options` behaves exactly as before).

Every option here extends a capability the Free plan already has (basic numeric cleanup, basic missing-value
handling, report-only duplicate detection, the /api/validate rule config), so none of them unlocks a Pro rule;
Pro rules and features still go through cleaning/engine/access.authorize. Options are data only, validated
against a fixed whitelist with hard size limits. Unknown keys are rejected, never ignored silently.
"""

from __future__ import annotations

from typing import Any, Optional

NUMBER_FORMATS = ("auto", "thousands_comma", "decimal_comma")
MAX_MARKERS, MAX_MARKER_LEN, MAX_KEYS = 50, 40, 10
_ALLOWED = {"number_format", "number_words", "missing_markers", "duplicate_keys", "validation"}


class OptionsError(ValueError):
    """Message is safe to show the user."""


def parse_options(raw: Any) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise OptionsError("'options' must be an object")
    unknown = sorted(set(raw) - _ALLOWED)
    if unknown:
        raise OptionsError(f"Unknown option(s): {', '.join(map(str, unknown))}")
    out: dict = {}
    if "number_format" in raw:
        if raw["number_format"] not in NUMBER_FORMATS:
            raise OptionsError(f"'number_format' must be one of: {', '.join(NUMBER_FORMATS)}")
        out["number_format"] = raw["number_format"]
    if "number_words" in raw:
        if not isinstance(raw["number_words"], bool):
            raise OptionsError("'number_words' must be true or false")
        out["number_words"] = raw["number_words"]
    if "missing_markers" in raw:
        m = raw["missing_markers"]
        if not (isinstance(m, list) and len(m) <= MAX_MARKERS
                and all(isinstance(x, str) and 0 < len(x.strip()) <= MAX_MARKER_LEN for x in m)):
            raise OptionsError(f"'missing_markers' must be a list of up to {MAX_MARKERS} short, non-empty strings")
        out["missing_markers"] = sorted({x.strip().lower() for x in m})
    if "duplicate_keys" in raw:
        k = raw["duplicate_keys"]
        if not (isinstance(k, list) and 0 < len(k) <= MAX_KEYS and all(isinstance(x, str) and x for x in k)):
            raise OptionsError(f"'duplicate_keys' must be a list of 1-{MAX_KEYS} column names")
        out["duplicate_keys"] = list(k)
    if "validation" in raw:
        v = raw["validation"]
        if not isinstance(v, dict):
            raise OptionsError("'validation' must be an object: {column_types, rules, defaults}")
        from cleaning.validation import normalize_config
        out["validation"] = v          # normalised (and unknown keys dropped) where it is used
        normalize_config(v)            # raises nothing; proves it parses
    return out


def validation_config(options: Optional[dict]) -> Optional[dict]:
    return (options or {}).get("validation")
