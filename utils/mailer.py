"""
Outbound email over SMTP (Namecheap Private Email by default).

Design rules
  * Configuration comes ONLY from environment variables (config.Config). Nothing secret is in code,
    templates or JavaScript.
  * Sending never raises into the caller: every path returns a SendResult. A flaky mail server can
    therefore never crash a request or break an unrelated user operation.
  * A SendResult is `ok` only after the SMTP server ACCEPTED the message. Callers must never tell a
    user an email was sent unless result.ok is True.
  * Logs carry the failure class, the SMTP status code, a short server reason and the recipient's
    DOMAIN. They never contain the password, message bodies, tokens or full recipient addresses.
  * Exactly one of implicit SSL (port 465) or STARTTLS (port 587) is used. Both enabled, or neither,
    is a configuration error (we refuse to send credentials in plaintext).
"""

from __future__ import annotations

import logging
import re
import smtplib
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

from config import Config

logger = logging.getLogger("omixa.mail")

_ADDRESS = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class SendResult:
    ok: bool
    code: str = ""          # machine-readable: "sent", "not_configured", "auth_failed", "timeout", ...
    detail: str = ""        # safe, human-readable diagnostic (never contains secrets)
    retryable: bool = False  # True only for failures that can plausibly succeed on a later attempt

    def __bool__(self) -> bool:
        return self.ok


# ----------------------------------------------------------------------------- validation

def valid_address(value: str) -> bool:
    """One plain address: no display name, no CR/LF/NUL, no list, sane length."""
    if not isinstance(value, str) or not value or len(value) > 254:
        return False
    if _CONTROL.search(value) or any(c in value for c in " ,;<>\"()[]\\"):
        return False
    local = value.rsplit("@", 1)[0]
    return len(local) <= 64 and bool(_ADDRESS.match(value))


def header_safe(value: str, limit: int = 200) -> str:
    """Single-line header text: control characters (including CR/LF, the header-injection vector) removed."""
    return _CONTROL.sub(" ", str(value or "")).strip()[:limit]


def config_problems() -> list[str]:
    """Names of what is wrong with the email configuration. Variable NAMES only, never values."""
    problems: list[str] = []
    if not Config.SMTP_HOST:
        problems.append("SMTP_HOST is not set")
    if not Config.SMTP_PORT or not (0 < Config.SMTP_PORT < 65536):
        problems.append("SMTP_PORT is missing or not a valid port number")
    if not Config.SMTP_USERNAME:
        problems.append("SMTP_USERNAME is not set")
    if not Config.SMTP_PASSWORD:
        problems.append("SMTP_PASSWORD is not set")
    if not Config.MAIL_DEFAULT_SENDER:
        problems.append("MAIL_DEFAULT_SENDER is not set")
    elif not valid_address(parseaddr(Config.MAIL_DEFAULT_SENDER)[1]):
        problems.append("MAIL_DEFAULT_SENDER is not a valid email address")
    ssl_on, tls_on = Config.SMTP_USE_SSL, Config.SMTP_USE_TLS
    if ssl_on and tls_on:
        problems.append("SMTP_USE_SSL and SMTP_USE_TLS are both true; enable exactly one "
                        "(465 -> SSL=true/TLS=false, 587 -> SSL=false/TLS=true)")
    elif not ssl_on and not tls_on:
        problems.append("neither SMTP_USE_SSL nor SMTP_USE_TLS is true; refusing to send credentials unencrypted")
    elif Config.SMTP_PORT == 465 and not ssl_on:
        problems.append("SMTP_PORT=465 needs SMTP_USE_SSL=true and SMTP_USE_TLS=false")
    elif Config.SMTP_PORT == 587 and not tls_on:
        problems.append("SMTP_PORT=587 needs SMTP_USE_TLS=true and SMTP_USE_SSL=false")
    return problems


def is_configured() -> bool:
    return not config_problems()


