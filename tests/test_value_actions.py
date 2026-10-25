"""The review pop-up's decisions (options.value_actions): clear a flagged value or delete its rows, only where the
user chose; everything else is untouched. Driven through the real upload -> process -> download API."""
import io

import pandas as pd

CSV = "name,phone,amount\nAda,+2348031234567,100\nBola,call me,200\nChi,abc,300\nDayo,0803123,400\nEfe,call me,500\n"


def _run(client, options):
    job = client.post("/api/upload/", data={"file": (io.BytesIO(CSV.encode()), "a.csv")},
                      content_type="multipart/form-data").get_json()["job_id"]
    r = client.post(f"/api/process/{job}", json={"options": options})
    return r, job


def _out(client, job):
    raw = client.get(f"/api/download/{job}").data.decode("utf-8-sig")
    return pd.read_csv(io.StringIO(raw), dtype=str, keep_default_na=False)


def test_clear_empties_only_the_chosen_values(client):
    r, job = _run(client, {"value_actions": {"phone": {"clear": ["call me"]}}})
    assert r.status_code == 200
    out = _out(client, job)
    assert out["phone"].tolist() == ["+2348031234567", "", "abc", "0803123", ""]
    assert len(out) == 5


def test_remove_rows_deletes_those_rows_only(client):
    r, job = _run(client, {"value_actions": {"phone": {"remove_rows": ["abc"], "clear": ["call me"]}}})
    out = _out(client, job)
    assert out["name"].tolist() == ["Ada", "Bola", "Dayo", "Efe"]


def test_no_choice_means_no_change(client):
    r, job = _run(client, {})
    assert _out(client, job)["phone"].tolist() == ["+2348031234567", "call me", "abc", "0803123", "call me"]


def test_contradictory_choice_is_rejected(client):
    r, _ = _run(client, {"value_actions": {"phone": {"clear": ["x"], "remove_rows": ["X"]}}})
    assert r.status_code == 400 and r.get_json()["code"] == "INVALID_OPTIONS"
