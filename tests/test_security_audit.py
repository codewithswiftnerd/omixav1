"""Regression tests for the security audit (docs/SECURITY_AUDIT.md). Synthetic accounts only; offline."""
import hashlib
import hmac
import json
import os
import time
import unittest

os.environ.setdefault("FLASK_ENV", "development")

from config import Config

Config.PAYSTACK_SECRET_KEY = "sk_test_secret"
Config.PAYSTACK_PLAN_MONTHLY = "PLN_month"
Config.PAYSTACK_PLAN_ANNUAL = "PLN_year"
Config.RATE_LIMIT_PER_MINUTE = 100000

from accounts import auth as auth_mod
from accounts import paystack
from accounts import store as store_mod
from accounts.entitlements import effective
from app import create_app
from export.exporter import _defuse_cell
from utils import session as session_utils


def _sign(body: bytes) -> str:
    return hmac.new(Config.PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.store = store_mod.MemoryStore()
        store_mod.set_store_for_tests(self.store)
        auth_mod.set_verifier_for_tests(lambda t: {"uid": t, "email": f"{t}@example.com", "name": t})
        self.app = create_app()
        self.app.config["TESTING"] = True

    def _login(self, uid):
        c = self.app.test_client()
        self.assertEqual(c.post("/api/auth/session", json={"idToken": uid}).status_code, 200)
        return c

    # --- payments: metadata alone must never grant Pro ------------------------------------
    def _webhook(self, c, data):
        raw = json.dumps({"event": "charge.success", "data": data}).encode()
        return c.post("/api/billing/webhook", data=raw, headers={"x-paystack-signature": _sign(raw)})

    def test_forged_metadata_charge_does_not_grant_pro(self):
        c = self._login("mallory")
        r = self._webhook(c, {"status": "success", "reference": "attacker_ref", "amount": 10000,
                              "metadata": {"uid": "mallory", "plan": "pro_annual", "omixa": True},
                              "customer": {"customer_code": "CUS_x"}})
        self.assertIn("ignored", r.get_json()["outcome"])
        self.assertFalse(effective(self.store.get_user("mallory"))["is_pro"])

    def test_own_checkout_reference_still_grants_pro(self):
        self.store.put_payment("omx_ok", {"uid": "alice", "plan": "pro_monthly", "status": "initiated"})
        self.store.upsert_user("alice", {"uid": "alice", "email": "a@x.com"})
        c = self.app.test_client()
        r = self._webhook(c, {"status": "success", "reference": "omx_ok", "amount": 1,
                              "metadata": {"uid": "alice", "plan": "pro_monthly", "omixa": True}})
        self.assertEqual(r.get_json()["outcome"], "activated")

    def test_webhook_bad_signature_rejected(self):
        r = self.app.test_client().post("/api/billing/webhook", data=b"{}", headers={"x-paystack-signature": "00"})
        self.assertEqual(r.status_code, 401)

    # --- sessions: logout revokes a copied cookie -------------------------------------------
    def test_logout_revokes_replayed_cookie(self):
        c = self._login("bob")
        with c.session_transaction() as s:
            stolen = dict(s)
        self.assertTrue(c.get("/api/auth/me").get_json()["user"])
        c.post("/api/auth/logout")
        replay = self.app.test_client()
        with replay.session_transaction() as s:
            s.update(stolen)
        self.assertEqual(replay.get("/api/auth/me").status_code, 401)
        self.assertEqual(replay.post("/api/billing/checkout", json={"plan": "pro_monthly"}).status_code, 401)

    # --- admin ------------------------------------------------------------------------------
    def test_admin_endpoints_require_admin(self):
        c = self._login("carol")
        for path in ("/admin/api/users", "/admin/api/stats", "/admin/api/jobs", "/admin/api/errors"):
            self.assertEqual(c.get(path).status_code, 401, path)

    def test_admin_session_expires(self):
        c = self.app.test_client()
        with c.session_transaction() as s:
            s["is_admin"] = True
            s["admin_at"] = time.time() - 2 * 3600
            s["admin_seen"] = time.time()
        self.assertEqual(c.get("/admin/api/stats").status_code, 401)

    def test_admin_session_without_timestamps_rejected(self):
        c = self.app.test_client()
        with c.session_transaction() as s:
            s["is_admin"] = True
        self.assertEqual(c.get("/admin/api/stats").status_code, 401)

    # --- misc -----------------------------------------------------------------------------
    def test_no_password_or_secret_fields_in_me(self):
        c = self._login("dave")
        body = json.dumps(c.get("/api/auth/me").get_json()).lower()
        for needle in ("password", "paystack", "token", "secret", "session_epoch"):
            self.assertNotIn(needle, body)

    def test_formula_cells_defused(self):
        for v in ("=1+1", "+cmd|' /C calc'!A0", "@SUM(A1)", "-2+3", '\t=1'):
            self.assertTrue(_defuse_cell(v).startswith("'"), v)
        self.assertEqual(_defuse_cell("-5"), "-5")

    def test_cross_origin_post_blocked(self):
        c = self._login("erin")
        r = c.post("/api/billing/checkout", json={"plan": "pro_monthly"}, headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
