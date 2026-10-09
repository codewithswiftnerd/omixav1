"""Cleaning-pipeline benchmark: python scripts/benchmark_cleaning.py [rows ...]
Default sizes: 1000 10000 50000 100000 (rows) plus a wide and a text-heavy dataset.
Prints seconds and peak RSS growth per dataset. Numbers depend on the machine; see docs/SCALING.md."""
import os
import random
import resource
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("FLASK_ENV", "development")
os.environ.setdefault("OMIXA_SECRET_KEY", "benchmark-" + "x" * 40)

import pandas as pd  # noqa: E402

from config import Config  # noqa: E402
from processing.pipeline import run_pipeline  # noqa: E402

JOB = "33333333-3333-4333-8333-333333333333"
rnd = random.Random(7)


def standard(n):
    return pd.DataFrame({
        "Name": [f" Person {i % 900} " for i in range(n)], "Email": [f"User{i % 700}@Example.com" for i in range(n)],
        "Country": [rnd.choice(["Nigeria", "NG", "ghana", "Kenya", "USA"]) for _ in range(n)],
        "Signed Up": [f"2024-{1 + i % 12:02d}-{1 + i % 27:02d}" for i in range(n)],
        "Amount": [f"${rnd.randint(1, 9999):,}" for _ in range(n)], "Active": [rnd.choice(["Yes", "No", "Y", "N"]) for _ in range(n)],
        "Phone": [f"080{rnd.randint(10000000, 99999999)}" for _ in range(n)], "Notes": ["ok"] * n})


def wide(n=2000, cols=150):
    return pd.DataFrame({f"Col {c}": [f" v{(i * c) % 50} " for i in range(n)] for c in range(cols)})


def text_heavy(n=5000):
    return pd.DataFrame({"id": range(n), "comment": [("lorem ipsum dolor sit amet " * rnd.randint(5, 40)).strip() for _ in range(n)],
                         "title": [f"Title {i % 300} " for i in range(n)]})


def run(label, df):
    Config.TEMP_DIR = tempfile.mkdtemp()
    os.makedirs(os.path.join(Config.TEMP_DIR, JOB))
    df.to_csv(os.path.join(Config.TEMP_DIR, JOB, "source.csv"), index=False)
    t = time.perf_counter()
    run_pipeline(JOB, rules=None)
    secs = time.perf_counter() - t
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(f"{label:28s} {df.shape[0]:>7,} x {df.shape[1]:<4d} {secs:7.1f} s   peak RSS {rss:6.0f} MB", flush=True)


if __name__ == "__main__":
    sizes = [int(a) for a in sys.argv[1:]] or [1000, 10000, 50000, 100000]
    for n in sizes:
        run("standard", standard(n))
    run("wide (150 columns)", wide())
    run("text-heavy", text_heavy())
