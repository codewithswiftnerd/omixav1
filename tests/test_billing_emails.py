"""
Billing emails (activated / renewed, payment failed, cancelled, expiring soon) and mail retry. Offline: the SMTP
layer is replaced by a recorder, so no server is contacted. Written against unittest only.
"""
import os
import time
import unittest
from unittest import mock

os.environ.setdefault("FLASK_ENV", "development")

from accounts import emails, paystack, reminders
from accounts.store import MemoryStore
from config import Config
from utils import email_templates, mailer

DAY = 86400
NOW = 1_800_000_000.0


def ok():
    return mailer.SendResult(True, "sent")


def fail(code, retryable=False):
    return mailer.SendResult(False, code, "x", retryable)


class Recorder:
    def __init__(self, results=None):
        self.sent, self.results = [], list(results or [])

    def __call__(self, to, subject, text, html=None, reply_to=None):
        self.sent.append({"to": to, "subject": subject, "text": text, "html": html})
        return self.results.pop(0) if self.results else ok()


class BillingCase(unittest.TestCase):
    def setUp(self):
        self.rec = Recorder()
        self.saved = {k: getattr(Config, k) for k in ("MAIL_RETRY_ATTEMPTS", "MAIL_RETRY_BASE_SECONDS")}
        Config.MAIL_RETRY_BASE_SECONDS = 0
        for target, repl in (("utils.mailer.send_email", self.rec),
                             ("utils.mailer.is_configured", lambda: True),
                             ("utils.mailer.run_in_background", lambda fn, *a, **k: (fn(*a, **k), True)[1]),
                             ("utils.mailer.time.sleep", lambda s: None)):
            p = mock.patch(target, repl)
            p.start()
            self.addCleanup(p.stop)
        self.store = MemoryStore()
        self.store.upsert_user("u1", {"email": "ada@example.com", "name": "Ada Obi", "plan": "free",
                                      "subscription_status": "free", "paystack_customer_code": "CUS_1"})

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(Config, k, v)

    def charge(self, ref="ref1", now=NOW, amount=500000):
        data = {"status": "success", "reference": ref, "amount": amount, "currency": "NGN",
                "customer": {"customer_code": "CUS_1"}, "metadata": {"uid": "u1", "plan": "pro_monthly"}}
        with mock.patch.object(paystack, "_resolve_plan", return_value="pro_monthly"):
            return paystack.handle_event(self.store, {"event": "charge.success", "data": data}, now)


class ActivationEmailTests(BillingCase):
    def test_first_payment_sends_activation_email_with_amount_and_date(self):
        self.assertEqual(self.charge(), "activated")
        self.assertEqual(len(self.rec.sent), 1)
        m = self.rec.sent[0]
        self.assertEqual(m["to"], "ada@example.com")
        self.assertIn("is active", m["subject"])
        self.assertIn("NGN 5,000.00", m["text"])
        self.assertIn(email_templates.format_date(NOW + 31 * DAY), m["text"])

    def test_second_payment_while_active_is_a_renewal(self):
        self.charge("ref1")
        self.charge("ref2", now=NOW + 30 * DAY)
        self.assertEqual(len(self.rec.sent), 2)
        self.assertIn("renewed", self.rec.sent[1]["subject"])

    def test_duplicate_webhook_delivery_sends_one_email(self):
        self.charge("ref1")
        self.assertEqual(self.charge("ref1"), "duplicate")
        self.assertEqual(len(self.rec.sent), 1)

    def test_user_without_email_is_still_upgraded_and_nothing_is_sent(self):
        self.store.upsert_user("u1", {"email": None})
        self.assertEqual(self.charge(), "activated")
        self.assertEqual(self.rec.sent, [])
        self.assertEqual(self.store.get_user("u1")["subscription_status"], "active")

    def test_mail_failure_cannot_break_the_grant(self):
        with mock.patch.object(emails, "send_pro_activated", side_effect=RuntimeError("boom")):
            self.assertEqual(self.charge(), "activated")
        self.assertEqual(self.store.get_user("u1")["subscription_status"], "active")

    def test_not_configured_sends_nothing_and_still_grants(self):
        with mock.patch("utils.mailer.is_configured", lambda: False):
            self.assertEqual(self.charge(), "activated")
        self.assertEqual(self.rec.sent, [])


class FailureAndCancelEmailTests(BillingCase):
    def event(self, kind, now):
        return paystack.handle_event(self.store, {"event": kind, "data": {"metadata": {"uid": "u1"}}}, now)

    def test_payment_failed_for_a_free_user_says_pro_is_not_active(self):
        self.event("charge.failed", NOW)
        self.assertEqual(len(self.rec.sent), 1)
        self.assertIn("Pro has not been activated", self.rec.sent[0]["text"])
        self.assertNotIn("not affected", self.rec.sent[0]["text"])

    def test_payment_failed_for_a_paid_up_user_names_the_access_date(self):
        self.charge()
        self.rec.sent.clear()
        self.event("invoice.payment_failed", NOW + 31 * DAY - 100)
        self.assertIn("not affected", self.rec.sent[0]["text"])

    def test_repeated_failure_deliveries_within_a_day_send_once(self):
        self.event("charge.failed", NOW)
        self.event("invoice.payment_failed", NOW + 3600)
        self.assertEqual(len(self.rec.sent), 1)
        self.event("charge.failed", NOW + 2 * DAY)
        self.assertEqual(len(self.rec.sent), 2)

    def test_cancellation_emails_once_with_access_until_date(self):
        self.charge()
        self.rec.sent.clear()
        self.event("subscription.disable", NOW + DAY)
        self.event("subscription.not_renew", NOW + DAY)     # Paystack often sends both
        self.assertEqual(len(self.rec.sent), 1)
        self.assertIn(email_templates.format_date(NOW + 31 * DAY), self.rec.sent[0]["text"])
        self.assertEqual(self.store.get_user("u1")["subscription_status"], "cancelled")

    def test_cancelling_a_free_account_sends_nothing(self):
        self.event("subscription.disable", NOW)
        self.assertEqual(self.rec.sent, [])


