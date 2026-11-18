"""
Feedback / reviews.

  GET  /feedback       the page (templates/feedback.html)
  POST /api/feedback   JSON {rating 1-5, category, message, name?, email?, website (honeypot), token}

Each submission is stored in the metadata database (db.feedback) and read by the operator in the
/admin dashboard. Nothing is shown publicly. Success is reported ONLY when the row was saved.

Abuse controls mirror the contact form: server-side validation of every field, a honeypot, a signed
form-age token, per-IP per-minute/per-hour limits, a site-wide hourly cap, a link cap and
control-character stripping. Name and email are optional; an email is only kept if it is valid.
"""

from __future__ import annotations

import logging
import re
import time

from flask import Blueprint, jsonify, render_template, request, session
from itsdangerous import BadSignature, URLSafeTimedSerializer

import db as metadata_db
from config import Config
from utils import mailer, observability
from utils.security import check_limit, client_key

logger = logging.getLogger("omixa.feedback")
feedback_bp = Blueprint("feedback", __name__)

CATEGORIES = {
    "general": "General feedback",
    "cleaning": "Cleaning results",
    "bug": "Something is broken",
    "idea": "Feature idea",
    "pricing": "Pricing and plans",
    "other": "Other",
}
NAME_MAX = 80
MESSAGE_MIN, MESSAGE_MAX = 10, 2000
MAX_LINKS = 2
MIN_FORM_AGE_SECONDS = 2
MAX_FORM_AGE_SECONDS = 24 * 3600

_CTRL_SINGLE = re.compile(r"[\x00-\x1f\x7f]")
_CTRL_MULTI = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")   # keeps \t \n \r in the message body
_LINK = re.compile(r"(?:https?://|www\.)", re.I)
_UNAVAILABLE = "Sorry, feedback is temporarily unavailable. Please try again in a little while."


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(Config.SECRET_KEY, salt="omixa-feedback-form")


def new_form_token() -> str:
    return _serializer().dumps({"t": int(time.time())})


def _token_ok(token) -> bool:
    if not isinstance(token, str) or not token or len(token) > 400:
        return False
    try:
        data = _serializer().loads(token, max_age=MAX_FORM_AGE_SECONDS)
        age = time.time() - float(data["t"])
    except (BadSignature, KeyError, TypeError, ValueError):
        return False
    return age >= MIN_FORM_AGE_SECONDS


def _clean_line(value) -> str:
    return " ".join(_CTRL_SINGLE.sub(" ", value).split())


def validate(payload: dict) -> tuple[dict, dict]:
    """(clean_values, errors). Errors map field name -> message the visitor can act on."""
    errors: dict[str, str] = {}

    rating = payload.get("rating")
    if isinstance(rating, str) and rating.strip().isdigit():
        rating = int(rating.strip())
    if isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5:
        errors["rating"] = "Please choose a rating from 1 to 5 stars."

    category = payload.get("category")
    if not isinstance(category, str) or category not in CATEGORIES:
        errors["category"] = "Please choose what your feedback is about."

    raw_message = payload.get("message")
    message = ""
    if not isinstance(raw_message, str):
        errors["message"] = "Please tell us a little about your experience."
    else:
        message = _CTRL_MULTI.sub("", raw_message).replace("\r\n", "\n").replace("\r", "\n").strip()
        if len(message) < MESSAGE_MIN:
            errors["message"] = f"Please write a little more (at least {MESSAGE_MIN} characters)."
        elif len(message) > MESSAGE_MAX:
            errors["message"] = f"Please keep your feedback under {MESSAGE_MAX:,} characters."
        elif len(_LINK.findall(message)) > MAX_LINKS:
            errors["message"] = f"Please include no more than {MAX_LINKS} links."

    name = payload.get("name")
    name = _clean_line(name) if isinstance(name, str) else ""
    if len(name) > NAME_MAX:
        errors["name"] = f"Please keep your name under {NAME_MAX} characters."

    email = payload.get("email")
    email = email.strip() if isinstance(email, str) else ""
    if email and not mailer.valid_address(email):
        errors["email"] = "That email address does not look right. You can also leave it empty."

    return {"rating": rating, "category": category, "message": message,
            "name": name, "email": email}, errors


def _limited(retry_after: int):
    observability.inc("omixa_ratelimited_total")
    resp = jsonify({"error": "You have sent several messages recently. Please wait a little before trying again."})
    resp.headers["Retry-After"] = str(retry_after)
    return resp, 429


@feedback_bp.get("/feedback")
def page():
    return render_template("feedback.html", form_token=new_form_token(), message_max=MESSAGE_MAX,
                           categories=CATEGORIES)


@feedback_bp.post("/api/feedback")
def submit():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Invalid request."}), 400

    ip = client_key()
    for ns, key, limit, window in (
        ("feedback_min", ip, Config.RATE_LIMIT_FEEDBACK_PER_MINUTE, 60),
        ("feedback_hour", ip, Config.RATE_LIMIT_FEEDBACK_PER_HOUR, 3600),
        ("feedback_all", "site", Config.RATE_LIMIT_FEEDBACK_GLOBAL_PER_HOUR, 3600),
    ):
        allowed, retry = check_limit(ns, key, limit, window)
        if not allowed:
            return _limited(retry)

    # Honeypot: real visitors never see or fill this field. Pretend success so bots learn nothing.
    if payload.get("website"):
        logger.info("feedback form: honeypot triggered")
        return jsonify({"ok": True, "message": "Thank you for your feedback."}), 200

    if not _token_ok(payload.get("token")):
        return jsonify({"error": "This form has expired. Please reload the page and try again."}), 400

    values, errors = validate(payload)
    if errors:
        return jsonify({"error": "Please check the highlighted fields.", "fields": errors}), 422

    try:
        metadata_db.add_feedback(
            rating=values["rating"], category=values["category"], message=values["message"],
            name=values["name"], email=values["email"], owner_uid=session.get("uid"),
            request_id=observability.get_request_id())
    except metadata_db._DB_ERRORS:
        logger.exception("feedback could not be stored")
        return jsonify({"error": _UNAVAILABLE}), 503
    return jsonify({"ok": True, "message": "Thank you! Your feedback has been received."}), 200
