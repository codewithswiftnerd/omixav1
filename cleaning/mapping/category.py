"""User-controlled categories. No built-in meaning: SMB == Small Business is the USER's statement.
Omixa only groups spellings that differ by case, spacing, hyphens, dots or punctuation, and proposes."""
from __future__ import annotations

from collections import Counter, defaultdict

from cleaning.mapping.base import Mapper
from cleaning.mapping.model import CANONICAL, REVIEW, SAFE, UNKNOWN, ColumnAnalysis, MappingRule, Resolution
from cleaning.mapping.normalize import fold, only_case_or_space_differs


def _pick(variants: Counter) -> str:
    # most common; ties prefer mixed-case ("Pending") over ALL CAPS / all lower, then the tidier spelling
    return max(variants, key=lambda v: (variants[v], not (v.isupper() or v.islower()), v.strip() == v, v)).strip()


class CategoryMapper(Mapper):
    category = "category"
    title = "Category labels"
    output_formats = {"as_written": "Use the target label"}
    default_output = "as_written"

    def analyze(self, counts, column, params):
        strs = {k: n for k, n in counts.items() if isinstance(k, str)}
        total = sum(counts.values())
        if not total or sum(strs.values()) / total < 0.9:
            return ColumnAnalysis(self.category, False, "weak", 0.0, "Not a text/category column.")
        limit = params.get("max_distinct", 200)
        if len(strs) > limit:
            return ColumnAnalysis(self.category, False, "weak", 0.0,
                                  f"{len(strs)} distinct values is too many for a category column.")
        anchors = {fold(a): a for a in (params.get("canonical") or [])}
        for src, dst in (params.get("mappings") or {}).items():
            anchors.setdefault(fold(dst), dst)
        groups = defaultdict(Counter)
        for v, n in strs.items():
            groups[fold(v)][v] += n
        plan = {}
        for key, variants in groups.items():
            chosen = anchors.get(key) or _pick(variants)
            for v in variants:
                if v == chosen:
                    plan[v] = (chosen, "same")
                elif only_case_or_space_differs(v, chosen):
                    plan[v] = (chosen, "case_space")
                else:
                    plan[v] = (chosen, "punctuation")
        return ColumnAnalysis(self.category, True, "moderate", 1.0, "Grouped spellings that differ only by case, spacing or punctuation.",
                              {"plan": plan, "distinct": len(strs)})

    def resolve(self, raw, analysis, params):
        if not isinstance(raw, str):
            return Resolution(UNKNOWN, None, None, "Not text. Left as typed.")
        chosen, kind = analysis.details["plan"].get(raw, (raw, "same"))
        if kind == "same":
            return Resolution(CANONICAL, chosen, None, "Already the preferred spelling.", 1.0, literal=chosen)
        rule = MappingRule("category", raw, chosen, "fold", "omixa-category-grouping", "1",
                           0.95 if kind == "case_space" else 0.7, kind == "case_space", (), f"omixa-category:{kind}")
        if kind == "case_space":
            return Resolution(SAFE, chosen, rule, "Differs only by capitalisation or spacing.", 0.95, literal=chosen)
        return Resolution(REVIEW, chosen, rule, "Differs by punctuation or hyphenation; confirm they mean the same thing.",
                          0.7, [chosen], literal=chosen)

    def render(self, target, output_format):
        return target
