"""Pro accounts, billing and Quality Profiles. Uses an in-memory store and a fake Paystack, so it runs offline."""
import hashlib
import hmac
import io
import json
import os
import time
import unittest

os.environ.setdefault("FLASK_ENV", "development")

from config import Config

Config.PAYSTACK_SECRET_KEY = "sk_test_secret"
Config.PAYSTACK_PLAN_MONTHLY = "PLN_month"
Config.PAYSTACK_PLAN_ANNUAL = "PLN_year"
Config.PRICE_NGN_MONTHLY, Config.PRICE_NGN_ANNUAL = 7500, 75000
Config.RATE_LIMIT_PER_MINUTE = 100000  # these tests make many calls from one client

from accounts import auth as auth_mod
from accounts import store as store_mod
from accounts.entitlements import effective
from app import create_app
from routes import billing as billing_mod

CSV = ("Name,Email,Signup Date,Country,Amount\n"
       "Ada ,ada@x.com,2024-01-05,Nigeria,\u20a61200\n"
       "Ada ,ada@x.com,2024-01-05,Nigeria,\u20a61200\n"
       "Kofi,bad-email,2024-13-45,Ghana,GH\u20b5 45\n"
       "Zed,zed@x.com,2024-02-01,nigeria,KSh 3000\n").encode()


class FakePaystack:
    def __init__(self):
        self.calls, self.verify_result = [], None

    def initialize(self, **kw):
        self.calls.append(kw)
        return {"authorization_url": "https://checkout.paystack.test/abc", "reference": kw["reference"]}

    def verify(self, ref):
        return self.verify_result

    def manage_link(self, code):
        return {"link": "https://paystack.test/manage"}


