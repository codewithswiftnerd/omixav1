# Pre-launch access gate and live naira pricing

Two independent features, both controlled only by Railway variables.

1. **Access gate** (temporary): keeps Omixa private until launch (13 November 2026).
2. **Live USD→NGN pricing** (permanent): prices are in dollars; the naira amount follows the exchange rate by itself.

---

## 1. Access gate

### Scope: SEO is untouched by default
`OMIXA_ACCESS_GATE_SCOPE` (default `app`):
* **`app`** - the pages search engines index (home, pricing, about and the guides: exactly the list in `routes/seo.py`
  `PUBLIC_PAGES`), the sitemap, `robots.txt`, favicons and static files stay **public and byte-for-byte as before**: same
  indexing tags, canonical URLs, structured data and headers. Everything that *does* something is locked: all `/api/*`,
  `/admin`, `/dashboard`, `/batch`, `/login`, `/profiles`, `/sessions`, `/contact`. A visitor can read the marketing pages
  but cannot upload, clean, sign in, register, pay or download. (The cleaning box on the home page shows
  "Omixa is private until launch." if a non-team visitor tries it.) A test checks every public page is identical with the gate on and off.
* **`site`** - everything is locked and hidden from search engines (see the last section). Use only if you accept leaving Google.

### What it does
With `OMIXA_ACCESS_GATE_ENABLED=true`, every request (outside the public pages in scope `app`) needs a valid gate cookie or the **server** refuses it before any
route, session, database or file code runs: pages get a branded "Omixa is preparing for launch" page (HTTP 403), `/api/*`
gets `401 {"code":"access_gate"}`, and static files, unknown URLs and `/admin` are refused too. Team members type the
password into the gate page (a POST form, never a URL); the server then sets a signed, expiring cookie
(`omixa_gate`: HttpOnly, SameSite=Lax, Secure in production, 12 h by default).

Omixa's own sign-in, registration, admin login, Pro plans, Redis, PostgreSQL, S3 and the worker are untouched: the gate
sits in front of them and adds no accounts, tables or migrations.

### Always reachable (on purpose)
| Path | Why |
|---|---|
| `/healthz`, `/readyz`, `/api/health` | Railway/orchestrator health checks (`railway.json` uses `/healthz`) |
| `POST /api/billing/webhook` | Paystack cannot hold a cookie; the route verifies Paystack's HMAC signature |
| `/api/metrics` | Needs `OMIXA_METRICS_TOKEN` (Bearer); answers 404 otherwise |
| `/robots.txt`, `/favicon.ico`, Google verification file | Crawl hints / verification; no private data |
| `OPTIONS` on `/api/*` | CORS preflights carry no data |
| a few static files | Only what the gate page itself needs (`gate.css`, `app.css`, logo, favicons) |

**Worker:** `worker.py` never calls the web service over HTTP (it uses PostgreSQL, Redis and S3 directly), so the gate
cannot affect it. A test proves jobs still run while the gate is on.
**Administrators:** an admin who is already signed in keeps working when the gate is switched on. A signed-out admin
enters the team password first and then uses the normal admin login.
**Presigned download links** come from the S3 bucket and are only handed out by gated API routes (valid 5 minutes).

### Railway variables
Set on the **web** service (the worker does not need them):

| Variable | Value |
|---|---|
| `OMIXA_ACCESS_GATE_ENABLED` | `true` |
| `OMIXA_ACCESS_GATE_PASSWORD` | the team password, **12+ characters** |
| `OMIXA_ACCESS_GATE_SECRET` | 32+ random characters (generate below). Must differ from `OMIXA_SECRET_KEY` |
| `OMIXA_ACCESS_GATE_OPEN_AT` | *(optional)* `2026-11-13T00:00:00+01:00`: the gate switches itself off at that moment |
| `OMIXA_ACCESS_GATE_SCOPE` | *(optional)* `app` (default: public marketing pages stay open, SEO untouched) or `site` (lock everything) |
| `OMIXA_ACCESS_GATE_SESSION_HOURS` | *(optional)* cookie lifetime, default `12` |
| `OMIXA_ACCESS_GATE_ATTEMPTS_PER_MINUTE` / `_PER_HOUR` | *(optional)* password attempts per client, default `6` / `40` |

Generate the secret locally: `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
Railway → web service → **Variables** → *New Variable* (or *Raw Editor*) → paste each → Railway redeploys. Never put the
password in git, in a URL, or in chat; share it with the team through a password manager.

### Enable (now)
1. Add the three required variables above (and `OMIXA_ACCESS_GATE_OPEN_AT` if you want automatic launch).
2. Let Railway redeploy (this code must be deployed first; nothing here deploys automatically).
3. Check: a private window on the site shows "Omixa is preparing for launch"; `https://<site>/api/health` still answers;
   entering the password lets you in.

