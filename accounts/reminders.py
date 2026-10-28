"""
"Pro is ending soon" reminders. Run once a day by scripts/send_expiry_reminders.py (a Railway cron service);
nothing in the web process calls this.

Who is reminded (a reminder is only useful when access will really lapse):
  * status "cancelled": the subscription will not renew, so Pro ends on subscription_expires.
  * status "active" AND the latest payment failed after the latest successful one: the renewal is at risk.
  Healthy auto-renewing subscribers are never emailed, so the reminder stays rare and meaningful.

Idempotent: a reminder is recorded in `expiry_reminder_for` (the expiry it was sent for) only after the SMTP
server accepted it, so a failed send is retried on the next run and a successful one is never repeated. A
renewal moves the expiry, which re-arms the reminder for the next period.
"""

from __future__ import annotations

import logging
import math
import time

from accounts import emails
from accounts.entitlements import PLAN_DAYS
from config import Config
from utils import mailer

logger = logging.getLogger("omixa.reminders")


def _eligible(user: dict, now: float, horizon: float):
    """(expires, will_renew) when this user should be reminded, else None."""
    expires = user.get("subscription_expires") or 0
    status = user.get("subscription_status")
    if user.get("plan") not in PLAN_DAYS or status not in ("active", "cancelled"):
        return None
    if not (now < expires <= horizon) or user.get("expiry_reminder_for") == expires:
        return None
    if status == "cancelled":
        return expires, False
    failed_at, paid_at = user.get("last_payment_failed_at") or 0, user.get("last_payment_at") or 0
    return (expires, True) if failed_at > paid_at else None


def send_expiry_reminders(store, now: float | None = None, days: int | None = None) -> dict:
    now = now or time.time()
    days = days or Config.MAIL_EXPIRY_REMINDER_DAYS
    horizon = now + days * 86400
    counts = {"sent": 0, "failed": 0, "skipped": 0}
    for user in store.users_expiring_between(now, horizon):
        due = _eligible(user, now, horizon)
        if due is None:
            counts["skipped"] += 1
            continue
        expires, will_renew = due
        days_left = max(1, math.ceil((expires - now) / 86400))
        result = emails.send_pro_expiring(user, expires, days_left, will_renew)
        if result.ok:
            store.upsert_user(user["uid"], {"expiry_reminder_for": expires})
            counts["sent"] += 1
        else:
            counts["failed"] += 1
            logger.error("expiry reminder not sent (%s)", result.code)
            if result.code in ("not_configured", "auth_failed"):
                break      # every remaining send would fail the same way
    return counts
