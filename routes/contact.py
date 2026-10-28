"""
Contact form.

  GET  /contact       the page (templates/contact.html)
  POST /api/contact   JSON {name, email, subject, message, website (honeypot), token}

The message is emailed to Config.CONTACT_FORM_RECIPIENT (support@getomixa.com) over SMTP with the
visitor's address in Reply-To. Nothing is stored. Success is reported ONLY when the SMTP server accepted
the message; otherwise the visitor is told to email the support address directly.

Abuse controls: server-side validation of every field, a honeypot, a signed form-age token (rejects
instant bot posts and stale pages), per-IP per-minute and per-hour limits, a site-wide hourly cap, a link
cap, header-injection-safe values, and HTML-escaped email bodies. SMTP details never reach the browser.
"""

from __future__ import annotations

import logging
import re
import time

from flask import Blueprint, jsonify, render_template, request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from config import Config
from utils import email_templates, mailer, observability
from utils.security import check_limit, client_key

logger = logging.getLogger("omixa.contact")
contact_bp = Blueprint("contact", __name__)

NAME_MAX, SUBJECT_MAX, SUBJECT_MIN = 80, 120, 3
MESSAGE_MIN, MESSAGE_MAX = 10, 5000
MAX_LINKS = 3
MIN_FORM_AGE_SECONDS = 2
MAX_FORM_AGE_SECONDS = 24 * 3600

_CTRL_SINGLE = re.compile(r"[\x00-\x1f\x7f]")
_CTRL_MULTI = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")   # keeps \t \n \r in the message body
_LINK = re.compile(r"(?:https?://|www\.)", re.I)
_UNAVAILABLE = ("Sorry, the contact form is temporarily unavailable. Please email us directly at "
                f"{Config.SUPPORT_EMAIL}.")


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(Config.SECRET_KEY, salt="omixa-contact-form")


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
    raw = {k: payload.get(k) for k in ("name", "email", "subject", "message")}
    for k, v in raw.items():
        if not isinstance(v, str):
            errors[k] = "This field is required."
    if errors:
        return {}, errors

    name = _clean_line(raw["name"])
    email = raw["email"].strip()
    subject = _clean_line(raw["subject"])
    message = _CTRL_MULTI.sub("", raw["message"]).replace("\r\n", "\n").replace("\r", "\n").strip()

    if not name:
        errors["name"] = "Please enter your name."
    elif len(name) > NAME_MAX:
        errors["name"] = f"Please keep your name under {NAME_MAX} characters."
    if not mailer.valid_address(email):
        errors["email"] = "Please enter a valid email address."
    if len(subject) < SUBJECT_MIN:
        errors["subject"] = "Please add a short subject."
    elif len(subject) > SUBJECT_MAX:
        errors["subject"] = f"Please keep the subject under {SUBJECT_MAX} characters."
    if len(message) < MESSAGE_MIN:
        errors["message"] = f"Please write a little more (at least {MESSAGE_MIN} characters)."
    elif len(message) > MESSAGE_MAX:
        errors["message"] = f"Please keep your message under {MESSAGE_MAX:,} characters."
    elif len(_LINK.findall(message)) > MAX_LINKS:
        errors["message"] = f"Please include no more than {MAX_LINKS} links."
    return {"name": name, "email": email, "subject": subject, "message": message}, errors


def _limited(retry_after: int):
    observability.inc("omixa_ratelimited_total")
    resp = jsonify({"error": "You have sent several messages recently. Please wait a little before trying again, "
                             f"or email {Config.SUPPORT_EMAIL}."})
    resp.headers["Retry-After"] = str(retry_after)
    return resp, 429


@contact_bp.get("/contact")
def page():
    return render_template("contact.html", form_token=new_form_token(), message_max=MESSAGE_MAX,
                           mail_available=mailer.is_configured())


@contact_bp.post("/api/contact")
def submit():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Invalid request."}), 400

    ip = client_key()
    for ns, key, limit, window in (
        ("contact_min", ip, Config.RATE_LIMIT_CONTACT_PER_MINUTE, 60),
        ("contact_hour", ip, Config.RATE_LIMIT_CONTACT_PER_HOUR, 3600),
        ("contact_all", "site", Config.RATE_LIMIT_CONTACT_GLOBAL_PER_HOUR, 3600),
    ):
        allowed, retry = check_limit(ns, key, limit, window)
        if not allowed:
            return _limited(retry)

    # Honeypot: real visitors never see or fill this field. Pretend success so bots learn nothing.
    if payload.get("website"):
        logger.info("contact form: honeypot triggered")
        return jsonify({"ok": True, "message": "Thanks, your message has been sent."}), 200

    if not _token_ok(payload.get("token")):
        return jsonify({"error": "This form has expired. Please reload the page and try again."}), 400

    values, errors = validate(payload)
    if errors:
        return jsonify({"error": "Please check the highlighted fields.", "fields": errors}), 422

    if not mailer.is_configured():
        logger.error("contact form submission could not be delivered: email is not configured")
        return jsonify({"error": _UNAVAILABLE}), 503

    subject, text, html = email_templates.contact_notification(
        values["name"], values["email"], values["subject"], values["message"],
        {"Request ID": observability.get_request_id()})
    result = mailer.send_email(Config.CONTACT_FORM_RECIPIENT, subject, text, html, reply_to=values["email"])
    if not result.ok:
        # Diagnostics are already logged by the mailer; the visitor gets a safe, honest message.
        return jsonify({"error": _UNAVAILABLE}), 503
    return jsonify({"ok": True, "message": "Thanks, your message has been sent. We will reply by email."}), 200
