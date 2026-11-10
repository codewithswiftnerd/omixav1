"""Single public hostname: Railway hostnames 301 to getomixa.com, canonical/sitemap/robots/head metadata point at
the official domain, the favicon is publicly reachable. Runs under pytest or `python -m unittest`."""
import os
import re
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("FLASK_ENV", "development")
os.environ.setdefault("OMIXA_SECRET_KEY", "pytest-fixed-secret-key-not-for-production-use")
os.environ.setdefault("OMIXA_DB_PATH", os.path.join(tempfile.mkdtemp(prefix="omixa-ch-"), "omixa.db"))
os.environ.setdefault("OMIXA_TEMP_DIR", os.path.join(tempfile.mkdtemp(prefix="omixa-ch-"), "temp"))
os.environ.setdefault("RATE_LIMIT_PER_MINUTE", "0")
os.environ.setdefault("LOGIN_RATE_LIMIT_PER_MINUTE", "0")

from config import Config  # noqa: E402

OFFICIAL = "https://getomixa.com"
RAILWAY = ("omixaa.up.railway.app", "web-production-888aa.up.railway.app", "web-production-01c985.up.railway.app")


class Base(unittest.TestCase):
    prod = True
    base = OFFICIAL

    @classmethod
    def setUpClass(cls):
        from app import app
        cls.client = app.test_client()

    def setUp(self):
        for name, value in (("IS_PRODUCTION", self.prod), ("PUBLIC_BASE_URL", self.base)):
            p = mock.patch.object(Config, name, value)
            p.start()
            self.addCleanup(p.stop)

    def get(self, path, host="getomixa.com", method="GET", **kw):
        return self.client.open(path, method=method, base_url=f"https://{host}", **kw)


class RedirectTests(Base):
    def test_every_railway_host_redirects_permanently_with_path_and_query(self):
        for host in RAILWAY:
            for path, expect in (("/", "/"), ("/pricing?plan=pro&ref=a%20b", "/pricing?plan=pro&ref=a%20b"),
                                 ("/about", "/about"), ("/robots.txt", "/robots.txt"), ("/sitemap.xml", "/sitemap.xml"),
                                 ("/favicon.ico", "/favicon.ico"), ("/static/css/app.css", "/static/css/app.css")):
                r = self.get(path, host)
                self.assertEqual(r.status_code, 301, (host, path))
                self.assertEqual(r.headers["Location"], OFFICIAL + expect, (host, path))

    def test_head_requests_redirect_too(self):
        self.assertEqual(self.get("/pricing", RAILWAY[0], method="HEAD").status_code, 301)

    def test_host_matching_ignores_case_and_port(self):
        for host in ("OMIXAA.UP.RAILWAY.APP", "omixaa.up.railway.app:443", "omixaa.up.railway.app."):
            self.assertEqual(self.get("/pricing", host).status_code, 301, host)

    def test_official_domain_is_never_redirected_and_there_is_no_loop(self):
        for path in ("/", "/pricing", "/about", "/clean", "/login", "/robots.txt", "/sitemap.xml", "/favicon.ico"):
            r = self.get(path)
            self.assertNotIn(r.status_code, (301, 302, 307, 308), path)
        target = self.get("/pricing?x=1", RAILWAY[1]).headers["Location"]
        self.assertEqual(self.get(target.replace(OFFICIAL, "") or "/").status_code, 200)   # follow it: lands, stops

    def test_unknown_hosts_are_not_trusted_and_not_redirected(self):
        for host in ("evil.example", "getomixa.com.evil.example", "localhost", "healthcheck.railway.app"):
            r = self.get("/pricing", host)
            self.assertNotIn(r.status_code, (301, 302, 307, 308), host)
            self.assertNotIn("evil", r.headers.get("Location", ""))

    def test_redirect_origin_comes_from_config_never_from_the_request(self):
        from app import app
        from utils import canonical_host
        for raw in ("//evil.example/x", "///evil.example", "/\\evil.example", "/%2f%2fevil.example"):
            with app.test_request_context("/", base_url=f"https://{RAILWAY[0]}", environ_overrides={"PATH_INFO": raw}):
                target = canonical_host.redirect_target()
            self.assertTrue(target.startswith(OFFICIAL + "/"), (raw, target))
            self.assertEqual(target.split("/")[2], "getomixa.com", raw)
        r = self.get("/a%0d%0aSet-Cookie:x=1", RAILWAY[0])
        self.assertNotIn("\r", r.headers["Location"]); self.assertNotIn("\n", r.headers["Location"])
        r = self.get("/pricing", RAILWAY[0], headers={"X-Forwarded-Host": "evil.example", "Origin": "https://evil.example"})
        self.assertEqual(r.headers["Location"], OFFICIAL + "/pricing")

    def test_non_get_requests_are_not_redirected_so_webhooks_and_api_clients_keep_working(self):
        for method in ("POST", "PUT", "DELETE"):
            r = self.get("/api/billing/webhook", RAILWAY[0], method=method)
            self.assertNotIn(r.status_code, (301, 302, 307, 308), method)
            self.assertIn("noindex", r.headers.get("X-Robots-Tag", ""))

    def test_health_checks_answer_on_every_host_without_redirect(self):
        for host in RAILWAY + ("getomixa.com", "healthcheck.railway.app"):
            for path in ("/healthz", "/readyz"):
                self.assertNotIn(self.get(path, host).status_code, (301, 302, 307, 308), (host, path))
        self.assertEqual(self.get("/healthz", RAILWAY[0]).status_code, 200)
        self.assertIn("noindex", self.get("/healthz", RAILWAY[0]).headers["X-Robots-Tag"])
        self.assertNotIn("X-Robots-Tag", {k.title(): v for k, v in self.get("/", "getomixa.com").headers.items()})