def status_line() -> str:
    """One safe line for the startup log."""
    problems = config_problems()
    if problems:
        return "Email is NOT configured (" + "; ".join(problems) + "). Contact form and automated emails are disabled."
    mode = "implicit SSL" if Config.SMTP_USE_SSL else "STARTTLS"
    return f"Email configured: {Config.SMTP_HOST}:{Config.SMTP_PORT} ({mode}), sender {_domain(Config.MAIL_DEFAULT_SENDER)}"


# ----------------------------------------------------------------------------- sending

def _domain(address: str) -> str:
    addr = parseaddr(address or "")[1]
    return addr.rsplit("@", 1)[-1] if "@" in addr else "?"


def _scrub(text: str) -> str:
    """Server replies are short, but make sure our password can never reach a log line."""
    text = _CONTROL.sub(" ", str(text or ""))
    pw = Config.SMTP_PASSWORD
    if pw and pw in text:
        text = text.replace(pw, "***")
    return text.strip()[:160]


# Worth retrying: the network or server was briefly unavailable. NOT retried: bad credentials, bad TLS settings,
# refused sender/recipient, invalid input (a retry cannot fix these and could lock the mailbox out).
_RETRYABLE_CODES = {"timeout", "connection_failed", "disconnected"}


def _is_retryable(code: str, smtp_code) -> bool:
    if code in _RETRYABLE_CODES:
        return True
    return code == "smtp_error" and isinstance(smtp_code, int) and 400 <= smtp_code < 500


def _fail(code: str, detail: str, to: str, exc: BaseException | None = None, smtp_code=None) -> SendResult:
    extra = f" smtp_code={smtp_code}" if smtp_code else ""
    logger.error("email send failed: %s (%s)%s to_domain=%s%s", code, detail,
                 f" exc={type(exc).__name__}" if exc is not None else "", _domain(to), extra)
    return SendResult(False, code, detail, _is_retryable(code, smtp_code))


def build_message(to: str, subject: str, text: str, html: str | None = None, reply_to: str | None = None) -> EmailMessage:
    sender = Config.MAIL_DEFAULT_SENDER
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = header_safe(subject, 250)
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=_domain(sender) if _domain(sender) != "?" else None)
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


def send_email(to: str, subject: str, text: str, html: str | None = None, reply_to: str | None = None) -> SendResult:
    """Send one message. Blocking (bounded by SMTP_TIMEOUT_SECONDS); never raises."""
    try:
        problems = config_problems()
        if problems:
            logger.warning("email not sent: not configured (%s)", "; ".join(problems))
            return SendResult(False, "not_configured", "Email is not configured on this server.")
        if not valid_address(to):
            return _fail("invalid_recipient", "recipient address is not valid", to)
        if reply_to is not None and not valid_address(reply_to):
            reply_to = None
        msg = build_message(to, subject, text, html, reply_to)
    except Exception as exc:  # building the message must never take a request down either
        return _fail("build_failed", "could not build the message", to or "", exc)

    timeout = Config.SMTP_TIMEOUT_SECONDS
    try:
        context = ssl.create_default_context()
        if Config.SMTP_USE_SSL:
            server = smtplib.SMTP_SSL(Config.SMTP_HOST, Config.SMTP_PORT, timeout=timeout, context=context)
        else:
            server = smtplib.SMTP(Config.SMTP_HOST, Config.SMTP_PORT, timeout=timeout)
        with server:
            server.ehlo()
            if Config.SMTP_USE_TLS and not Config.SMTP_USE_SSL:
                server.starttls(context=context)
                server.ehlo()
            server.login(Config.SMTP_USERNAME, Config.SMTP_PASSWORD)
            refused = server.send_message(msg)
        if refused:
            return _fail("recipient_refused", "the mail server refused the recipient", to)
    except smtplib.SMTPAuthenticationError as exc:
        return _fail("auth_failed", "SMTP login was rejected: check SMTP_USERNAME and SMTP_PASSWORD",
                     to, exc, exc.smtp_code)
    except smtplib.SMTPRecipientsRefused as exc:
        return _fail("recipient_refused", "the mail server refused the recipient", to, exc)
    except smtplib.SMTPSenderRefused as exc:
        return _fail("sender_refused", "the mail server refused the sender: MAIL_DEFAULT_SENDER must be an address "
                     "this mailbox may send as", to, exc, exc.smtp_code)
    except smtplib.SMTPConnectError as exc:
        return _fail("connection_failed", "the mail server refused the connection", to, exc, exc.smtp_code)
    except (socket.timeout, TimeoutError) as exc:
        return _fail("timeout", f"the mail server did not answer within {timeout}s", to, exc)
    except ssl.SSLError as exc:
        return _fail("tls_error", "TLS negotiation failed: check SMTP_PORT against SMTP_USE_SSL / SMTP_USE_TLS", to, exc)
    except smtplib.SMTPServerDisconnected as exc:
        return _fail("disconnected", "the mail server closed the connection unexpectedly", to, exc)
    except smtplib.SMTPResponseException as exc:
        return _fail("smtp_error", _scrub(exc.smtp_error.decode("utf-8", "replace") if isinstance(exc.smtp_error, bytes)
                                          else exc.smtp_error), to, exc, exc.smtp_code)
    except smtplib.SMTPException as exc:
        return _fail("smtp_error", "SMTP error", to, exc)
    except OSError as exc:  # DNS failure, network unreachable, connection reset
        return _fail("connection_failed", "could not reach the mail server (network or DNS)", to, exc)
    except Exception as exc:  # last resort: still never crash the caller
        return _fail("unexpected_error", "unexpected error while sending", to, exc)

    logger.info("email sent to_domain=%s", _domain(to))
    return SendResult(True, "sent", "")


