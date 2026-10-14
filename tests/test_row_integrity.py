"""Stage 1: record preservation, near-duplicate safety, and the unexplained-row-loss guard.

Uses a SYNTHETIC cafe-sales-shaped file (the real dataset is not in the repo)."""
import csv, os, random, shutil, uuid

import pandas as pd
import pytest

import processing.pipeline as pl
import utils.file_handler as fh
from config import Config


def _make_cafe(path, n=10000, seed=7):
    rnd = random.Random(seed)
    items = {"Coffee": 2, "Tea": 1.5, "Sandwich": 4, "Salad": 5, "Cake": 3, "Cookie": 1}
    def mark(v, p=0.06):
        return rnd.choice(["ERROR", "UNKNOWN", ""]) if rnd.random() < p else v
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Transaction ID", "Item", "Quantity", "Price Per Unit", "Total Spent", "Payment Method", "Location", "Transaction Date"])
        for i in range(n):
            it = rnd.choice(list(items)); q = rnd.randint(1, 5); pr = items[it]
            w.writerow([f"TXN_{1000000+i}", mark(it), mark(q), mark(pr), mark(q * pr),
                        rnd.choice(["Cash", "Credit Card", "Digital Wallet", "ERROR", "UNKNOWN", ""]),
                        rnd.choice(["In-store", "Takeaway", "UNKNOWN", "ERROR", ""]),
                        mark(f"2023-{rnd.randint(1,12):02d}-{rnd.randint(1,28):02d}")])


def _run(tmp_path, monkeypatch, src, **kw):
    monkeypatch.setattr(Config, "TEMP_DIR", str(tmp_path), raising=False)
    job = str(uuid.uuid4()); d = fh.job_dir_path(job); os.makedirs(d, exist_ok=True)
    shutil.copy(src, os.path.join(d, "source.csv"))
    res = pl.run_pipeline(job, **kw)
    out = pd.read_csv(os.path.join(d, "cleaned.csv"), dtype=str, keep_default_na=False)
    return res, out


@pytest.fixture
def cafe(tmp_path):
    p = str(tmp_path / "cafe.csv"); _make_cafe(p); return p


def test_a_default_cleaning_preserves_all_10000_records(cafe, tmp_path, monkeypatch):
    res, out = _run(tmp_path, monkeypatch, cafe)
    orig = pd.read_csv(cafe, dtype=str, keep_default_na=False)
    assert res["rows_in"] == 10000 and res["rows_out"] == 10000 and len(out) == 10000
    assert set(orig["Transaction ID"]) == set(out["Transaction ID"])
    rows = res["integrity_report"]["row_reconciliation"]
    assert rows["unexplained"] == 0 and rows["removed_audited"] == 0 and rows["retained"] == 10000


def test_b_near_duplicate_removal_never_deletes_distinct_transaction_ids(cafe, tmp_path, monkeypatch):
    # Before the fix this selection removed 96 records with distinct IDs on the synthetic file.
    res, out = _run(tmp_path, monkeypatch, cafe, resolutions=[
        {"column": "Transaction ID", "issue": "possible_duplicate_records", "choice": "remove_near_duplicates"}])
    assert len(out) == 10000
    assert res["resolutions_applied"] == []
    skipped = res["resolutions_skipped"] if "resolutions_skipped" in res else []
    assert not skipped or "identifier" in str(skipped)


def test_b2_finding_never_proposes_an_identifier_column(cafe):
    from cleaning.quality_report import generate_report
    df = pd.read_csv(cafe, dtype=str, keep_default_na=False).replace("", pd.NA)
    rep = generate_report(df)
    assert not [f for f in rep["findings"] if f["issue"] == "possible_duplicate_records" and f["column"] == "Transaction ID"]


def test_near_duplicates_still_removable_on_a_non_identifier_column(tmp_path, monkeypatch):
    p = str(tmp_path / "n.csv")
    pd.DataFrame({"name": ["Ann", "Ann", "Bob"], "city": ["X", "X", "Y"], "note": ["a", "b", "c"]}).to_csv(p, index=False)
    res, out = _run(tmp_path, monkeypatch, p, rules=["formatting"], resolutions=[
        {"column": "note", "issue": "possible_duplicate_records", "choice": "remove_near_duplicates"}])
    assert len(out) == 2 and res["integrity_report"]["row_reconciliation"]["removed_audited"] == 1
    assert res["integrity_report"]["row_reconciliation"]["unexplained"] == 0
    assert res["change_log"]["removed_rows"][0]["reason"]


def _tiny(tmp_path, n=20):
    p = str(tmp_path / "s.csv")
    pd.DataFrame({"a": [str(i) for i in range(n)], "b": ["x"] * n}).to_csv(p, index=False)
    return p


def test_a_rule_that_drops_rows_is_attributed_automatically(tmp_path, monkeypatch):
    """Row loss inside any audited step is recorded against that rule, whatever the rule intends."""
    import cleaning.rules as rules_mod
    real = rules_mod.RULE_DISPATCH["formatting"]
    def lossy(df, *a, **k):
        out = real(df, *a, **k)
        return (out[0].iloc[:-3],) + tuple(out[1:])
    monkeypatch.setitem(rules_mod.RULE_DISPATCH, "formatting", lossy)
    res, out = _run(tmp_path, monkeypatch, _tiny(tmp_path), rules=["formatting"])
    rows = res["integrity_report"]["row_reconciliation"]
    assert len(out) == 17 and rows["removed_audited"] == 3 and rows["unexplained"] == 0
    assert {e["rule"] for e in res["change_log"]["removed_rows"]} == {"formatting"}


def test_unexplained_row_loss_is_surfaced_not_accepted(tmp_path, monkeypatch):
    """Rows lost AFTER the audited steps (an orchestration bug) must be flagged, never accepted."""
    real = pl.apply_rules
    def lossy_apply(*a, **k):
        out = real(*a, **k)
        return (out[0].iloc[:-3],) + tuple(out[1:])
    monkeypatch.setattr(pl, "apply_rules", lossy_apply)
    res, _ = _run(tmp_path, monkeypatch, _tiny(tmp_path), rules=["formatting"])
    ir = res["integrity_report"]
    assert ir["row_reconciliation"]["unexplained"] == 3
    assert ir["status"] == "requires_manual_review" and ir["fully_clean"] is False
    assert any("unexplained" in w for w in ir["warnings"])
    assert res["processing_warnings"][0]["code"] == "unexplained_row_count_change"


def test_unknown_rule_fails_loudly_and_changes_nothing():
    from cleaning.rules import apply_rules
    from cleaning.engine.base import RuleConfigError
    df = pd.DataFrame({"a": [" x ", "y"]})
    with pytest.raises(RuleConfigError, match="Unsupported cleaning operation"):
        apply_rules(df, rules=["formatting", "does_not_exist"])
    assert df["a"].tolist() == [" x ", "y"]   # nothing half-applied to the caller's frame


def test_unknown_rule_through_pipeline_is_a_failure_not_a_success(tmp_path, monkeypatch):
    from cleaning.engine.base import RuleConfigError
    p = str(tmp_path / "u.csv"); pd.DataFrame({"a": ["1"]}).to_csv(p, index=False)
    with pytest.raises(RuleConfigError):
        _run(tmp_path, monkeypatch, p, rules=["no_such_rule"])
