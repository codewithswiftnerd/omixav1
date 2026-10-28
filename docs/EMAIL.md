# Email (support@getomixa.com)

Omixa sends email through Namecheap Private Email over SMTP. Everything is configured with environment
variables; nothing secret is in code, templates, JavaScript or git.

## What exists

| Feature | Where | How it works |
|---|---|---|
| Public support address | `Config.SUPPORT_EMAIL` (`OMIXA_SUPPORT_EMAIL`, default `support@getomixa.com`) | Footer, About page, `/contact`, JSON-LD (`routes/seo.py`). All links are `mailto:`. |
| Contact form | `GET /contact`, `POST /api/contact` (`routes/contact.py`, `static/js/contact.js`) | Validated in the browser and again on the server, emailed to `CONTACT_FORM_RECIPIENT` (default: the support address) with the visitor in `Reply-To`. Nothing is stored. |
| Welcome email | `accounts/auth.py` -> `accounts/emails.py` | Queued in the background when `sign_in` creates a NEW account record. Never for returning users. |
| Password reset | `POST /api/auth/password-reset`, `static/js/login.js` | Firebase still creates the reset link (secure, expiring, single-use); the server emails it from support@getomixa.com. Same answer for known and unknown addresses. If the server cannot send email, the sign-in page falls back to Firebase's own reset email. |
| Billing emails | `accounts/paystack.py` -> `accounts/emails.py` | **Pro activated / renewed** (first or repeat `charge.success`), **payment failed** (`charge.failed`, `invoice.payment_failed`; at most one per 24h per user; says whether paid access is unaffected), **subscription cancelled** (`subscription.disable` / `not_renew`; only on the real active -> cancelled transition, with the access-until date). Sent only by the webhook delivery that actually granted/changed state, so Paystack retries never email twice. A mail problem can never fail the webhook. |
| Expiry reminder | `accounts/reminders.py`, `scripts/send_expiry_reminders.py` | Daily cron job. Reminds `cancelled` users (will not renew) and `active` users whose latest renewal payment failed, `MAIL_EXPIRY_REMINDER_DAYS` (default 3) days before Pro ends. Recorded in `expiry_reminder_for` only after the server accepted the message; a failed send is retried next run, a sent one never repeats. Healthy auto-renewing users are never emailed. |
| Retry | `utils/mailer.py` (`send_email_with_retry`) | Welcome, reset and billing emails retry transient failures only (timeout, dropped connection, 4xx server reply), `MAIL_RETRY_ATTEMPTS` total attempts (default 3) with doubling waits from `MAIL_RETRY_BASE_SECONDS` (default 5). Wrong password, TLS misconfiguration and refused sender/recipient are never retried. The contact form stays single-attempt because the visitor is waiting. |
| Mailer | `utils/mailer.py`, `utils/email_templates.py` | Implicit SSL (465) or STARTTLS (587), never both, never plaintext. Never raises. |

## What does NOT exist (and what it would need)

* **Password storage / reset tokens in Flask**: not applicable. Authentication is Firebase Auth; passwords and reset
  tokens live in Firebase. Omixa has no password column to protect and no token table, so none was added.
* **Email verification emails**: Firebase can send them; not wired in.
* **Bounce handling / delivery tracking**: not built. Retry covers transient SMTP failures at send time only. A message
  the server accepted and later bounced (mailbox full, address does not exist) arrives as a bounce email in the
  support mailbox; Omixa does not read it. A durable retry queue (surviving a restart) is also not built: a
  retry in progress is lost if the web process restarts. The reminder job is the exception, it re-attempts on its
  next run.

## Railway variables

| Variable | Value |
|---|---|
| `SMTP_HOST` | `mail.privateemail.com` |
| `SMTP_PORT` | `465` |
| `SMTP_USERNAME` | `support@getomixa.com` |
| `SMTP_PASSWORD` | the mailbox password (Railway Variables only) |
| `SMTP_USE_SSL` | `true` |
| `SMTP_USE_TLS` | `false` |
| `MAIL_DEFAULT_SENDER` | `support@getomixa.com` |

