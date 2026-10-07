"""
Who is Pro, decided ONLY from the server-side subscription record (never from anything the
browser sends). States: free, pending, active, cancelled (paid up, will not renew), expired.
"""

from __future__ import annotations

import functools
import time

from flask import jsonify

from config import Config

FREE, PRO_MONTHLY, PRO_ANNUAL = "free", "pro_monthly", "pro_annual"
PLAN_DAYS = {PRO_MONTHLY: 31, PRO_ANNUAL: 366}


def effective(user: dict | None, now: float | None = None) -> dict:
    """Plan state as of `now`. Access is time-based, so an expired subscription loses Pro
    on the very next request with no background job needed."""
    now = now or time.time()
    user = user or {}
    plan = user.get("plan") or FREE
    status = user.get("subscription_status") or FREE
    expires = user.get("subscription_expires")
    is_pro = plan in PLAN_DAYS and status in ("active", "cancelled") and bool(expires) and expires > now
    if plan in PLAN_DAYS and status in ("active", "cancelled") and not is_pro:
        status = "expired"
    if not is_pro and status not in ("pending", "expired"):
        status = FREE if plan == FREE else status
    return {
        "plan": plan if is_pro else FREE,
        "billing_plan": plan,
        "status": status,
        "is_pro": is_pro,
        "start": user.get("subscription_start"),
        "expires": expires,
        "renews": bool(is_pro and status == "active"),
        "payment_failed": bool(user.get("last_payment_failed_at") and user.get("last_payment_failed_at") > (user.get("last_payment_at") or 0)),
    }


def max_upload_bytes(is_pro: bool) -> int:
    return (Config.PRO_MAX_UPLOAD_MB if is_pro else Config.FREE_MAX_UPLOAD_MB) * 1024 * 1024


def pro_required(view):
    """Server-side gate. 401 = sign in, 402 = needs Pro, 503 = accounts not available."""
    from accounts.auth import current_user_record

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        user, err = current_user_record()
        if err:
            return err
        if not effective(user)["is_pro"]:
            return jsonify({"error": "This feature is available with Omixa Pro.", "upgrade_required": True}), 402
        return view(*args, **kwargs)
    return wrapped
