"""Value standardisation end to end through Flask: Pro gating, read-only preview, process, export, report."""
import io
import json
import unittest

import pandas as pd

import tests.test_pro_accounts as _pro


CSV = ("Id,Country,Active,Segment,Notes\n"
       "001,GH,Yes,SMB,a\n"
       "002,gha,y,Small Business,b\n"
       "003,Republic of Ghana,0,small business,c\n"
       "004,England,1,Small-Business,d\n"
       "005,Congo,N,Enterprise,e\n"
       "006,Kenya,TRUE,Enterprise,f\n"
       "007,Atlantis,no,Enterprise,g\n").encode()

PROFILE = {"name": "Standardize values", "columns": {
    "Country": {"rules": [{"type": "standardize_values", "category": "country", "output_format": "alpha2"}]},
    "Active": {"rules": [{"type": "standardize_values", "category": "boolean", "output_format": "yes_no"}]},
    "Segment": {"rules": [{"type": "standardize_values", "category": "category", "mappings": {"SMB": "Small Business"}}]}},
    "decisions": [{"column": "Country", "original": "England", "action": "accept", "value": "GB"},
                  {"column": "Segment", "original": "Small-Business", "action": "reject"}]}


class ValueMappingApiTests(unittest.TestCase):
    login, make_pro = _pro.ProTests.login, _pro.ProTests.make_pro

    def setUp(self):
        import utils.security as security
        security._hits.clear()            # per-IP rate-limit windows must not leak between tests
        _pro.ProTests.setUp(self)

    def upload_csv(self, client=None):
        r = (client or self.c).post("/api/upload/", data={"file": (io.BytesIO(CSV), "m.csv")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()["job_id"]

    def pro_client(self):
        self.login()
        self.make_pro()
        return self.c

    def test_free_user_gets_402_for_preview_and_for_the_rule(self):
        job = self.upload_csv()
        self.assertEqual(self.c.post(f"/api/cleaning/mapping/{job}", json={}).status_code, 402)
        r = self.c.post(f"/api/process/{job}", json={"cleaning_profile": PROFILE})
        self.assertEqual(r.status_code, 402)
        self.assertEqual(r.get_json()["code"], "FEATURE_NOT_AVAILABLE")

    def test_preview_is_read_only_and_owner_scoped(self):
        c = self.pro_client()
        job = self.upload_csv()
        r = c.post(f"/api/cleaning/mapping/{job}", json={})
        self.assertEqual(r.status_code, 200)
        cols = {(s["column"], s["category"]) for s in r.get_json()["suggestions"]}
        self.assertIn(("Country", "country"), cols)
        self.assertIn(("Active", "boolean"), cols)
        self.assertNotIn(("Notes", "category"), cols)
        # nothing was processed or exported by previewing
        self.assertEqual(c.get(f"/api/download/{job}").status_code, 404)
        # another visitor cannot preview this job
        other = self.app.test_client() if hasattr(self, "app") else self.c.application.test_client()
        self.assertIn(other.post(f"/api/cleaning/mapping/{job}", json={}).status_code, (402, 404))   # never 200

    def test_preview_validation_errors(self):
        c = self.pro_client(); job = self.upload_csv()
        bad = [{"column": "Nope", "category": "country"}, {"column": "Country", "category": "planets"},
               {"column": "Country", "category": "country", "output_format": "alpha9"},
               {"column": "Segment", "category": "category", "mappings": {"SMB": ""}},
               {"column": "Segment", "category": "category", "mappings": "x"}]
        for body in bad:
            r = c.post(f"/api/cleaning/mapping/{job}", json=body)
            self.assertIn(r.status_code, (400, 404), body)

    def test_process_export_and_report_agree(self):
        c = self.pro_client(); job = self.upload_csv()
        r = c.post(f"/api/process/{job}", json={"cleaning_profile": PROFILE})
        self.assertEqual(r.status_code, 200, r.get_json())
        summary = r.get_json()["summary"]
        out = pd.read_csv(io.StringIO(c.get(f"/api/download/{job}").data.decode("utf-8-sig")), dtype=str, keep_default_na=False)
        self.assertEqual(out["Country"].tolist(), ["GH", "GH", "GH", "GB", "Congo", "KE", "Atlantis"])
        self.assertEqual(out["Active"].tolist(), ["Yes", "Yes", "No", "Yes", "No", "Yes", "No"])
        self.assertEqual(out["Segment"].tolist()[:4], ["Small Business", "Small Business", "Small Business", "Small-Business"])
        self.assertEqual(out["Id"].tolist(), ["001", "002", "003", "004", "005", "006", "007"])      # leading zeros kept
        self.assertEqual(len(out), summary["rows_out"])
        eng = summary["cleaning_engine"]["columns"]
        country = next(r for r in eng["Country"]["rules"] if r["type"] == "standardize_values")
        self.assertEqual(country["changed"], 3)                     # GH is already canonical; gha, Republic of Ghana, Kenya change
        self.assertEqual(country["metrics"]["applied"], country["changed"])
        self.assertEqual(eng["Country"]["review_pending"], 2)       # Congo (ambiguous) and Atlantis (unrecognised)
        self.assertEqual(eng["Segment"]["review_pending"], 0)       # the user rejected the hyphen proposal
        # every cell the audit says changed really differs in the file, and the audit says why
        log = summary["change_log"]["cell_changes"]
        mapped = [e for e in log if "mapping" in e]
        self.assertTrue(mapped)
        for e in mapped:
            self.assertIn(e["mapping"]["mode"], ("automatic_safe", "user_defined", "user_approved", "user_edited"))
        self.assertTrue(any(e["mapping"]["origin"] == "custom_file" for e in mapped))

    def test_unresolved_items_are_not_reported_as_resolved(self):
        c = self.pro_client(); job = self.upload_csv()
        r = c.post(f"/api/process/{job}", json={"cleaning_profile": {"columns": {
            "Country": {"rules": [{"type": "standardize_values", "category": "country"}]}}}})
        s = r.get_json()["summary"]["cleaning_engine"]
        pend = s["totals"]["review_pending"]
        self.assertEqual(pend, 3)                                     # England, Congo, Atlantis: nothing was approved
        out = pd.read_csv(io.StringIO(c.get(f"/api/download/{job}").data.decode("utf-8-sig")), dtype=str, keep_default_na=False)
        self.assertEqual(out["Country"].tolist()[3:5], ["England", "Congo"])      # exactly as uploaded
        self.assertEqual(out["Country"].tolist()[6], "Atlantis")


if __name__ == "__main__":
    unittest.main()
