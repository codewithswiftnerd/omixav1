"""Search-engine discovery: robots.txt, sitemap.xml, canonical URLs, indexing directives, structured data.

Design rules (see docs/SEO.md):
  * ONE registry (PUBLIC_PAGES) says which pages are indexable. The sitemap, canonical tags, robots meta and
    JSON-LD all read from it, so they cannot disagree.
  * Everything not in the registry is treated as private: `noindex` via both the X-Robots-Tag header and a
    <meta name="robots"> tag. robots.txt only blocks /api/ and /admin/; it deliberately does NOT block
    noindex pages, because a crawler must be able to fetch a page to see its noindex. robots.txt is a crawl
    hint, not access control; real protection stays in the auth and ownership checks.
  * Non-production environments serve `Disallow: /` and noindex everywhere so a staging copy never gets indexed.
"""
from flask import Blueprint, Response, request

from config import Config
from routes.guides import GUIDES, GUIDE_ORDER, guide_nav

seo_bp = Blueprint("seo", __name__)

# path -> canonical path. Only pages that exist, render without sign-in, and carry real content.
# "/clean" renders the same tool as "/", so it canonicalises to "/" and stays out of the sitemap.
PUBLIC_PAGES = {"/": "/", "/clean": "/", "/pricing": "/pricing", "/about": "/about"}
PUBLIC_PAGES.update({f"/{slug}": f"/{slug}" for slug in GUIDE_ORDER})

# Pages listed in the sitemap = distinct canonical targets only (never "/clean").
SITEMAP_PATHS = ("/", "/pricing", "/about") + tuple(f"/{slug}" for slug in GUIDE_ORDER)

# Public contact details supplied by the Omixa team.
CONTACT_EMAIL = "omixa.team@gmail.com"
SOCIAL_X = "https://x.com/omixahq"
SOCIAL_TIKTOK = "https://www.tiktok.com/@omixahq"
SOCIAL_FACEBOOK = "https://www.facebook.com/profile.php?id=61593523481913"

# Fixed, truthful descriptions of what the product does today (verified against landing/pricing templates).
SOFTWARE_FEATURES = (
    "Upload CSV, XLSX and XLS files",
    "Duplicate-row detection and removal",
    "Whitespace, date, phone, email and category standardisation",
    "Data-quality score and issue report",
    "Before and after preview with a change log",
)


def base_url() -> str:
    """Production origin. OMIXA_PUBLIC_BASE_URL wins; falling back to the request host keeps dev/test working."""
    return Config.PUBLIC_BASE_URL or request.host_url.rstrip("/")


def indexing_allowed() -> bool:
    return bool(Config.IS_PRODUCTION)


def is_indexable_path(path: str) -> bool:
    return indexing_allowed() and path in PUBLIC_PAGES


def canonical_url(path: str) -> str | None:
    target = PUBLIC_PAGES.get(path)
    return f"{base_url()}{target}" if target else None


def structured_data(path: str) -> list[dict] | None:
    """JSON-LD. Home: Organization (with the team's contact email and the X/TikTok profiles they supplied), WebSite and
    SoftwareApplication. Guides: BreadcrumbList. No ratings, reviews, counts, awards or founding data, because none
    exist. The only Offer is the Free plan ($0), which the pricing page states."""
    origin = base_url()
    slug = path.lstrip("/")
    if slug in GUIDES:   # guide pages: breadcrumb trail only
        return [{"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Omixa", "item": f"{origin}/"},
            {"@type": "ListItem", "position": 2, "name": GUIDES[slug]["nav"], "item": f"{origin}{path}"}]}]
    if path != "/":
        return None
    return [
        {"@context": "https://schema.org", "@type": "Organization", "name": "Omixa", "url": f"{origin}/",
         "logo": f"{origin}/static/brand/omixa-icon.png", "email": CONTACT_EMAIL,
         "contactPoint": {"@type": "ContactPoint", "contactType": "customer support", "email": CONTACT_EMAIL},
         "sameAs": [SOCIAL_X, SOCIAL_TIKTOK, SOCIAL_FACEBOOK]},
        {"@context": "https://schema.org", "@type": "WebSite", "name": "Omixa", "url": f"{origin}/"},
        {"@context": "https://schema.org", "@type": "SoftwareApplication", "name": "Omixa",
         "url": f"{origin}/", "applicationCategory": "BusinessApplication", "operatingSystem": "Web browser",
         "description": "Web app that cleans CSV and Excel files and reports on data quality.",
         "featureList": list(SOFTWARE_FEATURES),
         "offers": {"@type": "Offer", "name": "Free", "price": "0", "priceCurrency": "USD"}},
    ]


def seo_context() -> dict:
    path = request.path
    indexable = is_indexable_path(path)
    return {
        "seo_robots": "index,follow" if indexable else "noindex,nofollow",
        "seo_canonical": canonical_url(path) if indexable else None,
        "seo_jsonld": structured_data(path) if indexable else None,
        "seo_origin": base_url(),
        "contact_email": CONTACT_EMAIL, "social_x": SOCIAL_X, "social_tiktok": SOCIAL_TIKTOK, "social_facebook": SOCIAL_FACEBOOK,
        "guide_nav": guide_nav(),
    }


@seo_bp.get("/robots.txt")
def robots():
    if indexing_allowed():
        body = ("User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /admin/\n\n"
                f"Sitemap: {base_url()}/sitemap.xml\n")
    else:
        body = "User-agent: *\nDisallow: /\n"
    return Response(body, mimetype="text/plain")


@seo_bp.get("/sitemap.xml")
def sitemap():
    if not indexing_allowed():
        return Response("Not found", status=404, mimetype="text/plain")
    origin = base_url()
    urls = "".join(f"  <url><loc>{origin}{p}</loc></url>\n" for p in SITEMAP_PATHS)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + urls + "</urlset>\n")
    return Response(xml, mimetype="application/xml")
