"""Text folding used to compare values with reference data. Folding is ONLY for lookup: the value the
user sees (and the audit's `original`) is always the text as uploaded."""
from __future__ import annotations

import re
import unicodedata

_DROP = re.compile(r"[.'’`´]")          # U.S.A. -> USA, d'Ivoire -> divoire
_SEP = re.compile(r"[^a-z0-9]+")


def fold(text) -> str:
    """Case, diacritics, dots/apostrophes, '&', hyphens/underscores/slashes and spacing all collapse."""
    s = unicodedata.normalize("NFKD", str(text))
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    s = s.replace("&", " and ")
    s = _DROP.sub("", s)
    return _SEP.sub(" ", s).strip()


def only_case_or_space_differs(a: str, b: str) -> bool:
    return re.sub(r"\s+", " ", a.strip()).casefold() == re.sub(r"\s+", " ", b.strip()).casefold()
