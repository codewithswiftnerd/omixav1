"""Record-preservation regression tests (cafe-sales defect: 10,000 -> 9,859 rows).

Cause: the 'possible near-duplicate' fix treated a unique per-record column (Transaction ID) as the
ignorable one and deleted 141 records whose IDs were distinct. A record key is now recognised by its
values (not only its name), both when proposing the fix and when applying it.
"""
import os
import pandas as pd
import pytest

from cleaning import detectors
from cleaning.quality_report import generate_report
from cleaning.resolutions import apply_resolutions

REAL_CSV = os.environ.get("OMIXA_CAFE_CSV", "/mnt/user-data/uploads/dirty_cafe_sales.csv")
FIX = [{"issue": "possible_duplicate_records", "choice": "remove_near_duplicates"}]


def _sales(n=1000, id_name="Transaction ID"):
    # few distinct combinations -> many rows match on every column except the id
    return pd.DataFrame({
        id_name: [f"TXN_{i:07d}" for i in range(n)],
        "Item": [("Tea", "Cake", "Salad")[i % 3] for i in range(n)],
        "Quantity": [str(1 + i % 2) for i in range(n)],
        "Location": [("In-store", "Takeaway")[i % 2] for i in range(n)],
    })


@pytest.mark.parametrize("id_name", ["Transaction ID", "Ref", "Order", "Receipt", "Code"])
def test_unique_key_column_is_never_the_ignorable_column(id_name):
    df = _sales(id_name=id_name)
    offered = [f["column"] for f in generate_report(df)["findings"] if f["issue"] == "possible_duplicate_records"]
    assert id_name not in offered
    out, log = apply_resolutions(df.copy(), [{**FIX[0], "column": id_name}])
    assert len(out) == len(df)                      # nothing removed
    assert out[id_name].tolist() == df[id_name].tolist()


def test_refusal_is_reported_not_silent():
    df = _sales()
    _, log = apply_resolutions(df.copy(), [{**FIX[0], "column": "Transaction ID"}])
    entry = log[0] if isinstance(log, list) else log
    assert "unique" in str(log) or "identif" in str(log)


def test_legitimate_near_duplicate_removal_still_works_on_non_key_column():
    d = pd.DataFrame({"Item": ["Tea", "Tea", "Cake", "Cake", "Pie"],
                      "Qty": ["1", "1", "2", "2", "3"],
                      "Location": ["A", "B", "A", "A", "C"]})
    out, _ = apply_resolutions(d.copy(), [{**FIX[0], "column": "Location"}])
    assert len(out) == 4


def test_unique_free_text_column_can_still_be_ignored_in_a_large_file():
    n = 60
    d = pd.DataFrame({"name": ["Ann", "Bob", "Cy"] * (n // 3), "city": ["X", "Y", "Z"] * (n // 3),
                      "note": [f"call back on day {i}" for i in range(n)]})
    out, _ = apply_resolutions(d.copy(), [{**FIX[0], "column": "note"}])
    assert len(out) == 3


def test_is_unique_record_key_edges():
    assert detectors.is_unique_record_key(pd.Series([f"a{i}" for i in range(50)]))
    assert not detectors.is_unique_record_key(pd.Series(["x", "y"] * 25))
    assert not detectors.is_unique_record_key(pd.Series(["only"]))
    assert not detectors.is_unique_record_key(pd.Series(["a", "b", "c"]))                      # too few rows to infer
    assert not detectors.is_unique_record_key(pd.Series([f"free text note {i}" for i in range(50)]))  # free text
    assert not detectors.is_unique_record_key(pd.Series([None] * 40 + ["a", "b"]))  # mostly empty


@pytest.mark.skipif(not os.path.exists(REAL_CSV), reason="real cafe-sales CSV not available")
def test_real_cafe_sales_keeps_all_10000_records_end_to_end(tmp_path, monkeypatch):
    import uuid, shutil
    from config import Config
    import utils.file_handler as fh
    import processing.pipeline as pl
    monkeypatch.setattr(Config, "TEMP_DIR", str(tmp_path), raising=False)
    orig = pd.read_csv(REAL_CSV, dtype=str, keep_default_na=False)
    assert len(orig) == 10000
    job = str(uuid.uuid4()); d = fh.job_dir_path(job); os.makedirs(d, exist_ok=True)
    shutil.copy(REAL_CSV, os.path.join(d, "source.csv"))
    res = pl.run_pipeline(job, resolutions=[{**FIX[0], "column": "Transaction ID"}])
    out = pd.read_csv(os.path.join(d, "cleaned.csv"), dtype=str, keep_default_na=False)
    assert res["rows_in"] == res["rows_out"] == len(out) == 10000
    assert set(orig["Transaction ID"]) == set(out["Transaction ID"])
    assert res["change_log"]["removed_rows_total"] == 0
