"""`standardize_values`: the engine rule over the reusable mapping engine (cleaning/mapping/).

One rule, four categories (country, boolean, currency, category), one behaviour: apply what the reference
data and column context make safe, flag everything else (original kept, reason + suggestion attached) so the
existing review loop can accept / reject / edit it. A user's own mappings apply only to this run's file."""

from __future__ import annotations

from cleaning import model as M
from cleaning.engine.base import (BaseCleaningRule, ParamSpec, RuleConfigError, RuleResult, as_object)
from cleaning.audit import changed_cells_mask
from cleaning.mapping import MAPPERS, validate_custom_mappings
from cleaning.mapping.engine import apply_plan, build_plan


class StandardizeValuesRule(BaseCleaningRule):
    type = "standardize_values"
    title = "Standardize values (countries, yes/no, currencies, categories)"
    description = ("Maps spellings, aliases and codes to one consistent form using reference data (countries: ISO 3166-1; "
                   "currencies: ISO 4217 labels) and your own mappings. Uncertain or ambiguous values are never changed "
                   "automatically: they are proposed for your approval.")
    capability = "standardize_values"
    operation = M.NORMALIZATION
    phase = 48
    applies_to = ("any",)
    fix_confidence = 0.95
    params = {
        "category": ParamSpec("choice", required=True, choices=tuple(MAPPERS), description="What kind of values this column holds."),
        "output_format": ParamSpec("str", default=None, nullable=True,
                                   description="country: name | iso_name | alpha2 | alpha3; boolean: yes_no | true_false | one_zero | y_n; "
                                               "currency: code | name. Defaults to the category's usual form."),
        "mappings": ParamSpec("map_str", default={}, description="Your own {value: target} mappings. They apply to this file only."),
        "canonical": ParamSpec("list_str", default=[], description="Category labels only: the preferred spellings."),
        "confirm_numeric_flags": ParamSpec("bool", default=False,
                                           description="Boolean only: you confirm bare 0/1 values in this column are flags, not numbers."),
        "max_distinct": ParamSpec("int", default=200, minimum=2, maximum=2000),
    }

    def check_config(self):
        mapper = MAPPERS[self.p["category"]]
        fmt = self.p["output_format"]
        if fmt is not None and fmt not in mapper.output_formats:
            raise RuleConfigError(f"'output_format' for {self.p['category']} must be one of: {', '.join(mapper.output_formats)}.",
                                  rule=self.type)
        try:
            validate_custom_mappings(self.p["mappings"])
        except ValueError as exc:
            raise RuleConfigError(str(exc), rule=self.type)

    def apply(self, series, ctx):
        src = as_object(series)
        plan = build_plan(src, ctx.column, self.p["category"], output_format=self.p["output_format"],
                          mappings=self.p["mappings"], params={k: self.p[k] for k in ("canonical", "confirm_numeric_flags", "max_distinct")})
        res = apply_plan(src, plan)
        # The audit decides what counts as a change (a text "1" replacing the integer 1 is storage, not a change).
        # Reconcile to it so this rule's numbers can never disagree with the change ledger.
        mask = changed_cells_mask(src, res.values)
        prov = {i: p for i, p in res.provenance.items() if bool(mask.get(i, False))}
        stats = dict(res.stats)
        stats["storage_only"] = len(res.provenance) - len(prov)
        stats["applied"] = int(mask.sum())
        notes = []
        if not plan.analysis.applicable and not any(i.origin == "custom_file" for i in plan.items):
            notes.append(f"Skipped: {plan.analysis.reason}")
        metrics = {"category": plan.category, "output_format": plan.output_format, **stats,
                   "column_fit": {"applicable": plan.analysis.applicable, "strength": plan.analysis.strength,
                                  "evidence": round(plan.analysis.evidence, 2), "reason": plan.analysis.reason},
                   "summary": plan.summary(), "preview": plan.to_dict(limit=25)["items"]}
        return RuleResult(res.values, res.flags, metrics, notes, provenance=prov)
