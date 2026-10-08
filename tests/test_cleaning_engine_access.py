"""Free/Pro enforcement for the cleaning engine, end to end through Flask (inline mode), the Paystack
-> entitlement flow, and the worker path. Offline: in-memory store + fake Paystack."""
import io
import json
import time
import unittest

from tests.test_pro_accounts import ProTests, _sign
from accounts.entitlements import effective
from cleaning.engine import FeatureNotAvailable, authorize
from cleaning.engine import access

CSV = ("Name,Phone,Date,Amount,Status\n"
       "  jOhN   dOE ,0803-123-4567,31/12/2026,\u20a625000,pending\n"
       "mary smith,+2348031234567,01/02/2026,$25000,PENDING\n"
       "ada obi,123456,not a date,25000,pendng\n").encode()

PROFILE = {"name": "Nigerian Customer Dataset", "columns": {
    "Name": {"rules": [{"type": "trim_whitespace"}, {"type": "normalize_whitespace"}, {"type": "normalize_case", "mode": "title"}]},
    "Phone": {"rules": [{"type": "normalize_phone", "country": "NG", "output_format": "international"}]},
    "Date": {"rules": [{"type": "normalize_date", "output_format": "YYYY-MM-DD"}]},
    "Amount": {"rules": [{"type": "normalize_currency", "currency": "NGN"}]},
    "Status": {"rules": [{"type": "normalize_case", "mode": "title"}, {"type": "standardize_categories", "canonical": ["Pending"]}]}}}


