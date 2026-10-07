import json
import logging
import uuid

from flask import Blueprint, jsonify, redirect, request, url_for

from accounts import auth, paystack
from accounts.entitlements import PRO_ANNUAL, PRO_MONTHLY, effective
from accounts.store import get_store
from config import Config

billing_bp = Blueprint("billing", __name__)
logger = logging.getLogger("omixa.billing")
_client = paystack.PaystackClient()


def set_client_for_tests(client):
    global _client
    _client = client


def _base_url():
    return Config.PUBLIC_BASE_URL or request.host_url.rstrip("/")


@billing_bp.get("/plans")
def plans():
    return jsonify({
        "free": {"max_upload_mb": Config.FREE_MAX_UPLOAD_MB},
        "pro_monthly": {"usd": Config.PRICE_USD_MONTHLY, "ngn": Config.PRICE_NGN_MONTHLY},
        "pro_annual": {"usd": Config.PRICE_USD_ANNUAL, "ngn": Config.PRICE_NGN_ANNUAL,
                       "saves_usd": round(Config.PRICE_USD_MONTHLY * 12 - Config.PRICE_USD_ANNUAL, 2)},
        "payments_available": paystack.configured(),
    }), 200


@billing_bp.post("/checkout")
def checkout():
    user, err = auth.current_user_record()
    if err:
        return err
    if not paystack.configured():
        return jsonify({"error": "Payments are not available yet."}), 503
    plan = (request.get_json(silent=True) or {}).get("plan")
    if plan not in (PRO_MONTHLY, PRO_ANNUAL):
        return jsonify({"error": "Choose monthly or annual."}), 400
    if not user.get("email"):
        return jsonify({"error": "Your account needs an email address to subscribe."}), 400
    reference = "omx_" + uuid.uuid4().hex
    try:
        data = _client.initialize(email=user["email"], plan=plan, uid=user["uid"], reference=reference,
                                  callback_url=_base_url() + url_for("billing.checkout_return"))
    except Exception:
        logger.exception("Paystack initialize failed")
        return jsonify({"error": "Could not start checkout. Please try again."}), 502
    store = get_store()
    store.put_payment(reference, {"uid": user["uid"], "plan": plan, "status": "initiated"})
    if not effective(user)["is_pro"]:
        store.upsert_user(user["uid"], {"subscription_status": "pending"})
    return jsonify({"authorization_url": data["authorization_url"], "reference": reference}), 200


@billing_bp.get("/return")
def checkout_return():
    """Paystack sends the browser here after payment. We never trust the query string: the
    transaction is re-verified with Paystack and must belong to the signed-in user."""
    reference = (request.args.get("reference") or request.args.get("trxref") or "").strip()
    uid = auth.current_uid()
    if not reference or not uid:
        return redirect("/dashboard?payment=unknown")
    store = get_store()
    if store is None:
        return redirect("/dashboard?payment=unavailable")
    record = store.get_payment(reference)
    if not record or record.get("uid") != uid:
        return redirect("/dashboard?payment=unknown")
    try:
        data = _client.verify(reference)
    except Exception:
        logger.exception("Paystack verify failed")
        return redirect("/dashboard?payment=pending")
    if (data.get("metadata") or {}).get("uid") != uid:
        return redirect("/dashboard?payment=unknown")
    if data.get("status") == "success":
        paystack.apply_charge_success(store, data)
        return redirect("/dashboard?payment=success")
    if data.get("status") in ("failed", "abandoned"):
        store.upsert_user(uid, {"last_payment_failed_at": __import__("time").time()})
        user = store.get_user(uid) or {}
        if user.get("subscription_status") == "pending":
            store.upsert_user(uid, {"subscription_status": "free" if not user.get("subscription_expires") else "expired"})
        return redirect("/dashboard?payment=failed")
    return redirect("/dashboard?payment=pending")


@billing_bp.post("/webhook")
def webhook():
    raw = request.get_data()
    if not paystack.verify_signature(raw, request.headers.get("x-paystack-signature")):
        return jsonify({"error": "bad signature"}), 401
    store = get_store()
    if store is None:
        return jsonify({"error": "unavailable"}), 503  # Paystack will retry
    try:
        outcome = paystack.handle_event(store, json.loads(raw))
    except Exception:
        logger.exception("Webhook processing failed")
        return jsonify({"error": "processing failed"}), 500  # Paystack will retry
    return jsonify({"status": "ok", "outcome": outcome}), 200


@billing_bp.post("/manage")
def manage():
    user, err = auth.current_user_record()
    if err:
        return err
    code = user.get("paystack_subscription_code")
    if not code or not paystack.configured():
        return jsonify({"error": "No active subscription to manage."}), 404
    try:
        data = _client.manage_link(code)
    except Exception:
        return jsonify({"error": "Could not open subscription management."}), 502
    return jsonify({"url": data.get("link")}), 200
