"""Regression: "Fill gaps" must never write the text 'Unknown' (or a median) into a money column.

Reproduced on a workbook whose fee column held values like "₦25.50", "€300.00", "$7.25": the column was read as
text, so every blank became the word "Unknown" next to real amounts."""
import pandas as pd

from cleaning import profiling, rules

FEES = ["€300.00", "₦25.50", None, "$7.25", "£10.00", None, "₦300.00", "$40.00"]


def test_currency_symbol_strings_count_as_numeric_by_content():
    assert rules._mostly_numeric(pd.Series(FEES)) is True


def test_currency_column_is_never_imputed():
    prof = profiling.profile_column("fee", pd.Series(FEES), len(FEES))
    assert prof.semantic_type == profiling.CURRENCY
    assert profiling.never_impute(prof)


def test_fill_gaps_leaves_money_blank_but_still_fills_text():
    df = pd.DataFrame({"fee": FEES, "city": ["Lagos", None, "Abuja", None, "Lagos", None, "Abuja", "Lagos"]})
    out, _ = rules.handle_missing_values(df.copy(), {})
    assert out["fee"].isna().sum() == 2
    assert "Unknown" not in out["fee"].astype(str).tolist()
    assert (out["city"] == "Unknown").sum() == 3