# ----------------------------------------------------------------------------- background sending

_MAX_PENDING = 50
_pending = threading.BoundedSemaphore(_MAX_PENDING)
_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="omixa-mail")
        return _executor


def run_in_background(fn, *args, **kwargs) -> bool:
    """Run fn off the request thread (bounded: beyond _MAX_PENDING queued jobs the work is dropped and logged
    rather than piling up). Returns False when dropped. fn's own exceptions are caught and logged."""
    if not _pending.acquire(blocking=False):
        logger.error("email queue is full; dropping a background email job")
        return False

    def _run():
        try:
            fn(*args, **kwargs)
        except Exception as exc:
            logger.error("background email job failed: %s", type(exc).__name__)
        finally:
            _pending.release()

    try:
        _get_executor().submit(_run)
    except Exception as exc:  # interpreter shutting down
        _pending.release()
        logger.error("could not schedule background email job: %s", type(exc).__name__)
        return False
    return True


def send_email_with_retry(to: str, subject: str, text: str, html: str | None = None, reply_to: str | None = None,
                          *, attempts: int | None = None, base_delay: float | None = None) -> SendResult:
    """send_email, retried on transient failures with exponential backoff. Blocking: only for work the user is
    not waiting on (background jobs, cron). Returns the last result; never raises."""
    attempts = Config.MAIL_RETRY_ATTEMPTS if attempts is None else max(1, attempts)
    delay = Config.MAIL_RETRY_BASE_SECONDS if base_delay is None else base_delay
    result = send_email(to, subject, text, html, reply_to)
    for attempt in range(2, attempts + 1):
        if result.ok or not result.retryable:
            break
        logger.warning("email attempt %d/%d failed (%s); retrying in %ss to_domain=%s",
                       attempt - 1, attempts, result.code, delay, _domain(to))
        time.sleep(delay)
        delay *= 2
        result = send_email(to, subject, text, html, reply_to)
    if not result.ok and result.retryable:
        logger.error("email gave up after %d attempts (%s) to_domain=%s", attempts, result.code, _domain(to))
    return result


def send_email_in_background(to: str, subject: str, text: str, html: str | None = None, reply_to: str | None = None) -> bool:
    """Fire-and-forget send for emails the user is not waiting on (welcome, billing). Transient failures are
    retried (send_email_with_retry). The outcome is logged; the caller must not report it as delivered."""
    return run_in_background(send_email_with_retry, to, subject, text, html, reply_to)
