"""Cleaning-engine benchmark on representative datasets.

    python benchmarks/bench_cleaning.py            # 1k, 10k, 50k, 100k rows + wide + text-heavy
    python benchmarks/bench_cleaning.py --quick    # 1k and 10k only

Prints rows, cells, seconds and cells/second for apply_rules (default rules). Numbers depend on
the machine; use them to compare before/after a change, not as absolute promises.
"""
import os, sys, time
os.environ.setdefault("FLASK_ENV", "development")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import pandas as pd
from cleaning.rules import apply_rules


def standard(n):
    r = np.random.default_rng(1)
    return pd.DataFrame({
        "Name": [f" Person {i % 900} " for i in range(n)],
        "Email": [f"User{i % 700}@Example.com" if i % 9 else "bad-email" for i in range(n)],
        "Signup Date": [f"2024-0{1 + i % 9}-{10 + i % 18}" for i in range(n)],
        "Country": r.choice(["nigeria", "NG", "Ghana", "kenya", "Kenya "], n),
        "Active": r.choice(["yes", "No", "Y", "true"], n),
        "Amount": [f"${i % 5000}.50" for i in range(n)],
        "Notes": r.choice(["n/a", "ok", "", "NULL", "fine"], n),
    })


def wide(n, cols=120):
    r = np.random.default_rng(2)
    return pd.DataFrame({f"Col {c}": r.choice(["a ", " b", "N/A", "c"], n) for c in range(cols)})


def text_heavy(n):
    words = "alpha beta  gamma delta  epsilon zeta eta theta iota kappa".split(" ")
    return pd.DataFrame({f"Free text {c}": [" ".join(words[(i + c + k) % len(words)] for k in range(40)) for i in range(n)]
                         for c in range(4)})


def main():
    quick = "--quick" in sys.argv
    cases = [("standard", standard, n) for n in ([1000, 10000] if quick else [1000, 10000, 50000, 100000])]
    if not quick:
        cases += [("wide (120 cols)", wide, 5000), ("text-heavy", text_heavy, 5000)]
    print(f"{'dataset':<18}{'rows':>9}{'cells':>11}{'seconds':>9}{'cells/s':>12}")
    for name, make, n in cases:
        df = make(n)
        t = time.perf_counter()
        apply_rules(df.copy(), None)
        dt = time.perf_counter() - t
        print(f"{name:<18}{len(df):>9}{df.size:>11}{dt:>9.2f}{df.size / dt:>12,.0f}")


if __name__ == "__main__":
    main()
