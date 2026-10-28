"""
Email: configuration validation, the SMTP client (implicit SSL and STARTTLS) against an in-process fake
server, the contact form, the welcome email and password-reset delivery. Offline: no real mail server is
contacted and no credentials are used. The fake server speaks real SMTP over real TLS (a throwaway
self-signed certificate generated at test time), so smtplib's SSL/STARTTLS/auth code paths are exercised.
"""
import base64
import logging
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from email import message_from_bytes, policy
from unittest import mock

os.environ.setdefault("FLASK_ENV", "development")

from config import Config
from utils import mailer, security

PASSWORD = "S3cret-Pass-Word-XYZ"
USER = "support@getomixa.com"
_TMP = tempfile.mkdtemp(prefix="omixa-mail-test-")
CERT, KEY = os.path.join(_TMP, "cert.pem"), os.path.join(_TMP, "key.pem")


def setUpModule():
    if not shutil.which("openssl"):
        raise unittest.SkipTest("openssl not available to create a test certificate")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", KEY, "-out", CERT, "-days", "2",
         "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost"],
        check=True, capture_output=True)


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


def trusting_context(*a, **kw):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(CERT)
    return ctx


class FakeSMTP:
    """mode: "ssl" (implicit TLS) | "starttls". behaviour: "ok" | "hang" | "rcpt_refused" | "sender_refused"."""

    def __init__(self, mode="ssl", behaviour="ok"):
        self.mode, self.behaviour = mode, behaviour
        self.messages, self.auth_attempts = [], []
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(CERT, KEY)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                self._handle(conn)
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def _handle(self, conn):
        conn.settimeout(10)
        if self.mode == "ssl":
            conn = self.ctx.wrap_socket(conn, server_side=True)
        if self.behaviour == "hang":
            time.sleep(5)
            return
        buf = b""

        def send(line):
            conn.sendall(line.encode() + b"\r\n")

        def readline():
            nonlocal buf
            while b"\r\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    raise EOFError
                buf += chunk
            line, buf = buf.split(b"\r\n", 1)
            return line.decode(errors="replace")

        tls = self.mode == "ssl"
        send("220 fake ESMTP ready")
        while True:
            line = readline()
            cmd = line.split(" ", 1)[0].upper()
            if cmd == "EHLO":
                send("250-fake")
                if self.mode == "starttls" and not tls:
                    send("250-STARTTLS")
                send("250 AUTH PLAIN LOGIN")
            elif cmd == "STARTTLS":
                send("220 go ahead")
                conn = self.ctx.wrap_socket(conn, server_side=True)
                tls, buf = True, b""
            elif cmd == "AUTH":
                if not tls:
                    send("538 encryption required")
                    continue
                words = line.split(" ")
                if words[1].upper() != "PLAIN" or len(words) < 3:      # smtplib retries with LOGIN after a PLAIN failure
                    send("535 5.7.8 Authentication credentials invalid")
                    continue
                parts = base64.b64decode(words[2]).split(b"\0")
                self.auth_attempts.append((parts[1].decode(), parts[2].decode()))
                if parts[1].decode() == USER and parts[2].decode() == PASSWORD:
                    send("235 2.7.0 Authentication successful")
                else:
                    send("535 5.7.8 Authentication credentials invalid")
            elif cmd == "MAIL":
                if self.behaviour == "sender_refused":
                    send("553 5.7.1 Sender address rejected")
                else:
                    send("250 OK")
            elif cmd == "RCPT":
                send("550 5.1.1 No such user" if self.behaviour == "rcpt_refused" else "250 OK")
            elif cmd == "DATA":
                send("354 end with <CRLF>.<CRLF>")
                data = b""
                while not data.endswith(b"\r\n.\r\n"):
                    chunk = conn.recv(4096)
                    if not chunk:
                        raise EOFError
                    data += chunk
                self.messages.append(message_from_bytes(data[:-5], policy=policy.default))
                send("250 2.0.0 queued")
            elif cmd in ("RSET", "NOOP"):
                send("250 OK")
            elif cmd == "QUIT":
                send("221 bye")
                return
            else:
                send("502 not implemented")