class EngineAccessTests(unittest.TestCase):
    login, make_pro = ProTests.login, ProTests.make_pro

    def setUp(self):
        import utils.security as security
        security._hits.clear()            # per-IP rate-limit windows must not leak between tests
        ProTests.setUp(self)

    def upload(self, client=None):
        r = (client or self.c).post("/api/upload/", data={"file": (io.BytesIO(CSV), "customers.csv")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()["job_id"]

    def process(self, body, client=None, job=None):
        job = job or self.upload(client)
        return (client or self.c).post(f"/api/process/{job}", json=body), job

    def pay(self, uid="u1", plan="pro_monthly"):
        ref = self.c.post("/api/billing/checkout", json={"plan": plan}).get_json()["reference"]
        event = {"event": "charge.success", "data": {"status": "success", "reference": ref, "amount": 750000, "currency": "NGN",
                 "customer": {"customer_code": "CUS_1", "email": f"{uid}@example.com"}, "plan": {"plan_code": "PLN_month"},
                 "metadata": {"uid": uid, "plan": plan, "omixa": True}}}
        body = json.dumps(event).encode()
        self.assertEqual(self.c.post("/api/billing/webhook", data=body, headers={"x-paystack-signature": _sign(body)}).status_code, 200)

    # ---- pure access registry -------------------------------------------------
    def test_registry_free_vs_pro_sets(self):
        authorize(access.FREE_RULES, is_pro=False)                        # every Free rule is allowed for Free
        for rule in access.PRO_RULES | access.PRO_FEATURES:
            with self.assertRaises(FeatureNotAvailable, msg=rule):
                authorize([rule], is_pro=False)
        authorize(access.ALL_CAPABILITIES, is_pro=True)
        with self.assertRaises(FeatureNotAvailable):
            authorize(["totally_unknown"], is_pro=True)                   # unknown never allowed by default
        body = FeatureNotAvailable("normalize_phone").to_response()
        self.assertEqual((body["code"], body["rule"], body["required_plan"], body["upgrade_required"]),
                         ("FEATURE_NOT_AVAILABLE", "normalize_phone", "pro", True))

    # ---- API enforcement --------------------------------------------------------
    def test_existing_free_cleaning_still_works(self):
        r, _ = self.process({})
        self.assertEqual(r.status_code, 200, r.get_json())
        r, _ = self.process({"rules": ["formatting"]})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertNotIn("cleaning_engine", r.get_json()["summary"])

    def test_free_user_with_pro_rule_gets_402_and_nothing_runs(self):
        self.login()
        r, job = self.process({"cleaning_profile": PROFILE})
        self.assertEqual(r.status_code, 402)
        b = r.get_json()
        self.assertEqual((b["code"], b["required_plan"]), ("FEATURE_NOT_AVAILABLE", "pro"))
        self.assertIn(b["rule"], access.PRO_CAPABILITIES)
        self.assertEqual(self.c.get(f"/api/download/{job}").status_code, 404)   # nothing was produced

    def test_anonymous_user_is_free_too(self):
        r, _ = self.process({"cleaning_profile": PROFILE})
        self.assertEqual(r.status_code, 402)

    def test_free_user_using_only_a_free_rule_inside_a_profile_is_still_a_pro_feature(self):
        self.login()
        r, _ = self.process({"cleaning_profile": {"columns": {"Name": {"rules": [{"type": "trim_whitespace"}]}}}})
        self.assertEqual((r.status_code, r.get_json()["rule"]), (402, "cleaning_profiles"))

    def test_client_cannot_claim_pro(self):
        self.login()
        job = self.upload()
        for extra in ({"is_pro": True}, {"plan": "pro"}, {"user": {"is_pro": True}}, {"entitlement": {"is_pro": True}}):
            r = self.c.post(f"/api/process/{job}?is_pro=true&plan=pro", json={"cleaning_profile": PROFILE, **extra},
                            headers={"X-Plan": "pro", "X-Is-Pro": "true"})
            self.assertEqual(r.status_code, 402, extra)

    def test_pro_user_runs_profile_and_gets_audit_review_and_metrics(self):
        self.login()
        self.make_pro()
        r, job = self.process({"cleaning_profile": PROFILE})
        self.assertEqual(r.status_code, 200, r.get_json())
        eng = r.get_json()["summary"]["cleaning_engine"]
        self.assertIn("column_rules", r.get_json()["summary"]["rules_applied"])
        self.assertEqual(eng["columns"]["Phone"]["rules"][0]["changed"], 1)
        pend = {(i["column"], i["original"]): i for i in eng["review"]["items"]}
        self.assertEqual(pend[("Date", "not a date")]["status"], "pending")
        self.assertEqual(pend[("Amount", "$25000")]["reason"], "currency_mismatch")
        self.assertEqual(pend[("Status", "pendng")]["suggestion"], "Pending")
        out = self.c.get(f"/api/download/{job}").data.decode()
        self.assertIn("John Doe", out)
        self.assertIn("+2348031234567", out)
        self.assertIn("2026-12-31", out)
        self.assertIn("2026-02-01", out)                    # 31/12 in the same column settles day-first
        self.assertIn("$25000", out)                        # flagged, original kept
        self.assertIn("123456", out)                        # invalid phone not "fixed"
        self.assertIn("Pendng", out)                        # flagged typo kept (case rule applied as configured)

    def test_user_chosen_format_is_not_rewritten_by_default_rules(self):
        self.login()
        self.make_pro()
        prof = {"columns": {"Date": {"rules": [{"type": "normalize_date", "output_format": "DD/MM/YYYY"}]}}}
        r, job = self.process({"cleaning_profile": prof})
        self.assertEqual(r.status_code, 200)
        self.assertIn("31/12/2026", self.c.get(f"/api/download/{job}").data.decode())

    def test_review_decisions_are_applied_on_a_second_run(self):
        self.login()
        self.make_pro()
        prof = dict(PROFILE, decisions=[{"column": "Status", "original": "pendng", "action": "accept"},
                                        {"column": "Amount", "original": "$25000", "action": "accept", "value": 25000}])
        r, job = self.process({"cleaning_profile": prof})
        eng = r.get_json()["summary"]["cleaning_engine"]
        st = {(i["column"], i["original"]): i["status"] for i in eng["review"]["items"]}
        self.assertEqual((st[("Status", "pendng")], st[("Amount", "$25000")]), ("accepted", "accepted"))
        out = self.c.get(f"/api/download/{job}").data.decode()
        self.assertNotIn("pendng", out)
        self.assertNotIn("$25000", out)

    def test_invalid_profiles_are_rejected_with_400(self):
        self.login()
        self.make_pro()
        job = self.upload()
        bad = [{"columns": {"Name": {"rules": [{"type": "eval", "code": "__import__('os').system('id')"}]}}},
               {"columns": {"Name": {"rules": [{"type": "normalize_phone", "country": "NG", "callback": "x"}]}}},
               {"columns": {"Name": {"rules": [{"type": "custom_replacements", "replacements": "DROP TABLE users;"}]}}},
               {"columns": {"Name; DROP TABLE users": {"rules": []}}}, "rm -rf /", ["a"], {"columns": "x"}]
        for p in bad:
            r = self.c.post(f"/api/process/{job}", json={"cleaning_profile": p})
            self.assertEqual(r.status_code, 400, p)
            self.assertIn("code", r.get_json())

    def test_unknown_column_fails_cleanly_422(self):
        self.login()
        self.make_pro()
        r, _ = self.process({"cleaning_profile": {"columns": {"Missing": {"rules": [{"type": "trim_whitespace"}]}}}})
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.get_json()["code"], "UNKNOWN_COLUMN")

    # ---- Paystack -> entitlement -> engine ------------------------------------------
    def test_payment_flow_free_denied_paid_accepted_expired_and_cancelled_denied_again(self):
        self.login()
        job = self.upload()
        self.assertEqual(self.c.post(f"/api/process/{job}", json={"cleaning_profile": PROFILE}).status_code, 402)   # Free

        self.pay()                                                                                                    # verified payment
        self.assertTrue(effective(self.store.get_user("u1"))["is_pro"])
        job2 = self.upload()
        self.assertEqual(self.c.post(f"/api/process/{job2}", json={"cleaning_profile": PROFILE}).status_code, 200)    # Pro

        ev = {"event": "subscription.disable", "data": {"customer": {"customer_code": "CUS_1"}, "metadata": {"uid": "u1"}}}
        body = json.dumps(ev).encode()
        self.c.post("/api/billing/webhook", data=body, headers={"x-paystack-signature": _sign(body)})
        self.assertEqual(effective(self.store.get_user("u1"))["status"], "cancelled")
        # cancelled keeps access until the paid period ends (existing behaviour), then it is denied
        self.assertEqual(self.c.post(f"/api/process/{self.upload()}", json={"cleaning_profile": PROFILE}).status_code, 200)
        self.store.upsert_user("u1", {"subscription_expires": time.time() - 60})
        self.assertEqual(self.c.post(f"/api/process/{self.upload()}", json={"cleaning_profile": PROFILE}).status_code, 402)

    def test_expired_pro_is_denied(self):
        self.login()
        self.make_pro(days=-1)
        self.assertEqual(self.process({"cleaning_profile": PROFILE})[0].status_code, 402)

    def test_failed_or_forged_payment_does_not_unlock_engine(self):
        self.login()
        ref = self.c.post("/api/billing/checkout", json={"plan": "pro_monthly"}).get_json()["reference"]
        self.fake.verify_result = {"status": "failed", "reference": ref, "metadata": {"uid": "u1"}}
        self.c.get(f"/api/billing/return?reference={ref}")
        body = json.dumps({"event": "charge.success", "data": {"metadata": {"uid": "u1"}}}).encode()
        self.assertEqual(self.c.post("/api/billing/webhook", data=body, headers={"x-paystack-signature": "bad"}).status_code, 401)
        self.assertEqual(self.process({"cleaning_profile": PROFILE})[0].status_code, 402)

    # ---- catalogue / validation / recommendations ------------------------------------
    def test_rules_catalogue_reflects_server_side_plan_only(self):
        free = self.c.get("/api/cleaning/rules?is_pro=true", headers={"X-Plan": "pro"}).get_json()
        self.assertEqual(free["plan"], "free")
        by = {r["type"]: r for r in free["rules"]}
        self.assertTrue(by["trim_whitespace"]["available"] and not by["normalize_phone"]["available"])
        self.assertEqual(by["normalize_phone"]["required_plan"], "pro")
        self.assertIn("country", by["normalize_phone"]["parameters"])
        self.login()
        self.make_pro()
        pro = self.c.get("/api/cleaning/rules").get_json()
        self.assertEqual(pro["plan"], "pro")
        self.assertTrue(all(r["available"] for r in pro["rules"]))

    def test_profile_validate_endpoint(self):
        self.assertEqual(self.c.post("/api/cleaning/profile/validate", json={"cleaning_profile": PROFILE}).status_code, 402)
        self.login()
        self.make_pro()
        ok = self.c.post("/api/cleaning/profile/validate", json={"cleaning_profile": PROFILE})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.get_json()["execution_order"]["Status"], ["normalize_case", "standardize_categories"])
        self.assertEqual(self.c.post("/api/cleaning/profile/validate", json={"cleaning_profile": {"columns": {"A": {"rules": [{"type": "x"}]}}}}).status_code, 400)

    def test_recommendations_are_pro_only(self):
        job = self.upload()
        free = self.c.get(f"/api/report/{job}").get_json()
        self.assertTrue(free["engine_recommendations"]["locked"])
        self.assertNotIn("columns", free["engine_recommendations"])
        self.login()
        self.make_pro()
        pro = self.c.get(f"/api/report/{self.upload()}").get_json()["engine_recommendations"]
        self.assertIn("columns", pro)
        self.assertTrue(all(not rec["auto_apply"] for c in pro["columns"].values() for rec in c["recommendations"]))
        self.assertIn("Phone", pro["columns"])

    # ---- worker path --------------------------------------------------------------------
    def test_pipeline_defends_in_depth_when_called_without_entitlement(self):
        from processing.pipeline import run_pipeline
        job = self.upload()
        with self.assertRaises(FeatureNotAvailable):
            run_pipeline(job, pro=False, cleaning_profile=PROFILE)

    def test_worker_child_turns_config_errors_into_a_clean_job_failure(self):
        import jobs.service as svc
        job = self.upload()
        spec = {"pro": True, "cleaning_profile": {"columns": {"Nope": {"rules": [{"type": "trim_whitespace"}]}}}}
        with self.assertRaises(svc.JobFailed) as cm:
            svc.run_isolated(svc._clean_body, (job, spec), 60)
        self.assertEqual((cm.exception.error_type, cm.exception.retryable), ("CleaningConfigError", False))
        self.assertIn("Nope", cm.exception.public_message)
        ok = svc.run_isolated(svc._clean_body, (job, {"pro": True, "cleaning_profile": PROFILE}), 60)
        self.assertEqual(ok["cleaning_engine"]["columns"]["Phone"]["rules"][0]["changed"], 1)


if __name__ == "__main__":
    unittest.main()
