"""
Country phone formats

A small, curated reference, not a full telecom database, covering
common countries across the regions Omixa's users actually asked
for: Africa, Asia, India, Europe, and the Americas. Used by
cleaning/resolutions.py to let a user say "this column is Nigerian
numbers" and have Omixa normalize to "+234XXXXXXXXXX" wherever a
value's digit count actually matches what that implies, anything
that doesn't fit is left untouched rather than guessed at, same
philosophy as every other rule in this codebase.

Each entry:
    dial_code, country calling code, digits only, no "+"
    national_digits, (min, max) length of the number AFTER the
                         dial code / trunk prefix is removed
    trunk_prefix, the digit(s) dropped when dialing
                         internationally (usually "0"), or None if
                         the country doesn't use one for mobiles
"""

from __future__ import annotations

COUNTRIES: dict[str, dict] = {
    # --- Africa ---
    "NG": {"name": "Nigeria", "region": "Africa", "dial_code": "234", "national_digits": (10, 10), "trunk_prefix": "0"},
    "GH": {"name": "Ghana", "region": "Africa", "dial_code": "233", "national_digits": (9, 9), "trunk_prefix": "0"},
    "KE": {"name": "Kenya", "region": "Africa", "dial_code": "254", "national_digits": (9, 9), "trunk_prefix": "0"},
    "ZA": {"name": "South Africa", "region": "Africa", "dial_code": "27", "national_digits": (9, 9), "trunk_prefix": "0"},
    "EG": {"name": "Egypt", "region": "Africa", "dial_code": "20", "national_digits": (10, 10), "trunk_prefix": "0"},
    "ET": {"name": "Ethiopia", "region": "Africa", "dial_code": "251", "national_digits": (9, 9), "trunk_prefix": "0"},
    "TZ": {"name": "Tanzania", "region": "Africa", "dial_code": "255", "national_digits": (9, 9), "trunk_prefix": "0"},
    "UG": {"name": "Uganda", "region": "Africa", "dial_code": "256", "national_digits": (9, 9), "trunk_prefix": "0"},

    # --- India ---
    "IN": {"name": "India", "region": "India", "dial_code": "91", "national_digits": (10, 10), "trunk_prefix": "0"},

    # --- Asia ---
    "CN": {"name": "China", "region": "Asia", "dial_code": "86", "national_digits": (11, 11), "trunk_prefix": None},
    "JP": {"name": "Japan", "region": "Asia", "dial_code": "81", "national_digits": (10, 10), "trunk_prefix": "0"},
    "ID": {"name": "Indonesia", "region": "Asia", "dial_code": "62", "national_digits": (9, 12), "trunk_prefix": "0"},
    "PK": {"name": "Pakistan", "region": "Asia", "dial_code": "92", "national_digits": (10, 10), "trunk_prefix": "0"},
    "PH": {"name": "Philippines", "region": "Asia", "dial_code": "63", "national_digits": (10, 10), "trunk_prefix": "0"},
    "VN": {"name": "Vietnam", "region": "Asia", "dial_code": "84", "national_digits": (9, 10), "trunk_prefix": "0"},
    "BD": {"name": "Bangladesh", "region": "Asia", "dial_code": "880", "national_digits": (10, 10), "trunk_prefix": "0"},
    "TH": {"name": "Thailand", "region": "Asia", "dial_code": "66", "national_digits": (9, 9), "trunk_prefix": "0"},
    "KR": {"name": "South Korea", "region": "Asia", "dial_code": "82", "national_digits": (9, 10), "trunk_prefix": "0"},
    "MY": {"name": "Malaysia", "region": "Asia", "dial_code": "60", "national_digits": (9, 10), "trunk_prefix": "0"},
    "SA": {"name": "Saudi Arabia", "region": "Asia", "dial_code": "966", "national_digits": (9, 9), "trunk_prefix": "0"},
    "AE": {"name": "United Arab Emirates", "region": "Asia", "dial_code": "971", "national_digits": (9, 9), "trunk_prefix": "0"},

    # --- Europe ---
    "GB": {"name": "United Kingdom", "region": "Europe", "dial_code": "44", "national_digits": (10, 10), "trunk_prefix": "0"},
    "DE": {"name": "Germany", "region": "Europe", "dial_code": "49", "national_digits": (10, 11), "trunk_prefix": "0"},
    "FR": {"name": "France", "region": "Europe", "dial_code": "33", "national_digits": (9, 9), "trunk_prefix": "0"},
    "IT": {"name": "Italy", "region": "Europe", "dial_code": "39", "national_digits": (9, 10), "trunk_prefix": None},
    "ES": {"name": "Spain", "region": "Europe", "dial_code": "34", "national_digits": (9, 9), "trunk_prefix": None},
    "NL": {"name": "Netherlands", "region": "Europe", "dial_code": "31", "national_digits": (9, 9), "trunk_prefix": "0"},
    "PL": {"name": "Poland", "region": "Europe", "dial_code": "48", "national_digits": (9, 9), "trunk_prefix": None},
    "SE": {"name": "Sweden", "region": "Europe", "dial_code": "46", "national_digits": (7, 9), "trunk_prefix": "0"},
    "CH": {"name": "Switzerland", "region": "Europe", "dial_code": "41", "national_digits": (9, 9), "trunk_prefix": "0"},
    "IE": {"name": "Ireland", "region": "Europe", "dial_code": "353", "national_digits": (9, 9), "trunk_prefix": "0"},

    # --- Americas ---
    "US": {"name": "United States", "region": "Americas", "dial_code": "1", "national_digits": (10, 10), "trunk_prefix": "1"},
    "CA": {"name": "Canada", "region": "Americas", "dial_code": "1", "national_digits": (10, 10), "trunk_prefix": "1"},
    "MX": {"name": "Mexico", "region": "Americas", "dial_code": "52", "national_digits": (10, 10), "trunk_prefix": None},
    "BR": {"name": "Brazil", "region": "Americas", "dial_code": "55", "national_digits": (10, 11), "trunk_prefix": "0"},
    "AR": {"name": "Argentina", "region": "Americas", "dial_code": "54", "national_digits": (10, 10), "trunk_prefix": "0"},
    "CO": {"name": "Colombia", "region": "Americas", "dial_code": "57", "national_digits": (10, 10), "trunk_prefix": "0"},
    "CL": {"name": "Chile", "region": "Americas", "dial_code": "56", "national_digits": (9, 9), "trunk_prefix": "0"},
    "PE": {"name": "Peru", "region": "Americas", "dial_code": "51", "national_digits": (9, 9), "trunk_prefix": None},
    "VE": {"name": "Venezuela", "region": "Americas", "dial_code": "58", "national_digits": (10, 10), "trunk_prefix": "0"},
    "EC": {"name": "Ecuador", "region": "Americas", "dial_code": "593", "national_digits": (9, 9), "trunk_prefix": "0"},
    "BO": {"name": "Bolivia", "region": "Americas", "dial_code": "591", "national_digits": (8, 8), "trunk_prefix": None},
    "PY": {"name": "Paraguay", "region": "Americas", "dial_code": "595", "national_digits": (9, 9), "trunk_prefix": "0"},
    "UY": {"name": "Uruguay", "region": "Americas", "dial_code": "598", "national_digits": (8, 8), "trunk_prefix": None},
    "GT": {"name": "Guatemala", "region": "Americas", "dial_code": "502", "national_digits": (8, 8), "trunk_prefix": None},
    "HN": {"name": "Honduras", "region": "Americas", "dial_code": "504", "national_digits": (8, 8), "trunk_prefix": None},
    "SV": {"name": "El Salvador", "region": "Americas", "dial_code": "503", "national_digits": (8, 8), "trunk_prefix": None},
    "NI": {"name": "Nicaragua", "region": "Americas", "dial_code": "505", "national_digits": (8, 8), "trunk_prefix": None},
    "CR": {"name": "Costa Rica", "region": "Americas", "dial_code": "506", "national_digits": (8, 8), "trunk_prefix": None},
    "PA": {"name": "Panama", "region": "Americas", "dial_code": "507", "national_digits": (8, 8), "trunk_prefix": None},
    "CU": {"name": "Cuba", "region": "Americas", "dial_code": "53", "national_digits": (8, 8), "trunk_prefix": "0"},
    "DO": {"name": "Dominican Republic", "region": "Americas", "dial_code": "1", "national_digits": (10, 10), "trunk_prefix": "1"},
    "JM": {"name": "Jamaica", "region": "Americas", "dial_code": "1", "national_digits": (10, 10), "trunk_prefix": "1"},
    "TT": {"name": "Trinidad and Tobago", "region": "Americas", "dial_code": "1", "national_digits": (10, 10), "trunk_prefix": "1"},
    "HT": {"name": "Haiti", "region": "Americas", "dial_code": "509", "national_digits": (8, 8), "trunk_prefix": None},
    "PR": {"name": "Puerto Rico", "region": "Americas", "dial_code": "1", "national_digits": (10, 10), "trunk_prefix": "1"},

    # --- Africa (additional) ---
    "MA": {"name": "Morocco", "region": "Africa", "dial_code": "212", "national_digits": (9, 9), "trunk_prefix": "0"},
    "DZ": {"name": "Algeria", "region": "Africa", "dial_code": "213", "national_digits": (9, 9), "trunk_prefix": "0"},
    "TN": {"name": "Tunisia", "region": "Africa", "dial_code": "216", "national_digits": (8, 8), "trunk_prefix": None},
    "LY": {"name": "Libya", "region": "Africa", "dial_code": "218", "national_digits": (9, 9), "trunk_prefix": "0"},
    "SD": {"name": "Sudan", "region": "Africa", "dial_code": "249", "national_digits": (9, 9), "trunk_prefix": "0"},
    "SS": {"name": "South Sudan", "region": "Africa", "dial_code": "211", "national_digits": (9, 9), "trunk_prefix": "0"},
    "SN": {"name": "Senegal", "region": "Africa", "dial_code": "221", "national_digits": (9, 9), "trunk_prefix": None},
    "ML": {"name": "Mali", "region": "Africa", "dial_code": "223", "national_digits": (8, 8), "trunk_prefix": None},
    "NE": {"name": "Niger", "region": "Africa", "dial_code": "227", "national_digits": (8, 8), "trunk_prefix": None},
    "BF": {"name": "Burkina Faso", "region": "Africa", "dial_code": "226", "national_digits": (8, 8), "trunk_prefix": None},
    "CI": {"name": "Ivory Coast", "region": "Africa", "dial_code": "225", "national_digits": (10, 10), "trunk_prefix": None},
    "BJ": {"name": "Benin", "region": "Africa", "dial_code": "229", "national_digits": (8, 8), "trunk_prefix": None},
    "TG": {"name": "Togo", "region": "Africa", "dial_code": "228", "national_digits": (8, 8), "trunk_prefix": None},
    "CM": {"name": "Cameroon", "region": "Africa", "dial_code": "237", "national_digits": (9, 9), "trunk_prefix": None},
    "TD": {"name": "Chad", "region": "Africa", "dial_code": "235", "national_digits": (8, 8), "trunk_prefix": None},
    "GA": {"name": "Gabon", "region": "Africa", "dial_code": "241", "national_digits": (8, 8), "trunk_prefix": None},
    "CG": {"name": "Congo-Brazzaville", "region": "Africa", "dial_code": "242", "national_digits": (9, 9), "trunk_prefix": None},
    "CD": {"name": "DR Congo", "region": "Africa", "dial_code": "243", "national_digits": (9, 9), "trunk_prefix": "0"},
    "AO": {"name": "Angola", "region": "Africa", "dial_code": "244", "national_digits": (9, 9), "trunk_prefix": None},
    "ZM": {"name": "Zambia", "region": "Africa", "dial_code": "260", "national_digits": (9, 9), "trunk_prefix": "0"},
    "ZW": {"name": "Zimbabwe", "region": "Africa", "dial_code": "263", "national_digits": (9, 9), "trunk_prefix": "0"},
    "MZ": {"name": "Mozambique", "region": "Africa", "dial_code": "258", "national_digits": (9, 9), "trunk_prefix": None},
    "NA": {"name": "Namibia", "region": "Africa", "dial_code": "264", "national_digits": (9, 9), "trunk_prefix": "0"},
    "BW": {"name": "Botswana", "region": "Africa", "dial_code": "267", "national_digits": (8, 8), "trunk_prefix": None},
    "RW": {"name": "Rwanda", "region": "Africa", "dial_code": "250", "national_digits": (9, 9), "trunk_prefix": "0"},
    "BI": {"name": "Burundi", "region": "Africa", "dial_code": "257", "national_digits": (8, 8), "trunk_prefix": None},
    "SO": {"name": "Somalia", "region": "Africa", "dial_code": "252", "national_digits": (8, 8), "trunk_prefix": "0"},
    "MW": {"name": "Malawi", "region": "Africa", "dial_code": "265", "national_digits": (9, 9), "trunk_prefix": "0"},
    "SL": {"name": "Sierra Leone", "region": "Africa", "dial_code": "232", "national_digits": (8, 8), "trunk_prefix": "0"},
    "LR": {"name": "Liberia", "region": "Africa", "dial_code": "231", "national_digits": (8, 9), "trunk_prefix": "0"},
    "GN": {"name": "Guinea", "region": "Africa", "dial_code": "224", "national_digits": (9, 9), "trunk_prefix": None},
    "MU": {"name": "Mauritius", "region": "Africa", "dial_code": "230", "national_digits": (7, 7), "trunk_prefix": None},
    "MG": {"name": "Madagascar", "region": "Africa", "dial_code": "261", "national_digits": (9, 9), "trunk_prefix": "0"},
    "ER": {"name": "Eritrea", "region": "Africa", "dial_code": "291", "national_digits": (7, 7), "trunk_prefix": "0"},
    "DJ": {"name": "Djibouti", "region": "Africa", "dial_code": "253", "national_digits": (8, 8), "trunk_prefix": None},

    # --- Asia (additional) ---
    "NP": {"name": "Nepal", "region": "Asia", "dial_code": "977", "national_digits": (10, 10), "trunk_prefix": "0"},
    "LK": {"name": "Sri Lanka", "region": "Asia", "dial_code": "94", "national_digits": (9, 9), "trunk_prefix": "0"},
    "MM": {"name": "Myanmar", "region": "Asia", "dial_code": "95", "national_digits": (9, 9), "trunk_prefix": "0"},
    "KH": {"name": "Cambodia", "region": "Asia", "dial_code": "855", "national_digits": (9, 9), "trunk_prefix": "0"},
    "LA": {"name": "Laos", "region": "Asia", "dial_code": "856", "national_digits": (9, 9), "trunk_prefix": "0"},
    "MN": {"name": "Mongolia", "region": "Asia", "dial_code": "976", "national_digits": (8, 8), "trunk_prefix": None},
    "KZ": {"name": "Kazakhstan", "region": "Asia", "dial_code": "7", "national_digits": (10, 10), "trunk_prefix": "8"},
    "UZ": {"name": "Uzbekistan", "region": "Asia", "dial_code": "998", "national_digits": (9, 9), "trunk_prefix": "0"},
    "AF": {"name": "Afghanistan", "region": "Asia", "dial_code": "93", "national_digits": (9, 9), "trunk_prefix": "0"},
    "IQ": {"name": "Iraq", "region": "Asia", "dial_code": "964", "national_digits": (10, 10), "trunk_prefix": "0"},
    "IR": {"name": "Iran", "region": "Asia", "dial_code": "98", "national_digits": (10, 10), "trunk_prefix": "0"},
    "IL": {"name": "Israel", "region": "Asia", "dial_code": "972", "national_digits": (9, 9), "trunk_prefix": "0"},
    "JO": {"name": "Jordan", "region": "Asia", "dial_code": "962", "national_digits": (9, 9), "trunk_prefix": "0"},
    "LB": {"name": "Lebanon", "region": "Asia", "dial_code": "961", "national_digits": (8, 8), "trunk_prefix": "0"},
    "KW": {"name": "Kuwait", "region": "Asia", "dial_code": "965", "national_digits": (8, 8), "trunk_prefix": None},
    "QA": {"name": "Qatar", "region": "Asia", "dial_code": "974", "national_digits": (8, 8), "trunk_prefix": None},
    "BH": {"name": "Bahrain", "region": "Asia", "dial_code": "973", "national_digits": (8, 8), "trunk_prefix": None},
    "OM": {"name": "Oman", "region": "Asia", "dial_code": "968", "national_digits": (8, 8), "trunk_prefix": None},
    "YE": {"name": "Yemen", "region": "Asia", "dial_code": "967", "national_digits": (9, 9), "trunk_prefix": "0"},
    "SY": {"name": "Syria", "region": "Asia", "dial_code": "963", "national_digits": (9, 9), "trunk_prefix": "0"},
    "TW": {"name": "Taiwan", "region": "Asia", "dial_code": "886", "national_digits": (9, 9), "trunk_prefix": "0"},
    "HK": {"name": "Hong Kong", "region": "Asia", "dial_code": "852", "national_digits": (8, 8), "trunk_prefix": None},
    "MO": {"name": "Macau", "region": "Asia", "dial_code": "853", "national_digits": (8, 8), "trunk_prefix": None},
    "SG": {"name": "Singapore", "region": "Asia", "dial_code": "65", "national_digits": (8, 8), "trunk_prefix": None},
    "BN": {"name": "Brunei", "region": "Asia", "dial_code": "673", "national_digits": (7, 7), "trunk_prefix": None},
    "KP": {"name": "North Korea", "region": "Asia", "dial_code": "850", "national_digits": (10, 10), "trunk_prefix": "0"},

    # --- Europe (additional) ---
    "PT": {"name": "Portugal", "region": "Europe", "dial_code": "351", "national_digits": (9, 9), "trunk_prefix": None},
    "BE": {"name": "Belgium", "region": "Europe", "dial_code": "32", "national_digits": (9, 9), "trunk_prefix": "0"},
    "AT": {"name": "Austria", "region": "Europe", "dial_code": "43", "national_digits": (10, 11), "trunk_prefix": "0"},
    "DK": {"name": "Denmark", "region": "Europe", "dial_code": "45", "national_digits": (8, 8), "trunk_prefix": None},
    "NO": {"name": "Norway", "region": "Europe", "dial_code": "47", "national_digits": (8, 8), "trunk_prefix": None},
    "FI": {"name": "Finland", "region": "Europe", "dial_code": "358", "national_digits": (9, 9), "trunk_prefix": "0"},
    "GR": {"name": "Greece", "region": "Europe", "dial_code": "30", "national_digits": (10, 10), "trunk_prefix": None},
    "TR": {"name": "Turkey", "region": "Europe", "dial_code": "90", "national_digits": (10, 10), "trunk_prefix": "0"},
    "RU": {"name": "Russia", "region": "Europe", "dial_code": "7", "national_digits": (10, 10), "trunk_prefix": "8"},
    "UA": {"name": "Ukraine", "region": "Europe", "dial_code": "380", "national_digits": (9, 9), "trunk_prefix": "0"},
    "RO": {"name": "Romania", "region": "Europe", "dial_code": "40", "national_digits": (9, 9), "trunk_prefix": "0"},
    "BG": {"name": "Bulgaria", "region": "Europe", "dial_code": "359", "national_digits": (9, 9), "trunk_prefix": "0"},
    "HU": {"name": "Hungary", "region": "Europe", "dial_code": "36", "national_digits": (9, 9), "trunk_prefix": "0"},
    "CZ": {"name": "Czech Republic", "region": "Europe", "dial_code": "420", "national_digits": (9, 9), "trunk_prefix": None},
    "SK": {"name": "Slovakia", "region": "Europe", "dial_code": "421", "national_digits": (9, 9), "trunk_prefix": "0"},
    "HR": {"name": "Croatia", "region": "Europe", "dial_code": "385", "national_digits": (9, 9), "trunk_prefix": "0"},
    "RS": {"name": "Serbia", "region": "Europe", "dial_code": "381", "national_digits": (9, 9), "trunk_prefix": "0"},
    "SI": {"name": "Slovenia", "region": "Europe", "dial_code": "386", "national_digits": (8, 8), "trunk_prefix": "0"},
    "LT": {"name": "Lithuania", "region": "Europe", "dial_code": "370", "national_digits": (8, 8), "trunk_prefix": "8"},
    "LV": {"name": "Latvia", "region": "Europe", "dial_code": "371", "national_digits": (8, 8), "trunk_prefix": None},
    "EE": {"name": "Estonia", "region": "Europe", "dial_code": "372", "national_digits": (7, 8), "trunk_prefix": None},
    "IS": {"name": "Iceland", "region": "Europe", "dial_code": "354", "national_digits": (7, 7), "trunk_prefix": None},
    "LU": {"name": "Luxembourg", "region": "Europe", "dial_code": "352", "national_digits": (9, 9), "trunk_prefix": None},
    "MT": {"name": "Malta", "region": "Europe", "dial_code": "356", "national_digits": (8, 8), "trunk_prefix": None},
    "CY": {"name": "Cyprus", "region": "Europe", "dial_code": "357", "national_digits": (8, 8), "trunk_prefix": None},
    "BA": {"name": "Bosnia and Herzegovina", "region": "Europe", "dial_code": "387", "national_digits": (8, 8), "trunk_prefix": "0"},
    "AL": {"name": "Albania", "region": "Europe", "dial_code": "355", "national_digits": (9, 9), "trunk_prefix": "0"},
    "MK": {"name": "North Macedonia", "region": "Europe", "dial_code": "389", "national_digits": (8, 8), "trunk_prefix": "0"},
    "MD": {"name": "Moldova", "region": "Europe", "dial_code": "373", "national_digits": (8, 8), "trunk_prefix": "0"},
    "BY": {"name": "Belarus", "region": "Europe", "dial_code": "375", "national_digits": (9, 9), "trunk_prefix": "8"},
    "GE": {"name": "Georgia", "region": "Europe", "dial_code": "995", "national_digits": (9, 9), "trunk_prefix": "0"},
    "AM": {"name": "Armenia", "region": "Europe", "dial_code": "374", "national_digits": (8, 8), "trunk_prefix": "0"},
    "AZ": {"name": "Azerbaijan", "region": "Europe", "dial_code": "994", "national_digits": (9, 9), "trunk_prefix": "0"},

    # --- Oceania ---
    "AU": {"name": "Australia", "region": "Oceania", "dial_code": "61", "national_digits": (9, 9), "trunk_prefix": "0"},
    "NZ": {"name": "New Zealand", "region": "Oceania", "dial_code": "64", "national_digits": (8, 9), "trunk_prefix": "0"},
    "FJ": {"name": "Fiji", "region": "Oceania", "dial_code": "679", "national_digits": (7, 7), "trunk_prefix": None},
    "PG": {"name": "Papua New Guinea", "region": "Oceania", "dial_code": "675", "national_digits": (8, 8), "trunk_prefix": None},
}

