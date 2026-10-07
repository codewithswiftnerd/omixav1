"""
Currency symbols and abbreviations used when cleaning numbers stored as text
("₦1,200.50", "GH₵ 45", "KSh 3,000", "R 1 250", "NGN 5000").

Each row is (ISO code, countries, symbols/abbreviations). The ISO code itself is always
recognised as well. Adding a currency = adding a row; nothing else needs to change.

Matching rules (see strip_currency):
  - Pure symbol characters (₦ ₵ ₹ € £ ...) are stripped anywhere in the value.
  - Letter-based tokens (KSh, Rs, NGN, zł ...) are only stripped when they sit directly
    in front of the number ("Rs 500", "NGN 5000"). Behind the number only ISO codes
    ("500 NGN") and a short list of unambiguous abbreviations ("500 kr") are stripped,
    so units like "5 km", "10 ft" or "3 MT" are never mistaken for money.
  - A single-letter token (N, R, Q, L) is only stripped IN FRONT of the number.
  - ISO codes and single letters are case-sensitive (NGN, not "ngn"); other tokens are not.
Whatever is left must still parse as a number, otherwise the column is left untouched.
"""

from __future__ import annotations

import re

# (ISO code, countries, extra symbols / abbreviations)
CURRENCIES: list[tuple[str, str, tuple[str, ...]]] = [
    # --- Africa ---
    ("NGN", "Nigeria", ("₦", "N", "Naira")),
    ("GHS", "Ghana", ("₵", "GH₵", "GH¢", "GHC", "Cedi", "Cedis")),
    ("KES", "Kenya", ("KSh", "Ksh", "KShs")),
    ("ZAR", "South Africa", ("R",)),
    ("TZS", "Tanzania", ("TSh", "TZS", "Tsh")),
    ("UGX", "Uganda", ("USh", "UShs")),
    ("RWF", "Rwanda", ("FRw", "RF")),
    ("ETB", "Ethiopia", ("Br", "ብር")),
    ("EGP", "Egypt", ("E£", "LE", "ج.م")),
    ("MAD", "Morocco", ("DH", "Dhs", "د.م.")),
    ("DZD", "Algeria", ("DA", "دج")),
    ("TND", "Tunisia", ("DT", "د.ت")),
    ("LYD", "Libya", ("LD",)),
    ("XOF", "Senegal, Côte d'Ivoire, Mali, Burkina Faso, Benin, Togo, Niger, Guinea-Bissau", ("CFA", "FCFA", "F CFA")),
    ("XAF", "Cameroon, Chad, Gabon, Congo, Central African Republic, Equatorial Guinea", ("FCFA", "CFA")),
    ("ZMW", "Zambia", ("ZK", "K")),
    ("BWP", "Botswana", ("P",)),
    ("NAD", "Namibia", ("N$",)),
    ("MZN", "Mozambique", ("MT", "MTn")),
    ("AOA", "Angola", ("Kz",)),
    ("MWK", "Malawi", ("MK",)),
    ("SZL", "Eswatini", ("SZL",)),
    ("LSL", "Lesotho", ("LSL",)),
    ("MUR", "Mauritius", ("Rs",)),
    ("SCR", "Seychelles", ("SR", "SRe")),
    ("GMD", "Gambia", ("D",)),
    ("SLE", "Sierra Leone", ("Le",)),
    ("LRD", "Liberia", ("L$", "LD$")),
    ("CDF", "DR Congo", ("FC",)),
    ("ZWL", "Zimbabwe", ("Z$", "ZWL")),
    ("SDG", "Sudan", ("SDG", "ج.س.")),
    ("SSP", "South Sudan", ("SSP",)),
    ("SOS", "Somalia", ("Sh.So.", "SOS")),
    ("ERN", "Eritrea", ("Nfk",)),
    ("DJF", "Djibouti", ("Fdj",)),
    ("MGA", "Madagascar", ("Ar",)),
    ("BIF", "Burundi", ("FBu",)),
    ("CVE", "Cape Verde", ("Esc",)),
    ("GNF", "Guinea", ("FG", "GNF")),
    ("STN", "São Tomé and Príncipe", ("Db",)),
    ("MRU", "Mauritania", ("UM", "MRU")),
    ("KMF", "Comoros", ("CF", "KMF")),
    # --- Europe ---
    ("EUR", "Germany, France, Italy, Spain, Ireland, Netherlands, Austria, Belgium, Finland, Greece, Portugal, Croatia", ("€", "EUR")),
    ("GBP", "United Kingdom", ("£", "GBP")),
    ("CHF", "Switzerland, Liechtenstein", ("Fr.", "SFr.", "SFr")),
    ("SEK", "Sweden", ("kr", "Kr")),
    ("NOK", "Norway", ("kr", "Kr")),
    ("DKK", "Denmark", ("kr", "Kr", "DKK")),
    ("ISK", "Iceland", ("kr", "Kr", "ISK")),
    ("PLN", "Poland", ("zł", "zl")),
    ("CZK", "Czech Republic", ("Kč", "Kc")),
    ("HUF", "Hungary", ("Ft",)),
    ("RON", "Romania", ("lei", "Lei", "RON")),
    ("BGN", "Bulgaria", ("лв", "лв.")),
    ("RSD", "Serbia", ("дин.", "din.", "din", "РСД")),
    ("MKD", "North Macedonia", ("ден", "ден.", "den")),
    ("ALL", "Albania", ("Lek", "L")),
    ("BAM", "Bosnia and Herzegovina", ("KM",)),
    ("MDL", "Moldova", ("L", "lei")),
    ("UAH", "Ukraine", ("₴", "грн", "грн.")),
    ("RUB", "Russia", ("₽", "руб", "руб.", "р.")),
    ("BYN", "Belarus", ("Br", "BYN")),
    ("GEL", "Georgia", ("₾", "GEL")),
    ("AMD", "Armenia", ("֏", "դր.")),
    ("AZN", "Azerbaijan", ("₼", "AZN")),
    ("TRY", "Türkiye", ("₺", "TL")),
    # --- Americas ---
    ("USD", "United States, Ecuador, El Salvador, Panama, Puerto Rico", ("$", "US$", "U$", "USD")),
    ("CAD", "Canada", ("CA$", "C$", "CAD")),
    ("MXN", "Mexico", ("Mex$", "MX$", "MN$", "MXN")),
    ("BRL", "Brazil", ("R$",)),
    ("ARS", "Argentina", ("AR$", "$a")),
    ("CLP", "Chile", ("CL$", "CLP")),
    ("COP", "Colombia", ("COL$", "COP")),
    ("PEN", "Peru", ("S/", "S/.", "PEN")),
    ("UYU", "Uruguay", ("$U", "UYU")),
    ("BOB", "Bolivia", ("Bs", "Bs.", "BOB")),
    ("PYG", "Paraguay", ("₲", "Gs", "Gs.")),
    ("VES", "Venezuela", ("Bs.S", "Bs.", "VES")),
    ("GTQ", "Guatemala", ("Q",)),
    ("HNL", "Honduras", ("L",)),
    ("NIO", "Nicaragua", ("C$",)),
    ("CRC", "Costa Rica", ("₡",)),
    ("PAB", "Panama", ("B/.", "B/")),
    ("DOP", "Dominican Republic", ("RD$",)),
    ("JMD", "Jamaica", ("J$",)),
    ("TTD", "Trinidad and Tobago", ("TT$",)),
    ("BBD", "Barbados", ("Bds$", "BBD")),
    ("BSD", "Bahamas", ("B$", "BSD")),
    ("HTG", "Haiti", ("G", "HTG")),
    ("GYD", "Guyana", ("G$", "GY$")),
    ("SRD", "Suriname", ("SRD",)),
    ("BZD", "Belize", ("BZ$",)),
    ("CUP", "Cuba", ("CUP", "$MN")),
    # --- Middle East ---
    ("ILS", "Israel", ("₪", "NIS")),
    ("SAR", "Saudi Arabia", ("SR", "SAR", "﷼", "ر.س")),
    ("AED", "United Arab Emirates", ("AED", "Dhs", "د.إ")),
    ("QAR", "Qatar", ("QR", "QAR", "ر.ق")),
    ("KWD", "Kuwait", ("KD", "KWD", "د.ك")),
    ("BHD", "Bahrain", ("BD", "BHD", ".د.ب")),
    ("OMR", "Oman", ("OMR", "ر.ع.")),
    ("JOD", "Jordan", ("JD", "JOD", "د.ا")),
    ("LBP", "Lebanon", ("L£", "LL", "ل.ل")),
    ("IRR", "Iran", ("﷼", "IRR")),
    ("IQD", "Iraq", ("IQD", "ع.د")),
    ("SYP", "Syria", ("S£", "SYP")),
    ("YER", "Yemen", ("YER", "﷼")),
    # --- Asia ---
    ("JPY", "Japan", ("¥", "JP¥", "円")),
    ("CNY", "China", ("¥", "CN¥", "元", "RMB")),
    ("HKD", "Hong Kong", ("HK$", "HKD")),
    ("TWD", "Taiwan", ("NT$", "NT")),
    ("KRW", "South Korea", ("₩", "KRW")),
    ("INR", "India", ("₹", "Rs", "Rs.")),
    ("PKR", "Pakistan", ("Rs", "Rs.", "PKR")),
    ("BDT", "Bangladesh", ("৳", "Tk", "Tk.")),
    ("LKR", "Sri Lanka", ("Rs", "Rs.", "රු")),
    ("NPR", "Nepal", ("Rs", "Rs.", "रू", "रु")),
    ("BTN", "Bhutan", ("Nu.", "Nu")),
    ("MVR", "Maldives", ("Rf", "Rf.", "MVR")),
    ("AFN", "Afghanistan", ("؋", "Af")),
    ("THB", "Thailand", ("฿", "THB")),
    ("VND", "Vietnam", ("₫", "đ", "VND")),
    ("IDR", "Indonesia", ("Rp", "Rp.")),
    ("MYR", "Malaysia", ("RM",)),
    ("SGD", "Singapore", ("S$", "SGD")),
    ("PHP", "Philippines", ("₱", "PhP", "Php")),
    ("KHR", "Cambodia", ("៛", "KHR")),
    ("LAK", "Laos", ("₭", "LAK")),
    ("MMK", "Myanmar", ("K", "Ks", "MMK")),
    ("MNT", "Mongolia", ("₮", "MNT")),
    ("KZT", "Kazakhstan", ("₸", "KZT")),
    ("UZS", "Uzbekistan", ("so'm", "сўм", "UZS")),
    ("KGS", "Kyrgyzstan", ("сом", "KGS")),
    ("TJS", "Tajikistan", ("SM", "TJS")),
    ("TMT", "Turkmenistan", ("TMT",)),
    ("BND", "Brunei", ("B$", "BND")),
    # --- Oceania ---
    ("AUD", "Australia, Kiribati, Tuvalu, Nauru", ("A$", "AU$", "AUD")),
    ("NZD", "New Zealand", ("NZ$", "NZD")),
    ("FJD", "Fiji", ("FJ$", "FJD")),
    ("PGK", "Papua New Guinea", ("K", "PGK")),
    ("WST", "Samoa", ("WS$", "WST")),
    ("TOP", "Tonga", ("T$", "TOP")),
    ("VUV", "Vanuatu", ("VT", "VUV")),
    ("SBD", "Solomon Islands", ("SI$", "SBD")),
]


