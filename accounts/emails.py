"""
Account emails, wired into the EXISTING Firebase-based authentication (accounts/auth.py). There is no second
auth system here:

  * Welcome email: sent once, by auth.sign_in, when the account record is first created.
  * Password reset: Firebase owns the reset token (secure, expiring, single-use). We ask Firebase's Admin SDK
    for a reset link and deliver it from support@getomixa.com instead of Firebase's default sender.
    Omixa never sees, stores or emails a password.

Everything runs off the request thread and swallows its own failures: a mail outage can never break sign-in
or any unrelated operation, and no email is reported as sent unless the SMTP server accepted it.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Callable, Optional

from utils import email_templates, mailer

logger = logging.getLogger("omixa.account_mail")

_link_generator: Optional[Callable[[str], str]] = None


def set_reset_link_generator_for_tests(fn):
    global _link_generator
    _link_generator = fn


class UserNotFound(Exception):
    pass


def _generate_reset_link(email: str) -> str:
    """Single-use, expiring link from Firebase. Raises UserNotFound when there is no such account."""
    if _link_generator is not None:
        return _link_generator(email)
    from firebase_admin import auth as fb_auth
    from accounts import store as _s
    if _s.get_store() is None:          # make sure the Firebase app is initialised
        raise RuntimeError("accounts unavailable")
    try:
        return fb_auth.generate_password_reset_link(email)
    except fb_auth.UserNotFoundError as exc:
        raise UserNotFound() from exc


# ------------------------------------------------------------------ welcome

def send_welcome(email: Optional[str], name: Optional[str]) -> bool:
    """Queue the welcome email for a just-created account. Returns True if queued (not 'delivered')."""
    try:
        if not email or not mailer.valid_address(email):
            return False
        if not mailer.is_configured():
            logger.info("welcome email skipped: email is not configured")
            return False
        subject, text, html = email_templates.welcome(name)
        return mailer.send_email_in_background(email, subject, text, html)
    except Exception as exc:
        logger.error("welcome email could not be queued: %s", type(exc).__name__)
        return False


# ------------------------------------------------------------------ billing

def _queue(user: Optional[dict], build, *args, **kwargs) -> bool:
    """Queue one billing email for `user` (a user record). Returns True if queued, never raises: a mail problem
    must not affect payment processing, so Paystack never sees a failure because of email."""
    try:
        email = (user or {}).get("email")
        if not email or not mailer.valid_address(email):
            return False
        if not mailer.is_configured():
            logger.info("billing email skipped: email is not configured")
            return False
        subject, text, html = build((user or {}).get("name"), *args, **kwargs)
        return mailer.send_email_in_background(email, subject, text, html)
    except Exception as exc:
        logger.error("billing email could not be queued: %s", type(exc).__name__)
        return False


def amount_text(amount, currency) -> str:
    """Paystack amounts are in the smallest unit (kobo, cents). Empty string when unknown."""
    try:
        return f"{str(currency or '').upper()} {int(amount) / 100:,.2f}".strip()
    except (TypeError, ValueError):
        return ""


def send_pro_activated(user, plan, expires, renewed: bool, amount=None, currency=None) -> bool:
    return _queue(user, email_templates.pro_activated, plan, expires, renewed, amount_text(amount, currency))


def send_payment_failed(user, access_until=None) -> bool:
    return _queue(user, email_templates.payment_failed, access_until)


def send_subscription_cancelled(user, expires) -> bool:
    return _queue(user, email_templates.subscription_cancelled, expires)


def send_pro_expiring(user, expires, days_left: int, will_renew: bool) -> "mailer.SendResult":
    """Synchronous (with retry) because the reminder job must know whether it was accepted before it records
    the reminder as sent. Never raises."""
    try:
        email = (user or {}).get("email")
        if not email or not mailer.valid_address(email):
            return mailer.SendResult(False, "invalid_recipient", "no usable email address")
        subject, text, html = email_templates.pro_expiring(user.get("name"), expires, days_left, will_renew)
        return mailer.send_email_with_retry(email, subject, text, html)
    except Exception as exc:
        logger.error("expiry reminder could not be built: %s", type(exc).__name__)
        return mailer.SendResult(False, "build_failed", "could not build the reminder")


# ------------------------------------------------------------------ password reset

def address_key(email: str) -> str:
    """Stable, non-reversible key for per-address rate limits (the address itself is not kept)."""
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:32]


def _deliver_reset(email: str) -> None:
    try:
        link = _generate_reset_link(email)
    except UserNotFound:
        logger.info("password reset requested for an address with no account")   # silent to the requester
        return
    except Exception as exc:
        logger.error("password reset link could not be generated: %s", type(exc).__name__)
        return
    subject, text, html = email_templates.password_reset(link)
    result = mailer.send_email_with_retry(email, subject, text, html)
    if not result.ok:
        logger.error("password reset email was NOT delivered (%s)", result.code)


def request_password_reset(email: str) -> bool:
    """Queue the reset flow. The caller answers every valid request identically, so an attacker cannot learn
    which addresses have accounts. Returns False only when the work could not even be queued."""
    return mailer.run_in_background(_deliver_reset, email)
