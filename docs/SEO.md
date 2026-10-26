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
