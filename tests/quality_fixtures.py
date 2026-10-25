"""
Deterministic sample datasets for the quality-score calibration tests and the before/after table.

Every generator is seeded, so the same call always yields the same frame. They are built as
plain CSV text and read back with the production reader (processing.pipeline.read_source), so a
score measured here is the score a real upload gets.
"""
from __future__ import annotations

import csv
import io
import os
import random
import tempfile

import pandas as pd

ROWS = 200


def _rng(seed: int = 7) -> random.Random:
    return random.Random(seed)


def _good_row(i: int, r: random.Random) -> dict:
    return {
        "customer_id": f"C{1000 + i}",
        "full_name": f"Customer Name{i}",
        "email": f"customer{i}@example.com",
        "phone": f"080{r.randint(10000000, 99999999)}",
        "amount": f"{r.randint(500, 90000)}",
        "order_date": f"2025-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}",
        "status": r.choice(["Paid", "Pending", "Refunded"]),
        "notes": f"note {i}",
    }


def clean_rows(n: int = ROWS) -> list[dict]:
    r = _rng()
    return [_good_row(i, r) for i in range(n)]


def to_df(rows: list[dict], *, header: list[str] | None = None) -> pd.DataFrame:
    """Round-trips through a CSV file and the production reader, like a real upload."""
    from processing.pipeline import read_source
    cols = header or list(rows[0].keys())
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    w.writerows(rows)
    fd, path = tempfile.mkstemp(suffix=".csv")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
            fh.write(buf.getvalue())
        return read_source(path, has_header=True)
    finally:
        os.unlink(path)


def clean_df(n: int = ROWS) -> pd.DataFrame:
    return to_df(clean_rows(n))


def slightly_messy_df(n: int = ROWS) -> pd.DataFrame:
    """Stray spaces and case differences only: every value is still right."""
    rows = clean_rows(n)
    for i, row in enumerate(rows):
        if i % 10 == 0:
            row["full_name"] = f"  {row['full_name']} "
        if i % 8 == 0:
            row["email"] = row["email"].upper()
        if i % 9 == 0:
            row["status"] = row["status"].lower()
    return to_df(rows)


def one_column_invalid_df(share: float = 0.20, column: str = "email", n: int = ROWS) -> pd.DataFrame:
    rows = clean_rows(n)
    bad = int(n * share)
    for i in range(bad):
        rows[i][column] = "not-an-email" if column == "email" else "abc"
    return to_df(rows)


def very_messy_df(n: int = ROWS) -> pd.DataFrame:
    """40-50% of several important columns are missing, invalid or placeholders."""
    rows = clean_rows(n)
    r = _rng(11)
    for i, row in enumerate(rows):
        if i % 2 == 0:
            row["email"] = r.choice(["", "N/A", "bad@@mail", "no email"])
        if i % 5 in (0, 1):
            row["phone"] = r.choice(["", "-", "call me", "N/A", "0803"])
        if i % 2 == 1:
            row["amount"] = r.choice(["abc", "Unknown", "", "free", "N/A"])
        if i % 5 in (2, 3):
            row["order_date"] = r.choice(["", "N/A", "someday", "31/02/2025"])
    return to_df(rows)


def orders_with_letters_df(n: int = ROWS) -> pd.DataFrame:
    """Stand-in for an orders export with letters in the phone and amount columns."""
    rows = clean_rows(n)
    r = _rng(13)
    for i, row in enumerate(rows):
        if i % 4 == 0:
            row["phone"] = r.choice(["08O12345678", "phone", "070-ABC-1234", "N/A"])
        if i % 3 == 0:
            row["amount"] = r.choice(["abc", "12k", "NGN five thousand", "N/A", "-"])
    return to_df(rows)


def mixed_currency_df(n: int = ROWS) -> pd.DataFrame:
    rows = clean_rows(n)
    for i, row in enumerate(rows):
        row["amount"] = ["$%d" % (i + 10), "₦%d" % (i + 10), "€%d" % (i + 10), "%d" % (i + 10)][i % 4]
    return to_df(rows)


def with_duplicates_df(share: float = 0.10, n: int = ROWS) -> pd.DataFrame:
    rows = clean_rows(n)
    k = int(n * share)
    rows = rows[: n - k] + [dict(rows[i]) for i in range(k)]
    return to_df(rows)


def messy_sample_df() -> pd.DataFrame:
    from processing.pipeline import read_source
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "testing", "messy_sample_v2.csv")
    return read_source(path, has_header=True)
