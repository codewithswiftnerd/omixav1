"""Brand guard: colours come from the logo, text is Urbanist, and Pro pages stay mobile-safe.

Brand colours are sampled from static/brand/omixa-logo-square.jpg and defined once, in :root blocks.
Anywhere else a colour must be a var(--token), so an off-brand colour can't creep back in.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSS = [ROOT / "static/css/app.css", ROOT / "static/css/site.css"]
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
RGBA = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)")

# Logo-derived RGB triples that rgba() overlays may use (white, ink, indigo, violet, glow, tint)
ALLOWED_RGB = {(255, 255, 255), (14, 5, 36), (56, 21, 147), (165, 84, 247), (104, 52, 214), (244, 238, 253)}
OFF_BRAND = ["#f472b6", "#dff06c", "#8b5cf6", "#131313", "#111113", "#201159", "#0e0a22", "#3c2d78", "#2a2a30"]


def strip_root_blocks(css: str) -> str:
    out, i = [], 0
    for m in re.finditer(r":root\s*\{", css):
        out.append(css[i:m.start()])
        depth, j = 1, m.end()
        while depth and j < len(css):
            depth += {"{": 1, "}": -1}.get(css[j], 0)
            j += 1
        i = j
    out.append(css[i:])
    return "".join(out)


def strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def test_no_colour_literals_outside_root_tokens():
    for f in CSS:
        body = strip_root_blocks(strip_comments(f.read_text()))
        assert HEX.findall(body) == [], f"{f.name}: hex colour outside :root: {HEX.findall(body)}"


def test_rgba_overlays_use_brand_colours_only():
    for f in CSS:
        for r, g, b in RGBA.findall(strip_comments(f.read_text())):
            assert (int(r), int(g), int(b)) in ALLOWED_RGB, f"{f.name}: off-brand rgba({r},{g},{b})"


def test_off_brand_colours_are_gone_everywhere():
    for f in list((ROOT / "static").rglob("*.css")) + list((ROOT / "static").rglob("*.js")) + list((ROOT / "templates").rglob("*.html")) + [ROOT / "static/site.webmanifest"]:
        text = f.read_text().lower()
        for bad in OFF_BRAND:
            assert bad not in text, f"{f.relative_to(ROOT)} still contains {bad}"
    assert "--g-pink" not in (ROOT / "static/css/site.css").read_text()
    assert "--g-lime" not in (ROOT / "static/css/site.css").read_text()


def test_tokens_match_the_logo():
    app = (ROOT / "static/css/app.css").read_text().lower()
    for name, value in {"--omixa-violet": "#a554f7", "--omixa-indigo": "#381593", "--omixa-glow": "#6834d6",
                        "--omixa-indigo-deep": "#201551", "--omixa-ink": "#0e0524", "--omixa-canvas": "#e6ebe5"}.items():
        assert f"{name}: {value}" in app, name


def test_urbanist_is_the_only_font():
    for f in CSS:
        for decl in re.findall(r"font-family\s*:\s*([^;}]+)", strip_comments(f.read_text())):
            d = decl.strip()
            assert d.startswith("var(--font-sans)") or d.startswith('"Urbanist"') or d == "inherit", f"{f.name}: font-family {d}"
    for t in (ROOT / "templates").rglob("*.html"):
        assert "font-family" not in t.read_text(), f"{t.name}: inline font-family"
    base = (ROOT / "templates/base.html").read_text()
    assert "family=Urbanist" in base and "Roboto" not in base


def test_form_controls_and_code_inherit_urbanist():
    site = strip_comments((ROOT / "static/css/site.css").read_text())
    block = re.search(r"html, body, button, input[^{]*\{([^}]*)\}", site)
    assert block and "var(--font-sans)" in block.group(1)
    for tag in ("button", "input", "select", "textarea", "table", "code", "pre"):
        assert re.search(rf"\b{tag}\b", block.group(0).split("{")[0]), tag


def test_pricing_classes_are_styled():
    css = "".join(f.read_text() for f in CSS)
    for cls in ("price-card", "price-list", "price", "pricing-3", "featured", "plain-list"):
        assert re.search(rf"\.{cls}\b", css), f".{cls} has no CSS"


def test_mobile_basics_present():
    base = (ROOT / "templates/base.html").read_text()
    assert "viewport-fit=cover" in base
    css = (ROOT / "static/css/site.css").read_text()
    assert "@media (max-width: 640px)" in css and "font-size: 16px" in css      # no iOS zoom on focus
    assert "env(safe-area-inset-bottom" in css and "min-height: 44px" in css     # notches + tap targets


def test_no_inline_styles_or_duplicate_scripts_in_pro_templates():
    for name in ("pricing", "dashboard", "batch", "profile_edit", "session", "login"):
        t = (ROOT / "templates" / f"{name}.html").read_text()
        assert "style=" not in t.replace('style="display:none"', ""), name
        assert "pro-common.js" not in t and "nav-auth.js" not in t, f"{name} reloads base scripts"
        assert not re.search(r'class="[^"]*"[^<>]*\sclass="', t), f"{name}: duplicate class attribute"
