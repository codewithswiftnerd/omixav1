# Omixa security audit (2026-10-29)

Scope: the uploaded repository (Flask API, Firebase auth, Firestore/SQLite/Postgres metadata, S3/local job storage,
Paystack billing, static JS). Static review plus synthetic-data tests. **Not covered:** git history (no `.git` in the
upload), live deployment settings, dependency CVE scan (no network), and the pytest-style suites (pytest could not be
installed offline, so only unittest-style suites were run: `tests/test_security_audit.py` 10/10, `tests/test_pro_accounts.py` 19/19).

## Fixed in this pass
| # | Severity | Finding | Fix |
|---|----------|---------|-----|
| 1 | High | Paystack `charge.success` fell back to client-controlled `metadata.plan/uid`. Anyone with the public Paystack key could pay a tiny amount with forged metadata and receive annual Pro via the (genuinely signed) webhook. | `accounts/paystack.py`: plan comes from the configured plan code, or from a checkout Omixa initiated (reference stored, same uid and plan). Reference is URL-quoted in `verify`. |
| 2 | Medium | Logout only deleted the cookie client-side; the signed cookie stayed valid for 12 h (replay after logout). | `accounts/auth.py`: per-user `session_epoch`; sign-out rotates it, all copies of the cookie are rejected. Side effect: signing out signs out all devices. |
| 3 | Medium | Admin session had no server-enforced lifetime; a copied admin cookie worked 12 h, even after admin logout. | `utils/session.py`: 60 min absolute, 20 min idle, checked on every admin request. |
| 4 | Low | `/api/download/<id>` returned a 409 for batch jobs before checking ownership (job-ID existence oracle). | `routes/download.py`: ownership first. |
| 5 | Low | No XML-bomb protection when openpyxl parses uploaded .xlsx. | `defusedxml` added to requirements (openpyxl uses it automatically when installed). |
| 6 | Info | `.env.example` held a value starting `sk_live_` (a placeholder, not a real key) that trips secret scanners. | Replaced with a neutral placeholder. |

## Checked and found sound
Parameterised SQL throughout `db.py`; job/batch/session/profile access scoped by session hash or Firebase uid with
404 for non-owners; job IDs validated as UUIDs before touching paths; uploads renamed server-side, magic-byte, zip
size/part and macro checks, size/row/cell caps; CSV/XLSX formula defusing (header cells too); CSRF via Origin check
plus admin CSRF token; CORS never wildcard with credentials; CSP with nonces, HSTS, XFO, nosniff; HMAC-SHA512 webhook
signature with constant-time compare and atomic per-reference idempotency; `/api/auth/me` exposes no payment IDs or
hashes; no password storage (Firebase); frontend `innerHTML` sites escape spreadsheet-derived values; Firestore rules deny all browser writes.

## Remaining risks / recommendations (not changed)
- **Rotate secrets that were once committed.** `config.py` blocklists one old `OMIXA_SECRET_KEY` by hash, proving a past leak. Confirm it was rotated, and scan git history (`gitleaks`/`trufflehog`); also rotate anything else that lived in that commit.
- Ship no `data/omixa.db` (the zip contained a dev DB with 11 job rows: filenames and session hashes). It is gitignored; make sure it is not in images or archives.
- Dev DB aside, `docker-compose.yml` uses throwaway passwords; never expose its ports publicly.
- Accounts are not required to have a verified email (`email_verified` is read but unused). Billing/welcome emails can go to unverified addresses; require verification before sending or before checkout.
- `PUBLIC_BASE_URL` unset falls back to the Host header (callbacks, canonical URLs). Set it in production.
- Rate limiting falls back to per-process memory if Redis fails (documented trade-off); keep Redis healthy and alert on `omixa_ratelimit_fallback_total`.
- Admin is a single shared account with no MFA/lockout beyond a per-IP limiter. Consider IP allow-listing or SSO.
- Pin and scan dependencies (`pip-audit`); `gunicorn==22.0.0` and `requests` should be checked against current advisories. Docker image runs non-root (good); add `--read-only` and drop capabilities.
- No security-event alerting pipeline: forward 401/403/429 and admin-login failures from JSON logs to your alerting.

## Incident response (short)
Credential exposed: revoke at the provider (Paystack, Firebase service account, SMTP, S3), set new values in host secrets,
redeploy, then review audit logs. Admin compromise: change `OMIXA_ADMIN_PASSWORD_HASH` and `OMIXA_SECRET_KEY` (invalidates every session), review `audit_log`. Suspected DB access: rotate `DATABASE_URL` credentials, restrict network access, check Paystack payments against granted subscriptions.
