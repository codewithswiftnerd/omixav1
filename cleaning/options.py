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
DATE_ORDERS = ("auto", "day_first", "month_first")
MISSING_ACTIONS = ("leave", "fill", "flag", "remove_rows")
MAX_COLUMNS, MAX_MAPPINGS, MAX_VALUE_LEN = 100, 200, 100
<<<<<<< HEAD
_ALLOWED = {"number_format", "number_words", "missing_markers", "not_missing", "duplicate_keys", "validation",
=======
DUPLICATE_REMOVALS = ("report", "keep_first", "keep_last")
MAX_TOLERANCE = 0.05
_ALLOWED = {"number_format", "number_words", "missing_markers", "not_missing", "duplicate_keys", "duplicate_removal",
            "consistency", "validation",
>>>>>>> 302fe70 (changes)
            "date_order", "date_orders", "phone_country", "missing_values", "value_mappings", "value_actions"}


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
    if "not_missing" in raw:
        # Words the user says are REAL values in their data ("None", "N/A", "Unknown"): never blanked, never counted
        # as missing. Wins over both the built-in markers and 'missing_markers'.
        m = raw["not_missing"]
        if not (isinstance(m, list) and len(m) <= MAX_MARKERS
                and all(isinstance(x, str) and 0 < len(x.strip()) <= MAX_MARKER_LEN for x in m)):
            raise OptionsError(f"'not_missing' must be a list of up to {MAX_MARKERS} short, non-empty strings")
        out["not_missing"] = sorted({x.strip().lower() for x in m})
        if "missing_markers" in out:
            out["missing_markers"] = [x for x in out["missing_markers"] if x not in out["not_missing"]]
    if "duplicate_keys" in raw:
        k = raw["duplicate_keys"]
        if not (isinstance(k, list) and 0 < len(k) <= MAX_KEYS and all(isinstance(x, str) and x for x in k)):
            raise OptionsError(f"'duplicate_keys' must be a list of 1-{MAX_KEYS} column names")
        out["duplicate_keys"] = list(k)
    if "duplicate_removal" in raw:
        # "report" (the default) only lists rows that share the key; the other two REMOVE them, so they need the key.
        if raw["duplicate_removal"] not in DUPLICATE_REMOVALS:
            raise OptionsError(f"'duplicate_removal' must be one of: {', '.join(DUPLICATE_REMOVALS)}")
        if raw["duplicate_removal"] != "report":
            if not out.get("duplicate_keys"):
                raise OptionsError("'duplicate_removal' needs 'duplicate_keys': say which columns identify a duplicate")
            out["duplicate_removal"] = raw["duplicate_removal"]
    if "consistency" in raw:
        c = raw["consistency"]
        if not isinstance(c, dict) or set(c) - {"enabled", "tolerance", "ignore_columns"}:
            raise OptionsError("'consistency' must be an object with any of: enabled, tolerance, ignore_columns")
        cc = {}
        if "enabled" in c:
            if not isinstance(c["enabled"], bool):
                raise OptionsError("'consistency.enabled' must be true or false")
            cc["enabled"] = c["enabled"]
        if "tolerance" in c:
            t = c["tolerance"]
            if isinstance(t, bool) or not isinstance(t, (int, float)) or not (0 <= t <= MAX_TOLERANCE):
                raise OptionsError(f"'consistency.tolerance' must be a number from 0 to {MAX_TOLERANCE} "
                                   "(0.005 = values may differ by 0.5% before they count as a mismatch)")
            cc["tolerance"] = float(t)
        if "ignore_columns" in c:
            ic = c["ignore_columns"]
            if not (isinstance(ic, list) and len(ic) <= MAX_COLUMNS and all(isinstance(x, str) and x for x in ic)):
                raise OptionsError(f"'consistency.ignore_columns' must be a list of up to {MAX_COLUMNS} column names")
            cc["ignore_columns"] = list(dict.fromkeys(ic))
        out["consistency"] = cc
    if "date_order" in raw:
        if raw["date_order"] not in DATE_ORDERS:
            raise OptionsError(f"'date_order' must be one of: {', '.join(DATE_ORDERS)}")
        out["date_order"] = raw["date_order"]
    if "date_orders" in raw:
        out["date_orders"] = _column_map(raw["date_orders"], "date_orders",
                                         lambda v, c: _choice(v, DATE_ORDERS, f"'date_orders' for '{c}'"))
    if "phone_country" in raw:
        out["phone_country"] = _phone_country(raw["phone_country"])
    if "missing_values" in raw:
        out["missing_values"] = _missing_values(raw["missing_values"])
    if "value_mappings" in raw:
        out["value_mappings"] = _value_mappings(raw["value_mappings"])
    if "value_actions" in raw:
        out["value_actions"] = _value_actions(raw["value_actions"])
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


# --------------------------------------------------------------------------- helpers for the structured options
def _choice(v, choices, what):
    if v not in choices:
        raise OptionsError(f"{what} must be one of: {', '.join(choices)}")
    return v


def _column_map(raw, name, check):
    if not isinstance(raw, dict) or len(raw) > MAX_COLUMNS:
        raise OptionsError(f"'{name}' must be an object keyed by column name (up to {MAX_COLUMNS} columns)")
    out = {}
    for col, v in raw.items():
        if not isinstance(col, str) or not col:
            raise OptionsError(f"'{name}' has an invalid column name")
        out[col] = check(v, col)
    return out


def _phone_country(raw):
    """\"NG\" (applies to every phone column) or {column: \"NG\"}. Never assumed: no option, no country."""
    from cleaning.phone_formats import COUNTRIES

    def one(v, _c=None):
        from cleaning.phone_formats import split_country_spec
        code, local = split_country_spec(v)
        if code is None:
            raise OptionsError("'phone_country' must be a supported ISO country code such as NG, "
                               "or NG:local to keep numbers in local format (0803...)")
        return code + (":local" if local else "")
    if isinstance(raw, dict):
        return _column_map(raw, "phone_country", one)
    return one(raw)


def _missing_rule(v, where):
    if not isinstance(v, dict) or set(v) - {"action", "value"}:
        raise OptionsError(f"{where} must be {{action, value?}}")
    action = _choice(v.get("action"), MISSING_ACTIONS, f"{where} action")
    out = {"action": action}
    if action == "fill":
        val = v.get("value")
        if isinstance(val, bool) or not isinstance(val, (str, int, float)) or (isinstance(val, str) and not val.strip()):
            raise OptionsError(f"{where}: 'fill' needs a non-empty 'value' you choose")
        if isinstance(val, str) and len(val) > MAX_VALUE_LEN:
            raise OptionsError(f"{where}: 'value' is too long")
        out["value"] = val
    elif "value" in v:
        raise OptionsError(f"{where}: 'value' only applies to action 'fill'")
    return out


def _missing_values(raw):
    """{default?: {action, value?}, columns?: {column: {action, value?}}}. Nothing here is on by default:
    without this option empty cells are left as they are (and counted as missing in the report)."""
    if not isinstance(raw, dict) or set(raw) - {"default", "columns"}:
        raise OptionsError("'missing_values' must be {default?, columns?}")
    out = {}
    if "default" in raw:
        out["default"] = _missing_rule(raw["default"], "'missing_values.default'")
    if "columns" in raw:
        out["columns"] = _column_map(raw["columns"], "missing_values.columns",
                                     lambda v, c: _missing_rule(v, f"'missing_values' for '{c}'"))
    return out


def _value_mappings(raw):
    """{column: {approve: {from: to}, reject: [from, ...]}}: mappings the USER approved or rejected."""
    def one(v, col):
        if not isinstance(v, dict) or set(v) - {"approve", "reject"}:
            raise OptionsError(f"'value_mappings' for '{col}' must be {{approve?, reject?}}")
        ap, rj = v.get("approve") or {}, v.get("reject") or []
        if not isinstance(ap, dict) or len(ap) > MAX_MAPPINGS or not all(
                isinstance(a, str) and a.strip() and isinstance(b, str) and len(a) <= MAX_VALUE_LEN
                and len(b) <= MAX_VALUE_LEN for a, b in ap.items()):
            raise OptionsError(f"'approve' for '{col}' must map text to text (up to {MAX_MAPPINGS} pairs)")
        if not isinstance(rj, list) or len(rj) > MAX_MAPPINGS or not all(isinstance(a, str) for a in rj):
            raise OptionsError(f"'reject' for '{col}' must be a list of text values")
        return {"approve": dict(ap), "reject": list(rj)}
    return _column_map(raw, "value_mappings", one)


def _value_actions(raw):
    """{column: {clear?: [value, ...], remove_rows?: [value, ...]}}: what the USER decided to do with specific flagged
    values from the review pop-up. "Keep" is simply the absence of an entry. Values are matched as text (ignoring
    case and extra spaces) in that column only."""
    def one(v, col):
        if not isinstance(v, dict) or set(v) - {"clear", "remove_rows"}:
            raise OptionsError(f"'value_actions' for '{col}' must be {{clear?, remove_rows?}}")
        out = {}
        for k in ("clear", "remove_rows"):
            vals = v.get(k) or []
            if not isinstance(vals, list) or len(vals) > MAX_MAPPINGS or not all(
                    isinstance(a, str) and a.strip() and len(a) <= MAX_VALUE_LEN for a in vals):
                raise OptionsError(f"'{k}' for '{col}' must be a list of up to {MAX_MAPPINGS} short, non-empty text values")
            out[k] = list(vals)
        both = {a.strip().casefold() for a in out["clear"]} & {a.strip().casefold() for a in out["remove_rows"]}
        if both:
            raise OptionsError(f"'{col}': a value cannot be both cleared and have its rows removed")
        return out
    return _column_map(raw, "value_actions", one)
