"""Sign-in hardening: optional verified-email requirement, and no elevated/pre-login state carried into a user session."""
import os
import unittest

os.environ.setdefault("FLASK_ENV", "development")

from accounts import auth as auth_mod
from accounts import store as store_mod
from app import create_app
from config import Config


class SignInHardening(unittest.TestCase):
    def setUp(self):
        self.store = store_mod.MemoryStore()
        store_mod.set_store_for_tests(self.store)
        self.claims = {"uid": "u1", "email": "a@example.com", "name": "A", "provider": "password", "email_verified": False}
        auth_mod.set_verifier_for_tests(lambda t: dict(self.claims))
        self.addCleanup(auth_mod.set_verifier_for_tests, None)
        self._orig = Config.REQUIRE_VERIFIED_EMAIL
        self.addCleanup(setattr, Config, "REQUIRE_VERIFIED_EMAIL", self._orig)
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.c = self.app.test_client()

    def _sign_in(self):
        return self.c.post("/api/auth/session", json={"idToken": "t"})

    def test_default_allows_unverified_password_account(self):
        Config.REQUIRE_VERIFIED_EMAIL = False
        self.assertEqual(self._sign_in().status_code, 200)

    def test_flag_blocks_unverified_password_account(self):
        Config.REQUIRE_VERIFIED_EMAIL = True
        self.assertEqual(self._sign_in().status_code, 403)
        self.assertIsNone(self.store.get_user("u1"))

    def test_flag_allows_verified_password_and_google(self):
        Config.REQUIRE_VERIFIED_EMAIL = True
        self.claims["email_verified"] = True
        self.assertEqual(self._sign_in().status_code, 200)
        self.claims.update(uid="u2", provider="google.com", email_verified=False)
        self.assertEqual(self._sign_in().status_code, 200)

    def test_signin_drops_admin_flag_and_keeps_anonymous_sid(self):
        with self.c.session_transaction() as s:
            s["is_admin"] = True
            s["csrf_token"] = "old"
            s["sid"] = "anon-sid"
        Config.REQUIRE_VERIFIED_EMAIL = False
        self.assertEqual(self._sign_in().status_code, 200)
        with self.c.session_transaction() as s:
            self.assertNotIn("is_admin", s)
            self.assertNotIn("csrf_token", s)
            self.assertEqual(s["sid"], "anon-sid")
            self.assertEqual(s["uid"], "u1")


if __name__ == "__main__":
    unittest.main()