For 587: `SMTP_PORT=587`, `SMTP_USE_SSL=false`, `SMTP_USE_TLS=true`. Both true, or both false, is rejected at send time with
a message naming the variables. Optional: `MAIL_RETRY_ATTEMPTS`, `MAIL_RETRY_BASE_SECONDS`, `MAIL_EXPIRY_REMINDER_DAYS`, `OMIXA_SUPPORT_EMAIL`, `CONTACT_FORM_RECIPIENT`, `SMTP_TIMEOUT_SECONDS`,
`RATE_LIMIT_CONTACT_*`, `RATE_LIMIT_RESET_EMAIL_*` (see `.env.example`). Set the variables on the **web** service; the
worker service does not send email. The **reminder cron service** (below) needs the same `SMTP_*` and Firebase variables.

## Expiry reminder cron (Railway)

Create a second service from this repo: start command `python scripts/send_expiry_reminders.py`, cron schedule
`0 8 * * *` (daily, UTC), same `SMTP_*` and Firebase variables as web. Exit code 0 = ok, 1 = some sends failed (retried
next run), 2 = email or accounts not configured. Running it more often is harmless.

## Verifying a deployment

1. Deploy, then check the boot log for `Email configured: mail.privateemail.com:465 (implicit SSL) ...`. A missing
   variable is reported by name (`Email is NOT configured (SMTP_PASSWORD is not set) ...`); the site keeps running.
2. `railway run python scripts/send_test_email.py you@example.com` validates the configuration and sends one real test message.
   It prints the failure reason (`auth_failed`, `timeout`, `tls_error`, ...) and never the password.
3. Submit `/contact` once; the message should arrive at the support mailbox with Reply-To set to you.
4. Create a test account (welcome email) and use "Forgot password?" (reset email).
5. Billing emails: in Paystack test mode complete a checkout (activation email), then send the test webhooks
   `charge.failed` and `subscription.disable` from the Paystack dashboard.
6. `railway run python scripts/send_expiry_reminders.py` prints how many reminders were sent / not due.

## Failure behaviour

* Contact form: success is shown only when the SMTP server accepted the message. Otherwise the visitor gets a 503 and
  the support address to write to.
* Welcome and reset emails run in a bounded background pool. A failure is logged and cannot affect sign-in or any other request.
* Log lines contain the failure class, SMTP status code, a short server reason and the recipient's domain. Never the
  password, message bodies, reset links or full addresses.

## Deliverability (DNS, not changed by this code)

Without SPF, DKIM and DMARC, mail from `support@getomixa.com` may land in spam or be rejected, and Gmail/Yahoo
require them for bulk senders. This repository does not touch DNS; add them in the Namecheap DNS panel for
`getomixa.com` (Advanced DNS). Use the exact values Namecheap shows under **Private Email -> Domain -> DNS records**;
they are specific to the mailbox. The shape is:

| Type | Host | Purpose |
|---|---|---|
| `TXT` | `@` | **SPF**: starts `v=spf1`, includes Namecheap's sender (shown in the Private Email panel). Exactly ONE SPF record per domain. |
| `TXT` | `default._domainkey` (selector shown in the panel) | **DKIM**: `v=DKIM1; k=rsa; p=...` public key. Enable DKIM signing in Private Email first. |
| `TXT` | `_dmarc` | **DMARC**: start with `v=DMARC1; p=none; rua=mailto:support@getomixa.com`, move to `quarantine` after a few weeks of clean reports. |

Check with `dig TXT getomixa.com`, `dig TXT _dmarc.getomixa.com` or mail-tester.com (send `send_test_email.py` output there).
If the site later sends through another provider (SendGrid, Postmark, ...), its records must be added to SPF/DKIM too.
