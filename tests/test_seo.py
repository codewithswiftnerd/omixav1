"""SEO guard: discovery files, indexing directives, canonicals and structured data stay correct."""
import json
import re

import pytest

from routes import seo

PRIVATE = ["/login", "/dashboard", "/profiles/new", "/profiles/abc", "/sessions/abc", "/batch", "/admin/login"]


@pytest.fixture()
def client():
    from app import app
    return app.test_client()


def _prod(monkeypatch, base="https://omixa.example"):
    from config import Config
    monkeypatch.setattr(Config, "IS_PRODUCTION", True)
    monkeypatch.setattr(Config, "PUBLIC_BASE_URL", base)


def test_robots_in_production_allows_site_blocks_api_admin_and_points_to_sitemap(client, monkeypatch):
    _prod(monkeypatch)
    r = client.get("/robots.txt")
    assert r.status_code == 200 and r.mimetype == "text/plain"
    t = r.get_data(as_text=True)
    assert "Allow: /" in t and "Disallow: /api/" in t and "Disallow: /admin/" in t
    assert "Sitemap: https://omixa.example/sitemap.xml" in t
    assert not re.search(r"^Disallow:\s*/\s*$", t, re.M), "production robots.txt must not block the whole site"
    assert "Set-Cookie" not in r.headers


def test_robots_outside_production_blocks_everything(client):
    t = client.get("/robots.txt").get_data(as_text=True)   # tests run with FLASK_ENV=development
    assert re.search(r"^Disallow:\s*/\s*$", t, re.M)
    assert client.get("/sitemap.xml").status_code == 404


def test_sitemap_lists_only_canonical_public_pages(client, monkeypatch):
    _prod(monkeypatch)
    r = client.get("/sitemap.xml")
    assert r.status_code == 200 and r.mimetype == "application/xml"
    locs = re.findall(r"<loc>(.*?)</loc>", r.get_data(as_text=True))
    assert locs == [f"https://omixa.example{p}" for p in seo.SITEMAP_PATHS]
    assert len(locs) == len(set(locs)) == 11
    paths = {u.replace("https://omixa.example", "") for u in locs}
    for bad in ("/clean", "/login", "/dashboard", "/admin", "/api", "/sessions", "/profiles", "/batch"):
        assert bad not in paths and not any(p.startswith(bad + "/") for p in paths)


@pytest.mark.parametrize("path,canon", [("/", "https://omixa.example/"), ("/pricing", "https://omixa.example/pricing"),
                                         ("/clean", "https://omixa.example/")])
def test_public_pages_are_indexable_with_one_canonical(client, monkeypatch, path, canon):
    _prod(monkeypatch)
    r = client.get(path)
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "X-Robots-Tag" not in r.headers
    assert re.findall(r'<link rel="canonical" href="([^"]+)"', html) == [canon]
    assert '<meta name="robots" content="index,follow">' in html
    assert len(re.findall(r"<title>", html)) == 1 and len(re.findall(r'name="description"', html)) == 1


@pytest.mark.parametrize("path", PRIVATE + ["/does-not-exist", "/api/health"])
def test_private_and_error_pages_are_noindex(client, monkeypatch, path):
    _prod(monkeypatch)
    r = client.get(path)
    assert "noindex" in r.headers.get("X-Robots-Tag", "")
    if "text/html" in r.content_type:
        assert 'content="noindex,nofollow"' in r.get_data(as_text=True)
        assert "rel=\"canonical\"" not in r.get_data(as_text=True)


def test_titles_and_descriptions_are_unique_across_public_pages(client, monkeypatch):
    _prod(monkeypatch)
    seen = {}
    for p in ("/", "/pricing"):
        h = client.get(p).get_data(as_text=True)
        t = re.search(r"<title>(.*?)</title>", h).group(1)
        d = re.search(r'name="description" content="([^"]*)"', h).group(1)
        assert 20 < len(t) <= 70 and 70 < len(d) <= 320
        assert t not in seen.get("t", set()) and d not in seen.get("d", set())
        seen.setdefault("t", set()).add(t); seen.setdefault("d", set()).add(d)


