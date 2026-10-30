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


def _initials(label: str) -> str:
    return "".join(w[0] for w in label.split() if w)


def _is_subsequence(short: str, long: str) -> bool:
    it = iter(long)
    return all(ch in it for ch in short)


def _abbreviation_of(short_key: str, long_key: str) -> bool:
    """True when `short_key` plausibly abbreviates `long_key` (both already folded). Deliberately strict, because
    the result is only ever a proposal for the user to approve: an initialism of a multi-word label (hr / human
    resources) or a shortened single word that keeps its first and last letter and drops at least three
    (ops / operations, mgmt / management). Never prefixes of near-equal length, so aba / abuja is not proposed."""
    if short_key == long_key or " " in short_key or len(short_key) < 2:
        return False
    if " " in long_key and _initials(long_key) == short_key:
        return True
    return (len(long_key) - len(short_key) >= 3 and len(short_key) >= 3 and " " not in long_key
            and short_key[0] == long_key[0] and short_key[-1] == long_key[-1]
            and _is_subsequence(short_key, long_key))


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
        # Abbreviations (HR / Human Resources, Ops / Operations): proposed for approval, never applied silently.
        # Only when exactly one other label in this column fits, so an ambiguous short form is left alone.
        chosen_by_key = {key: anchors.get(key) or _pick(variants) for key, variants in groups.items()}
        for key in groups:
            if key in anchors:
                continue
            fits = [m for m in groups if m != key and _abbreviation_of(key, m)]
            if len(fits) == 1:
                for v in groups[key]:
                    plan[v] = (chosen_by_key[fits[0]], "abbreviation")
        return ColumnAnalysis(self.category, True, "moderate", 1.0, "Grouped spellings that differ only by case, spacing or punctuation.",
                              {"plan": plan, "distinct": len(strs)})

    def resolve(self, raw, analysis, params):
        if not isinstance(raw, str):
            return Resolution(UNKNOWN, None, None, "Not text. Left as typed.")
        chosen, kind = analysis.details["plan"].get(raw, (raw, "same"))
        if kind == "same":
            return Resolution(CANONICAL, chosen, None, "Already the preferred spelling.", 1.0, literal=chosen)
        if kind == "abbreviation":
            rule = MappingRule("category", raw, chosen, "abbreviation", "omixa-category-grouping", "1",
                               0.6, False, (), "omixa-category:abbreviation")
            return Resolution(REVIEW, chosen, rule, f"Looks like a short form of \"{chosen}\" in this column; confirm they mean the same thing.",
                              0.6, [chosen], literal=chosen)
        rule = MappingRule("category", raw, chosen, "fold", "omixa-category-grouping", "1",
                           0.95 if kind == "case_space" else 0.7, kind == "case_space", (), f"omixa-category:{kind}")
        if kind == "case_space":
            return Resolution(SAFE, chosen, rule, "Differs only by capitalisation or spacing.", 0.95, literal=chosen)
        return Resolution(REVIEW, chosen, rule, "Differs by punctuation or hyphenation; confirm they mean the same thing.",
                          0.7, [chosen], literal=chosen)

    def render(self, target, output_format):
        return target
