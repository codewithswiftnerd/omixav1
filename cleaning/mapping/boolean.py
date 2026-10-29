"""Booleans: words are strong evidence; bare 0/1 is only evidence when the column corroborates it."""
from __future__ import annotations

from typing import Any

from cleaning.mapping.base import Mapper
from cleaning.mapping.model import REVIEW, SAFE, UNKNOWN, ColumnAnalysis, MappingRule, Resolution
from cleaning.mapping.normalize import fold

TRUE_WORDS = frozenset({"yes", "y", "true", "t"})
FALSE_WORDS = frozenset({"no", "n", "false", "f"})
_RENDER = {"yes_no": ("Yes", "No"), "true_false": ("True", "False"), "one_zero": ("1", "0"), "y_n": ("Y", "N")}


def token(raw: Any):
    """(tier, bool) or (None, None). tier: 'word' (yes/true/...) or 'numeric' (1/0)."""
    if isinstance(raw, bool) or type(raw).__name__ == "bool_":
        return "word", bool(raw)
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        if raw == 1:
            return "numeric", True
        if raw == 0:
            return "numeric", False
        return None, None
    if hasattr(raw, "item") and not isinstance(raw, str):       # numpy scalars
        try:
            return token(raw.item())
        except Exception:
            return None, None
    if not isinstance(raw, str):
        return None, None
    k = fold(raw)
    if k in TRUE_WORDS:
        return "word", True
    if k in FALSE_WORDS:
        return "word", False
    if k == "1":
        return "numeric", True
    if k == "0":
        return "numeric", False
    return None, None


class BooleanMapper(Mapper):
    category = "boolean"
    title = "Yes / No flags"
    output_formats = {"yes_no": "Yes / No", "true_false": "True / False", "one_zero": "1 / 0", "y_n": "Y / N"}
    default_output = "yes_no"

    @staticmethod
    def name_hint(column: str) -> bool:
        from cleaning import detectors
        return bool(detectors.is_boolean_flag_column(str(column)))

    def analyze(self, counts, column, params):
        total = sum(counts.values())
        if not total:
            return ColumnAnalysis(self.category, False, "weak", 0.0, "The column is empty.")
        words = numeric = 0
        for raw, n in counts.items():
            tier, _ = token(raw)
            words += n if tier == "word" else 0
            numeric += n if tier == "numeric" else 0
        evidence = (words + numeric) / total
        hint = self.name_hint(column)
        details = {"word_cells": words, "numeric_cells": numeric, "other_cells": total - words - numeric,
                   "name_hint": hint, "numeric_only": words == 0 and numeric > 0}
        ok = evidence >= 0.9 or (hint and evidence >= 0.6)
        if not ok:
            return ColumnAnalysis(self.category, False, "weak", evidence,
                                  f"Only {evidence:.0%} of the values are yes/no-style flags; this is not treated as a Boolean column.",
                                  details)
        strong = words > 0 and evidence >= 0.9
        return ColumnAnalysis(self.category, True, "strong" if strong else "moderate", evidence,
                              "Values read as yes/no flags." if words else "Only 0/1 values; they may be numbers rather than flags.",
                              details)

    def resolve(self, raw, analysis, params):
        tier, val = token(raw)
        if tier is None:
            return Resolution(UNKNOWN, None, None, "Not a recognised yes/no value. Left as typed.")
        key = "true" if val else "false"
        if tier == "word":
            return Resolution(SAFE, key, self._rule(raw, key, True), "Recognised yes/no/true/false spelling.", 0.98)
        # bare 1 / 0
        if analysis.details.get("word_cells"):
            return Resolution(SAFE, key, self._rule(raw, key, True),
                              "0/1 alongside yes/no words in the same column.", 0.9)
        if params.get("confirm_numeric_flags") is True:
            return Resolution(SAFE, key, self._rule(raw, key, True), "You confirmed 0/1 are flags in this column.", 0.9)
        return Resolution(REVIEW, key, self._rule(raw, key, False),
                          "Only 0/1 values: they could be counts or codes, not flags. Confirm to convert.", 0.5, [key])

    def _rule(self, raw, key, auto):
        return MappingRule("boolean", str(raw), key, "fold", "omixa-boolean", "1", 0.95 if auto else 0.5, auto,
                           ("boolean",), f"omixa-boolean:{key}")

    def render(self, target, output_format):
        t, f = _RENDER[output_format]
        return t if target == "true" else f
