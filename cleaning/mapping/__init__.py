"""Reusable value standardisation and mapping engine.

    mapper (what a value means)  ->  engine (propose / preview / apply)  ->  executor (audit + review)

Reference data lives in ``reference/*.json`` and never inside the logic. A new category (phone formats,
units, ...) is a new ``Mapper`` subclass registered in ``engine.MAPPERS``; nothing else changes.
"""
from cleaning.mapping.engine import (MAPPERS, MappingPlan, build_plan, suggest_for_dataframe,  # noqa: F401
                                     validate_custom_mappings)
from cleaning.mapping.model import MappingRule, Resolution  # noqa: F401
