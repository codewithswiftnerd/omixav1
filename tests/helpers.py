"""Small dataframe builders shared by the quality-model tests."""
import pandas as pd


def df_of(**cols):
    return pd.DataFrame(cols)


def finding(report, issue, column=None):
    hits = [f for f in report["findings"] if f["issue"] == issue and (column is None or f["column"] == column)]
    return hits[0] if hits else None


def issues(report):
    return {f["issue"] for f in report["findings"]}


def norm_nulls(df):
    """Compare frames ignoring NaN/NA/None representation and dtype."""
    return df.astype(object).where(df.notna(), None)