def _sign(body: bytes) -> str:
    return hmac.new(Config.PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()


class ProTests(unittest.TestCase):
    def setUp(self):
        self.store = store_mod.MemoryStore()
        store_mod.set_store_for_tests(self.store)
        self.fake = FakePaystack()
        billing_mod.set_client_for_tests(self.fake)
        auth_mod.set_verifier_for_tests(lambda t: {"uid": t, "email": f"{t}@example.com", "name": t.title()})
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.c = self.app.test_client()

    def login(self, client=None, uid="u1"):
        client = client or self.c
        r = client.post("/api/auth/session", json={"idToken": uid})
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()["user"]

    def make_pro(self, uid="u1", days=30, status="active", plan="pro_monthly"):
        self.store.upsert_user(uid, {"plan": plan, "subscription_status": status,
                                     "subscription_expires": time.time() + days * 86400, "subscription_start": time.time()})

    def upload(self, client=None):
        r = (client or self.c).post("/api/upload/", data={"file": (io.BytesIO(CSV), "survey.csv")}, content_type="multipart/form-data")
        self.assertEqual(r.status_code, 201, r.get_json())
        return r.get_json()["job_id"]

    # ---- free users -------------------------------------------------------
    def test_anonymous_free_cleaning_has_no_pro_block(self):
        job = self.upload()
        r = self.c.post(f"/api/process/{job}", json={})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.get_json()["summary"]["pro"])

    def test_free_signed_in_user_cannot_use_pro_endpoints(self):
        self.login()
        for path in ("/api/profiles/", "/api/sessions/"):
            r = self.c.get(path)
            self.assertEqual(r.status_code, 402, path)
            self.assertTrue(r.get_json()["upgrade_required"])
        self.assertEqual(self.c.post("/api/profiles/", json={"name": "x"}).status_code, 402)

    def test_unauthenticated_pro_endpoints_return_401(self):
        self.assertEqual(self.c.get("/api/profiles/").status_code, 401)

    def test_free_user_selecting_a_profile_is_told_it_is_pro(self):
        self.login()
        job = self.upload()
        r = self.c.post(f"/api/process/{job}", json={"profile_id": "anything"})
        self.assertEqual(r.status_code, 402)

    def test_free_upload_limit_is_enforced_and_pro_gets_more(self):
        old = Config.FREE_MAX_UPLOAD_MB
        Config.FREE_MAX_UPLOAD_MB = 0
        try:
            r = self.c.post("/api/upload/", data={"file": (io.BytesIO(CSV), "a.csv")}, content_type="multipart/form-data")
            self.assertEqual(r.status_code, 402)
            self.assertIn("limit", r.get_json()["error"])
        finally:
            Config.FREE_MAX_UPLOAD_MB = old

    # ---- the browser cannot grant itself Pro ------------------------------------
    def test_client_supplied_flags_cannot_unlock_pro(self):
        self.login()
        r = self.c.get("/api/profiles/?is_pro=true", headers={"X-Plan": "pro"})
        self.assertEqual(r.status_code, 402)
        self.assertEqual(self.c.post("/api/auth/session", json={"idToken": "u1", "is_pro": True}).get_json()["user"]["is_pro"], False)

    def test_forged_webhook_is_rejected(self):
        body = json.dumps({"event": "charge.success", "data": {}}).encode()
        self.assertEqual(self.c.post("/api/billing/webhook", data=body, headers={"x-paystack-signature": "deadbeef"}).status_code, 401)
        self.assertEqual(self.c.post("/api/billing/webhook", data=body).status_code, 401)

    # ---- payments ---------------------------------------------------------------
    def test_checkout_then_webhook_activates_pro_and_is_idempotent(self):
        self.login()
        r = self.c.post("/api/billing/checkout", json={"plan": "pro_annual"})
        self.assertEqual(r.status_code, 200)
        ref = r.get_json()["reference"]
        self.assertEqual(self.fake.calls[0]["plan"], "pro_annual")
        self.assertEqual(self.store.get_user("u1")["subscription_status"], "pending")

        event = {"event": "charge.success", "data": {"status": "success", "reference": ref, "amount": 7500000, "currency": "NGN",
                 "customer": {"customer_code": "CUS_1", "email": "u1@example.com"},
                 "plan": {"plan_code": "PLN_year"}, "metadata": {"uid": "u1", "plan": "pro_annual", "omixa": True}}}
        body = json.dumps(event).encode()
        for _ in range(2):  # Paystack retries: second delivery must not double-extend
            r = self.c.post("/api/billing/webhook", data=body, headers={"x-paystack-signature": _sign(body)})
            self.assertEqual(r.status_code, 200)
        u = self.store.get_user("u1")
        self.assertEqual((u["plan"], u["subscription_status"]), ("pro_annual", "active"))
        self.assertAlmostEqual(u["subscription_expires"], time.time() + 366 * 86400, delta=60)
        self.assertTrue(effective(u)["is_pro"])
        self.assertEqual(self.c.get("/api/profiles/").status_code, 200)

    def test_renewal_extends_from_current_expiry_and_matches_user_by_customer_code(self):
        self.login()
        self.make_pro(days=10)
        self.store.upsert_user("u1", {"paystack_customer_code": "CUS_9"})
        before = self.store.get_user("u1")["subscription_expires"]
        event = {"event": "charge.success", "data": {"status": "success", "reference": "renew_1", "customer": {"customer_code": "CUS_9"},
                 "plan": {"plan_code": "PLN_month"}, "metadata": {}}}
        body = json.dumps(event).encode()
        self.assertEqual(self.c.post("/api/billing/webhook", data=body, headers={"x-paystack-signature": _sign(body)}).status_code, 200)
        self.assertAlmostEqual(self.store.get_user("u1")["subscription_expires"], before + 31 * 86400, delta=5)

    def test_unrelated_paystack_charge_does_not_grant_pro(self):
        self.login()
        event = {"event": "charge.success", "data": {"status": "success", "reference": "other_1", "metadata": {"uid": "u1"}, "plan": {}}}
        body = json.dumps(event).encode()
        self.c.post("/api/billing/webhook", data=body, headers={"x-paystack-signature": _sign(body)})
        self.assertFalse(effective(self.store.get_user("u1"))["is_pro"])

    def test_return_url_verifies_with_paystack_and_requires_same_user(self):
        self.login()
        ref = self.c.post("/api/billing/checkout", json={"plan": "pro_monthly"}).get_json()["reference"]
        self.fake.verify_result = {"status": "success", "reference": ref, "customer": {"customer_code": "CUS_2"},
                                   "plan": {"plan_code": "PLN_month"}, "metadata": {"uid": "u1", "plan": "pro_monthly", "omixa": True}}
        r = self.c.get(f"/api/billing/return?reference={ref}")
        self.assertIn("payment=success", r.headers["Location"])
        self.assertTrue(effective(self.store.get_user("u1"))["is_pro"])
        # another user cannot claim someone else's reference
        other = self.app.test_client()
        self.login(other, "u2")
        self.assertIn("payment=unknown", other.get(f"/api/billing/return?reference={ref}").headers["Location"])

    def test_failed_payment_does_not_unlock_and_is_reported(self):
        self.login()
        ref = self.c.post("/api/billing/checkout", json={"plan": "pro_monthly"}).get_json()["reference"]
        self.fake.verify_result = {"status": "failed", "reference": ref, "metadata": {"uid": "u1"}}
        self.assertIn("payment=failed", self.c.get(f"/api/billing/return?reference={ref}").headers["Location"])
        u = self.store.get_user("u1")
        self.assertFalse(effective(u)["is_pro"])
        self.assertEqual(self.c.get("/api/profiles/").status_code, 402)

    def test_expired_subscription_loses_access_immediately(self):
        self.login()
        self.make_pro(days=-1)
        self.assertEqual(effective(self.store.get_user("u1"))["status"], "expired")
        self.assertEqual(self.c.get("/api/profiles/").status_code, 402)

    def test_cancelled_keeps_access_until_expiry(self):
        self.login()
        self.make_pro(days=5)
        event = {"event": "subscription.disable", "data": {"customer": {"customer_code": "CUS_x"}, "metadata": {"uid": "u1"}}}
        body = json.dumps(event).encode()
        self.c.post("/api/billing/webhook", data=body, headers={"x-paystack-signature": _sign(body)})
        eff = effective(self.store.get_user("u1"))
        self.assertEqual(eff["status"], "cancelled")
        self.assertTrue(eff["is_pro"])

    # ---- profiles, sessions, isolation --------------------------------------
    PROFILE = {"name": "Research Dataset Standard", "required_columns": ["name", "email", "signup_date", "country", "amount"],
               "expected_types": {"amount": "number", "signup_date": "date"}, "required_fields": ["name"],
               "date_columns": ["signup_date"], "email_columns": ["email"], "max_missing_pct": 10,
               "max_duplicate_pct": 0, "min_quality_score": 70}

    def test_profile_crud(self):
        self.login()
        self.make_pro()
        p = self.c.post("/api/profiles/", json=self.PROFILE).get_json()["profile"]
        self.assertEqual(self.c.get("/api/profiles/").get_json()["profiles"][0]["id"], p["id"])
        upd = {**self.PROFILE, "name": "Renamed", "min_quality_score": 90}
        self.assertEqual(self.c.put(f"/api/profiles/{p['id']}", json=upd).get_json()["profile"]["name"], "Renamed")
        dup = self.c.post(f"/api/profiles/{p['id']}/duplicate").get_json()["profile"]
        self.assertEqual(dup["name"], "Renamed (copy)")
        self.assertEqual(self.c.delete(f"/api/profiles/{p['id']}").status_code, 200)
        self.assertEqual(self.c.get(f"/api/profiles/{p['id']}").status_code, 404)
        self.assertEqual(self.c.post("/api/profiles/", json={"name": ""}).status_code, 400)
        self.assertEqual(self.c.post("/api/profiles/", json={"name": "x", "expected_types": {"a": "banana"}}).status_code, 400)

    def test_full_pro_workflow_history_reports_and_isolation(self):
        self.login()
        self.make_pro()
        pid = self.c.post("/api/profiles/", json=self.PROFILE).get_json()["profile"]["id"]
        job = self.upload()
        r = self.c.post(f"/api/process/{job}", json={"profile_id": pid})
        self.assertEqual(r.status_code, 200, r.get_json())
        pro = r.get_json()["summary"]["pro"]
        self.assertIn(pro["profile"]["results"][0]["status"], ("PASS", "WARNING", "FAIL"))
        self.assertGreaterEqual(pro["profile"]["compliance_after"], pro["profile"]["compliance_before"])
        self.assertTrue(pro["what_changed"]["lines"])
        sid = pro["session_id"]

        listing = self.c.get("/api/sessions/").get_json()["sessions"]
        self.assertEqual(listing[0]["dataset_name"], "survey.csv")
        self.assertEqual(listing[0]["profile_name"], "Research Dataset Standard")
        stored = json.dumps(self.store.get_session("u1", sid))
        for secret in ("ada@x.com", "bad-email", "Kofi"):  # raw data must never be stored
            self.assertNotIn(secret, stored)

        pdf = self.c.get(f"/api/sessions/{sid}/report.pdf")
        self.assertEqual((pdf.status_code, pdf.data[:4]), (200, b"%PDF"))
        self.assertIn(b"WHAT CHANGED", self.c.get(f"/api/sessions/{sid}/what-changed.txt").data)
        self.assertTrue(self.c.get(f"/api/sessions/{sid}/change-log.csv").data.startswith(b"rule,column"))

        # another Pro user can't see or download it, or use the first user's profile
        other = self.app.test_client()
        self.login(other, "u2")
        self.make_pro("u2")
        self.assertEqual(other.get(f"/api/sessions/{sid}").status_code, 404)
        self.assertEqual(other.get(f"/api/sessions/{sid}/report.pdf").status_code, 404)
        self.assertEqual(other.get(f"/api/profiles/{pid}").status_code, 404)
        job2 = self.upload(other)
        self.assertEqual(other.post(f"/api/process/{job2}", json={"profile_id": pid}).status_code, 404)
        self.assertEqual(other.get("/api/sessions/").get_json()["sessions"], [])

    def test_logout_and_login_persistence(self):
        self.login()
        self.make_pro()
        self.assertEqual(self.c.get("/api/profiles/").status_code, 200)
        self.c.post("/api/auth/logout")
        self.assertEqual(self.c.get("/api/profiles/").status_code, 401)
        self.login()  # same account again: subscription and data are still there
        self.assertEqual(self.c.get("/api/profiles/").status_code, 200)

    def test_pages_render_and_admin_counts(self):
        for path in ("/", "/clean", "/pricing", "/login", "/dashboard", "/profiles/new", "/sessions/abc"):
            self.assertEqual(self.c.get(path).status_code, 200, path)
        self.login()
        self.make_pro()
        self.login(self.app.test_client(), "u2")
        stats = self.store.user_stats()
        self.assertEqual((stats["total_users"], stats["pro_users"], stats["free_users"]), (2, 1, 1))

    def test_free_works_when_accounts_are_unavailable(self):
        store_mod.set_store_for_tests(None)
        store_mod._store_error = "down"
        old = Config.ALLOW_MEMORY_STORE
        Config.ALLOW_MEMORY_STORE = False
        Config.FIREBASE_PROJECT_ID = Config.FIREBASE_SERVICE_ACCOUNT_JSON = ""
        try:
            job = self.upload()
            self.assertEqual(self.c.post(f"/api/process/{job}", json={}).status_code, 200)
            self.assertEqual(self.c.post("/api/auth/session", json={"idToken": "x"}).status_code, 503)
        finally:
            Config.ALLOW_MEMORY_STORE = old


if __name__ == "__main__":
    unittest.main()
