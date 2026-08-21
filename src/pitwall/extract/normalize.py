"""Name / company / title / URL normalization — §8.2 of PITWALL_PLAN.md.

Motorsport entrant names are a moving target of sponsor prefixes that change
every season, so matching happens on a normalized form:

    "Oracle Red Bull Racing"     -> "red bull racing"
    "Scuderia Ferrari HP"        -> "scuderia ferrari"
    "Mercedes-AMG PETRONAS F1"   -> "mercedes"
    "Visa Cash App RB"           -> "racing bulls"

Two mechanisms do the work: `SPONSOR_TOKENS`, stripped before matching, and
`CANONICAL_MAP`, which pins the handful of entrants whose season name shares no
tokens with the organisation you actually mean. Everything volatile beyond that
belongs in the `aliases` column of `companies.csv`, not in this file.
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import unquote, urlsplit, urlunsplit

# ─────────────────────────────── constants ───────────────────────────────

#: Title sponsors and sponsor-ish tokens stripped before company matching.
SPONSOR_TOKENS: frozenset[str] = frozenset(
    {
        "oracle", "petronas", "bwt", "moneygram", "aramco", "hp", "visa",
        "cash", "app", "atlassian", "revolut", "mastercard", "twg", "tgr",
        "kick", "stake", "totalenergies", "gulf", "hertz", "akkodis",
        "sunenergy1", "idec", "vista", "orlen", "castrol", "pirelli",
        "rokit", "unipol", "amr", "thor", "genesis", "vf",
    }
)

#: Legal-entity suffixes.
LEGAL_SUFFIXES: frozenset[str] = frozenset(
    {
        "ltd", "ltd.", "limited", "plc", "llc", "l.l.c", "inc", "inc.",
        "gmbh", "ag", "sa", "s.a", "sas", "sarl", "srl", "s.r.l", "spa",
        "s.p.a", "bv", "b.v", "nv", "n.v", "oy", "ab", "as", "kg",
        "co", "corp", "corporation", "company", "holdings", "group",
    }
)

#: Generic motorsport words that carry no disambiguating power on their own.
GENERIC_TOKENS: frozenset[str] = frozenset(
    {"f1", "formula", "one", "1", "team", "motorsport", "motorsports", "grand", "prix"}
)

#: Season name -> the organisation you actually mean (§8.2 table).
CANONICAL_MAP: dict[str, str] = {
    "oracle red bull racing": "red bull racing",
    "red bull racing": "red bull racing",
    "red bull technology": "red bull racing",
    "red bull powertrains": "red bull racing",
    "rbr": "red bull racing",
    "scuderia ferrari hp": "ferrari",
    "scuderia ferrari": "ferrari",
    "ferrari s p a": "ferrari",
    "mercedes amg petronas f1": "mercedes",
    "mercedes amg petronas": "mercedes",
    "mercedes amg": "mercedes",
    "mercedes benz grand prix": "mercedes",
    "mercedes f1": "mercedes",
    "visa cash app rb": "racing bulls",
    "vcarb": "racing bulls",
    "alphatauri": "racing bulls",
    "toro rosso": "racing bulls",
    "rb": "racing bulls",
    "audi revolut f1 team": "audi",
    "audi f1": "audi",
    "sauber motorsport ag": "sauber",
    "kick sauber": "sauber",
    "bwt alpine f1 team": "alpine",
    "alpine f1 team": "alpine",
    "alpine racing": "alpine",
    "signatech alpine": "alpine",
    "aston martin aramco": "aston martin",
    "amr gp": "aston martin",
    "aston martin thor team": "aston martin",
    "moneygram haas f1 team": "haas",
    "tgr haas": "haas",
    "haas formula llc": "haas",
    "atlassian williams racing": "williams",
    "williams grand prix engineering": "williams",
    "williams racing": "williams",
    "twg cadillac formula 1 team": "cadillac",
    "andretti global": "cadillac",
    "tgr e": "toyota gazoo racing",
    "tmg": "toyota gazoo racing",
    "toyota gazoo racing europe": "toyota gazoo racing",
    "mclaren f1": "mclaren racing",
    "mclaren mastercard": "mclaren racing",
    "mtc": "mclaren racing",
    "mclaren technology centre": "mclaren racing",
    "porsche penske motorsport": "porsche motorsport",
    "bmw m team wrt": "bmw m motorsport",
    "team wrt": "wrt",
    "hertz team jota": "jota",
    "jota sport": "jota",
    "prema powerteam": "prema racing",
    "art gp": "art grand prix",
    "hitech gp": "hitech grand prix",
    "rodin carlin": "rodin motorsport",
    "virtuosi racing": "invicta racing",
    "var": "van amersfoort racing",
    "manthey ema": "manthey",
    "manthey racing": "manthey",
    "asp team": "akkodis asp team",
    "ferrari af corse": "af corse",
    "vista af corse": "af corse",
}

#: Title abbreviations expanded before role matching.
TITLE_ABBREVIATIONS: dict[str, str] = {
    "ml": "machine learning",
    "ds": "data science",
    "ai": "artificial intelligence",
    "vd": "vehicle dynamics",
    "vpe": "vehicle performance engineer",
    "dil": "driver in loop",
    "cfd": "computational fluid dynamics",
    "swe": "software engineer",
    "sre": "site reliability engineer",
    "or": "operations research",
    "r&d": "research and development",
    "nvh": "noise vibration harshness",
    "hpp": "high performance powertrains",
}

#: Seniority prefixes lifted out of a job title into their own field.
SENIORITY_PREFIXES: dict[str, str] = {
    "intern": "intern",
    "internship": "intern",
    "placement": "intern",
    "apprentice": "intern",
    "graduate": "graduate",
    "grad": "graduate",
    "trainee": "graduate",
    "junior": "junior",
    "jr": "junior",
    "associate": "junior",
    "assistant": "junior",
    "mid": "mid",
    "senior": "senior",
    "sr": "senior",
    "staff": "senior",
    "principal": "lead",
    "lead": "lead",
    "leader": "lead",
    "head": "head",
    "chief": "head",
    "director": "executive",
    "vp": "executive",
    "president": "executive",
    "cto": "executive",
    "ceo": "executive",
}

#: Post-nominal credentials stripped from names.
CREDENTIALS = (
    "phd", "ph.d", "msc", "m.sc", "bsc", "b.sc", "meng", "m.eng", "beng",
    "b.eng", "mba", "ceng", "ieng", "mimeche", "cfa", "pmp", "mba.", "dr",
    "prof", "eur ing", "mres", "ma", "ms", "bs", "ba",
)

_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_WS_RE = re.compile(r"\s+")
_EMOJI_RE = re.compile(
    "[" "\U0001f300-\U0001faff" "\U00002600-\U000027bf" "\U0001f1e6-\U0001f1ff"
    "\U00002190-\U000021ff" "\U00002b00-\U00002bff" "️‍" "]+",
    flags=re.UNICODE,
)

COUNTRIES = (
    "united kingdom", "england", "scotland", "wales", "northern ireland",
    "italy", "germany", "france", "spain", "switzerland", "austria",
    "netherlands", "belgium", "united states", "usa", "canada", "australia",
    "japan", "china", "india", "brazil", "portugal", "sweden", "denmark",
    "finland", "norway", "poland", "czechia", "czech republic", "ireland",
    "monaco", "united arab emirates", "uae", "lithuania", "hungary", "romania",
)


# ──────────────────────────────── URLs ───────────────────────────────────

_TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "mc_", "ref", "trk", "original_referer")


def canonicalize_url(url: str) -> str:
    """Stable identity for a SERP row: lowercase host, no query/fragment, no
    trailing slash. LinkedIn profile URLs collapse to `.../in/<slug>`."""
    if not url:
        return ""
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url.lower()

    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host.endswith(".linkedin.com"):  # uk./de./fr. country mirrors
        host = "linkedin.com"

    path = unquote(parts.path or "")
    m = re.search(r"/in/([^/?#]+)", path, re.I)
    if host == "linkedin.com" and m:
        return f"https://linkedin.com/in/{m.group(1).strip('/').lower()}"

    path = path.rstrip("/")
    query = "&".join(
        kv
        for kv in (parts.query or "").split("&")
        if kv and not any(kv.lower().startswith(p) for p in _TRACKING_PREFIXES)
    )
    return urlunsplit(("https", host, path, query, "")).lower()


def extract_linkedin_slug(url: str) -> str | None:
    """`https://uk.linkedin.com/in/Jane-Doe-1a2b/` -> `jane-doe-1a2b`."""
    if not url:
        return None
    # `\w` is Unicode-aware, so percent-decoded slugs such as
    # `/in/j%C3%BCrgen-m%C3%BCller` survive intact instead of truncating at
    # the first non-ASCII byte.
    m = re.search(r"linkedin\.com/in/([\w\-%.]+)", unquote(url), re.I | re.UNICODE)
    if not m:
        return None
    slug = m.group(1).strip("/").lower()
    return slug or None


# ─────────────────────────────── strings ─────────────────────────────────


def strip_accents(text: str) -> str:
    return "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )


def squash(text: str) -> str:
    """Lowercase, drop punctuation and accents, collapse whitespace."""
    if not text:
        return ""
    t = strip_accents(str(text)).lower()
    t = t.replace("&", " and ")
    t = _PUNCT_RE.sub(" ", t)
    return _WS_RE.sub(" ", t).strip()


def normalize_name(name: str) -> str:
    """Collapse whitespace, strip emoji and post-nominal credentials."""
    if not name:
        return ""
    n = _EMOJI_RE.sub(" ", str(name))
    n = re.sub(r"\s*\|\s*LinkedIn\s*$", "", n, flags=re.I)
    # drop everything after a credential comma:  "Jane Doe, PhD, CEng" -> "Jane Doe"
    head, sep, tail = n.partition(",")
    if sep and squash(tail).split(" ")[0] in CREDENTIALS:
        n = head
    n = re.sub(
        r"[,\-–]\s*(" + "|".join(re.escape(c) for c in CREDENTIALS) + r")\b\.?",
        " ",
        n,
        flags=re.I,
    )
    n = re.sub(r"\((?:he|she|they)/[^)]*\)", " ", n, flags=re.I)  # pronoun tags
    n = n.replace('"', " ").replace("“", " ").replace("”", " ")
    return _WS_RE.sub(" ", n).strip(" .,-–|·")


def strip_legal_suffixes(text: str) -> str:
    tokens = [t for t in squash(text).split() if t not in LEGAL_SUFFIXES]
    return " ".join(tokens)


def strip_sponsors(text: str) -> str:
    """Drop sponsor tokens, but never reduce a name to nothing."""
    tokens = squash(text).split()
    kept = [t for t in tokens if t not in SPONSOR_TOKENS]
    if not kept or (len(kept) == 1 and len(kept[0]) <= 2 and len(tokens) > 1):
        return " ".join(tokens)
    return " ".join(kept)


def normalize_company(name: str) -> str:
    """Canonical matching form for a company name.

    Order matters: the canonical map is consulted on the plain squashed form
    first (so ``TGR-E`` is recognised before ``tgr`` is stripped as a sponsor),
    then again after sponsor and legal-suffix removal.
    """
    if not name:
        return ""
    # "Toyota Gazoo Racing (TGR-E)" -> try the outer name before the abbreviation
    outer = re.sub(r"\([^)]*\)", " ", str(name)).strip()
    if outer and squash(outer) and squash(outer) != squash(name):
        resolved = normalize_company(outer)
        if resolved:
            return resolved
    plain = squash(name)
    if plain in CANONICAL_MAP:
        return CANONICAL_MAP[plain]

    no_legal = strip_legal_suffixes(plain)
    if no_legal in CANONICAL_MAP:
        return CANONICAL_MAP[no_legal]

    stripped = strip_sponsors(no_legal)
    if stripped in CANONICAL_MAP:
        return CANONICAL_MAP[stripped]

    # last chance: drop generic motorsport filler ("f1 team", "formula 1")
    lean = " ".join(t for t in stripped.split() if t not in GENERIC_TOKENS)
    if lean and lean in CANONICAL_MAP:
        return CANONICAL_MAP[lean]
    return stripped or plain


def company_tokens(name: str) -> set[str]:
    """Distinctive tokens of a company name, generic filler removed."""
    return {
        t
        for t in normalize_company(name).split()
        if t not in GENERIC_TOKENS and len(t) > 1
    }


def split_seniority(title: str) -> tuple[str, str | None]:
    """`"Senior Data Scientist"` -> `("data scientist", "senior")`."""
    if not title:
        return "", None
    tokens = squash(title).split()
    found: str | None = None
    while tokens and tokens[0] in SENIORITY_PREFIXES:
        found = SENIORITY_PREFIXES[tokens[0]]
        tokens = tokens[1:]
    if found is None:
        for tok in tokens:
            if tok in SENIORITY_PREFIXES and tok not in {"or", "ma", "ms", "bs"}:
                found = SENIORITY_PREFIXES[tok]
                break
    return " ".join(tokens), found


def normalize_title(title: str) -> str:
    """Lowercase, de-punctuate, expand abbreviations, drop the seniority prefix."""
    if not title:
        return ""
    body, _ = split_seniority(title)
    out = [TITLE_ABBREVIATIONS.get(tok, tok) for tok in body.split()]
    return " ".join(out)


def title_tokens(title: str) -> set[str]:
    return {t for t in normalize_title(title).split() if len(t) > 1}


def detect_seniority(text: str) -> str | None:
    """Best-effort seniority read of a free-text headline."""
    if not text:
        return None
    squashed = f" {squash(text)} "
    for word, level in SENIORITY_PREFIXES.items():
        if len(word) <= 2:
            continue
        if f" {word} " in squashed:
            return level
    return None


_LOCATION_LABEL_RE = re.compile(r"(?:location|based in|located in)\s*[:\-–]?\s*([^·|\n]{3,60})", re.I)
_GEO_RE = re.compile(
    r"([A-ZÀ-Ý][\w'’\-\.]+(?:\s+[A-ZÀ-Ý][\w'’\-\.]+){0,3}"
    r"(?:,\s*[A-ZÀ-Ý][\w'’\-\.]+(?:\s+[A-ZÀ-Ý][\w'’\-\.]+){0,3}){1,2})"
)


def extract_location(snippet: str) -> str | None:
    """Pull a `City, Region, Country` style location out of a SERP snippet."""
    if not snippet:
        return None
    m = _LOCATION_LABEL_RE.search(snippet)
    if m:
        return _WS_RE.sub(" ", m.group(1)).strip(" .,·|-")
    for chunk in re.split(r"[·|•]|\s{2,}|\. ", snippet):
        chunk = chunk.strip()
        if not chunk or len(chunk) > 70:
            continue
        low = squash(chunk)
        if any(low.endswith(c) or f" {c}" in f" {low}" for c in COUNTRIES) and "," in chunk:
            g = _GEO_RE.search(chunk)
            if g:
                return _WS_RE.sub(" ", g.group(1)).strip(" .,·|-")
    return None


def country_matches(location: str | None, country: str | None) -> bool:
    """Loose check that a candidate's location sits in the company's country."""
    if not location or not country:
        return False
    loc = squash(location)
    for part in re.split(r"[/,]", country):
        c = squash(part)
        if not c:
            continue
        if c in loc:
            return True
        if c in {"uk", "united kingdom"} and any(
            x in loc for x in ("united kingdom", "england", "scotland", "wales", "london")
        ):
            return True
        if c in {"usa", "united states"} and any(
            x in loc for x in ("united states", "usa", "us")
        ):
            return True
    return False


def contains_phrase(haystack: str, phrase: str) -> bool:
    """Whole-phrase, case/punctuation-insensitive containment test."""
    if not haystack or not phrase:
        return False
    h = f" {squash(haystack)} "
    p = f" {squash(phrase)} "
    return p.strip() != "" and p in h
