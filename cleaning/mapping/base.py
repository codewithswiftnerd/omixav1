"""Mapper interface. A mapper answers two questions: *is this column of my kind?* (analyze) and
*what does this value mean?* (resolve). Turning answers into changes is the engine's job."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from cleaning.mapping.model import ColumnAnalysis, Resolution

REFERENCE_DIR = Path(__file__).parent / "reference"


@lru_cache(maxsize=None)
def load_reference(name: str) -> dict:
    with open(REFERENCE_DIR / name, encoding="utf-8") as fh:
        return json.load(fh)


class Mapper:
    category: str = ""
    title: str = ""
    output_formats: dict = {}          # key -> human label
    default_output: str = ""
    name_hint = staticmethod(lambda column: False)

    def analyze(self, counts: dict, column: str, params: dict) -> ColumnAnalysis:  # pragma: no cover
        raise NotImplementedError

    def resolve(self, raw: Any, analysis: ColumnAnalysis, params: dict) -> Resolution:  # pragma: no cover
        raise NotImplementedError

    def render(self, target: str, output_format: str) -> str:  # pragma: no cover
        raise NotImplementedError

    def describe(self) -> dict:
        return {"category": self.category, "title": self.title, "output_formats": dict(self.output_formats),
                "default_output": self.default_output}