# Display order for the region groups (used by the frontend to build
# grouped dropdown options rather than one long flat list).
REGION_ORDER = ["Africa", "India", "Asia", "Europe", "Americas", "Oceania"]

# Common abbreviations and alternate names that don't match any
# COUNTRIES `name` field (case-insensitively) or ISO code directly.
# E.g. "USA" isn't the same string as "US", and "UK" isn't "GB".
# Values are the ISO code to resolve through, so each alias always
# stays consistent with whatever canonical name COUNTRIES has on file
# for that code (change it in one place, both lookups follow).
# Deliberately NOT exhaustive/fuzzy-matched, same "flag it, don't
# guess" philosophy as everything else in cleaning/detectors.py; this
# only covers alternate names common enough to be unambiguous.
COUNTRY_ALIASES: dict[str, str] = {
    "USA": "US", "U.S.A": "US", "U.S.A.": "US", "U.S": "US", "U.S.": "US", "AMERICA": "US",
    "UK": "GB", "U.K": "GB", "U.K.": "GB", "GREAT BRITAIN": "GB", "BRITAIN": "GB", "ENGLAND": "GB",
    "UAE": "AE", "U.A.E": "AE", "U.A.E.": "AE",
    "RSA": "ZA",
    "DRC": "CD", "DR CONGO": "CD", "CONGO-KINSHASA": "CD", "DEMOCRATIC REPUBLIC OF CONGO": "CD",
    "CONGO-BRAZZAVILLE": "CG", "REPUBLIC OF CONGO": "CG",
    "COTE D'IVOIRE": "CI", "COTE D IVOIRE": "CI",
    "BURMA": "MM",
    "HOLLAND": "NL",
    "SOUTH KOREA": "KR", "KOREA, SOUTH": "KR", "REPUBLIC OF KOREA": "KR",
    "NORTH KOREA": "KP", "KOREA, NORTH": "KP", "DPRK": "KP",
    "CZECHIA": "CZ",
    "MACEDONIA": "MK",
}


