"""Cross-column consistency: do related columns agree with each other?  READ-ONLY.

Nothing in this module edits, fills or removes a value. It only *notices* that columns which behave as if
they were related (quantity x unit price = total, subtotal + tax = total, start date <= end date,
minimum <= maximum) disagree on some rows, and reports exactly which rows, with the values and the value the
relationship expected. Which of the columns is the wrong one is never decided: any of them could be.

How relationships are found (no column names are hard-coded):

  product   C = A x B       found by testing every pair of numeric columns against every other column
  sum       C = A + B (+ D) found the same way for two and three addends
  date      A <= B          two date columns where one is (almost) never after the other
  range     min <= max      two numeric columns whose NAMES say minimum/maximum (or low/high) over the same subject

A relationship is only believed when the data supports it: it must hold on most of the rows where all of its
columns are filled in, on enough rows to not be a coincidence, and not trivially (a column that merely copies
another, a factor that is always 1, or zeros everywhere). Column names never create a relationship on their own;
they only lower the amount of evidence needed for the date and range orderings, whose names are explicit
("order date" / "ship date", "min temp" / "max temp"). Datasets that contain no such relationship produce no
findings at all, and a relationship that holds on every row produces none either.

Limits worth knowing: a relationship that is broken on most rows is not detected (there is no evidence it exists);
text that is not clearly a number is skipped; ambiguous dates (03/04/2024) are never used as evidence in either
direction; at most MAX_NUMERIC_COLUMNS numeric columns are searched (the report says when that cap was hit).
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from cleaning import currencies, detectors, profiling

# ---- evidence thresholds ------------------------------------------------------------------------------
MAX_NUMERIC_COLUMNS = 40          # columns searched for arithmetic relationships (3-term sums use the first 12)
MAX_SUM3_COLUMNS = 12
DISCOVERY_ROWS = 2000             # rows sampled to FIND a relationship; every row is then checked against it
MIN_COMPARABLE = 4                # rows where every column of the relationship is filled in
MIN_HOLD_RATIO = 0.60             # share of those rows on which it must hold ...
MIN_HOLD_RATIO_SUM3 = 0.75        # ... higher for 3-term sums, where many more combinations are searched
MIN_HOLDING_ROWS = 3              # absolute number of rows that agree
MIN_MARGIN_OVER_CHANCE = 0.40     # ... and beat coincidence: hold ratio minus how often it "holds" on a misaligned column
MIN_NONTRIVIAL_ROWS = 2           # holding rows that are not a copy / x1 / +0
REL_TOL, ABS_TOL = 0.005, 0.01    # 0.5% plus one cent: money rounded to the cent is not a contradiction
SEARCH_BUDGET_SECONDS = 8.0       # hard stop for the arithmetic search: a worker is never held by one wide file
MAX_EXAMPLES = 10
MAX_RELATIONS = 12
MAX_OBJECT_PARSE_FAILURES = 50_000

DATE_ORDER_MIN_COMPARABLE_NAMED, DATE_ORDER_RATIO_NAMED = 4, 0.75
DATE_ORDER_MIN_COMPARABLE_BLIND, DATE_ORDER_RATIO_BLIND = 10, 0.95

_PERCENT_TOKENS = {"pct", "percent", "percentage", "rate"}

# Words that say "this date normally comes first" / "this date normally comes later".
_EARLY = {"start", "starts", "started", "begin", "begins", "began", "from", "open", "opened", "opening", "created",
          "create", "order", "ordered", "admit", "admitted", "admission", "hire", "hired", "issue", "issued",
          "checkin", "arrival", "arrive", "arrived", "enrol", "enroll", "enrolled", "enrolment", "enrollment",
          "register", "registered", "registration", "signup", "invoice", "invoiced", "booked", "booking", "purchase", "purchased",
          "birth", "dob", "born", "effective", "published", "release", "released"}
_LATE = {"end", "ends", "ended", "finish", "finished", "to", "close", "closed", "closing", "ship", "shipped",
         "shipping", "shipment", "deliver", "delivered", "delivery", "discharge", "discharged",
         "term", "terminated", "termination", "exit", "resign", "resigned", "expiry", "expire", "expires", "expired",
         "expiration", "due", "checkout", "updated", "modified", "completed", "complete", "completion", "resolved",
         "resolution", "departure", "depart", "departed", "return", "returned", "refund", "refunded", "death",
         "died", "cancel", "cancelled", "canceled", "graduation", "graduated", "renewal", "renewed", "paid",
         "payment", "settled"}
_BIRTH = {"birth", "dob", "born", "birthday"}
_MIN_TOKENS = {"min", "minimum", "low", "lower", "lowest", "floor", "least"}
_MAX_TOKENS = {"max", "maximum", "high", "upper", "highest", "ceiling", "cap", "most"}


@dataclass
class Relation:
    kind: str                         # product | sum | date_order | range_order
    target: str                       # the column that is "the result" (product/sum) or the later/larger column
    terms: list                       # the other column(s)
    comparable: int
    holding: int
    scale: float = 1.0                # product only: 0.01 when one factor is a percentage written as 12.5 for 12.5%
    violations: list = field(default_factory=list)   # [{label, record, values, expected, difference}]
    violation_count: int = 0
    summary: str = ""
    named: bool = False

    @property
    def ratio(self) -> float:
        return self.holding / self.comparable if self.comparable else 0.0


# =============================================================================================== parsing
def _label_record(label, position: int) -> int:
    """1-based number of the data row (header not counted), from the row label when it is the original 0-based
    row number, otherwise from the position."""
    try:
        if isinstance(label, (int, np.integer)) and not isinstance(label, bool):
            return int(label) + 1
    except Exception:
        pass
    return position + 1


def _to_float_series(series: pd.Series) -> Optional[pd.Series]:
    """Numbers from a numeric column, or from a text column that is clearly numbers written with formatting
    ("1,200.50", "$45", "(30)"). None when the column is not numeric, mixes currencies, or is too messy."""
    if pd.api.types.is_bool_dtype(series):
        return None
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_numeric(series, errors="coerce").astype(float)
    if pd.api.types.is_datetime64_any_dtype(series):
        return None
    non_null = series.dropna()
    if non_null.empty:
        return None
    text = non_null.astype(str)
    real = text[~text.str.strip().str.lower().isin(detectors.missing_tokens())]
    if real.empty:
        return None
    fast = pd.to_numeric(real, errors="coerce")
    bad = real[fast.isna()]
    if len(bad) > MAX_OBJECT_PARSE_FAILURES:
        return None
    out = fast.astype(float)
    if len(bad):
        markers = {currencies.currency_marker(v) for v in real.head(2000) if isinstance(v, str)} - {None}
        if len(markers) > 1:
            return None                      # a column that mixes currencies is never added up
        for idx, v in bad.items():
            status, num, _ = detectors.normalize_numeric_cell(v)
            if status == "converted" and num is not None:
                out.at[idx] = float(num)
    if out.notna().sum() < 0.8 * len(real):
        return None                          # mostly words: not a number column
    return out.reindex(series.index)


def _to_datetime_series(name: str, series: pd.Series) -> Optional[pd.Series]:
    """Dates that can be read in exactly ONE way. Ambiguous or impossible cells come back as NaT, so they are
    never evidence for or against a relationship."""
    if pd.api.types.is_datetime64_any_dtype(series):
        return pd.to_datetime(series, errors="coerce")
    if pd.api.types.is_numeric_dtype(series) or pd.api.types.is_bool_dtype(series):
        return None
    try:
        if not detectors.is_probable_date_column(str(name), series):
            return None
        parsed, _stats = detectors.unambiguous_date_cells(series)
    except Exception:
        return None
    if parsed is None:
        return None
    parsed = pd.to_datetime(parsed, errors="coerce")
    if parsed.notna().sum() < max(MIN_COMPARABLE, 0.5 * series.notna().sum()):
        return None
    return parsed


def _tokens(name) -> set:
    return set(profiling.name_tokens(str(name)))


# ============================================================================================ arithmetic
def _numeric_candidates(df: pd.DataFrame, profile: dict) -> tuple[dict, list]:
    """{column: float Series} for columns that can sensibly take part in arithmetic, plus a note per skipped
    reason. Identifiers, phone numbers, yes/no flags, constants and dates never do."""
    cols, notes = {}, []
    for col in df.columns:
        key = str(col)
        prof = profile.get(key)
        if prof is not None and (prof.semantic_type in (profiling.IDENTIFIER, profiling.PHONE, profiling.BOOLEAN,
                                                        profiling.DATE, profiling.DATETIME)
                                 or prof.is_identifier):
            continue
        if detectors.has_date_name(key) and not pd.api.types.is_numeric_dtype(df[col]):
            continue
        vals = _to_float_series(df[col])
        if vals is None:
            continue
        v = vals.dropna()
        if len(v) < MIN_COMPARABLE or v.nunique() <= 2 or not np.isfinite(v.to_numpy()).all():
            continue
        cols[key] = vals
    return cols, notes


def _tol(expected: np.ndarray) -> np.ndarray:
    return ABS_TOL + REL_TOL * np.abs(expected)


def _chance_rate(expected: np.ndarray, target: np.ndarray) -> float:
    """How often the relationship 'holds' when the target column is deliberately misaligned (rolled by a few rows).
    Columns of small whole numbers agree by luck surprisingly often (2 x 3 = 6 beside a column of sixes); a real
    relationship holds on the aligned rows and almost never on the shifted ones."""
    n = len(target)
    worst = 0.0
    for shift in {1, max(2, n // 3), max(3, (2 * n) // 3)}:
        if shift % n == 0:
            continue
        t = np.roll(target, shift)
        valid = ~np.isnan(expected) & ~np.isnan(t) & ~((expected == 0) & (t == 0))
        if valid.sum() < MIN_COMPARABLE:
            continue
        worst = max(worst, float((valid & (np.abs(expected - t) <= _tol(t))).sum() / valid.sum()))
    return worst


def _percent_hint(col_name: str) -> bool:
    return bool(_tokens(col_name) & _PERCENT_TOKENS) or "%" in str(col_name)


def _sample_positions(n: int) -> np.ndarray:
    if n <= DISCOVERY_ROWS:
        return np.arange(n)
    return np.linspace(0, n - 1, DISCOVERY_ROWS).astype(int)


def _find_arithmetic(df: pd.DataFrame, profile: dict) -> tuple[list, dict]:
    cols, _ = _numeric_candidates(df, profile)
    names = list(cols)
    info = {"numeric_columns_considered": len(names), "numeric_columns_searched": 0, "search_capped": False,
            "search_timed_out": False}
    deadline = time.monotonic() + SEARCH_BUDGET_SECONDS
    if len(names) < 3:
        return [], info
    if len(names) > MAX_NUMERIC_COLUMNS:
        # Keep the columns most likely to take part in a calculation: money / quantity words first, then order.
        def priority(c):
            t = _tokens(c)
            return 0 if (t & detectors.MONEY_NAME_TOKENS or t & {"qty", "quantity", "units", "count", "rate", "hours",
                                                                   "price", "tax", "vat", "discount"}) else 1
        names = sorted(names, key=lambda c: (priority(c), list(cols).index(c)))[:MAX_NUMERIC_COLUMNS]
        info["search_capped"] = True
    info["numeric_columns_searched"] = len(names)

    full = np.column_stack([cols[c].to_numpy(dtype=float) for c in names])      # rows x n, NaN = empty
    pos = _sample_positions(len(full))
    X = full[pos]
    n = len(names)
    found: list[Relation] = []

    def accept(kind, ti, term_idx, scale, hold, comparable, nontrivial):
        return comparable >= MIN_COMPARABLE and nontrivial >= MIN_NONTRIVIAL_ROWS and hold >= MIN_HOLDING_ROWS and \
               hold / comparable >= MIN_HOLD_RATIO

    # ---- products: A x B (x 0.01 when a factor is a percentage) = C
    for i, j in itertools.combinations(range(n), 2):
        if time.monotonic() > deadline:
            info["search_timed_out"] = True
            break
        scales = [1.0]
        if _percent_hint(names[i]) or _percent_hint(names[j]):
            scales.append(0.01)
        for scale in scales:
            P = X[:, i] * X[:, j] * scale
            for k in range(n):
                if k in (i, j):
                    continue
                valid = ~np.isnan(P) & ~np.isnan(X[:, k]) & ~((P == 0) & (X[:, k] == 0))
                comparable = int(valid.sum())
                if comparable < MIN_COMPARABLE:
                    continue
                holds = valid & (np.abs(P - X[:, k]) <= _tol(X[:, k]))
                hold = int(holds.sum())
                if hold / comparable < MIN_HOLD_RATIO:
                    continue
                # not a trivial relation: both factors must be something other than 0 / 1 on enough holding rows
                a, b = X[:, i], X[:, j]
                nontrivial = int((holds & ~np.isclose(a, 1) & ~np.isclose(b, 1) & (a != 0) & (b != 0)).sum()) \
                    if scale == 1.0 else int((holds & (a != 0) & (b != 0)).sum())
                if accept("product", k, (i, j), scale, hold, comparable, nontrivial) and \
                        hold / comparable - _chance_rate(P, X[:, k]) >= MIN_MARGIN_OVER_CHANCE:
                    found.append(Relation("product", names[k], [names[i], names[j]], comparable, hold, scale=scale))

    # ---- sums: A + B = C and A + B + D = C
    def sums(term_count, limit):
        m = min(n, limit)
        for combo in itertools.combinations(range(m), term_count):
            if time.monotonic() > deadline:
                info["search_timed_out"] = True
                return
            S = X[:, list(combo)].sum(axis=1)
            any_nan = np.isnan(X[:, list(combo)]).any(axis=1)
            S = np.where(any_nan, np.nan, S)
            for k in range(n):
                if k in combo:
                    continue
                valid = ~np.isnan(S) & ~np.isnan(X[:, k]) & ~((S == 0) & (X[:, k] == 0))
                comparable = int(valid.sum())
                if comparable < MIN_COMPARABLE:
                    continue
                holds = valid & (np.abs(S - X[:, k]) <= _tol(X[:, k]))
                hold = int(holds.sum())
                need = MIN_HOLD_RATIO_SUM3 if term_count == 3 else MIN_HOLD_RATIO
                if hold < MIN_HOLDING_ROWS or hold / comparable < need:
                    continue
                # trivial when it only holds because every other addend is 0 (the total just copies one column)
                nonzero_terms = (np.abs(X[:, list(combo)]) > 0).sum(axis=1)
                nontrivial = int((holds & (nonzero_terms >= 2)).sum())
                if nontrivial >= MIN_NONTRIVIAL_ROWS and \
                        hold / comparable - _chance_rate(S, X[:, k]) >= MIN_MARGIN_OVER_CHANCE:
                    found.append(Relation("sum", names[k], [names[c] for c in combo], comparable, hold))

    sums(2, n)
    if n >= 4:
        sums(3, MAX_SUM3_COLUMNS)

    # ---- one explanation per result column: strongest evidence, products before sums when equal
    best: dict = {}
    for r in found:
        rank = (r.ratio, r.holding, 1 if r.kind == "product" else 0, -len(r.terms))
        cur = best.get(r.target)
        if cur is None or rank > cur[0]:
            best[r.target] = (rank, r)
    chosen = [r for _, r in sorted(best.values(), key=lambda t: t[0], reverse=True)]

    # a column cannot both explain another and be explained by it in a circle: keep the stronger one
    kept, used_as_target = [], set()
    for r in chosen:
        if r.target in {t for k in kept for t in k.terms} and any(t in used_as_target for t in r.terms):
            continue
        kept.append(r)
        used_as_target.add(r.target)
    kept = kept[:MAX_RELATIONS]

    # ---- evaluate each accepted relation on EVERY row, not just the sample
    index = df.index
    for r in kept:
        _evaluate_arithmetic(r, cols, index)
    return kept, info


def _evaluate_arithmetic(r: Relation, cols: dict, index) -> None:
    tgt = cols[r.target].to_numpy(dtype=float)
    parts = [cols[t].to_numpy(dtype=float) for t in r.terms]
    if r.kind == "product":
        expected = parts[0] * parts[1] * r.scale
    else:
        expected = np.sum(parts, axis=0)
    valid = ~np.isnan(expected) & ~np.isnan(tgt)
    bad = valid & (np.abs(expected - tgt) > _tol(tgt))
    r.comparable = int(valid.sum())
    r.holding = int((valid & ~bad).sum())
    r.violation_count = int(bad.sum())
    sym = "×" if r.kind == "product" else "+"
    scale_txt = " ÷ 100" if r.scale != 1.0 else ""
    r.summary = f"{r.target} = " + f" {sym} ".join(r.terms) + scale_txt
    for p in np.flatnonzero(bad)[:MAX_EXAMPLES]:
        label = index[p]
        values = {r.target: _clean_num(tgt[p]), **{t: _clean_num(parts[n][p]) for n, t in enumerate(r.terms)}}
        r.violations.append({"label": label, "record": _label_record(label, int(p)), "values": values,
                             "expected": {r.target: _clean_num(expected[p])},
                             "difference": _clean_num(tgt[p] - expected[p])})


def _clean_num(x) -> float | int:
    x = float(x)
    if x == int(x) and abs(x) < 1e15:
        return int(x)
    return round(x, 6)


# ================================================================================================ orderings
def _pair_hint(a: str, b: str) -> bool:
    """The names say a normally comes before b: 'order date' / 'ship date', 'start' / 'end', 'dob' / anything."""
    ta, tb = _tokens(a), _tokens(b)
    if ta & _BIRTH and not tb & _BIRTH:
        return True
    return bool(ta & _EARLY and tb & _LATE and not (ta & _LATE and tb & _EARLY))


def _find_date_orders(df: pd.DataFrame, profile: dict) -> list:
    dates = {}
    for col in df.columns:
        if len(dates) >= 30:
            break
        key = str(col)
        prof = profile.get(key)
        if prof is not None and prof.semantic_type in (profiling.IDENTIFIER, profiling.PHONE):
            continue
        d = _to_datetime_series(key, df[col])
        if d is not None and d.notna().sum() >= MIN_COMPARABLE:
            dates[key] = d
    out: list[Relation] = []
    for a, b in itertools.permutations(dates, 2):
        named = _pair_hint(a, b)
        da, db = dates[a], dates[b]
        valid = da.notna() & db.notna()
        comparable = int(valid.sum())
        min_rows = DATE_ORDER_MIN_COMPARABLE_NAMED if named else DATE_ORDER_MIN_COMPARABLE_BLIND
        if comparable < min_rows:
            continue
        holds = valid & (da <= db)
        hold = int(holds.sum())
        need = DATE_ORDER_RATIO_NAMED if named else DATE_ORDER_RATIO_BLIND
        if hold / comparable < need or hold == comparable:
            continue                                   # not supported as an ordering, or no contradiction to report
        # equal dates everywhere is not an ordering, it is the same column: require some strict ordering
        if int((valid & (da < db)).sum()) < MIN_NONTRIVIAL_ROWS:
            continue
        rel = Relation("date_order", b, [a], comparable, hold, named=named)
        bad = valid & (da > db)
        rel.violation_count = int(bad.sum())
        rel.summary = f"{a} should not be later than {b}"
        for p in np.flatnonzero(bad.to_numpy())[:MAX_EXAMPLES]:
            label = df.index[p]
            rel.violations.append({
                "label": label, "record": _label_record(label, int(p)),
                "values": {a: _fmt_date(da.iloc[p]), b: _fmt_date(db.iloc[p])},
                "expected": {b: f"on or after {_fmt_date(da.iloc[p])}"},
                "difference": f"{a} is {abs((da.iloc[p] - db.iloc[p]).days)} day(s) after {b}",
            })
        out.append(rel)
    # a pair reported both ways (named hint can fire for both) keeps only the better-supported direction
    best: dict = {}
    for r in out:
        key = frozenset([r.target, r.terms[0]])
        if key not in best or r.ratio > best[key].ratio:
            best[key] = r
    return list(best.values())[:MAX_RELATIONS]


def _fmt_date(ts) -> str:
    try:
        return pd.Timestamp(ts).strftime("%Y-%m-%d")
    except Exception:
        return str(ts)


def _find_range_orders(df: pd.DataFrame, profile: dict) -> list:
    """minimum/maximum (or low/high) pairs that share a subject: 'min_temp'/'max_temp', 'Low'/'High'."""
    mins, maxs = {}, {}
    for col in df.columns:
        t = _tokens(col)
        if not t:
            continue
        subject = frozenset(t - _MIN_TOKENS - _MAX_TOKENS)
        if t & _MIN_TOKENS and not t & _MAX_TOKENS:
            mins[subject] = str(col)
        elif t & _MAX_TOKENS and not t & _MIN_TOKENS:
            maxs[subject] = str(col)
    out: list[Relation] = []
    for subject, lo in mins.items():
        hi = maxs.get(subject)
        if not hi:
            continue
        a, b = _to_float_series(df[lo]), _to_float_series(df[hi])
        if a is None or b is None:
            continue
        valid = a.notna() & b.notna()
        comparable = int(valid.sum())
        if comparable < 3:
            continue
        bad = valid & (a > b + _tol(b.to_numpy()))
        n_bad = int(bad.sum())
        if not n_bad or n_bad / comparable > 0.5:
            continue          # nothing wrong, or the labels are more likely swapped than individual rows wrong
        rel = Relation("range_order", hi, [lo], comparable, comparable - n_bad, named=True)
        rel.violation_count = n_bad
        rel.summary = f"{lo} should not be greater than {hi}"
        for p in np.flatnonzero(bad.to_numpy())[:MAX_EXAMPLES]:
            label = df.index[p]
            rel.violations.append({"label": label, "record": _label_record(label, int(p)),
                                   "values": {lo: _clean_num(a.iloc[p]), hi: _clean_num(b.iloc[p])},
                                   "expected": {hi: f"at least {_clean_num(a.iloc[p])}"},
                                   "difference": _clean_num(b.iloc[p] - a.iloc[p])})
        out.append(rel)
    return out


# ============================================================================================ entry point
def find_relationships(df: pd.DataFrame, profile: dict) -> dict:
    """{"relations": [Relation, ...], "search": {...}}: every relationship the data supports, each already
    evaluated on all rows (violations listed, capped for size; violation_count is exact)."""
    if df is None or len(df) < MIN_COMPARABLE or len(df.columns) < 2:
        return {"relations": [], "search": {"numeric_columns_considered": 0, "numeric_columns_searched": 0,
                                           "search_capped": False, "search_timed_out": False}}
    arithmetic, search = _find_arithmetic(df, profile)
    relations = list(arithmetic) + _find_date_orders(df, profile) + _find_range_orders(df, profile)
    return {"relations": relations, "search": search}


def relation_to_example_rows(r: Relation) -> list:
    """JSON-safe rows for the report: record number, the values found, what the relationship expected."""
    out = []
    for v in r.violations:
        out.append({"record": v["record"], "source_row": v["record"] + 1, "values": v["values"],
                    "expected": v["expected"], "difference": v["difference"]})
    return out


def summarize(found: dict) -> dict:
    """What the engine inferred, for the report: every relationship it believed (including the ones that hold
    on every row, which are good news) with how many rows it checked. Nothing here is a verdict about which
    value is wrong."""
    rels = []
    for r in found.get("relations", []):
        rels.append({
            "kind": r.kind, "relationship": r.summary, "result_column": r.target, "other_columns": list(r.terms),
            "rows_checked": r.comparable, "rows_consistent": r.holding if r.kind in ("product", "sum")
            else r.comparable - r.violation_count,
            "rows_inconsistent": r.violation_count,
            "evidence": "inferred from the values" if not r.named else "inferred from the values and the column names",
        })
    return {"relationships": rels, "search": found.get("search", {})}