def test_structured_data_is_home_only_valid_json_and_has_no_invented_claims(client, monkeypatch):
    _prod(monkeypatch)
    h = client.get("/").get_data(as_text=True)
    blocks = [json.loads(b) for b in re.findall(r'<script type="application/ld\+json">(.*?)</script>', h, re.S)]
    assert [b["@type"] for b in blocks] == ["Organization", "WebSite", "SoftwareApplication"]
    keys = set()
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items(): keys.add(k.lower()); walk(v)
        elif isinstance(o, list):
            for v in o: walk(v)
    walk(blocks)
    for forbidden in ("aggregaterating", "review", "award", "foundingdate", "ratingvalue", "potentialaction"):
        assert forbidden not in keys
    org = blocks[0]
    assert org["email"] == "omixa.team@gmail.com"
    assert org["sameAs"] == ["https://x.com/omixahq", "https://www.tiktok.com/@omixahq",
                             "https://www.facebook.com/profile.php?id=61593523481913"]   # only profiles the team gave us
    app_block = blocks[2]
    assert app_block["offers"]["price"] == "0"
    assert "ld+json" not in client.get("/pricing").get_data(as_text=True)
    assert "ld+json" not in client.get("/about").get_data(as_text=True)
    assert "ld+json" not in client.get("/clean").get_data(as_text=True)


def test_robots_and_sitemap_do_not_start_a_session(client, monkeypatch):
    _prod(monkeypatch)
    assert "Set-Cookie" not in client.get("/sitemap.xml").headers
    assert "Set-Cookie" not in client.get("/robots.txt").headers


def test_registry_and_sitemap_agree():
    assert set(seo.SITEMAP_PATHS) <= set(seo.PUBLIC_PAGES)
    assert all(seo.PUBLIC_PAGES[p] == p for p in seo.SITEMAP_PATHS)   # sitemap holds canonical targets only


GUIDE_PATHS = ["/" + s for s in seo.GUIDE_ORDER]


@pytest.mark.parametrize("path", GUIDE_PATHS + ["/about"])
def test_content_pages_render_with_one_h1_canonical_and_unique_meta(client, monkeypatch, path):
    _prod(monkeypatch)
    r = client.get(path)
    h = r.get_data(as_text=True)
    assert r.status_code == 200 and "X-Robots-Tag" not in r.headers
    assert len(re.findall(r"<h1[ >]", h)) == 1
    assert re.findall(r'<link rel="canonical" href="([^"]+)"', h) == [f"https://omixa.example{path}"]
    assert "{" not in re.search(r"<title>(.*?)</title>", h).group(1)
    assert "{free_mb}" not in h and "{ttl_minutes}" not in h and "{pro_mb}" not in h
    assert 'href="/clean"' in h                                    # every content page links to the tool
    if path != "/about":
        ld = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', h, re.S).group(1))
        assert ld["@type"] == "BreadcrumbList" and ld["itemListElement"][1]["item"].endswith(path)


def test_all_titles_and_descriptions_are_unique(client, monkeypatch):
    _prod(monkeypatch)
    titles, descs = [], []
    for p in ["/", "/pricing", "/about"] + GUIDE_PATHS:
        h = client.get(p).get_data(as_text=True)
        titles.append(re.search(r"<title>(.*?)</title>", h).group(1))
        descs.append(re.search(r'name="description" content="([^"]*)"', h).group(1))
    assert len(set(titles)) == len(titles) and len(set(descs)) == len(descs)
    assert all(len(t) <= 70 for t in titles) and all(len(d) <= 175 for d in descs)


def test_every_public_page_is_linked_from_the_footer_and_home(client):
    home = client.get("/").get_data(as_text=True)
    for p in GUIDE_PATHS + ["/about", "/pricing"]:
        assert f'href="{p}"' in home, f"{p} is not linked from the home page (internal linking)"


def test_contact_details_are_shown_with_all_three_profiles(client):
    h = client.get("/about").get_data(as_text=True)
    assert "mailto:omixa.team@gmail.com" in h and "https://x.com/omixahq" in h and "tiktok.com/@omixahq" in h
    assert "facebook.com/profile.php?id=61593523481913" in h


def test_home_h1_names_the_product_category(client):
    h = client.get("/").get_data(as_text=True)
    h1 = re.sub(r"<[^>]+>", "", re.search(r"<h1.*?</h1>", h, re.S).group(0))
    assert "CSV" in h1 and "Excel" in h1
    low = h.lower()
    assert "instantly" not in low.split("</h1>")[0].split("<h1")[-1]   # unverifiable speed claim removed from the hero
    assert " ai " not in re.sub(r"<[^>]+>", " ", low)                  # not positioned as an AI product