def normalize_phone(raw_value: str, country_code: str) -> str | None:
    """
    Tries to normalize a single phone value to "+<dial_code><national
    number>" for the given ISO country code. Returns None (meaning:
    leave the original value untouched) unless the value carries
    explicit evidence it belongs to that country, either the
    country's own dial code or its domestic trunk prefix is present.

    Deliberately does NOT fall back to "the digit count happens to
    match" alone: national number lengths overlap heavily across
    countries (a US number and a Nigerian number can both be 10
    digits), so a bare length match is not evidence of country, it's a coincidence, and normalizing on a coincidence would
    silently misassign someone else's number. Never truncates, pads,
    or reformats past what the value itself supports.
    """
    import re

    country = COUNTRIES.get(country_code)
    if not country or not isinstance(raw_value, str):
        return None

    digits = re.sub(r"\D", "", raw_value)
    if not digits:
        return None

    dial = country["dial_code"]
    trunk = country["trunk_prefix"]
    lo, hi = country["national_digits"]

    # 1) Already carries this country's dial code.
    if digits.startswith(dial) and lo <= len(digits) - len(dial) <= hi:
        national = digits[len(dial):]
    # 2) Carries this country's domestic trunk prefix (e.g. a leading
    #    "0" before the rest of a Nigerian number), still explicit
    #    evidence, unlike a bare digit-count coincidence.
    elif trunk and digits.startswith(trunk) and lo <= len(digits) - len(trunk) <= hi:
        national = digits[len(trunk):]
    else:
        return None

    return f"+{dial}{national}"
