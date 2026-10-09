# Omixa brand rules

## Colours come from the logo
Sampled from `static/brand/omixa-logo-square.jpg`:

| Token | Value | Where it is in the logo |
|---|---|---|
| `--omixa-violet` | `#A554F7` | the middle tile |
| `--omixa-violet-dark` | `#8541C8` | the tile's shadow |
| `--omixa-glow` | `#6834D6` | the bright top-right of the background |
| `--omixa-indigo` | `#381593` | the background |
| `--omixa-indigo-deep` | `#201551` | the lower background |
| `--omixa-ink` | `#0E0524` | the outline of the "O" |
| `--omixa-canvas` | `#E6EBE5` | the off-white tile and letter |

Tints (`--omixa-tint`, `--omixa-violet-light`, `--omixa-canvas-soft`) are lighter versions of the above, and the grey
scale is tinted from the ink. Status colours (green / amber / red) exist only to signal meaning; they are not brand colours.
Pink, lime, beige and neutral black are not part of Omixa.

**Rule:** a colour literal may only be written inside a `:root` block in `static/css/app.css` or `site.css`.
Everywhere else use `var(--token)`. `tests/test_brand_consistency.py` fails the build otherwise.

## Type
Urbanist for all frontend text, including form controls, tables, code and numbers (`var(--font-sans)`).
It is loaded once in `templates/base.html`. Do not write `font-family` anywhere except `--font-sans`.

## Mobile
- Content never scrolls the page sideways; wide tables scroll inside their card.
- Inputs are 16px on phones (no iOS zoom) and tap targets are at least 44px.
- Notches and home indicators are respected (`viewport-fit=cover` + `env(safe-area-inset-*)`).
- No inline `style=` attributes in Pro templates: use the utility classes at the end of `site.css`.
