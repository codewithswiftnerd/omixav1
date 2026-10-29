"""Countries: ISO 3166-1 reference data, conservative resolution."""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from cleaning.mapping.base import Mapper, load_reference
from cleaning.mapping.model import (AMBIGUOUS, REVIEW, SAFE, UNKNOWN, ColumnAnalysis, MappingRule, Resolution)
from cleaning.mapping.normalize import fold

# Codes that are also everyday words/placeholders. A lower-case "in", "is" or "it" is far more likely a word.
WORD_A2 = frozenset({"AM", "AS", "AT", "BE", "BY", "DO", "ID", "IN", "IS", "IT", "ME", "MY", "NA", "NO", "SO", "TO", "US"})
WORD_A3 = frozenset({"AND", "CAN", "COL", "COM", "DOM", "FIN", "GIN", "MAR", "PER", "SUR", "TON"})
# Never automatic, even in an obviously-country column: "NA" is the missing-value placeholder, "NO" is an answer.
ALWAYS_REVIEW = frozenset({"NA", "NO"})


@lru_cache(maxsize=1)
def _index() -> dict:
    ref = load_reference("countries.json")
    meta = ref["_meta"]
    rows = {c["alpha2"]: c for c in ref["countries"]}
    names, aliases, a2, a3 = {}, {}, {}, {}
    for code, c in rows.items():
        names[fold(c["name"])] = code
        names[fold(c["iso_name"])] = code
        a2[c["alpha2"].lower()] = code
        a3[c["alpha3"].lower()] = code
        for al in c["aliases"]:
            aliases.setdefault(fold(al), code)
    ambiguous = {fold(k): v for k, v in ref["ambiguous"].items()}
    return {"rows": rows, "names": names, "aliases": aliases, "a2": a2, "a3": a3, "ambiguous": ambiguous,
            "version": meta["version"], "source": meta["id"]}


def lookup(raw: Any):
    """(kind, alpha2) without context. kind: name | alias | alpha2 | alpha3 | None."""
    ix = _index()
    if not isinstance(raw, str):
        return None, None
    key = fold(raw)
    if not key:
        return None, None
    if key in ix["ambiguous"]:
        return "ambiguous", None
    if key in ix["names"]:
        return "name", ix["names"][key]
    if key in ix["aliases"]:
        return "alias", ix["aliases"][key]
    compact = key.replace(" ", "")
    if len(compact) == 2 and compact in ix["a2"] and " " not in key:
        return "alpha2", ix["a2"][compact]
    if len(compact) == 3 and compact in ix["a3"] and " " not in key:
        return "alpha3", ix["a3"][compact]
    return None, None


class CountryMapper(Mapper):
    category = "country"
    title = "Countries (ISO 3166-1)"
    output_formats = {"name": "Country name", "iso_name": "Official ISO short name",
                      "alpha2": "ISO alpha-2 code", "alpha3": "ISO alpha-3 code"}
    default_output = "name"

    @staticmethod
    def name_hint(column: str) -> bool:
        from cleaning import detectors
        return bool(detectors.is_country_column(str(column)))

    def analyze(self, counts, column, params):
        total = sum(counts.values())
        if not total:
            return ColumnAnalysis(self.category, False, "weak", 0.0, "The column is empty.")
        hit = 0
        for raw, n in counts.items():
            kind, code = lookup(raw)
            if kind in ("name", "alias"):
                hit += n
            elif kind in ("alpha2", "alpha3") and code and not self._collides(raw, code, kind):
                hit += n
        evidence = hit / total
        hint = self.name_hint(column)
        applicable = evidence >= 0.5 or (hint and evidence >= 0.4)
        if not applicable:
            return ColumnAnalysis(self.category, False, "weak", evidence,
                                  f"Only {evidence:.0%} of the values look like countries.")
        strong = evidence >= 0.8 or (hint and evidence >= 0.6)
        return ColumnAnalysis(self.category, True, "strong" if strong else "moderate", evidence,
                              f"{evidence:.0%} of the values are recognised countries" + (" and the column is named like a country." if hint else "."),
                              {"name_hint": hint})

    @staticmethod
    def _collides(raw: str, code: str, kind: str) -> bool:
        return (kind == "alpha2" and code in WORD_A2) or (kind == "alpha3" and raw.strip().upper() in WORD_A3)

    def resolve(self, raw, analysis, params):
        ix = _index()
        kind, code = lookup(raw)
        if kind == "ambiguous":
            info = ix["ambiguous"][fold(raw)]
            cands = info["candidates"]
            if len(cands) == 1:
                return Resolution(REVIEW, cands[0], self._rule(raw, "alias", cands[0], False), info["reason"], 0.5, cands)
            return Resolution(AMBIGUOUS, None, None, info["reason"], 0.0, cands)
        if kind is None:
            return Resolution(UNKNOWN, None, None, "Not recognised as a country name or ISO code. Left as typed.")
        if kind in ("name", "alias"):
            return Resolution(SAFE, code, self._rule(raw, kind, code, True), "Matches ISO 3166-1 reference data.",
                              1.0 if kind == "name" else 0.97)
        # codes
        s = raw.strip()
        if code in ALWAYS_REVIEW and kind == "alpha2":
            why = ("'NA' is also the usual missing-value placeholder." if code == "NA"
                   else "'No' is also an answer, not only Norway's code.")
            return Resolution(REVIEW, code, self._rule(raw, kind, code, False), why, 0.4, [code])
        if self._collides(s, code, kind):
            if s.isupper() and analysis.strength == "strong":
                return Resolution(SAFE, code, self._rule(raw, kind, code, True),
                                  "Upper-case ISO code in a column that is clearly countries.", 0.9)
            return Resolution(REVIEW, code, self._rule(raw, kind, code, False),
                              f"'{s}' is also an everyday word; confirm it is the country code.", 0.5, [code])
        return Resolution(SAFE, code, self._rule(raw, kind, code, True),
                          f"ISO 3166-1 {'alpha-2' if kind == 'alpha2' else 'alpha-3'} code.", 0.95)

    def _rule(self, raw, kind, code, auto) -> MappingRule:
        ix = _index()
        return MappingRule("country", str(raw), code, "fold", ix["source"], ix["version"],
                           1.0 if auto else 0.5, auto, ("country",), f"{ix['source']}:{kind}:{code}")

    def render(self, target, output_format):
        row = _index()["rows"][target]
        return {"name": row["name"], "iso_name": row["iso_name"], "alpha2": row["alpha2"],
                "alpha3": row["alpha3"]}[output_format]