# Abbreviations that are safe to strip AFTER a number (not also common units of measure).
_TRAILING_SAFE = (
    "kr", "zł", "zl", "Kč", "Kc", "lei", "TL", "руб.", "руб", "грн.", "грн", "лв.", "лв",
    "дин.", "ден.", "CFA", "FCFA", "Naira", "Cedi", "Cedis", "元", "円", "Lek", "Tk.", "Tk",
    "Rp.", "Rp", "Rs.", "Rs", "KSh", "Ksh", "TSh", "USh", "RM", "SR", "QR", "KD", "JD",
)


def _collect() -> tuple[str, list[str], list[str], list[str]]:
    """Returns (symbol_chars, iso_codes, other_multi_char_tokens, single_letter_tokens)."""
    chars: set[str] = set()
    iso: set[str] = set()
    multi: set[str] = set()
    single: set[str] = set()
    for code, _countries, tokens in CURRENCIES:
        iso.add(code)
        for tok in tokens:
            if len(tok) == 1 and not tok.isalpha():
                chars.add(tok)
            elif len(tok) == 1:
                single.add(tok)
            elif tok in iso or (len(tok) == 3 and tok.isupper() and tok.isalpha()):
                iso.add(tok)
            else:
                multi.add(tok)
    by_len = lambda items: sorted(items, key=lambda t: (-len(t), t))
    return "".join(sorted(chars)), by_len(iso), by_len(multi), by_len(single)