Fail-closed: if it is enabled but the password/secret is missing or too short, the site stays **locked for everyone**
(503 notice, no form) and the log names the missing variable (never its value). It never falls open by mistake.

### Rotate / sign everybody out
Change `OMIXA_ACCESS_GATE_PASSWORD` or `OMIXA_ACCESS_GATE_SECRET`: every existing cookie stops working immediately.

### Launch day (13 November 2026)
**Automatic:** with `OMIXA_ACCESS_GATE_OPEN_AT=2026-11-13T00:00:00+01:00` the gate opens at midnight Lagos time with no
action. (An offset is required to be unambiguous; a value without one is read as UTC; an unreadable value is ignored and
the gate stays on.)
**Manual (any time):** set `OMIXA_ACCESS_GATE_ENABLED=false` (or delete the variable). Redeploy. The site is public, `/robots.txt`
and `/sitemap.xml` return to their normal production content, and `/__access` returns 404.

### Remove the gate for good (after launch, optional cleanup)
1. Delete the variables `OMIXA_ACCESS_GATE_*` from Railway.
2. In `app.py` delete the three lines `from utils import access_gate` / `access_gate.init_app(app)` (+ comment).
3. In `routes/seo.py` change `indexing_allowed()` back to `return bool(Config.IS_PRODUCTION)`.
4. Delete `utils/access_gate.py`, `templates/access_gate.html`, `static/css/gate.css`, `tests/test_access_gate.py`.
5. Remove the gate block from `config.py` and `.env.example`; run the tests.

### Search engines
With the default scope `app` nothing changes for search engines: the indexed pages are public, `robots.txt` and the sitemap are
unchanged, and only the locked app/API responses carry `noindex`. The rest of this section applies to scope `site` only.

While the gate is on in scope `site`, every response carries `X-Robots-Tag: noindex, nofollow, noarchive, nosnippet, noimageindex`, the gate page has the
same `<meta robots>`, `robots.txt` says `Disallow: /` and the sitemap is withdrawn. **These only ask well-behaved crawlers to
stay away; they are not access control**; the server-side gate is what keeps people out. They also cannot guarantee that
results Google already has will disappear: `Disallow: /` stops Google re-visiting pages (so it may never see a noindex),
though the 403/401 answers make Google drop them over time. To speed that up, use **Google Search Console → Removals** for
getomixa.com and the Railway `*.up.railway.app` hostnames. After launch `robots.txt` returns to normal automatically;
request re-indexing in Search Console.

---

## 2. Live USD→NGN pricing

Prices stay in dollars (`OMIXA_PRICE_USD_MONTHLY=5`, `OMIXA_PRICE_USD_ANNUAL=50`). The naira amount is
`dollar price × today's rate`, rounded to ₦100, and updates by itself.

* Rate source: `open.er-api.com` (free, no key, updates daily; the pricing page shows the credit its terms ask for).
  Fetched on the server, cached 6 h, shared between workers via Redis, refreshed by the first visit after expiry.
* If the rate service is down: the last good rate keeps being used (retry every 10 min). If none was ever fetched:
  `OMIXA_FX_FALLBACK_USD_NGN`, then `OMIXA_PRICE_NGN_*`, then the naira line is hidden. A page never breaks.
* A rate outside ₦100–₦20,000 per dollar is rejected as garbage.
* **What Paystack charges:** a Paystack plan has one fixed naira amount. At checkout Omixa compares the plan with today's price and, if they differ by more than 2 %,
  updates the plan amount through Paystack's API (`update_existing_subscriptions=false`: **new subscribers only**; people already subscribed keep
  their price). Any failure is logged and checkout carries on. Switch off with `OMIXA_PAYSTACK_AUTO_PRICE=false`.

Variables (all optional): `OMIXA_FX_MODE` (`auto`|`fixed`), `OMIXA_FX_URL`, `OMIXA_FX_CACHE_SECONDS`, `OMIXA_FX_FALLBACK_USD_NGN`,
`OMIXA_FX_BUFFER_PERCENT` (e.g. `3` to cover rate drift and fees), `OMIXA_FX_ROUND_TO_NGN`, `OMIXA_FX_MIN_RATE`, `OMIXA_FX_MAX_RATE`,
`OMIXA_PAYSTACK_AUTO_PRICE`, `OMIXA_PAYSTACK_AUTO_PRICE_TOLERANCE_PERCENT`.
**Action for you:** clear `OMIXA_PRICE_NGN_MONTHLY` and `OMIXA_PRICE_NGN_ANNUAL` in Railway (or leave them as fallbacks), and
make sure the Paystack secret key and both plan codes are set. Test one checkout with Paystack **test keys** before launch.
Note: Paystack's "plan" API behaviour was taken from its public docs and verified here only against a fake; the test checkout is the real proof.

## Tests
`pytest tests/test_access_gate.py tests/test_fx_pricing.py`
