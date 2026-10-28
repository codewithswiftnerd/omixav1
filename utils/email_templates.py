"""
Email content. Plain text is always included (some clients and spam filters prefer it); the HTML part uses
inline styles and tables because email clients ignore stylesheets. Colours and wording follow docs/BRAND.md:
indigo/violet, calm and professional, never an "AI" pitch. Every user-supplied value is HTML-escaped.
"""

from __future__ import annotations

from html import escape

from config import Config

INDIGO = "#381593"
INK = "#201551"
LAVENDER = "#f7f5fb"
BORDER = "#dcd8e6"
MUTED = "#6b6580"


def site_url() -> str:
    return (Config.PUBLIC_BASE_URL or "https://getomixa.com").rstrip("/")


def _first_name(name: str | None) -> str:
    parts = (name or "").strip().split()
    return parts[0][:40] if parts else ""


def _button(label: str, url: str) -> str:
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" style="margin:22px 0"><tr>'
            f'<td style="background:{INDIGO};border-radius:8px"><a href="{escape(url, quote=True)}" '
            f'style="display:inline-block;padding:12px 24px;color:#ffffff;text-decoration:none;font-weight:700;'
            f'font-size:15px">{escape(label)}</a></td></tr></table>')


def _wrap(title: str, body_html: str, footer_note: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title></head>
<body style="margin:0;padding:0;background:{LAVENDER};font-family:Urbanist,'Segoe UI',Helvetica,Arial,sans-serif;color:{INK}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{LAVENDER};padding:24px 12px"><tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff;border:1px solid {BORDER};border-radius:12px;overflow:hidden">
<tr><td style="background:{INDIGO};padding:20px 28px"><span style="color:#ffffff;font-size:22px;font-weight:800;letter-spacing:.2px">Omixa</span></td></tr>
<tr><td style="padding:28px;font-size:15px;line-height:1.6">{body_html}</td></tr>
<tr><td style="padding:16px 28px 24px;border-top:1px solid {BORDER};font-size:12px;line-height:1.5;color:{MUTED}">{footer_note}<br>
Omixa &middot; <a href="{escape(site_url())}" style="color:{MUTED}">{escape(site_url().split('//', 1)[-1])}</a> &middot; <a href="mailto:{escape(Config.SUPPORT_EMAIL)}" style="color:{MUTED}">{escape(Config.SUPPORT_EMAIL)}</a></td></tr>
</table></td></tr></table></body></html>"""


# --------------------------------------------------------------------------- welcome

def welcome(name: str | None) -> tuple[str, str, str]:
    """(subject, text, html)"""
    first = _first_name(name)
    hello = f"Hi {first}," if first else "Hello,"
    start = f"{site_url()}/"
    subject = "Welcome to Omixa"
    text = (
        f"{hello}\n\n"
        "Your Omixa account is ready.\n\n"
        "Omixa cleans CSV and Excel files and reports on data quality. It shows what is wrong, fixes what is safe "
        "to fix, flags what is not, and leaves ambiguous decisions to you. Your original file is never changed.\n\n"
        f"Clean a file: {start}\n\n"
        f"Questions? Reply to this email or write to {Config.SUPPORT_EMAIL}.\n\n"
        "The Omixa team\n"
    )
    body = (
        f"<p style=\"margin:0 0 14px\">{escape(hello)}</p>"
        "<p style=\"margin:0 0 14px\"><strong>Your Omixa account is ready.</strong></p>"
        "<p style=\"margin:0 0 14px\">Omixa cleans CSV and Excel files and reports on data quality. It shows what is wrong, "
        "fixes what is safe to fix, flags what is not, and leaves ambiguous decisions to you. "
        "Your original file is never changed.</p>"
        + _button("Clean a file", start)
        + f"<p style=\"margin:0\">Questions? Reply to this email or write to "
          f"<a href=\"mailto:{escape(Config.SUPPORT_EMAIL)}\" style=\"color:{INDIGO}\">{escape(Config.SUPPORT_EMAIL)}</a>.</p>"
    )
    footer = "You are receiving this because an Omixa account was created with this email address."
    return subject, text, _wrap(subject, body, footer)


# --------------------------------------------------------------------------- password reset

def password_reset(link: str, name: str | None = None) -> tuple[str, str, str]:
    """`link` is a single-use, expiring Firebase action link. It is never logged."""
    first = _first_name(name)
    hello = f"Hi {first}," if first else "Hello,"
    subject = "Reset your Omixa password"
    text = (
        f"{hello}\n\n"
        "We received a request to reset the password for your Omixa account. Open this link to choose a new one:\n\n"
        f"{link}\n\n"
        "The link can be used once and expires after a short time. If you did not ask for this, ignore this email: "
        "your password stays as it is.\n\n"
        f"Need help? Write to {Config.SUPPORT_EMAIL}.\n\n"
        "The Omixa team\n"
    )
    body = (
        f"<p style=\"margin:0 0 14px\">{escape(hello)}</p>"
        "<p style=\"margin:0 0 14px\">We received a request to reset the password for your Omixa account.</p>"
        + _button("Choose a new password", link)
        + f"<p style=\"margin:0 0 14px;font-size:13px;color:{MUTED}\">The link can be used once and expires after a short time. "
          "If the button does not work, copy this address into your browser:<br>"
          f"<span style=\"word-break:break-all\">{escape(link)}</span></p>"
        "<p style=\"margin:0\">If you did not ask for this, ignore this email: your password stays as it is.</p>"
    )
    footer = "You are receiving this because a password reset was requested for this email address."
    return subject, text, _wrap(subject, body, footer)


# --------------------------------------------------------------------------- billing

_PLAN_LABELS = {"pro_monthly": "Pro Monthly", "pro_annual": "Pro Annual"}


def plan_label(plan: str | None) -> str:
    return _PLAN_LABELS.get(plan or "", "Pro")


def format_date(ts: float | int | None) -> str:
    """'14 November 2026' in UTC. Empty string when there is no usable timestamp."""
    import datetime
    try:
        return datetime.datetime.fromtimestamp(float(ts), datetime.timezone.utc).strftime("%d %B %Y").lstrip("0")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _paragraphs(hello: str, lines: list[str], button: tuple[str, str] | None, closing: str) -> tuple[str, str]:
    """Builds the plain-text and HTML bodies from the same paragraphs so the two can never drift apart."""
    text = hello + "\n\n" + "\n\n".join(lines)
    if button:
        text += f"\n\n{button[0]}: {button[1]}"
    text += f"\n\n{closing} {Config.SUPPORT_EMAIL}.\n\nThe Omixa team\n"
    html = f'<p style="margin:0 0 14px">{escape(hello)}</p>'
    html += "".join(f'<p style="margin:0 0 14px">{escape(line)}</p>' for line in lines)
    if button:
        html += _button(*button)
    html += (f'<p style="margin:0">{escape(closing)} <a href="mailto:{escape(Config.SUPPORT_EMAIL)}" '
             f'style="color:{INDIGO}">{escape(Config.SUPPORT_EMAIL)}</a>.</p>')
    return text, html


def _hello(name: str | None) -> str:
    first = _first_name(name)
    return f"Hi {first}," if first else "Hello,"


def _billing_footer() -> str:
    return "You are receiving this because of a payment or subscription event on your Omixa account."


def pro_activated(name: str | None, plan: str | None, expires: float | None, renewed: bool,
                  amount_text: str = "") -> tuple[str, str, str]:
    label, until = plan_label(plan), format_date(expires)
    if renewed:
        subject = f"Your Omixa {label} plan was renewed"
        lines = [f"Thanks. Your payment{f' of {amount_text}' if amount_text else ''} went through and your {label} plan was renewed."]
    else:
        subject = f"Omixa {label} is active"
        lines = [f"Thanks. Your payment{f' of {amount_text}' if amount_text else ''} went through and {label} is now active on your account."]
    if until:
        lines.append(f"Your access runs until {until} (UTC).")
    text, html = _paragraphs(_hello(name), lines, ("Open your dashboard", f"{site_url()}/dashboard"), "Questions about billing? Write to")
    return subject, text, _wrap(subject, html, _billing_footer())


def payment_failed(name: str | None, access_until: float | None = None) -> tuple[str, str, str]:
    """`access_until` is set only when the person still has paid Pro access, so the wording never claims
    access that does not exist."""
    subject = "We could not process your Omixa payment"
    lines = ["We tried to take a payment for your Omixa Pro plan and it did not go through. "
             "This is usually an expired card, insufficient funds or a bank decline."]
    until = format_date(access_until)
    if until:
        lines.append(f"Your Pro access is not affected yet: it runs until {until} (UTC). "
                     "Update your payment method before then to keep it.")
    else:
        lines.append("Pro has not been activated. You can try again with the same or a different card.")
    text, html = _paragraphs(_hello(name), lines, ("Go to pricing", f"{site_url()}/pricing"), "Need help? Write to")
    return subject, text, _wrap(subject, html, _billing_footer())


def subscription_cancelled(name: str | None, expires: float | None) -> tuple[str, str, str]:
    subject = "Your Omixa subscription will not renew"
    until = format_date(expires)
    lines = ["Your Omixa Pro subscription has been cancelled and will not renew. You will not be charged again."]
    lines.append(f"You keep Pro until {until} (UTC); after that your account returns to the Free plan."
                 if until else "You keep Pro until the end of the period you already paid for.")
    text, html = _paragraphs(_hello(name), lines, ("Resubscribe", f"{site_url()}/pricing"), "Cancelled by mistake? Write to")
    return subject, text, _wrap(subject, html, _billing_footer())


def pro_expiring(name: str | None, expires: float | None, days_left: int, will_renew: bool) -> tuple[str, str, str]:
    until = format_date(expires)
    when = "tomorrow" if days_left <= 1 else f"in {days_left} days"
    subject = f"Your Omixa Pro access ends {when}"
    if will_renew:
        lines = [f"Your Omixa Pro access ends {when}" + (f" ({until}, UTC)" if until else "") + ", and the last renewal payment did not go through.",
                 "Update your payment method to keep Pro without a gap."]
    else:
        lines = [f"Your Omixa Pro access ends {when}" + (f" ({until}, UTC)" if until else "") + " and will not renew automatically.",
                 "After that your account returns to the Free plan. Your files and history are not deleted."]
    text, html = _paragraphs(_hello(name), lines, ("Keep Pro", f"{site_url()}/pricing"), "Questions? Write to")
    return subject, text, _wrap(subject, html, _billing_footer())


# --------------------------------------------------------------------------- contact form (internal)

def contact_notification(name: str, email: str, subject: str, message: str, meta: dict) -> tuple[str, str, str]:
    """Sent to the support mailbox. Reply-To is the visitor, so 'Reply' answers them directly."""
    mail_subject = f"[Omixa contact] {subject}"
    meta_lines = "\n".join(f"{k}: {v}" for k, v in meta.items() if v)
    text = (
        f"New message from the Omixa contact form.\n\n"
        f"Name: {name}\nEmail: {email}\nSubject: {subject}\n\n"
        f"{message}\n\n--\n{meta_lines}\n"
    )
    rows = "".join(
        f"<tr><td style=\"padding:4px 12px 4px 0;color:{MUTED}\">{escape(k)}</td><td style=\"padding:4px 0\">{escape(str(v))}</td></tr>"
        for k, v in (("Name", name), ("Email", email), ("Subject", subject)))
    body = (
        "<p style=\"margin:0 0 14px\"><strong>New message from the Omixa contact form</strong></p>"
        f"<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" style=\"margin:0 0 16px;font-size:14px\">{rows}</table>"
        f"<div style=\"white-space:pre-wrap;border-left:3px solid {BORDER};padding:4px 0 4px 14px\">{escape(message)}</div>"
        f"<p style=\"margin:18px 0 0;font-size:12px;color:{MUTED}\">{escape(' · '.join(f'{k}: {v}' for k, v in meta.items() if v))}</p>"
        f"<p style=\"margin:10px 0 0;font-size:13px\">Reply to this email to answer {escape(name)} directly.</p>"
    )
    return mail_subject, text, _wrap(mail_subject, body, "Delivered by the Omixa website contact form.")