SYMBOL_CHARS, _ISO, _MULTI, _SINGLE = _collect()


def _alt(tokens) -> str:
    return "|".join(re.escape(t) for t in tokens)


# Every Unicode currency symbol (block U+20A0-20CF) counts as a pure symbol too, so a
# currency we have not listed yet is still stripped.
_SYMBOL_CHAR_CLASS = "[" + re.escape(SYMBOL_CHARS) + r"\u20a0-\u20cf$\u00a2-\u00a5]"
SYMBOL_CHAR_RE = re.compile(_SYMBOL_CHAR_CLASS)

# Case-insensitive multi-char tokens first (longest first, so "R$" beats "R" and "GH₵"
# beats "G"), then case-sensitive ISO codes and single letters.
_LEAD_TOKEN = "(?:" + _alt(_MULTI) + "|(?-i:" + _alt(_ISO) + "|" + _alt(_SINGLE) + "))"
_TRAIL_TOKEN = "(?:(?-i:" + _alt(_ISO) + ")|" + _alt(sorted(_TRAILING_SAFE, key=lambda t: -len(t))) + ")"

# Sign / opening parenthesis that may sit in front of a leading token: "-NGN 5", "(R 5)".
_LEAD_RE = re.compile(r"^(\s*[-+(]*\s*)" + _LEAD_TOKEN + r"\s*(?=[-+(]?\.?\d)", re.IGNORECASE)
_TRAIL_RE = re.compile(r"(?<=[\d.)])\s*" + _TRAIL_TOKEN + r"\s*(\)?\s*)$", re.IGNORECASE)

# For classifying a column as "currency" in reports.
CURRENCY_HINT_RE = re.compile(
    "(?:" + _SYMBOL_CHAR_CLASS + ")"
    r"|(?:^\s*[-+(]*\s*" + _LEAD_TOKEN + r"\s*[-+(]?\.?\d)"
    r"|(?:[\d.)]\s*" + _TRAIL_TOKEN + r"\s*\)?\s*$)",
    re.IGNORECASE,
)


def strip_currency(value: str) -> str:
    """Removes currency symbols/abbreviations from around a number. Leaves everything else."""
    v = value.strip()
    v = _LEAD_RE.sub(r"\1", v, count=1)
    v = _TRAIL_RE.sub(r"\1", v, count=1)
    return SYMBOL_CHAR_RE.sub("", v)


def has_currency(value: str) -> bool:
    return bool(CURRENCY_HINT_RE.search(value))


def supported_countries() -> list[str]:
    """Flat list of every country/region the table covers (for docs and the UI)."""
    out: list[str] = []
    for _code, countries, _t in CURRENCIES:
        out.extend(c.strip() for c in countries.split(","))
    return sorted(set(out))


def currency_count() -> int:
    return len({code for code, _c, _t in CURRENCIES})
