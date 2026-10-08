"""
Paystack subscriptions. Secret key stays on the server. A user becomes Pro ONLY through:
  1. a webhook whose x-paystack-signature (HMAC-SHA512 of the raw body) checks out, or
  2. the redirect handler re-verifying the transaction with Paystack's API server-to-server.
Both end in apply_charge_success(), which is idempotent per transaction reference.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time

import requests

from accounts.entitlements import PLAN_DAYS, PRO_ANNUAL, PRO_MONTHLY
from config import Config

logger = logging.getLogger("omixa.paystack")
API = "https://api.paystack.co"


def plan_codes() -> dict[str, str]:
    return {PRO_MONTHLY: Config.PAYSTACK_PLAN_MONTHLY, PRO_ANNUAL: Config.PAYSTACK_PLAN_ANNUAL}


def plan_for_code(code: str | None) -> str | None:
    for plan, c in plan_codes().items():
        if c and code and c == code:
            return plan
    return None


def configured() -> bool:
    return bool(Config.PAYSTACK_SECRET_KEY and Config.PAYSTACK_PLAN_MONTHLY and Config.PAYSTACK_PLAN_ANNUAL)


def verify_signature(raw_body: bytes, signature: str | None) -> bool:
    if not Config.PAYSTACK_SECRET_KEY or not signature:
        return False
    expected = hmac.new(Config.PAYSTACK_SECRET_KEY.encode(), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature.strip().lower())


class PaystackClient:
    def __init__(self, http=requests):
        self.http = http

    def _headers(self):
        return {"Authorization": f"Bearer {Config.PAYSTACK_SECRET_KEY}", "Content-Type": "application/json"}

    def _call(self, method, path, **kw):
        resp = self.http.request(method, API + path, headers=self._headers(), timeout=15, **kw)
        body = resp.json()
        if resp.status_code >= 400 or not body.get("status"):
            raise RuntimeError(body.get("message") or f"Paystack error {resp.status_code}")
        return body["data"]

    def initialize(self, *, email, plan, uid, callback_url, reference=None):
        amount_ngn = Config.PRICE_NGN_MONTHLY if plan == PRO_MONTHLY else Config.PRICE_NGN_ANNUAL
        payload = {
            "email": email,
            "amount": int(amount_ngn) * 100,  # ignored by Paystack when a plan code is given; kept as a safeguard
            "currency": "NGN",
            "plan": plan_codes()[plan],
            "callback_url": callback_url,
            "metadata": {"uid": uid, "plan": plan, "omixa": True},
        }
        if reference:
            payload["reference"] = reference
        return self._call("POST", "/transaction/initialize", json=payload)

    def verify(self, reference):
        return self._call("GET", f"/transaction/verify/{reference}")

    def manage_link(self, subscription_code):
        return self._call("GET", f"/subscription/{subscription_code}/manage/link")


def _resolve_plan(data: dict) -> str | None:
    plan_obj = data.get("plan_object") or data.get("plan") or {}
    code = plan_obj.get("plan_code") if isinstance(plan_obj, dict) else None
    plan = plan_for_code(code)
    if plan:
        return plan
    meta = data.get("metadata") or {}
    if isinstance(meta, dict) and meta.get("omixa") and meta.get("plan") in PLAN_DAYS:
        return meta["plan"]
    return None


def _resolve_user(store, data: dict):
    meta = data.get("metadata") or {}
    uid = meta.get("uid") if isinstance(meta, dict) else None
    if uid:
        user = store.get_user(uid)
        if user:
            return user
    customer = data.get("customer") or {}
    return store.find_user("paystack_customer_code", customer.get("customer_code"))


def apply_charge_success(store, data: dict, now: float | None = None) -> str:
    """Grants / extends Pro for a successful Paystack charge. Returns a short outcome string."""
    now = now or time.time()
    if data.get("status") != "success":
        return "ignored: not successful"
    reference = data.get("reference")
    plan = _resolve_plan(data)
    if not reference or not plan:
        return "ignored: not an Omixa subscription charge"
    user = _resolve_user(store, data)
    if not user:
        logger.warning("Paystack charge %s: no matching user", reference)
        return "ignored: unknown user"
    uid = user["uid"]
    customer = data.get("customer") or {}

    def compute(current: dict) -> dict:
        # Evaluated against the user record AS READ INSIDE the transaction, so concurrent charges
        # for the same user cannot both extend from a stale expiry.
        current_exp = current.get("subscription_expires") or 0
        still_active = current.get("subscription_status") in ("active", "cancelled") and current_exp > now
        base = max(now, current_exp) if still_active else now
        fields = {
            "plan": plan,
            "subscription_status": "active",
            "subscription_expires": base + PLAN_DAYS[plan] * 86400,
            "last_payment_at": now,
            "paystack_customer_code": customer.get("customer_code") or current.get("paystack_customer_code"),
        }
        if not still_active or not current.get("subscription_start"):
            fields["subscription_start"] = now
        return fields

    meta = {"uid": uid, "plan": plan, "amount": data.get("amount"), "currency": data.get("currency"), "paid_at": now}
    if hasattr(store, "grant_payment"):
        granted = store.grant_payment(reference, uid, compute, meta)
        if granted is None:
            return "duplicate"
    else:  # legacy stores without atomic grant (kept for test doubles)
        if not store.claim_payment(reference):
            return "duplicate"
        store.upsert_user(uid, compute(user))
        store.put_payment(reference, meta)
    try:  # append-only audit trail; never blocks the grant
        import db as metadata_db
        metadata_db.audit("paystack", "payment.granted", reference, meta={"plan": plan, "uid": uid})
    except Exception:
        logger.exception("audit write failed for payment %s", reference)
    return "activated"


def handle_event(store, event: dict, now: float | None = None) -> str:
    now = now or time.time()
    kind, data = event.get("event"), event.get("data") or {}
    if kind == "charge.success":
        return apply_charge_success(store, data, now)

    user = _resolve_user(store, data) or store.find_user("paystack_subscription_code", data.get("subscription_code"))
    if not user:
        return f"ignored: {kind}"
    uid = user["uid"]
    if kind == "subscription.create":
        store.upsert_user(uid, {"paystack_subscription_code": data.get("subscription_code"),
                                "paystack_customer_code": (data.get("customer") or {}).get("customer_code") or user.get("paystack_customer_code")})
        return "subscription recorded"
    if kind in ("subscription.disable", "subscription.not_renew"):
        if user.get("subscription_status") == "active":
            store.upsert_user(uid, {"subscription_status": "cancelled", "cancelled_at": now})
        return "cancelled"
    if kind in ("invoice.payment_failed", "charge.failed"):
        store.upsert_user(uid, {"last_payment_failed_at": now})
        return "payment failure recorded"
    return f"ignored: {kind}"
