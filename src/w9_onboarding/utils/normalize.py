"""Shared text normalisation used by extraction, validation and matching."""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA",
    "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
    "PR", "VI", "GU", "AS", "MP",  # territories - still W-9 eligible
}

# Legal-entity suffixes that carry no identity signal for matching.
LEGAL_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "llc", "l l c", "ltd", "limited",
    "lp", "llp", "pllc", "pc", "plc", "the", "group", "holdings", "na",
}

STREET_ABBREV = {
    "street": "st", "avenue": "ave", "av": "ave", "road": "rd", "boulevard": "blvd", "drive": "dr",
    "lane": "ln", "parkway": "pkwy", "court": "ct", "place": "pl", "highway": "hwy", "suite": "ste",
    "apartment": "apt", "building": "bldg", "north": "n", "south": "s", "east": "e", "west": "w",
    "terrace": "ter", "circle": "cir", "square": "sq", "floor": "fl", "#": "ste",
}

_OCR_DIGIT_FIX = str.maketrans({"O": "0", "o": "0", "D": "0", "l": "1", "I": "1", "|": "1", "S": "5",
                                "B": "8", "Z": "2", "g": "9"})


def squash(s: str) -> str:
    """Lowercase alnum-only: used to compare labels robustly to OCR spacing loss."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def respace(s: str) -> tuple[str, bool]:
    """Re-insert spaces OCR dropped ('2450BayshorePkwy' -> '2450 Bayshore Pkwy'). Returns (text, changed)."""
    out = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)
    out = re.sub(r"(?<=\d)(?=[A-Za-z]{2,})", " ", out)
    out = re.sub(r"(?<=[A-Za-z]{2})(?=\d)", " ", out)
    out = re.sub(r",(?=\S)", ", ", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out, out != s.strip()


def fix_ocr_digits(s: str) -> tuple[str, bool]:
    fixed = s.translate(_OCR_DIGIT_FIX)
    return fixed, fixed != s


def normalize_name(name: Optional[str]) -> str:
    """Canonical company name for fuzzy matching: 'ACME, Corp.' -> 'acme'."""
    if not name:
        return ""
    s = name.lower().replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    tokens = [t for t in s.split() if t]
    # drop trailing legal suffixes (repeatedly: 'acme holdings inc' -> 'acme')
    while tokens and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    if tokens and tokens[0] == "the":
        tokens = tokens[1:]
    return " ".join(tokens)


def normalize_address(addr: Optional[str]) -> str:
    if not addr:
        return ""
    s = addr.lower().replace("#", " # ")
    s = re.sub(r"[^\w\s#]", " ", s)
    return " ".join(STREET_ABBREV.get(t, t) for t in s.split())


_DATE_FORMATS = ["%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%m-%d-%y", "%Y-%m-%d", "%B %d, %Y", "%b %d, %Y",
                 "%B %d %Y", "%b %d %Y", "%d %B %Y", "%m.%d.%Y"]
_DATE_RE = re.compile(
    r"(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{4}-\d{2}-\d{2}|"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s*\d{1,2},?\s*\d{4})", re.I)


def find_date(text: str) -> Optional[str]:
    m = _DATE_RE.search(text)
    return m.group(1) if m else None


def parse_date(text: Optional[str]) -> Optional[date]:
    if not text:
        return None
    t = re.sub(r"\bSept\b", "Sep", text.strip(), flags=re.I)
    t = re.sub(r"([A-Za-z]{3,})\.", r"\1", t)  # 'Sep. 2' -> 'Sep 2'
    t = re.sub(r"([A-Za-z]+)(\d)", r"\1 \2", t)  # 'Sep2, 2026' -> 'Sep 2, 2026'
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


CSZ_RE = re.compile(r"^(?P<city>.*?)[,\s]+(?P<state>[A-Z]{2})[\s,.]*(?P<zip>\d{5})(?:\s*-?\s*(?P<zip4>\d{4}))?\s*$")


def split_city_state_zip(s: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    m = CSZ_RE.match(s.strip())
    if not m:
        return None, None, None
    z = m.group("zip") + (f"-{m.group('zip4')}" if m.group("zip4") else "")
    return m.group("city").strip(" ,"), m.group("state"), z


# --- TIN from free text (deterministic; never the LLM) ---
_SSN_DASHED = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_EIN_DASHED = re.compile(r"(?<!\d)\d{2}-\d{7}(?!\d)")


def tin_from_text(text: str) -> Optional[tuple[str, str, float, list[str]]]:
    """Find dashed SSN/EIN patterns in parsed text. Returns (digits, type, confidence, notes) or None.

    Plain code, never the LLM: the TIN is the most sensitive and most transposition-prone value.
    Lower confidence than the positional reader because the box position is not verified.
    """
    cands = [(m.replace("-", ""), "SSN") for m in sorted(set(_SSN_DASHED.findall(text)))]
    cands += [(m.replace("-", ""), "EIN") for m in sorted(set(_EIN_DASHED.findall(text)))]
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0][0], cands[0][1], 0.85, ["read from parsed text by pattern; box position not verified"]
    return cands[0][0], cands[0][1], 0.5, ["multiple TIN-shaped values in text"]
