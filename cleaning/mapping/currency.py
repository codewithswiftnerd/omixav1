"""Currency IDENTIFICATION for label columns. Never extracts or converts amounts, never converts rates."""
from __future__ import annotations

import re

from functools import lru_cache

from cleaning.mapping.base import Mapper, load_reference
from cleaning.mapping.model import (AMBIGUOUS, NOT_APPLICABLE, REVIEW, SAFE, UNKNOWN, ColumnAnalysis, MappingRule,
                                    Resolution)
from cleaning.mapping.normalize import fold

CODE_WORDS = frozenset({"TRY"})           # a lower-case "try" is a word
_HAS_DIGIT = re.compile(r"\d")


@lru_cache(maxsize=1)
def _index() -> dict:
    ref = load_reference("currencies.json")
    rows = {c["code"]: c for c in ref["currencies"]}
    names, symbols = {}, {}
    for code, c in rows.items():
        names[fold(c["name"])] = code
        for a in c["aliases"]:
            names[fold(a)] = code
        for s in c["symbols"]:
            symbols[s] = code
    amb = {}
    for k, v in ref["ambiguous"].items():
        amb[k] = v                              # exact (symbols) ...
        amb.setdefault(k.lower(), v)            # ... and lower-cased (words)
    return {"rows": rows, "names": names, "symbols": symbols, "amb": amb,
            "version": ref["_meta"]["version"], "source": ref["_meta"]["id"]}


def _lookup(raw: str):
    ix = _index()
    t = raw.strip()
    if not t:
        return None, None
    if _HAS_DIGIT.search(t):
        return "amount", None
    if t in ix["symbols"]:
        return "symbol", ix["symbols"][t]
    if t in ix["amb"] or t.lower() in ix["amb"]:
        return "ambiguous", ix["amb"].get(t) or ix["amb"][t.lower()]
    if t.upper() in ix["rows"] and len(t) == 3 and t.isalpha():
        return "code", t.upper()
    k = fold(t)
    if k in ix["amb"]:
        return "ambiguous", ix["amb"][k]
    if k in ix["names"]:
        return "name", ix["names"][k]
    return None, None


class CurrencyMapper(Mapper):
    category = "currency"
    title = "Currency labels (identification only)"
    output_formats = {"code": "ISO 4217 code", "name": "Currency name"}
    default_output = "code"

    @staticmethod
    def name_hint(column: str) -> bool:
        from cleaning import profiling
        return bool({"currency", "ccy", "curr"} & set(profiling.name_tokens(str(column))))

    def analyze(self, counts, column, params):
        total = sum(counts.values())
        if not total:
            return ColumnAnalysis(self.category, False, "weak", 0.0, "The column is empty.")
        hit = amounts = 0
        for raw, n in counts.items():
            kind, _ = _lookup(raw) if isinstance(raw, str) else (None, None)
            if kind in ("symbol", "code", "name", "ambiguous"):
                hit += n
            elif kind == "amount":
                amounts += n
        evidence = hit / total
        hint = self.name_hint(column)
        if amounts / total > 0.3:
            return ColumnAnalysis(self.category, False, "weak", evidence,
                                  "This column holds amounts, not currency labels. Omixa identifies currencies only in label columns "
                                  "and never extracts or converts amounts here.")
        ok = evidence >= 0.8 or (hint and evidence >= 0.5)
        if not ok:
            return ColumnAnalysis(self.category, False, "weak", evidence, f"Only {evidence:.0%} of the values look like currency labels.")
        codes = {_lookup(r)[1] for r in counts if isinstance(r, str) and _lookup(r)[0] in ("symbol", "code", "name")}
        return ColumnAnalysis(self.category, True, "strong" if hint else "moderate", evidence,
                              "Values read as currency labels.", {"currencies_found": sorted(c for c in codes if c),
                                                                  "mixed_currencies": len({c for c in codes if c}) > 1})

    def resolve(self, raw, analysis, params):
        ix = _index()
        if not isinstance(raw, str):
            return Resolution(UNKNOWN, None, None, "Not text. Left as typed.")
        kind, val = _lookup(raw)
        if kind == "amount":
            return Resolution(NOT_APPLICABLE, None, None, "Contains a number: an amount, not a currency label. Never changed here.")
        if kind == "ambiguous":
            return Resolution(AMBIGUOUS, None, None, val["reason"], 0.0, val["candidates"])
        if kind is None:
            return Resolution(UNKNOWN, None, None, "Not a currency Omixa can identify with certainty. Left as typed.")
        rule = MappingRule("currency", raw, val, "fold", ix["source"], ix["version"], 0.98, True, ("currency",),
                           f"{ix['source']}:{kind}:{val}")
        if kind == "code" and raw.strip() != raw.strip().upper() and raw.strip().upper() in CODE_WORDS:
            return Resolution(REVIEW, val, MappingRule("currency", raw, val, "fold", ix["source"], ix["version"], 0.5, False,
                                                       ("currency",), rule.rule_id), f"'{raw.strip()}' is also an everyday word.", 0.5, [val])
        return Resolution(SAFE, val, rule, "Matches ISO 4217 reference data.", 0.98)

    def render(self, target, output_format):
        row = _index()["rows"][target]
        return row["code"] if output_format == "code" else row["name"]
