"""
Audit trail and reversibility for every transformation.

`record_step` compares a dataframe before and after ONE cleaning step and writes:

  * a public audit entry per affected column (what changed, why, which rule, how many
    cells/rows, before/after examples, confidence, automatic vs user-approved), and
  * a private ledger holding the original value of every changed cell, removed row,
    dropped column and renamed header, which is what makes `revert` possible.

Doing the comparison here, generically, instead of asking each of the rule functions to
count their own changes means a rule can never report a number that disagrees with
what actually happened to the data.

Row identity: rows keep their ORIGINAL index label for the whole run (nothing resets the
index until export), so "row 17" in the audit is row 17 of the uploaded file.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd

from cleaning import model as M

EXAMPLES_PER_ENTRY = 5
EXAMPLE_MAX_CHARS = 80
# Upper bound on retained original values per run. Beyond this the audit counts stay
# exact but the ledger stops growing and the run is reported as only partly reversible.
LEDGER_MAX_CELLS = 200_000
# Public per-cell change records returned to the client (the totals are always exact).
CELL_LOG_MAX = 5_000
REMOVED_ROWS_LOG_MAX = 1_000


def _scalar(v: Any) -> Any:
    """JSON-safe, comparable representation of a cell."""
    if v is None or v is pd.NA:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    if hasattr(v, "item"):  # numpy scalar
        try:
            return v.item()
        except Exception:
            pass
    return v


def _short(v: Any) -> Any:
    v = _scalar(v)
    if isinstance(v, str) and len(v) > EXAMPLE_MAX_CHARS:
        return v[:EXAMPLE_MAX_CHARS] + "…"
    return v


def _is_str(v) -> bool:
    return isinstance(v, str)


def _is_bool(v) -> bool:
    return isinstance(v, (bool,)) or type(v).__name__ in ("bool_", "bool")


def changed_cells_mask(before: pd.Series, after: pd.Series) -> pd.Series:
    """True where a cell's *value* changed.

    The comparison is deliberately exact for text: "2348061234567" -> "+2348061234567" IS a
    change even though both parse as the same number (an earlier numeric-tolerance shortcut
    silently missed exactly this). The only things NOT counted are pure storage changes where
    the displayed value is the same number: the text "43" becoming the integer 43, or 43.0
    becoming 43. A text value that becomes a boolean always counts."""
    b_null, a_null = before.isna(), after.isna()
    both_null = b_null & a_null
    one_null = b_null ^ a_null

    b_obj = before.astype(object)
    a_obj = after.astype(object)
    b_str, a_str = b_obj.map(_is_str), a_obj.map(_is_str)
    b_bool, a_bool = b_obj.map(_is_bool), a_obj.map(_is_bool)

    # text vs text: exact comparison
    both_str = b_str & a_str
    str_changed = both_str & (b_obj.where(both_str) != a_obj.where(both_str))

    # bool involved: unchanged only if both are the same bool
    any_bool = b_bool | a_bool
    bool_changed = any_bool & ~(b_bool & a_bool & (b_obj == a_obj))

    # everything else (number vs number, text-number vs number): compare as numbers when both
    # sides are numeric, else as text
    rest = ~(both_str | any_bool) & ~(b_null | a_null)
    b_num = pd.to_numeric(b_obj.where(rest), errors="coerce").astype("float64")
    a_num = pd.to_numeric(a_obj.where(rest), errors="coerce").astype("float64")
    both_numeric = rest & b_num.notna() & a_num.notna()
    num_changed = both_numeric & ((b_num - a_num).abs() > 1e-12)
    text_changed = rest & ~both_numeric & (b_obj.astype("string").fillna("\x00") != a_obj.astype("string").fillna("\x00"))

    changed = one_null | (~both_null & ~(b_null | a_null) & (str_changed | bool_changed | num_changed | text_changed))
    return changed.fillna(False).astype(bool)


@dataclass
class AuditLog:
    entries: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)  # private: restore data per step
    ledger_cells: int = 0
    ledger_truncated: bool = False
    _counter: int = 0
    # Public, per-cell and per-row change records (original AND new value). Capped so a huge run cannot
    # exhaust memory; the totals below stay exact even when the lists are truncated.
    cell_log: list = field(default_factory=list)
    removed_rows_log: list = field(default_factory=list)
    cell_changes_total: int = 0
    removed_rows_total: int = 0
    # Exact, per original column: how many cells BECAME missing / STOPPED being missing, and which rule did it.
    # A rise in a column's missing count after cleaning is explained here, never silent.
    missing_flow: dict = field(default_factory=dict)

    def _next_id(self) -> str:
        self._counter += 1
        return f"chg-{self._counter:04d}"

    # ---------------------------------------------------------------- recording
    def record_step(
        self,
        step_name: str,
        before: pd.DataFrame,
        after: pd.DataFrame,
        *,
        rule_id: Optional[str],
        operation: str,
        confidence: Optional[float],
        approval: str,
        reason: str,
        reversibility: str = M.REVERSIBLE,
        issue: Optional[str] = None,
        column_labels: Optional[dict] = None,
    ) -> list[dict]:
        """`column_labels` maps current column names -> the names the user saw in the
        original file, so entries refer to the headers they recognise even after a
        later step renames them."""
        step = {"name": step_name, "renamed": {}, "dropped_columns": [], "removed_rows": None, "cells": {}}
        new_entries: list[dict] = []
        labels = column_labels or {}

        def label(col):
            return labels.get(col, col)

        common = [c for c in before.columns if c in after.columns]

        # --- header renames (positional: rules never reorder columns) ---
        if list(before.columns) != list(after.columns) and len(before.columns) == len(after.columns):
            renamed = {str(b): str(a) for b, a in zip(before.columns, after.columns) if b != a}
            if renamed:
                step["renamed"] = renamed
                common = [c for c in before.columns if c == c and c in after.columns and c not in renamed]
                new_entries.append(self._entry(
                    step_name, rule_id, operation=M.NORMALIZATION, column=None, reason=reason, issue=issue,
                    confidence=confidence, approval=approval, reversibility=reversibility,
                    renamed_columns=renamed, cells_changed=0, rows_affected=0,
                    examples=[{"before": b, "after": a} for b, a in list(renamed.items())[:EXAMPLES_PER_ENTRY]],
                ))

        # --- dropped columns ---
        dropped = [c for c in before.columns if c not in after.columns and c not in step["renamed"]]
        for c in dropped:
            pos = list(before.columns).index(c)
            vals = {int(i) if isinstance(i, (int,)) or hasattr(i, "item") else i: _scalar(v) for i, v in before[c].items()} \
                if len(before) * 1 <= LEDGER_MAX_CELLS - self.ledger_cells else None
            if vals is None:
                self.ledger_truncated = True
            else:
                self.ledger_cells += len(vals)
            step["dropped_columns"].append({"column": c, "position": pos, "values": vals})
            new_entries.append(self._entry(
                step_name, rule_id, operation=M.DELETION, column=label(c), reason=reason, issue=issue,
                confidence=confidence, approval=approval,
                reversibility=reversibility if vals is not None else M.IRREVERSIBLE,
                cells_changed=int(before[c].notna().sum()), rows_affected=int(before[c].notna().sum()),
                examples=[{"before": f"column '{label(c)}' ({int(before[c].notna().sum())} values)", "after": "removed"}],
            ))

        # --- removed rows ---
        removed_idx = before.index.difference(after.index)
        if len(removed_idx):
            rows = before.loc[removed_idx]
            keep = len(rows) * max(1, len(rows.columns)) <= LEDGER_MAX_CELLS - self.ledger_cells
            if keep:
                self.ledger_cells += len(rows) * max(1, len(rows.columns))
                step["removed_rows"] = {
                    "index": [_idx(i) for i in rows.index],
                    "data": {str(c): [_scalar(v) for v in rows[c]] for c in rows.columns},
                    "columns": [str(c) for c in rows.columns],
                }
            else:
                self.ledger_truncated = True
            ex = []
            for i in list(removed_idx)[:EXAMPLES_PER_ENTRY]:
                ex.append({"row": _idx(i), "before": {str(c): _short(rows.at[i, c]) for c in list(rows.columns)[:4]}, "after": "row removed"})
            self.removed_rows_total += int(len(removed_idx))
            for i in list(removed_idx)[:max(0, REMOVED_ROWS_LOG_MAX - len(self.removed_rows_log))]:
                self.removed_rows_log.append({
                    "row": _idx(i), "rule": step_name, "rule_id": rule_id, "reason": reason, "approval": approval,
                    "values": {str(label(c)): _scalar(rows.at[i, c]) for c in rows.columns}})
            new_entries.append(self._entry(
                step_name, rule_id, operation=M.DELETION, column=None, reason=reason, issue=issue,
                confidence=confidence, approval=approval,
                reversibility=reversibility if keep else M.IRREVERSIBLE,
                cells_changed=0, rows_affected=int(len(removed_idx)), rows_removed=int(len(removed_idx)), examples=ex,
            ))

        # --- cell-level changes on surviving rows ---
        surviving = before.index.intersection(after.index)
        if len(surviving):
            for c in common:
                b, a = before.loc[surviving, c], after.loc[surviving, c]
                mask = changed_cells_mask(b, a)
                n = int(mask.sum())
                if not n:
                    continue
                idxs = list(b.index[mask])
                if n <= LEDGER_MAX_CELLS - self.ledger_cells:
                    self.ledger_cells += n
                    step["cells"][c] = {_idx(i): _scalar(b.at[i]) for i in idxs}
                else:
                    self.ledger_truncated = True
                ex = [{"row": _idx(i), "before": _short(b.at[i]), "after": _short(a.at[i])} for i in idxs[:EXAMPLES_PER_ENTRY]]
                entry = self._entry(
                    step_name, rule_id, operation=operation, column=label(c), reason=reason, issue=issue,
                    confidence=confidence, approval=approval,
                    reversibility=reversibility if c in step["cells"] else M.PARTIALLY_REVERSIBLE,
                    cells_changed=n, rows_affected=n, examples=ex,
                )
                new_entries.append(entry)
                to_missing = int((a[mask].isna() & b[mask].notna()).sum())
                from_missing = int((a[mask].notna() & b[mask].isna()).sum())
                if to_missing or from_missing:
                    flow = self.missing_flow.setdefault(label(c), {"converted_to_missing": 0, "filled_from_missing": 0, "by_rule": {}})
                    flow["converted_to_missing"] += to_missing
                    flow["filled_from_missing"] += from_missing
                    r = flow["by_rule"].setdefault(step_name, {"to_missing": 0, "from_missing": 0, "reason": reason})
                    r["to_missing"] += to_missing
                    r["from_missing"] += from_missing
                self.cell_changes_total += n
                room = max(0, CELL_LOG_MAX - len(self.cell_log))
                for i in idxs[:room]:
                    self.cell_log.append({
                        "row": _idx(i), "column": label(c), "original": _scalar(b.at[i]), "new": _scalar(a.at[i]),
                        "rule": step_name, "rule_id": rule_id, "operation": operation, "reason": reason,
                        "approval": approval, "change_id": entry["id"]})

        if new_entries:
            self.steps.append(step)
            self.entries.extend(new_entries)
        return new_entries

    def _entry(self, step_name, rule_id, *, operation, column, reason, issue, confidence, approval,
               reversibility, cells_changed, rows_affected, examples, **extra) -> dict:
        e = {
            "id": self._next_id(),
            "rule": step_name,
            "rule_id": rule_id,
            "issue": issue,
            "column": column,
            "operation": operation,
            "reason": reason,
            "cells_changed": cells_changed,
            "rows_affected": rows_affected,
            "confidence": None if confidence is None else round(float(confidence), 2),
            "approval": approval,
            "reversibility": reversibility,
            "examples": examples,
        }
        e.update(extra)
        return e

    # ---------------------------------------------------------------- reporting
    def public_entries(self) -> list[dict]:
        return list(self.entries)

    def change_log(self) -> dict:
        """Every recorded change as {row, column, original, new, rule, reason, approval}. Capped lists,
        exact totals: `truncated` says whether the lists hold fewer rows than actually changed."""
        return {
            "cell_changes": list(self.cell_log), "cell_changes_total": self.cell_changes_total,
            "removed_rows": list(self.removed_rows_log), "removed_rows_total": self.removed_rows_total,
            "truncated": self.cell_changes_total > len(self.cell_log) or self.removed_rows_total > len(self.removed_rows_log),
        }

    def summary(self) -> dict:
        by_op: dict[str, int] = {}
        for e in self.entries:
            n = e.get("cells_changed", 0) + e.get("rows_removed", 0)
            by_op[e["operation"]] = by_op.get(e["operation"], 0) + n
        if not self.entries:
            rev = M.NOT_APPLICABLE
        elif self.ledger_truncated or any(e["reversibility"] in (M.IRREVERSIBLE, M.PARTIALLY_REVERSIBLE) for e in self.entries):
            rev = M.PARTIALLY_REVERSIBLE
        else:
            rev = M.REVERSIBLE
        return {
            "total_changes": len(self.entries),
            "cells_changed": sum(e.get("cells_changed", 0) for e in self.entries),
            "rows_removed": sum(e.get("rows_removed", 0) for e in self.entries),
            "columns_removed": sum(1 for e in self.entries if e["operation"] == M.DELETION and e.get("column") and not e.get("rows_removed") and "removed" in str(e["examples"])),
            "by_operation": by_op,
            "automatic_changes": sum(1 for e in self.entries if e["approval"] == M.APPROVAL_AUTOMATIC),
            "user_approved_changes": sum(1 for e in self.entries if e["approval"] != M.APPROVAL_AUTOMATIC),
            "reversibility": rev,
            "ledger_cells_retained": self.ledger_cells,
            "ledger_truncated": self.ledger_truncated,
        }


def _idx(i):
    return i.item() if hasattr(i, "item") else i


# ------------------------------------------------------------------ reverting
def revert(df: pd.DataFrame, log: AuditLog, *, steps_back: Optional[int] = None) -> pd.DataFrame:
    """
    Undo recorded steps, newest first, and return the restored frame.

    Restores changed cells, removed rows, dropped columns and renamed headers from the
    ledger. Raises ValueError if the ledger was truncated, because a silent partial
    restore would look like a full one.
    """
    if log.ledger_truncated:
        raise ValueError("The change ledger was truncated for size; this run cannot be fully reverted.")
    out = df.copy()
    todo = log.steps if steps_back is None else log.steps[-steps_back:]
    for step in reversed(todo):
        # cells first (columns still carry post-step names)
        for col, cells in step["cells"].items():
            if col not in out.columns:
                continue
            out[col] = out[col].astype(object)
            for i, v in cells.items():
                out.at[i, col] = pd.NA if v is None else v
        # renames back
        if step["renamed"]:
            out = out.rename(columns={new: old for old, new in step["renamed"].items()})
        # dropped columns
        for d in sorted(step["dropped_columns"], key=lambda x: x["position"]):
            if d["values"] is None:
                raise ValueError(f"Column {d['column']!r} was dropped without a ledger; cannot restore.")
            ser = pd.Series({k: (pd.NA if v is None else v) for k, v in d["values"].items()}, dtype=object)
            out.insert(min(d["position"], len(out.columns)), d["column"], ser.reindex(out.index))
        # removed rows
        rr = step["removed_rows"]
        if rr:
            restored = pd.DataFrame({c: [pd.NA if v is None else v for v in rr["data"][c]] for c in rr["columns"]},
                                    index=rr["index"])
            out = pd.concat([out.astype(object), restored.astype(object)]).sort_index()
            out = out[[c for c in rr["columns"] if c in out.columns] + [c for c in out.columns if c not in rr["columns"]]]
    return out