class MailTestCase(unittest.TestCase):
    """Saves/restores the email settings and trusts the test certificate."""
    _KEYS = ("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_USE_SSL", "SMTP_USE_TLS",
             "MAIL_DEFAULT_SENDER", "SMTP_TIMEOUT_SECONDS", "CONTACT_FORM_RECIPIENT",
             "RATE_LIMIT_CONTACT_PER_MINUTE", "RATE_LIMIT_CONTACT_PER_HOUR", "RATE_LIMIT_CONTACT_GLOBAL_PER_HOUR",
             "RATE_LIMIT_RESET_EMAIL_PER_MINUTE", "RATE_LIMIT_RESET_EMAIL_PER_ADDRESS_HOUR", "RATE_LIMIT_PER_MINUTE")

    def setUp(self):
        self._saved = {k: getattr(Config, k) for k in self._KEYS}
        patcher = mock.patch("utils.mailer.ssl.create_default_context", trusting_context)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.servers = []
        security._hits.clear()
        Config.RATE_LIMIT_PER_MINUTE = 0
        Config.RATE_LIMIT_CONTACT_PER_MINUTE = 100
        Config.RATE_LIMIT_CONTACT_PER_HOUR = 1000
        Config.RATE_LIMIT_CONTACT_GLOBAL_PER_HOUR = 1000
        Config.RATE_LIMIT_RESET_EMAIL_PER_MINUTE = 100
        Config.RATE_LIMIT_RESET_EMAIL_PER_ADDRESS_HOUR = 100
        Config.CONTACT_FORM_RECIPIENT = USER
        Config.SMTP_TIMEOUT_SECONDS = 5

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(Config, k, v)
        for s in self.servers:
            s.close()
        security._hits.clear()

    def server(self, mode="ssl", behaviour="ok"):
        s = FakeSMTP(mode, behaviour)
        self.servers.append(s)
        return s

    def configure(self, srv, password=PASSWORD):
        Config.SMTP_HOST, Config.SMTP_PORT = "127.0.0.1", srv.port
        Config.SMTP_USERNAME, Config.SMTP_PASSWORD = USER, password
        Config.SMTP_USE_SSL, Config.SMTP_USE_TLS = srv.mode == "ssl", srv.mode == "starttls"
        Config.MAIL_DEFAULT_SENDER = USER


# ------------------------------------------------------------------------------------ configuration

class ConfigValidationTests(MailTestCase):
    def test_complete_ssl_config_is_valid(self):
        self.configure(self.server("ssl"))
        self.assertEqual(mailer.config_problems(), [])
        self.assertTrue(mailer.is_configured())

    def test_missing_credentials_are_named_without_values(self):
        Config.SMTP_USERNAME = Config.SMTP_PASSWORD = Config.MAIL_DEFAULT_SENDER = ""
        problems = " | ".join(mailer.config_problems())
        for name in ("SMTP_USERNAME", "SMTP_PASSWORD", "MAIL_DEFAULT_SENDER"):
            self.assertIn(name, problems)
        self.assertFalse(mailer.is_configured())
        self.assertIn("NOT configured", mailer.status_line())

    def test_ssl_and_tls_together_are_rejected(self):
        self.configure(self.server("ssl"))
        Config.SMTP_USE_SSL = Config.SMTP_USE_TLS = True
        self.assertTrue(any("both true" in p for p in mailer.config_problems()))

    def test_neither_ssl_nor_tls_is_rejected(self):
        self.configure(self.server("ssl"))
        Config.SMTP_USE_SSL = Config.SMTP_USE_TLS = False
        self.assertTrue(any("unencrypted" in p for p in mailer.config_problems()))

    def test_port_and_mode_mismatch_is_rejected(self):
        self.configure(self.server("ssl"))
        Config.SMTP_PORT, Config.SMTP_USE_SSL, Config.SMTP_USE_TLS = 465, False, True
        self.assertTrue(any("465" in p for p in mailer.config_problems()))
        Config.SMTP_PORT, Config.SMTP_USE_SSL, Config.SMTP_USE_TLS = 587, True, False
        self.assertTrue(any("587" in p for p in mailer.config_problems()))

    def test_bad_port_and_sender(self):
        self.configure(self.server("ssl"))
        Config.SMTP_PORT = 0
        Config.MAIL_DEFAULT_SENDER = "not-an-address"
        problems = " | ".join(mailer.config_problems())
        self.assertIn("SMTP_PORT", problems)
        self.assertIn("MAIL_DEFAULT_SENDER", problems)

    def test_status_line_never_contains_the_password(self):
        self.configure(self.server("ssl"))
        self.assertNotIn(PASSWORD, mailer.status_line())

    def test_address_validation(self):
        for good in ("a@b.co", "first.last+tag@sub.example.org"):
            self.assertTrue(mailer.valid_address(good), good)
        for bad in ("", "a@b", "a b@c.com", "a@b.com\r\nBcc: x@y.com", "a@b.com, c@d.com", "<a@b.com>", "x" * 300 + "@b.com", None):
            self.assertFalse(mailer.valid_address(bad), repr(bad))