class ReminderTests(BillingCase):
    def pro(self, **fields):
        base = {"plan": "pro_monthly", "subscription_status": "cancelled", "subscription_expires": NOW + 2 * DAY,
                "last_payment_at": NOW - 29 * DAY}
        self.store.upsert_user("u1", {**base, **fields})

    def run_job(self, now=NOW):
        return reminders.send_expiry_reminders(self.store, now, 3)

    def test_cancelled_user_inside_the_window_is_reminded_once(self):
        self.pro()
        self.assertEqual(self.run_job()["sent"], 1)
        self.assertEqual(self.run_job()["sent"], 0)
        self.assertEqual(len(self.rec.sent), 1)
        self.assertIn("will not renew", self.rec.sent[0]["text"])

    def test_outside_the_window_and_already_expired_are_not_reminded(self):
        self.pro(subscription_expires=NOW + 10 * DAY)
        self.assertEqual(self.run_job()["sent"], 0)
        self.pro(subscription_expires=NOW - DAY)
        self.assertEqual(self.run_job()["sent"], 0)

    def test_healthy_auto_renewing_user_is_not_reminded(self):
        self.pro(subscription_status="active")
        self.assertEqual(self.run_job()["sent"], 0)

    def test_active_user_whose_renewal_failed_is_reminded(self):
        self.pro(subscription_status="active", last_payment_failed_at=NOW - DAY)
        self.assertEqual(self.run_job()["sent"], 1)
        self.assertIn("did not go through", self.rec.sent[0]["text"])

    def test_failed_send_is_retried_by_the_next_run(self):
        self.pro()
        self.rec.results = [fail("auth_failed")]
        self.assertEqual(self.run_job()["failed"], 1)
        self.assertIsNone(self.store.get_user("u1").get("expiry_reminder_for"))
        self.assertEqual(self.run_job()["sent"], 1)

    def test_renewal_rearms_the_reminder(self):
        self.pro()
        self.run_job()
        self.pro(subscription_expires=NOW + 33 * DAY)
        self.assertEqual(self.run_job(NOW + 31 * DAY)["sent"], 1)


class RetryTests(unittest.TestCase):
    def run_with(self, results, attempts=3):
        rec = Recorder(results)
        with mock.patch("utils.mailer.send_email", rec), mock.patch("utils.mailer.time.sleep") as sleep:
            result = mailer.send_email_with_retry("a@example.com", "s", "t", attempts=attempts, base_delay=2)
        return result, rec, sleep

    def test_transient_failure_then_success(self):
        result, rec, sleep = self.run_with([fail("timeout", True), fail("connection_failed", True), ok()])
        self.assertTrue(result.ok)
        self.assertEqual(len(rec.sent), 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2, 4])    # exponential backoff

    def test_gives_up_after_the_configured_attempts(self):
        result, rec, _ = self.run_with([fail("timeout", True)] * 5, attempts=3)
        self.assertFalse(result.ok)
        self.assertEqual(len(rec.sent), 3)

    def test_permanent_failures_are_never_retried(self):
        for code in ("auth_failed", "recipient_refused", "sender_refused", "tls_error", "not_configured"):
            result, rec, sleep = self.run_with([fail(code)])
            self.assertEqual(len(rec.sent), 1, code)
            sleep.assert_not_called()

    def test_success_is_not_repeated(self):
        _, rec, _ = self.run_with([ok()])
        self.assertEqual(len(rec.sent), 1)

    def test_only_4xx_smtp_replies_are_retryable(self):
        self.assertTrue(mailer._is_retryable("smtp_error", 451))
        self.assertFalse(mailer._is_retryable("smtp_error", 550))
        self.assertFalse(mailer._is_retryable("smtp_error", None))
        self.assertTrue(mailer._is_retryable("disconnected", None))

    def test_real_failures_are_classified(self):
        Config_saved = (mailer.Config.SMTP_HOST, mailer.Config.SMTP_PORT)
        # nothing listens on port 9 locally: connection refused is transient, not a config error
        with mock.patch.multiple(mailer.Config, SMTP_HOST="127.0.0.1", SMTP_PORT=9, SMTP_USERNAME="u",
                                 SMTP_PASSWORD="p", SMTP_USE_SSL=True, SMTP_USE_TLS=False,
                                 MAIL_DEFAULT_SENDER="support@getomixa.com", SMTP_TIMEOUT_SECONDS=3):
            result = mailer.send_email("a@example.com", "s", "t")
        self.assertEqual((result.ok, result.code, result.retryable), (False, "connection_failed", True))


class TemplateTests(unittest.TestCase):
    def test_user_name_is_escaped_and_text_and_html_agree(self):
        subject, text, html = email_templates.subscription_cancelled('<script>alert(1)</script> Bob', NOW)
        self.assertNotIn("<script>", html)
        self.assertIn(email_templates.format_date(NOW), text)
        self.assertIn(email_templates.format_date(NOW), html)

    def test_bad_timestamps_do_not_crash(self):
        for bad in (None, "x", float("inf"), 1e30):
            self.assertEqual(email_templates.format_date(bad), "")
        email_templates.pro_expiring(None, None, 1, False)


if __name__ == "__main__":
    unittest.main()
