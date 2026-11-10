# SEO notes

- `routes/seo.py` is the single source of truth: `PUBLIC_PAGES` (indexable + canonical target), `SITEMAP_PATHS`,
  robots.txt, sitemap.xml, JSON-LD (home page only). Add a new public page there and it flows to canonical,
  robots meta, sitemap and tests. Anything not listed is `noindex,nofollow` (meta + `X-Robots-Tag`).
- Set `OMIXA_PUBLIC_BASE_URL=https://<production domain>` (no trailing slash). Without it, canonicals/sitemap use
  the request host, which is wrong behind some proxies.
- `FLASK_ENV` defaults to production. Any other value serves `Disallow: /` and no sitemap, so staging is never indexed.
- robots.txt blocks only `/api/` and `/admin/`. Private app pages use noindex (a blocked page cannot show its noindex).
  robots.txt is not access control; auth and ownership checks are.
- Run `tests/test_seo.py` after any template/route change.

## One public hostname (getomixa.com)

The same app answers on getomixa.com and on Railway-generated hostnames. Search engines index every hostname that
returns a page, so the Railway URLs appeared as duplicate results. `utils/canonical_host.py` (registered first in
`app.py`) fixes this:

- **GET/HEAD on an allowlisted Railway hostname -> 301** to the same path and query on `OMIXA_PUBLIC_BASE_URL`.
  Allowlist: `OMIXA_REDIRECT_HOSTS` (comma-separated, default = the three Railway hostnames). The redirect origin
  always comes from config, never from the request, so a forged Host header cannot redirect anywhere else.
- **Active only when** `FLASK_ENV=production` AND `OMIXA_PUBLIC_BASE_URL` are both set. If the base URL is missing,
  boot logs a warning and nothing is redirected (canonicals then follow the request host).
- **Not redirected:** the canonical host, any host not in the allowlist (localhost, `healthcheck.railway.app`),
  `/healthz` `/readyz` `/health` on every host, and POST/PUT/PATCH/DELETE (a 3xx can break webhooks and API clients
  that do not follow it). Anything still answered from an allowlisted hostname carries `X-Robots-Tag: noindex, nofollow`.
- **Before deploying:** check Paystack (webhook/callback URL), Firebase Auth "Authorized domains" and any other
  integration that still points at a Railway hostname, and move it to `https://getomixa.com`. Browser flows that
  start on a Railway hostname will land on getomixa.com after the redirect and must work there.
- Add `www.getomixa.com` to `OMIXA_REDIRECT_HOSTS` only if that name is also attached to this service.

### Favicon
`/favicon.ico` (root, stable URL; 16/32/48 px ICO) plus 192, 32 and 16 px PNGs, all linked from `templates/base.html`,
all served without cookies, authentication, redirects or `noindex`. Source mark: `static/brand/omixa-icon.png`
(identical to `android-chrome-192x192.png`). Google needs a square icon whose size is a multiple of 48 px and
recrawls on its own schedule, so the globe may stay for days or weeks.
