"""Categorical standardisation and duplicate detection."""

from __future__ import annotations

import difflib
import re
from collections import Counter

import pandas as pd

from cleaning import model as M
from cleaning.engine.base import (BaseCleaningRule, Flag, ParamSpec, RuleConfigError, RuleResult, as_object,
                                  blank_mask, str_mask)

_KEY_WS = re.compile(r"\s+")


def _key(v: str) -> str:
    return _KEY_WS.sub(" ", v.strip()).casefold()


class StandardizeCategoriesRule(BaseCleaningRule):
    type = "standardize_categories"
    title = "Standardize categories"
    description = ("Merges case/spacing variants (pending, Pending, PENDING -> Pending) and applies the mappings you "
                   "define. Probable typos (pendng) are only SUGGESTED, never applied automatically.")
    capability = "standardize_categories"
    operation = M.CORRECTION
    phase = 50
    applies_to = ("categorical", "text")
    fix_confidence = 0.9
    params = {
        "mappings": ParamSpec("map_str", default={}, description="Explicit {value: canonical} mappings (case-insensitive)."),
        "canonical": ParamSpec("list_str", default=[], description="The allowed spellings. Variants are merged into these."),
        "prefer": ParamSpec("choice", default="most_common", choices=("most_common", "title", "lower", "upper"),
                            description="When no canonical list covers a group, which spelling wins."),
        "suggest_typos": ParamSpec("bool", default=True),
        "typo_cutoff": ParamSpec("float", default=0.84, minimum=0.6, maximum=0.99,
                                 description="Similarity (0-1) required before a typo is suggested."),
        "max_distinct": ParamSpec("int", default=200, minimum=2, maximum=2000,
                                  description="Skip columns with more distinct values than this (not categorical)."),
    }

    def check_config(self):
        seen = {}
        for src, dst in self.p["mappings"].items():
            k = _key(src)
            if k in seen and seen[k] != dst:
                raise RuleConfigError(f"'{src}' is mapped to two different values.", rule=self.type)
            seen[k] = dst

    def apply(self, series, ctx):
        out = as_object(series).copy()
        m = str_mask(out) & ~blank_mask(out)
        vals = out[m]
        counts = Counter(vals)
        notes, flags = [], []
        if len(counts) > self.p["max_distinct"]:
            return RuleResult(out, metrics={"skipped": "too_many_distinct_values", "distinct": len(counts)},
                              notes=[f"Skipped: {len(counts)} distinct values is too many for a category column."])

        groups: dict[str, Counter] = {}
        for v, n in counts.items():
            groups.setdefault(_key(v), Counter())[v] += n
        canon_by_key = {_key(c): c for c in self.p["canonical"]}
        explicit = {_key(a): b for a, b in self.p["mappings"].items()}

        target: dict[str, str] = {}          # original text -> canonical text (applied)
        group_canon: dict[str, str] = {}
        for k, variants in groups.items():
            if k in explicit:
                chosen = explicit[k]
            elif k in canon_by_key:
                chosen = canon_by_key[k]
            else:
                chosen = self._pick(variants)
            group_canon[k] = chosen
            for v in variants:
                if v != chosen:
                    target[v] = chosen

        applied = 0
        if target:
            repl = vals.map(lambda v: target.get(v, v))
            diff = repl != vals
            if diff.any():
                out.loc[diff[diff].index] = repl[diff]
                applied = int(diff.sum())

        suggestions = 0
        if self.p["suggest_typos"]:
            anchors = {_key(c): c for c in self.p["canonical"]} or {
                k: group_canon[k] for k, vs in groups.items() if sum(vs.values()) >= 2 or k in explicit}
            anchor_keys = list(anchors)
            for k, variants in groups.items():
                if k in anchors or k in explicit:
                    continue
                best = difflib.get_close_matches(k, anchor_keys, n=1, cutoff=self.p["typo_cutoff"])
                if not best:
                    continue
                sug = anchors[best[0]]
                for v in variants:
                    for idx in vals.index[vals == v]:
                        flags.append(Flag(idx, v, "possible_typo",
                                          f"Looks like a misspelling of '{sug}'. Not changed automatically.", suggestion=sug))
                        suggestions += 1
        return RuleResult(out, flags, metrics={"merged_variants": applied, "typo_suggestions": suggestions,
                                               "distinct_before": len(counts),
                                               "distinct_after": int(pd.unique(out[m]).size)})

    def _pick(self, variants: Counter) -> str:
        p = self.p["prefer"]
        if p == "title":
            from cleaning.engine.text import title_case
            return title_case(max(variants, key=lambda v: (variants[v], v)))
        if p == "lower":
            return next(iter(variants)).strip().lower()
        if p == "upper":
            return next(iter(variants)).strip().upper()
        # most common; ties prefer mixed-case ("Pending") over ALL CAPS / all lower, then alphabetical
        def score(v):
            return (variants[v], not (v.isupper() or v.islower()), v.strip() == v, v)
        return max(variants, key=score).strip()


class DuplicateDetectionRule(BaseCleaningRule):
    type = "basic_duplicate_detection"
    title = "Duplicate detection"
    description = "Counts repeated values in this column (case/space-insensitive). Reports only; removes nothing."
    capability = "basic_duplicate_detection"
    operation = M.NONE
    mutates = False
    phase = 70

    def apply(self, series, ctx):
        out = as_object(series).copy()
        mask = ~blank_mask(out)
        keys = out[mask].map(lambda v: _key(v) if isinstance(v, str) else v)
        dup = keys.duplicated(keep=False)
        return RuleResult(out, [], metrics={
            "values_in_duplicate_groups": int(dup.sum()), "duplicate_groups": int(keys[dup].nunique()),
            "extra_copies": int(keys.duplicated(keep="first").sum()), "checked": int(mask.sum())})