class DisabledTests(unittest.TestCase):
    """No redirect unless production AND OMIXA_PUBLIC_BASE_URL are both set (dev, tests, staging stay untouched)."""

    @classmethod
    def setUpClass(cls):
        from app import app
        cls.client = app.test_client()

    def hit(self, prod, base):
        with mock.patch.object(Config, "IS_PRODUCTION", prod), mock.patch.object(Config, "PUBLIC_BASE_URL", base):
            return self.client.get("/pricing", base_url=f"https://{RAILWAY[0]}")

    def test_not_production(self):
        self.assertEqual(self.hit(False, OFFICIAL).status_code, 200)

    def test_base_url_unset(self):
        self.assertEqual(self.hit(True, "").status_code, 200)


class MetadataTests(Base):
    PAGES = ("/", "/pricing", "/about", "/clean")

    def test_canonical_is_single_and_official_with_no_noindex(self):
        canon = {"/": "/", "/pricing": "/pricing", "/about": "/about", "/clean": "/"}
        for path in self.PAGES:
            r = self.get(path)
            html = r.get_data(as_text=True)
            self.assertEqual(r.status_code, 200, path)
            self.assertEqual(re.findall(r'<link rel="canonical" href="([^"]+)"', html), [OFFICIAL + canon[path]], path)
            self.assertNotIn("X-Robots-Tag", r.headers, path)
            self.assertEqual(re.findall(r'<meta name="robots" content="([^"]+)"', html), ["index,follow"], path)
            self.assertNotIn("railway", html.lower(), path)

    def test_homepage_title_description_og_theme_and_icons(self):
        html = self.get("/").get_data(as_text=True)
        self.assertEqual(len(re.findall(r"<title>", html)), 1)
        self.assertIn("<title>Omixa: Clean CSV and Excel Files, Check Data Quality</title>", html)
        self.assertEqual(len(re.findall(r'<meta name="description"', html)), 1)
        self.assertEqual(len(re.findall(r'rel="canonical"', html)), 1)
        self.assertIn('<meta name="theme-color" content="#381593">', html)
        self.assertIn(f'<meta property="og:url" content="{OFFICIAL}/">', html)
        self.assertIn(f'<meta property="og:image" content="{OFFICIAL}/static/brand/omixa-logo-square.jpg">', html)
        self.assertIn('<link rel="icon" href="/favicon.ico" sizes="any">', html)
        for size, name in (("192x192", "android-chrome-192x192.png"), ("32x32", "favicon-32x32.png"), ("16x16", "favicon-16x16.png")):
            self.assertRegex(html, rf'<link rel="icon" type="image/png" sizes="{size}" href="/static/{name}">')
        self.assertIn('rel="apple-touch-icon"', html)

    def test_robots_and_sitemap_use_only_the_official_origin(self):
        robots = self.get("/robots.txt").get_data(as_text=True)
        self.assertIn(f"Sitemap: {OFFICIAL}/sitemap.xml", robots)
        self.assertNotRegex(robots, r"(?m)^Disallow:\s*/\s*$")
        locs = re.findall(r"<loc>(.*?)</loc>", self.get("/sitemap.xml").get_data(as_text=True))
        self.assertTrue(locs and all(u.startswith(OFFICIAL + "/") for u in locs))

    def test_json_ld_uses_official_origin_and_the_brand_icon(self):
        html = self.get("/").get_data(as_text=True)
        self.assertIn(f"{OFFICIAL}/static/brand/omixa-icon.png", html)
        self.assertNotIn("railway", html.lower())


class FaviconTests(Base):
    def test_root_favicon_is_public_a_real_ico_and_cookie_and_noindex_free(self):
        r = self.get("/favicon.ico")
        self.assertEqual(r.status_code, 200)
        self.assertIn(r.mimetype, ("image/x-icon", "image/vnd.microsoft.icon"))
        self.assertEqual(r.data[:4], b"\x00\x00\x01\x00")
        self.assertNotIn("Set-Cookie", r.headers)
        self.assertNotIn("X-Robots-Tag", r.headers)
        with open("static/favicon.ico", "rb") as f:
            self.assertEqual(r.data, f.read())

    def test_every_linked_icon_resolves_to_a_square_png_or_ico(self):
        import struct
        for name, side in (("android-chrome-192x192.png", 192), ("favicon-32x32.png", 32), ("favicon-16x16.png", 16),
                           ("apple-touch-icon.png", 180)):
            r = self.get("/static/" + name)
            self.assertEqual((r.status_code, r.mimetype), (200, "image/png"), name)
            self.assertEqual(struct.unpack(">II", r.data[16:24]), (side, side), name)
            self.assertNotIn("Set-Cookie", r.headers)
        self.assertEqual(self.get("/static/site.webmanifest").status_code, 200)


class BrandingTests(Base):
    def test_header_and_signin_use_the_official_mark(self):
        for path in ("/", "/login"):
            self.assertIn("brand/omixa-icon.png", self.get(path).get_data(as_text=True), path)


if __name__ == "__main__":
    unittest.main()
