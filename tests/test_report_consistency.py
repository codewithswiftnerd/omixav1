"""Reports must agree with each other and with the exported file (upload -> process -> download)."""
import io
import unittest

import pandas as pd

import tests.test_pro_accounts as _pro

CSV = ("Id,Email,Signup,Currency\n"
       "001,a@x.com,04/12/2024,NGN\n"
       "002,,08/01/2025,$\n"
       "003,N/A,13/02/2025,usd\n"
       "004,not-an-email,05/06/2025,naira\n"
       "005,a@x.com,xx,NGN\n"
       "006,b@x.com,01/02/2025,NGN\n"
       "006,b@x.com,01/02/2025,NGN\n").encode()
DAY_FIRST = [{"column": "Signup", "issue": "ambiguous_date_format", "choice": "day_first"}]


class ReportConsistencyTests(unittest.TestCase):
    def setUp(self):
        import utils.security as security
        security._hits.clear()
        _pro.ProTests.setUp(self)
        r = self.c.post("/api/upload/", data={"file": (io.BytesIO(CSV), "r.csv")}, content_type="multipart/form-data")
        self.job = r.get_json()["job_id"]
        r = self.c.post(f"/api/process/{self.job}", json={"resolutions": DAY_FIRST})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.s = r.get_json()["summary"]
        raw = self.c.get(f"/api/download/{self.job}").data.decode("utf-8-sig")
        self.out = pd.read_csv(io.StringIO(raw), dtype=str, keep_default_na=False)

    def test_selected_day_first_order_reaches_the_exported_file(self):
        self.assertEqual(self.out["Signup"].tolist(), ["2024-12-04", "2025-01-08", "2025-02-13", "2025-06-05", "xx", "2025-02-01"])
        self.assertEqual(self.s["resolutions_applied"][0]["choice"], "day_first")

    def test_invalid_value_figure_is_the_same_on_every_screen(self):
        ir = self.s["integrity_report"]["invalid_values"]
        counts = self.s["cleaning_summary"]["counts"]
        self.assertEqual(counts["invalid_values_detected"], ir["detected"])
        self.assertIn(f"Invalid values detected: {ir['detected']}", self.s["cleaning_summary"]["text"])
        self.assertEqual(counts["invalid_values_remaining"], sum(ir["by_kind_after"].values()))

    def test_row_and_duplicate_counts_match_the_file(self):
        self.assertEqual((self.s["rows_in"], self.s["rows_out"], len(self.out)), (7, 6, 6))
        self.assertEqual(self.s["cleaning_summary"]["counts"]["duplicate_rows_removed"], 1)
        self.assertEqual(self.s["integrity_report"]["row_reconciliation"]["unexplained"], 0)
        self.assertEqual(self.out["Id"].tolist(), ["001", "002", "003", "004", "005", "006"])      # leading zeros kept

    def test_missing_emails_are_not_reported_as_invalid_syntax(self):
        email = next(c for c in self.s["validation_after"]["columns"] if c["column"] == "Email")
        self.assertEqual(email["counts"]["missing"], 2)      # the empty cell and the N/A placeholder
        self.assertEqual(email["counts"]["invalid"], 1)      # only "not-an-email"
        self.assertEqual(self.out["Email"].tolist()[1:3], ["", ""])

    def test_dollar_sign_is_never_assumed_to_be_usd(self):
        self.assertEqual(self.out["Currency"].tolist()[1], "$")
        self.assertEqual(self.out["Currency"].tolist()[3], "NGN")           # "naira" names one currency


if __name__ == "__main__":
    unittest.main()


class ImputationReportingTests(unittest.TestCase):
    """A fill the user approved through a resolution must be reported everywhere, and match the file."""

    def setUp(self):
        import utils.security as security
        security._hits.clear()
        _pro.ProTests.setUp(self)
        rows = ["Id,Age,Score,City"] + [
            f"{i},{'' if i % 5 == 0 else 20 + i % 30},{'' if i % 8 == 0 else 50 + i % 40},{'' if i % 7 == 0 else 'Lagos'}"
            for i in range(1, 41)]
        r = self.c.post("/api/upload/", data={"file": (io.BytesIO(("\n".join(rows) + "\n").encode()), "m.csv")},
                        content_type="multipart/form-data")
        self.job = r.get_json()["job_id"]
        res = [{"column": c, "issue": "missing_values", "choice": "impute"} for c in ("Age", "Score")]
        self.s = self.c.post(f"/api/process/{self.job}", json={"resolutions": res}).get_json()["summary"]
        raw = self.c.get(f"/api/download/{self.job}").data.decode("utf-8-sig")
        self.out = pd.read_csv(io.StringIO(raw), dtype=str, keep_default_na=False)

    def test_imputation_counts_match_everywhere(self):
        ir = self.s["integrity_report"]
        self.assertEqual(ir["imputation"]["estimated"], 13)                       # 8 Age + 5 Score
        self.assertEqual(self.s["cleaning_summary"]["counts"]["missing_values_filled"], 13)
        self.assertEqual(ir["missing_values"]["filled"], 13)                      # not double counted
        self.assertTrue(any("filled with an estimate" in w for w in ir["warnings"]))

    def test_estimates_are_in_the_audit_as_user_approved_and_untouched_blanks_stay_blank(self):
        entries = [e for e in self.s["audit"]["entries"] if e["rule"] == "resolution:missing_values:impute"]
        self.assertEqual(sorted(e["cells_changed"] for e in entries), [5, 8])
        self.assertTrue(all(e["approval"] == "user_approved" for e in entries))
        self.assertEqual((self.out["Age"] == "").sum() + (self.out["Score"] == "").sum(), 0)
        self.assertEqual((self.out["City"] == "").sum(), 5)                        # never invented without a choice