# ------------------------------------------------------------------------------------ SMTP client

class SendTests(MailTestCase):
    def test_not_configured_does_not_attempt_and_reports_failure(self):
        Config.SMTP_PASSWORD = ""
        r = mailer.send_email("a@example.com", "Hi", "body")
        self.assertFalse(r.ok)
        self.assertEqual(r.code, "not_configured")

    def test_implicit_ssl_delivery(self):
        srv = self.server("ssl")
        self.configure(srv)
        r = mailer.send_email("person@example.com", "Hello", "Plain body", "<p>Html body</p>", reply_to="visitor@example.org")
        self.assertTrue(r.ok, r)
        self.assertEqual(len(srv.messages), 1)
        m = srv.messages[0]
        self.assertEqual(m["From"], USER)
        self.assertEqual(m["To"], "person@example.com")
        self.assertEqual(m["Reply-To"], "visitor@example.org")
        self.assertEqual(m["Subject"], "Hello")
        self.assertIn("Plain body", m.get_body(("plain",)).get_content())
        self.assertIn("Html body", m.get_body(("html",)).get_content())
        self.assertEqual(srv.auth_attempts, [(USER, PASSWORD)])

    def test_starttls_delivery(self):
        srv = self.server("starttls")
        self.configure(srv)
        r = mailer.send_email("person@example.com", "Hello", "Body")
        self.assertTrue(r.ok, r)
        self.assertEqual(len(srv.messages), 1)

    def test_credentials_are_never_sent_unencrypted(self):
        # STARTTLS server but the client is (mis)configured for implicit SSL: the handshake fails, nothing is sent.
        srv = self.server("starttls")
        self.configure(srv)
        Config.SMTP_USE_SSL, Config.SMTP_USE_TLS = True, False
        r = mailer.send_email("person@example.com", "Hello", "Body")
        self.assertFalse(r.ok)
        self.assertEqual(srv.auth_attempts, [])

    def test_header_injection_in_subject_is_neutralised(self):
        srv = self.server("ssl")
        self.configure(srv)
        r = mailer.send_email("person@example.com", "Hi\r\nBcc: attacker@example.com", "Body")
        self.assertTrue(r.ok, r)
        self.assertIsNone(srv.messages[0]["Bcc"])

    def test_recipient_with_newline_is_refused_before_sending(self):
        srv = self.server("ssl")
        self.configure(srv)
        r = mailer.send_email("a@example.com\r\nBcc: x@example.com", "Hi", "Body")
        self.assertEqual(r.code, "invalid_recipient")
        self.assertEqual(srv.messages, [])

    def test_wrong_password_gives_useful_error_and_never_logs_secrets(self):
        srv = self.server("ssl")
        self.configure(srv, password="wrong-Password-123")
        with self.assertLogs("omixa.mail", level="ERROR") as logs:
            r = mailer.send_email("person@example.com", "Hello", "Body")
        self.assertFalse(r.ok)
        self.assertEqual(r.code, "auth_failed")
        self.assertIn("SMTP_USERNAME", r.detail)
        blob = "\n".join(logs.output) + r.detail
        self.assertNotIn("wrong-Password-123", blob)
        self.assertNotIn(PASSWORD, blob)
        self.assertNotIn("person@example.com", blob)       # only the recipient's domain is logged
        self.assertEqual(srv.messages, [])

    def test_recipient_refused(self):
        srv = self.server("ssl", "rcpt_refused")
        self.configure(srv)
        r = mailer.send_email("nobody@example.com", "Hi", "Body")
        self.assertFalse(r.ok)
        self.assertEqual(r.code, "recipient_refused")

    def test_sender_refused(self):
        srv = self.server("ssl", "sender_refused")
        self.configure(srv)
        r = mailer.send_email("person@example.com", "Hi", "Body")
        self.assertEqual(r.code, "sender_refused")

    def test_unreachable_server_does_not_raise(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()                                           # nothing listens here any more
        self.configure(self.server("ssl"))
        Config.SMTP_PORT = port
        r = mailer.send_email("person@example.com", "Hi", "Body")
        self.assertFalse(r.ok)
        self.assertEqual(r.code, "connection_failed")

    def test_unresponsive_server_times_out(self):
        srv = self.server("ssl", "hang")
        self.configure(srv)
        Config.SMTP_TIMEOUT_SECONDS = 1
        started = time.time()
        r = mailer.send_email("person@example.com", "Hi", "Body")
        self.assertFalse(r.ok)
        self.assertIn(r.code, ("timeout", "tls_error", "disconnected", "connection_failed"))
        self.assertLess(time.time() - started, 4)

    def test_background_job_exception_is_contained(self):
        done = threading.Event()

        def boom():
            try:
                raise RuntimeError("boom")
            finally:
                done.set()
        self.assertTrue(mailer.run_in_background(boom))
        self.assertTrue(done.wait(3))


# ------------------------------------------------------------------------------------ contact form

class ContactFormTests(MailTestCase):
    def setUp(self):
        super().setUp()
        from app import create_app
        self.app = create_app()
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()

    def token(self, age=5):
        from routes import contact
        with mock.patch("routes.contact.time.time", return_value=time.time() - age):
            return contact.new_form_token()

    def payload(self, **over):
        p = {"name": "Ada Obi", "email": "ada@example.com", "subject": "Question about dates",
             "message": "How does Omixa handle day-first dates in my file?", "website": "", "token": self.token()}
        p.update(over)
        return p

    def test_page_renders_with_form_and_mailto(self):
        self.configure(self.server("ssl"))
        html = self.client.get("/contact").get_data(as_text=True)
        self.assertIn('id="contactForm"', html)
        self.assertIn("mailto:support@getomixa.com", html)
        self.assertNotIn(PASSWORD, html)
        self.assertNotIn("SMTP", html)

    def test_page_degrades_to_mailto_when_email_is_not_configured(self):
        Config.SMTP_PASSWORD = ""
        html = self.client.get("/contact").get_data(as_text=True)
        self.assertNotIn('id="contactForm"', html)
        self.assertIn("mailto:support@getomixa.com", html)

    def test_valid_submission_is_delivered_to_support_with_reply_to(self):
        srv = self.server("ssl")
        self.configure(srv)
        r = self.client.post("/api/contact", json=self.payload())
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(len(srv.messages), 1)
        m = srv.messages[0]
        self.assertEqual(m["To"], "support@getomixa.com")
        self.assertEqual(m["Reply-To"], "ada@example.com")
        self.assertIn("[Omixa contact] Question about dates", m["Subject"])
        self.assertIn("day-first dates", m.get_body(("plain",)).get_content())

    def test_user_html_is_escaped_in_the_email(self):
        srv = self.server("ssl")
        self.configure(srv)
        r = self.client.post("/api/contact", json=self.payload(message="<script>alert(1)</script> please help me"))
        self.assertEqual(r.status_code, 200)
        html = srv.messages[0].get_body(("html",)).get_content()
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_header_injection_through_form_fields(self):
        srv = self.server("ssl")
        self.configure(srv)
        r = self.client.post("/api/contact", json=self.payload(subject="Hello\r\nBcc: attacker@example.com"))
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(srv.messages[0]["Bcc"])
        r = self.client.post("/api/contact", json=self.payload(email="ada@example.com\r\nBcc: attacker@example.com"))
        self.assertEqual(r.status_code, 422)
        self.assertIn("email", r.get_json()["fields"])

    def test_validation_errors_are_field_specific(self):
        self.configure(self.server("ssl"))
        r = self.client.post("/api/contact", json=self.payload(name="", email="nope", subject="x", message="short"))
        self.assertEqual(r.status_code, 422)
        self.assertEqual(set(r.get_json()["fields"]), {"name", "email", "subject", "message"})

    def test_length_and_link_limits(self):
        self.configure(self.server("ssl"))
        r = self.client.post("/api/contact", json=self.payload(message="x" * 5001))
        self.assertIn("message", r.get_json()["fields"])
        r = self.client.post("/api/contact", json=self.payload(name="n" * 81))
        self.assertIn("name", r.get_json()["fields"])
        spam = " ".join(f"http://spam{i}.example" for i in range(5)) + " buy now please"
        r = self.client.post("/api/contact", json=self.payload(message=spam))
        self.assertIn("message", r.get_json()["fields"])

    def test_wrong_types_are_rejected_not_crashed(self):
        self.configure(self.server("ssl"))
        r = self.client.post("/api/contact", json=self.payload(name=["a"], message={"x": 1}))
        self.assertEqual(r.status_code, 422)
        r = self.client.post("/api/contact", data="not json", content_type="text/plain")
        self.assertEqual(r.status_code, 400)

    def test_honeypot_looks_like_success_but_sends_nothing(self):
        srv = self.server("ssl")
        self.configure(srv)
        r = self.client.post("/api/contact", json=self.payload(website="http://bot.example"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(srv.messages, [])

    def test_missing_forged_instant_and_stale_tokens_are_rejected(self):
        srv = self.server("ssl")
        self.configure(srv)
        for tok in ("", "forged.token.value", self.token(age=0)):
            r = self.client.post("/api/contact", json=self.payload(token=tok))
            self.assertEqual(r.status_code, 400, tok)
        stale = self.token(age=25 * 3600)
        self.assertEqual(self.client.post("/api/contact", json=self.payload(token=stale)).status_code, 400)
        self.assertEqual(srv.messages, [])

    def test_rate_limit_per_minute(self):
        srv = self.server("ssl")
        self.configure(srv)
        Config.RATE_LIMIT_CONTACT_PER_MINUTE = 2
        codes = [self.client.post("/api/contact", json=self.payload()).status_code for _ in range(4)]
        self.assertEqual(codes, [200, 200, 429, 429])
        self.assertEqual(len(srv.messages), 2)
        r = self.client.post("/api/contact", json=self.payload())
        self.assertIn("Retry-After", r.headers)

    def test_rate_limit_per_hour_and_site_wide(self):
        self.configure(self.server("ssl"))
        Config.RATE_LIMIT_CONTACT_PER_HOUR = 1
        self.assertEqual(self.client.post("/api/contact", json=self.payload()).status_code, 200)
        self.assertEqual(self.client.post("/api/contact", json=self.payload()).status_code, 429)
        security._hits.clear()
        Config.RATE_LIMIT_CONTACT_PER_HOUR = 1000
        Config.RATE_LIMIT_CONTACT_GLOBAL_PER_HOUR = 1
        self.assertEqual(self.client.post("/api/contact", json=self.payload()).status_code, 200)
        self.assertEqual(self.client.post("/api/contact", json=self.payload()).status_code, 429)

    def test_smtp_failure_is_reported_honestly_without_leaking_details(self):
        srv = self.server("ssl")
        self.configure(srv, password="wrong-Password-123")
        r = self.client.post("/api/contact", json=self.payload())
        self.assertEqual(r.status_code, 503)
        body = r.get_data(as_text=True)
        self.assertNotIn('"ok"', body)
        self.assertIn("support@getomixa.com", body)
        for secret in ("wrong-Password-123", PASSWORD, "535", "SMTP"):
            self.assertNotIn(secret, body)

    def test_not_configured_returns_503_not_success(self):
        Config.SMTP_PASSWORD = ""
        r = self.client.post("/api/contact", json=self.payload())
        self.assertEqual(r.status_code, 503)
        self.assertNotIn("ok", r.get_json())

    def test_cross_origin_post_is_blocked(self):
        self.configure(self.server("ssl"))
        r = self.client.post("/api/contact", json=self.payload(), headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)

    def test_footer_and_about_link_to_support_address(self):
        for path in ("/about", "/pricing"):
            h = self.client.get(path).get_data(as_text=True)
            self.assertIn("mailto:support@getomixa.com", h)
            self.assertIn('href="/contact"', h)


# ------------------------------------------------------------------------------------ welcome + reset

class AccountEmailTests(MailTestCase):
    def setUp(self):
        super().setUp()
        from accounts import auth, emails, store as store_mod
        from app import create_app
        self.auth, self.emails, self.store_mod = auth, emails, store_mod
        self.store = store_mod.MemoryStore()
        store_mod.set_store_for_tests(self.store)
        self.app = create_app()
        self.app.config.update(TESTING=True)
        self.client = self.app.test_client()
        self.claims = {"uid": "u1", "email": "new@example.com", "name": "Ada Obi", "provider": "password"}
        auth.set_verifier_for_tests(lambda tok: dict(self.claims))
        self.run_sync = mock.patch("utils.mailer.run_in_background", lambda fn, *a, **k: (fn(*a, **k), True)[1])
        self.run_sync.start()
        self.addCleanup(self.run_sync.stop)
        self.addCleanup(store_mod.set_store_for_tests, None)
        self.addCleanup(auth.set_verifier_for_tests, None)
        self.addCleanup(emails.set_reset_link_generator_for_tests, None)

    def sign_in(self):
        return self.client.post("/api/auth/session", json={"idToken": "x"})

    def test_welcome_email_sent_once_after_account_creation(self):
        srv = self.server("ssl")
        self.configure(srv)
        self.assertEqual(self.sign_in().status_code, 200)
        self.assertEqual(len(srv.messages), 1)
        m = srv.messages[0]
        self.assertEqual(m["To"], "new@example.com")
        self.assertEqual(m["Subject"], "Welcome to Omixa")
        self.assertIn("Hi Ada,", m.get_body(("plain",)).get_content())
        self.assertEqual(self.sign_in().status_code, 200)       # returning user: no second welcome
        self.assertEqual(len(srv.messages), 1)

    def test_no_welcome_when_sign_in_is_rejected(self):
        srv = self.server("ssl")
        self.configure(srv)
        self.auth.set_verifier_for_tests(mock.Mock(side_effect=ValueError("bad token")))
        self.assertEqual(self.sign_in().status_code, 401)
        self.assertEqual(srv.messages, [])
        self.assertIsNone(self.store.get_user("u1"))

    def test_sign_in_survives_smtp_failure(self):
        srv = self.server("ssl")
        self.configure(srv, password="wrong-Password-123")
        r = self.sign_in()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["user"]["email"], "new@example.com")
        self.assertEqual(srv.messages, [])

    def test_sign_in_survives_missing_email_configuration(self):
        Config.SMTP_PASSWORD = ""
        self.assertEqual(self.sign_in().status_code, 200)

    def test_sign_in_survives_a_crashing_mailer(self):
        self.configure(self.server("ssl"))
        with mock.patch("utils.mailer.send_email_in_background", side_effect=RuntimeError("boom")):
            self.assertEqual(self.sign_in().status_code, 200)

    # ----- password reset
    def test_reset_email_contains_the_firebase_link_for_existing_accounts_only(self):
        srv = self.server("ssl")
        self.configure(srv)
        link = "https://omixa-test.firebaseapp.com/__/auth/action?mode=resetPassword&oobCode=ABC123"

        def gen(email):
            if email == "known@example.com":
                return link
            raise self.emails.UserNotFound()
        self.emails.set_reset_link_generator_for_tests(gen)

        r1 = self.client.post("/api/auth/password-reset", json={"email": "known@example.com"})
        r2 = self.client.post("/api/auth/password-reset", json={"email": "stranger@example.com"})
        self.assertEqual((r1.status_code, r2.status_code), (200, 200))
        self.assertEqual(r1.get_json(), r2.get_json())           # identical answers: no account enumeration
        self.assertEqual(len(srv.messages), 1)
        m = srv.messages[0]
        self.assertEqual(m["To"], "known@example.com")
        self.assertEqual(m["Subject"], "Reset your Omixa password")
        self.assertIn(link, m.get_body(("plain",)).get_content())

    def test_reset_logs_never_contain_the_link_or_address(self):
        srv = self.server("ssl")
        self.configure(srv)
        link = "https://x.firebaseapp.com/__/auth/action?oobCode=TOPSECRETCODE"
        self.emails.set_reset_link_generator_for_tests(lambda e: link)
        logging.getLogger().setLevel(logging.DEBUG)
        with self.assertLogs(level="DEBUG") as logs:
            self.client.post("/api/auth/password-reset", json={"email": "known@example.com"})
        blob = "\n".join(logs.output)
        self.assertNotIn("TOPSECRETCODE", blob)
        self.assertNotIn("known@example.com", blob)
        self.assertNotIn(PASSWORD, blob)

    def test_reset_smtp_failure_is_logged_not_claimed_and_does_not_crash(self):
        srv = self.server("ssl")
        self.configure(srv, password="wrong-Password-123")
        self.emails.set_reset_link_generator_for_tests(lambda e: "https://x.test/?oobCode=1")
        with self.assertLogs("omixa.account_mail", level="ERROR") as logs:
            r = self.client.post("/api/auth/password-reset", json={"email": "known@example.com"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("NOT delivered", "\n".join(logs.output))
        self.assertEqual(srv.messages, [])

    def test_reset_falls_back_signal_when_email_is_not_configured(self):
        Config.SMTP_PASSWORD = ""
        r = self.client.post("/api/auth/password-reset", json={"email": "known@example.com"})
        self.assertEqual(r.status_code, 503)
        self.assertTrue(r.get_json()["fallback"])

    def test_reset_rejects_malformed_addresses(self):
        self.configure(self.server("ssl"))
        for bad in ("", "nope", "a@b.com\r\nBcc: x@y.com", None, 5):
            r = self.client.post("/api/auth/password-reset", json={"email": bad})
            self.assertEqual(r.status_code, 400, repr(bad))

    def test_reset_rate_limits_per_address_and_ip(self):
        srv = self.server("ssl")
        self.configure(srv)
        self.emails.set_reset_link_generator_for_tests(lambda e: "https://x.test/?oobCode=1")
        Config.RATE_LIMIT_RESET_EMAIL_PER_ADDRESS_HOUR = 2
        codes = [self.client.post("/api/auth/password-reset", json={"email": "Known@Example.com"}).status_code for _ in range(4)]
        self.assertEqual(codes, [200, 200, 429, 429])            # case-insensitive per-address cap
        self.assertEqual(len(srv.messages), 2)
        self.assertEqual(self.client.post("/api/auth/password-reset", json={"email": "other@example.com"}).status_code, 200)
        security._hits.clear()
        Config.RATE_LIMIT_RESET_EMAIL_PER_ADDRESS_HOUR = 100
        Config.RATE_LIMIT_RESET_EMAIL_PER_MINUTE = 1
        self.assertEqual(self.client.post("/api/auth/password-reset", json={"email": "a1@example.com"}).status_code, 200)
        self.assertEqual(self.client.post("/api/auth/password-reset", json={"email": "a2@example.com"}).status_code, 429)


if __name__ == "__main__":
    unittest.main()
