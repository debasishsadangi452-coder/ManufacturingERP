"""
Work out a GST state code from data the ERP already holds (GSTIN, address text).

Conservative on purpose: a GSTIN with a valid state prefix wins; otherwise exactly one state name must appear in
the text. Anything ambiguous returns None so the user is asked rather than a place of supply being guessed.
"""
import re

STATES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh", "05": "Uttarakhand",
    "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur", "15": "Mizoram", "16": "Tripura",
    "17": "Meghalaya", "18": "Assam", "19": "West Bengal", "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh",
    "23": "Madhya Pradesh", "24": "Gujarat", "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra",
    "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
}
VALID_CODES = set(STATES) | {"25", "28", "97", "99"}  # retired / special codes still valid inside a GSTIN
ALIASES = {"orissa": "21", "pondicherry": "34", "jammu & kashmir": "01", "andaman & nicobar": "35",
           "daman and diu": "26", "dadra and nagar haveli": "26", "daman & diu": "26", "nct of delhi": "07",
           "new delhi": "07"}
GSTIN_RE = re.compile(r"\b(\d{2})([A-Z]{5}\d{4}[A-Z])([1-9A-Z])Z([0-9A-Z])\b")


def _names():
    pairs = [(n.lower(), c) for c, n in STATES.items()] + list(ALIASES.items())
    pairs.append(("jammu and kashmir", "01"))
    return sorted(set(pairs), key=lambda p: -len(p[0]))   # longest first so "new delhi" is consumed before "delhi"


_NAMES = _names()


def gstin_in(text):
    """(gstin, state_code) for the first well-formed GSTIN with a known state prefix, else None."""
    for m in GSTIN_RE.finditer((text or "").upper()):
        if m.group(1) in VALID_CODES:
            return m.group(0), m.group(1)
    return None


def state_from_text(text):
    """State code if exactly one state is named in ``text``, else None."""
    t = " " + re.sub(r"[^a-z& ]+", " ", (text or "").lower()) + " "
    found = set()
    for name, code in _NAMES:
        pat = " " + name + " "
        while pat in t:
            found.add(code)
            t = t.replace(pat, "  ", 1)
    return found.pop() if len(found) == 1 else None


def derive(*texts):
    """(state_code, gstin, source) from the first usable text; source is 'gstin' | 'address' | ''."""
    for text in texts:
        g = gstin_in(text)
        if g:
            return g[1], g[0], "gstin"
    for text in texts:
        code = state_from_text(text)
        if code:
            return code, "", "address"
    return "", "", ""
