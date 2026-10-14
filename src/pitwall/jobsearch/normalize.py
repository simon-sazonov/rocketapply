"""One stored posting -> the fields the rest of the job search reads — §9.

Pure: a `jobs` row goes in, a dict of column values comes out, and nothing here
touches the database or the network. That is what lets the §89 cases be tested
directly, and what lets `NORMALIZER_VERSION` re-normalise every stored posting
when the rules improve — the raw text is kept, so nothing has to be re-fetched.

What each source states in its own structure (Lever's `workplaceType`,
Arbeitnow's `remote`, Adzuna's country) outranks what the text implies.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from . import experience as experience_mod
from . import roles as roles_mod
from . import visa as visa_mod
from .companies import normalize_company, normalize_title, title_key
from .locations import country_code, parse_location
from .sections import split_requirements
from .skills import Vocabulary

#: Bump when a rule changes; every open posting is re-normalised on the next run.
NORMALIZER_VERSION = "js10"  # js2: qualitative seniority, decoded Greenhouse HTML
#                              js3: remote_regions as a list; the source's own
#                                   remote flag and search country used
#                              js4: seven more posting languages; US level
#                                   suffixes (Engineer II, Scientist IV) in titles;
#                                   nice-to-have skills; recruitment agencies;
#                                   PhD in the title; "read and write French"
#                              js5: citizenship and clearance requirements;
#                                   "Fluency in Dutch"; "PhD in …" as a
#                                   requirement line; "(Based in Hong Kong)"
#                              js6: customer-facing titles are not engineering
#                                   roles; task marketplaces count as agencies
#                              js7: Singapore agency licences and names
#                              js8: a degree line under "Nice to have" is a wish
#                              js9: the lowest degree a posting requires decides;
#                                   "Skills that will differentiate…" is a wish
#                              js10: every years phrase in a sentence, softeners
#                                   only where they govern the years; levels
#                                   stated in the body; clause-bound language
#                                   verbs; languages of short previews; how
#                                   much of a posting was read (`text_depth`);
#                                   courses and task platforms (`offer_kind`)

# ───────────────────────────── canonical URL ─────────────────────────────

_TRACKING_PARAMS = re.compile(
    r"^(?:utm_\w+|gh_src|gh_jid|source|src|ref|referrer|lever-source|lever-origin|"
    r"trk|trackingid|fbclid|gclid|mc_\w+|_hs\w+|__hs\w+|icid|campaign|medium)$",
    re.I,
)


def canonical_url(url: str | None) -> str:
    """The same page, however it was linked to (§11 "exact duplicate")."""
    if not url:
        return ""
    parts = urlparse(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
             if not _TRACKING_PARAMS.match(k)]
    path = parts.path.rstrip("/") or "/"
    return urlunparse(("https", host, path, "", urlencode(sorted(query)), ""))


# ─────────────────────────── employment type ─────────────────────────────

_EMPLOYMENT: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"working[- ]student|werkstudent", re.I), "working_student"),
    (re.compile(r"\bintern(?:ship)?\b|praktik|\bstage\b|stagiaire|alternan", re.I), "internship"),
    # A graduate PROGRAMME; "Graduate Data Scientist" is a level, read by
    # `experience`, not an employment type.
    (re.compile(r"graduate (?:programme|program|scheme)|trainee(?:ship|programm?e?)?",
                re.I), "graduate_programme"),
    (re.compile(r"part[- _]?time|teilzeit|temps partiel", re.I), "part_time"),
    (re.compile(r"full[- _]?time|vollzeit|temps plein|\bcdi\b", re.I), "full_time"),
    (re.compile(r"fixed[- ]term|befristet|\bcdd\b|temporary|temporär", re.I), "fixed_term"),
    (re.compile(r"permanent|unbefristet|festanstellung|\bcdi\b", re.I), "permanent"),
    (re.compile(r"\bcontract(?:or)?\b", re.I), "contract"),
    (re.compile(r"freelance|freiberuf|indépendant|self-employed", re.I), "freelance"),
)


def employment_types(stated: str | None, title: str | None) -> list[str]:
    """What the source states, plus what the title makes unambiguous."""
    found: list[str] = []
    for text in (stated or "", title or ""):
        for pattern, kind in _EMPLOYMENT:
            if pattern.search(text) and kind not in found:
                found.append(kind)
    return found


# ──────────────────────────────── salary ─────────────────────────────────

_CURRENCY = {"€": "EUR", "eur": "EUR", "euro": "EUR", "euros": "EUR", "£": "GBP",
             "gbp": "GBP", "$": "USD", "usd": "USD", "chf": "CHF", "sek": "SEK",
             "dkk": "DKK", "nok": "NOK", "pln": "PLN"}
_AMOUNT = r"(\d{1,3}(?:[.,\s]\d{3})+|\d+(?:[.,]\d+)?)\s*(k|K|tsd\.?|000)?"
_SALARY_RE = re.compile(
    rf"(?P<cur1>€|£|\$|\b(?:EUR|GBP|USD|CHF|SEK|DKK|NOK|PLN)\b)?\s*{_AMOUNT}"
    rf"\s*(?:-|–|—|to|bis|à)\s*"
    rf"(?P<cur2>€|£|\$|\b(?:EUR|GBP|USD|CHF|SEK|DKK|NOK|PLN)\b)?\s*{_AMOUNT}"
    rf"\s*(?P<cur3>€|£|\$|\b(?:EUR|GBP|USD|CHF|SEK|DKK|NOK|PLN|euros?)\b)?"
    rf"(?P<period>\s*(?:/|per|a|pro|par)\s*(?:year|annum|yr|month|mo|monat|jahr|an|mois))?",
    re.I,
)


#: A cheap gate: no currency anywhere means no salary, whatever the numbers.
_CURRENCY_HINT = re.compile(r"[€£$]|\b(?:EUR|GBP|USD|CHF|SEK|DKK|NOK|PLN|euros?)\b", re.I)


def _amount(text: str, suffix: str | None) -> float | None:
    raw = text.replace(" ", "")
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", raw):
        raw = raw.replace(",", "").replace(".", "")
    else:
        raw = raw.replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        return None
    if suffix and suffix.lower().startswith(("k", "tsd")) or suffix == "000":
        value *= 1000
    return value


def parse_salary(text: str | None) -> dict[str, Any]:
    """`€60k–€75k`, `60.000 - 75.000 EUR`, `€5,000/month` -> annual min/max.

    Only a range next to a currency counts: a bare "60-75" is as likely to be a
    headcount or a percentage. Missing salary is never a reason to reject (§61).
    """
    if not text or not _CURRENCY_HINT.search(text):
        return {}
    for m in _SALARY_RE.finditer(text):
        currency = m.group("cur1") or m.group("cur2") or m.group("cur3")
        if not currency:
            continue
        lo = _amount(m.group(2), m.group(3))
        hi = _amount(m.group(5), m.group(6))
        if lo is None or hi is None:
            continue
        if m.group(3) and not m.group(6):
            hi *= 1000  # "60-75k": the k covers both ends
        period = (m.group("period") or "").lower()
        if re.search(r"month|mo\b|monat|mois", period):
            lo, hi = lo * 12, hi * 12
        if lo > hi:
            lo, hi = hi, lo
        if not 5_000 <= hi <= 1_000_000:
            continue
        return {
            "salary_min": round(lo), "salary_max": round(hi),
            "salary_currency": _CURRENCY.get(currency.lower(), currency.upper()),
            "salary_text": " ".join(m.group(0).split()),
        }
    return {}


# ─────────────────────────────── education ───────────────────────────────

# Not "Promotion": it is German for a doctorate and English for a pay rise,
# and a false "PhD required" is a false rejection.
_PHD = re.compile(r"\bph\.?\s?d\b|doctorate|doctoral|doktor(?:and|arbeit|grad|titel)", re.I)
_MASTERS = re.compile(r"master'?s?\b|\bm\.?\s?sc\b|\bmsc\b|master of science|"
                      r"graduate degree|advanced degree|postgraduate", re.I)
_BACHELORS = re.compile(r"bachelor'?s?\b|\bb\.?\s?sc\b|\bbsc\b|undergraduate degree", re.I)
_EDU_PREFERRED = re.compile(r"prefer|plus\b|ideally|nice|desirable|advantage|bonus|"
                            r"von vorteil|wünschenswert|or equivalent|or similar", re.I)
_EDU_REQUIRED = re.compile(r"require|must|mandatory|essential|minimum|you (?:hold|have)|"
                           r"completed|degree in|erforderlich|abgeschlossen", re.I)


_DEGREE_HINT = re.compile(r"ph\.?\s?d|doctor|doktor|master|m\.?\s?sc|bachelor|b\.?\s?sc|"
                          r"degree|postgraduate", re.I)


_EDU_LEAD = re.compile(
    r"^\W*(?:a\s+|an\s+)?(?:ph\.?\s?d|doctorate|doctoral\s+degree|master'?s?|m\.?sc|"
    r"bachelor'?s?|b\.?sc)\b[^.\n]{0,25}?\b(?:in|degree)\b", re.I)

#: "(Based in Hong Kong)" in a title beats the address a career fair or a
#: recruiter's office put in the location field (28 Sep 2026: a Hong Kong job
#: listed at a London career fair).
_BASED_IN = re.compile(r"\bbased\s+in\s+([A-Za-z][A-Za-z .'-]{2,30}?)\s*(?:[)\],;|]|$)", re.I)


def education(description: str | None) -> tuple[str | None, str | None]:
    """`(required, preferred)` — each `phd`, `masters`, `bachelors` or None (§16).

    A PhD counts as REQUIRED only when the sentence names no lesser degree
    beside it and has no "preferred / or equivalent" softener — "MSc or PhD"
    asks for an MSc. The filter acts on nothing else.
    """
    required: str | None = None
    preferred: str | None = None
    rank = {"bachelors": 1, "masters": 2, "phd": 3}
    if not _DEGREE_HINT.search(description or ""):
        return None, None
    # The "a line that opens with a degree" rule only holds outside the
    # wishes: "Nice to have • PhD in model evaluation…" is not a requirement
    # (Helsing AI Safety, wrongly filtered for a few minutes on 28 Sep 2026).
    core = split_requirements(description)[0]
    lead: list[str] = []
    for sentence in re.split(r"(?<=[.!?;])\s+|\n+", description or ""):
        if not _DEGREE_HINT.search(sentence):
            continue
        levels = [lvl for lvl, pat in (("phd", _PHD), ("masters", _MASTERS),
                                       ("bachelors", _BACHELORS)) if pat.search(sentence)]
        if not levels:
            continue
        lowest = min(levels, key=lambda lvl: rank[lvl])
        # "• PhD in Computer Science, AI or a related discipline." under a
        # Requirements heading: a line that opens with the degree IS the
        # requirement, with no "required" in it (NTU, 28 Sep 2026).
        stated = _EDU_REQUIRED.search(sentence)
        if not stated and not _EDU_PREFERRED.search(sentence) and _EDU_LEAD.match(sentence) \
                and sentence.strip() and sentence.strip() in core:
            lead.append(lowest)
            continue
        if _EDU_PREFERRED.search(sentence):
            best = max(levels, key=lambda lvl: rank[lvl])
            if preferred is None or rank[best] > rank[preferred]:
                preferred = best
        elif stated:
            lead.append(lowest)
    if lead:
        # Several requirement lines naming degrees ("Master's degree in X" …
        # "PhD in Y"): the lowest is what makes someone eligible. A false
        # rejection is the costly error (§90), so the floor, not the ceiling.
        required = min(lead, key=lambda lvl: rank[lvl])
    return required, preferred


# ─────────────────────────────── language ────────────────────────────────

_STOPWORDS = {
    "de": {"und", "der", "die", "das", "mit", "für", "wir", "sie", "ist", "eine", "ein",
           "auf", "von", "zu", "bei", "den", "dem", "oder", "auch", "sich"},
    "fr": {"et", "le", "la", "les", "des", "une", "pour", "avec", "nous", "vous", "est",
           "dans", "sur", "du", "au", "en"},
    "en": {"and", "the", "with", "for", "you", "our", "are", "will", "your", "this",
           "that", "have", "from", "we", "to", "of"},
    # The languages of the countries the search covers since Sep 2026. Only
    # words distinctive enough to tell neighbours apart: Swedish and Danish,
    # Spanish and Portuguese share many.
    "pt": {"não", "você", "com", "uma", "dos", "das", "nosso", "nossa", "são", "também",
           "equipa", "equipe", "para", "os", "as", "em"},
    "es": {"y", "el", "los", "las", "una", "del", "con", "nuestro", "nuestra", "somos",
           "también", "equipo", "para", "más", "sus"},
    "it": {"il", "della", "delle", "degli", "gli", "sono", "siamo", "nostro", "nostra",
           "che", "per", "nel", "anche", "una", "con", "di", "questa", "sarà", "tua", "tuo",
           "cerchiamo", "offriamo", "nostri"},
    "nl": {"het", "een", "voor", "wij", "jij", "zijn", "onze", "ons", "bij", "ook",
           "naar", "van", "je", "met", "en"},
    "sv": {"och", "att", "är", "av", "ett", "till", "våra", "vår", "inom", "som", "med",
           "för", "vi", "du", "det"},
    "da": {"og", "er", "af", "et", "til", "vores", "inden", "som", "med", "for", "vi",
           "du", "det", "på"},
    "fi": {"ja", "että", "tai", "ovat", "olet", "meidän", "kanssa", "jossa", "joka",
           "myös", "sinä", "sinun", "on"},
}
_LANGUAGE_REQUIRED = {
    "de": re.compile(r"(?:fluen\w*|native|business[- ]level|excellent|very good|strong|"
                     r"proficien\w*|c1|c2|b2)\W+(?:\w+\W+){0,3}german|german\W+(?:\w+\W+){0,2}"
                     r"(?:is )?(?:required|mandatory|essential|a must)|german[- ]speaking|"
                     r"(?:sehr gute|fließende|verhandlungssichere)\s+deutschkenntnisse|"
                     r"deutsch\s+(?:auf\s+)?(?:c1|c2|muttersprach)", re.I),
    "fr": re.compile(r"(?:fluen\w*|native|business[- ]level|excellent|proficien\w*|c1|c2)\W+"
                     r"(?:\w+\W+){0,3}french|french\W+(?:\w+\W+){0,2}(?:is )?(?:required|"
                     r"mandatory|essential)|french[- ]speaking|français\s+(?:courant|"
                     r"obligatoire|indispensable)", re.I),
    "nl": re.compile(r"(?:fluen\w*|native|proficien\w*)\W+(?:\w+\W+){0,3}dutch|dutch\W+(?:\w+\W+)"
                     r"{0,2}(?:is )?(?:required|mandatory)|dutch[- ]speaking", re.I),
    "it": re.compile(r"(?:fluen\w*|native|proficien\w*)\W+(?:\w+\W+){0,3}italian|italian\W+"
                     r"(?:\w+\W+){0,2}(?:is )?(?:required|mandatory)", re.I),
    "es": re.compile(r"(?:fluen\w*|native|proficien\w*)\W+(?:\w+\W+){0,3}spanish|spanish\W+"
                     r"(?:\w+\W+){0,2}(?:is )?(?:required|mandatory)", re.I),
    "sv": re.compile(r"(?:fluen\w*|native|proficien\w*)\W+(?:\w+\W+){0,3}swedish|swedish\W+"
                     r"(?:\w+\W+){0,2}(?:is )?(?:required|mandatory)", re.I),
}


#: Each pattern above needs one of these words; checking for the word first
#: skips a backtracking regex on the ninety-odd percent of postings without it.
_LANGUAGE_WORDS = {
    "de": ("german", "deutsch"), "fr": ("french", "français"), "nl": ("dutch",),
    "it": ("italian",), "es": ("spanish",), "sv": ("swedish",),
}


def posting_language(text: str | None) -> str | None:
    words = re.findall(r"[a-zà-ÿäöüß]+", (text or "")[:3000].lower())
    if len(words) < 20:
        return None
    counts = {lang: sum(1 for w in words if w in stop) for lang, stop in _STOPWORDS.items()}
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    (lang, n), (_, runner_up) = ranked[0], ranked[1]
    if len(words) >= 150:
        return lang if n >= 5 else None
    # A 500-character preview has a third of the words, so fewer stopwords
    # decide — provided the winner is clear of the rest. "Entra nel nostro
    # mondo… Quale sarà la tua sfida?" was undetected on 29 Sep 2026, so an
    # Italian posting passed as English.
    return lang if n >= 3 and n >= runner_up + 2 else None


#: "Read and write French", "you speak Dutch", "able to write in German": the
#: ability stated as a verb. Missed by the patterns above until a rating said
#: so (Meetdavis, 28 Sep 2026: "Read and write French: our sources and
#: deliverables are in French").
_LANGUAGE_NAMES_EN = {"de": "german", "fr": "french", "nl": "dutch", "it": "italian",
                      "es": "spanish", "sv": "swedish", "pt": "portuguese", "da": "danish",
                      "fi": "finnish"}
_LANGUAGE_VERB = {
    lang: re.compile(rf"\b(?:read|write|speak|communicate|work)s?\b(?:\W+\w+){{0,4}}\W+"
                     rf"(?:in\W+)?{name}\b|\b{name}\W+(?:\w+\W+){{0,2}}(?:is |are )?"
                     rf"(?:needed|necessary|essential|mandatory)\b", re.I)
    for lang, name in _LANGUAGE_NAMES_EN.items()
}


#: A wish right after the language: "German is an advantage", "Dutch a plus".
_LANGUAGE_WISH = re.compile(
    r"^[^;.\n]{0,25}?\b(?:is\s+|are\s+|would\s+be\s+)?(?:an?\s+)?(?:big\s+|strong\s+)?"
    r"(?:plus|advantage|advantageous|bonus|asset|nice\s+to\s+have|beneficial|preferred|"
    r"desirable|welcome|von\s+vorteil|wünschenswert|un\s+plus|atout)\b", re.I)


def _stated(pattern: re.Pattern[str], text: str) -> bool:
    """A match that stays inside one clause and is not then called a wish.
    "Fluency in English and Dutch" asks for Dutch; "Fluent English required;
    German is nice to have" does not, and neither does "You communicate
    confidently in English; German is an advantage" (Quantum-Systems, read as
    German required until 29 Sep 2026)."""
    return any(not re.search(r"[;.\n]", m.group(0)) and not _LANGUAGE_WISH.match(text[m.end():])
               for m in pattern.finditer(text))


def languages_required(title: str | None, text: str | None, language: str | None) -> list[str]:
    """Spoken languages the posting asks for beyond English. A posting written
    entirely in German asks for German whether or not it says so."""
    blob = f"{title or ''}\n{text or ''}"
    lowered = blob.lower()
    found = [lang for lang, pattern in _LANGUAGE_REQUIRED.items()
             if any(word in lowered for word in _LANGUAGE_WORDS[lang]) and _stated(pattern, blob)]
    for lang, name in _LANGUAGE_NAMES_EN.items():
        if lang not in found and name in lowered and _stated(_LANGUAGE_VERB[lang], blob):
            found.append(lang)
    if language and language != "en" and language not in found:
        found.append(language)
    return found


_PHD_TITLE = re.compile(r"\bph\.?\s?d\b|\b(?:post-?)?doctoral\b|\bpost-?docs?\b|\bdoktorand\w*|"
                        r"\bdoctorant\w*", re.I)
_PHD_OR_MASTERS = re.compile(
    r"\b(?:ms|m\.?sc|master'?s?|ma)\s*(?:/|or|and|&)\s*ph\.?\s?d\b|"
    r"\bph\.?\s?d\s*(?:/|or|&)\s*(?:ms|m\.?sc|master'?s?)\b", re.I)


# ─────────────────────────── recruitment agencies ────────────────────────

#: Agencies that appeared as the posting company on the first live runs
#: (Randstad on MyCareersFuture, Brunel on EURES, Hays on ITJobs.pt's
#: example), plus the large ones like them. The employer is then hidden, so
#: the target list, the sponsor registers and dedupe cannot see it.
_AGENCY_NAME = re.compile(
    r"\b(?:randstad|adecco|manpower\w*|hays|michael page|page (?:personnel|executive)|"
    r"robert (?:half|walters)|brunel|gi group|kelly services|experis|harvey nash|"
    r"computer futures|jefferson frank|nigel frank|sthree|huxley|progressive recruitment|"
    r"hirewell|staffing|recruit\w*|personalvermittlung|personaldienstleist\w*|"
    r"personalberatung|headhunt\w*|talent (?:solutions|partners|acquisition)|"
    r"search partners|executive search|hire feed|micro1|mercor|toptal|outlier ai|"
    # Singapore agency names: "GOLDTECH RESOURCES PTE LTD", "MAESTRO HUMAN
    # RESOURCE PTE. LTD.", "BUSINESS EDGE PERSONNEL SERVICES".
    r"human\s+resources?|resources\s+pte|personnel|hr\s+(?:advisory|consult\w*|services|"
    r"solutions)|jobster)\b", re.I)
_AGENCY_TEXT = re.compile(
    r"\b(?:on behalf of (?:our|a) client|our client(?:,| is| are)|für unseren kunden|"
    r"unser(?:e)? kunde\w*|notre client|pour (?:le compte de )?(?:notre|un) client|"
    r"o nosso cliente|nuestro cliente|il nostro cliente|onze opdrachtgever|"
    # Task marketplaces that sell expert hours to AI labs: not an employer either.
    r"per accepted task|paid per task|compensation is output-based)\b|"
    # Singapore requires an agency to print its licence and each recruiter's
    # registration number: "EA Licence 18C9105", "EA Reg R1104661", "CEI:
    # R1104482". 296 of 1,261 MyCareersFuture postings did (28 Sep 2026).
    r"(?-i:\bEA)\s*(?:licen[cs]e|reg(?:istration)?)\b|"
    r"\breg(?:istration)?\.?\s*(?:no|number)\.?\s*:?\s*R\d{6,}\b|\bCEI\b\s*(?:no\.?)?\s*:?\s*R\d{6,}|"
    r"\(R\d{7}\)", re.I)


def is_agency(company: str | None, text: str | None) -> bool:
    return bool(_AGENCY_NAME.search(company or "")) or \
        bool(_AGENCY_TEXT.search((text or "")[:4000]))


# ──────────────────────────── source hints ───────────────────────────────


@dataclass(frozen=True)
class SourceHints:
    remote: bool | None = None
    workplace: str | None = None
    country: str | None = None
    #: A region an ATS states outright (Ashby's "European Union").
    region: str | None = None
    level_tags: tuple[str, ...] = ()
    employment: str | None = None


def source_hints(raw: dict[str, Any]) -> SourceHints:
    """What the source's own structure says, whichever source it was."""
    remote: bool | None = None
    workplace = raw.get("workplaceType")
    if isinstance(raw.get("remote"), bool):
        remote = raw["remote"]
    if isinstance(raw.get("isRemote"), bool) and raw["isRemote"]:
        remote = True
    if isinstance(raw.get("telecommuting"), bool) and raw["telecommuting"]:
        remote = True
    raw_loc = raw.get("location")
    loc: dict[str, Any] = raw_loc if isinstance(raw_loc, dict) else {}
    if loc.get("remote") is True:
        remote = True
    if loc.get("hybrid") is True:
        workplace = workplace or "hybrid"

    country = None
    region = None
    for candidate in (raw.get("country"), loc.get("country")):
        if isinstance(candidate, str) and candidate:
            country = country_code(candidate)
            if country:
                break
    raw_address = raw.get("address")
    postal = raw_address.get("postalAddress") if isinstance(raw_address, dict) else None
    addr_country = postal.get("addressCountry") if isinstance(postal, dict) else None
    if isinstance(addr_country, str) and addr_country:
        if addr_country.strip().lower() in {"european union", "eu", "europe"}:
            region = "EU"
        else:
            country = country or country_code(addr_country)
    area = loc.get("area")
    if isinstance(area, list) and area:
        country = country or country_code(str(area[0]))

    tags: list[str] = []
    # `_levels` is not the board's field but the adapter's: a board whose level
    # words mean something else elsewhere (MyCareersFuture's "Executive" is an
    # early-career grade) maps them to entry / mid / senior before storing.
    for key in ("job_types", "tags", "_levels"):
        tags += [str(t) for t in raw.get(key) or [] if isinstance(t, str)]
    level = raw.get("experienceLevel")
    if isinstance(level, dict) and level.get("id"):
        tags.append(str(level["id"]))
    return SourceHints(remote, workplace if isinstance(workplace, str) else None,
                       country, region, tuple(tags))


# ─────────────────────────────── the whole ───────────────────────────────


def normalize(row: dict[str, Any], *, vocab: Vocabulary,
              target: dict[str, Any] | None = None,
              company_fallback: str | None = None) -> dict[str, Any]:
    """Every derived column for one `jobs` row.

    `target` is the target-company row when one is already known (the posting
    came from that company's ATS); `company_fallback` is a name to use when the
    posting names no employer (Lever and Ashby payloads do not).
    """
    try:
        raw = json.loads(row.get("raw_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    title = row.get("title") or ""
    description = row.get("description") or ""
    hints = source_hints(raw)

    company = (row.get("posting_company") or (target or {}).get("name")
               or company_fallback or "")
    # What the source stated outright (schema v7) backs up what its payload
    # says: Adzuna's search country is not in its payload at all.
    stated_remote = row.get("source_remote")
    loc = parse_location(
        row.get("location"),
        remote_flag=hints.remote if hints.remote is not None
        else (None if stated_remote is None else bool(stated_remote)),
        workplace=hints.workplace,
        country_hint=hints.country or row.get("source_country"),
        title=title,
    )
    based = _BASED_IN.search(title)
    based_code = country_code(based.group(1).strip()) if based else None
    if based_code:
        loc = replace(loc, countries=(based_code,), cities=())
    regions = list(loc.remote_regions)
    if hints.region and loc.remote_status == "remote" and hints.region not in regions:
        regions.insert(0, hints.region)
    exp = experience_mod.extract(title, description, list(hints.level_tags))
    # The title too: "Data Engineer SC Cleared" says it nowhere else.
    visa = visa_mod.extract(f"{title}\n{description}",
                            company_sponsorship=(target or {}).get("sponsorship"))
    role = roles_mod.classify(title, description)
    skills = vocab.extract(f"{title}\n{description}")
    core, wishes = split_requirements(description)
    required_here = set(vocab.extract(f"{title}\n{core}"))
    skills_nice = [s for s in vocab.extract(wishes) if s in skills and s not in required_here]
    edu_required, edu_preferred = education(description)
    # "AI Research Intern (PhD)", "PhD Student Researcher": the title requires
    # it. "MS/PhD" and "Master's or PhD" in a title accept either.
    if _PHD_TITLE.search(title) and not _PHD_OR_MASTERS.search(title):
        edu_required = "phd"
    salary = {} if row.get("salary_min") else parse_salary(description)
    language = posting_language(description)

    return {
        "posting_company": row.get("posting_company") or company or None,
        "company_norm": normalize_company(company),
        "title_norm": normalize_title(title),
        "title_key": title_key(title),
        "canonical_url": canonical_url(row.get("url")),
        "country": loc.country,
        "countries": json.dumps(list(loc.countries)),
        "city": loc.city,
        "cities": json.dumps(list(loc.cities), ensure_ascii=False),
        "remote_status": loc.remote_status,
        "remote_region": regions[0] if regions else None,
        "remote_regions": json.dumps(regions),
        "employment_types": json.dumps(
            employment_types(row.get("employment_type"), title)
        ),
        "experience_min": exp.min_years,
        "experience_max": exp.max_years,
        "experience_level": exp.level,
        "experience_required": int(exp.required),
        "experience_confidence": exp.confidence,
        "experience_evidence": exp.evidence,
        "education_required": edu_required,
        "education_preferred": edu_preferred,
        **salary,
        "visa_status": visa.status,
        "visa_evidence": visa.evidence,
        "visa_scope": visa.scope,
        "relocation": int(visa.relocation),
        "role_labels": json.dumps(list(role.labels)),
        "skills": json.dumps(skills),
        "skills_nice": json.dumps(skills_nice),
        "agency": int(is_agency(company, description)),
        "language": language,
        "languages_required": json.dumps(languages_required(title, description, language)),
        "normalized_hash": row.get("content_hash"),
        "normalizer_version": NORMALIZER_VERSION,
    }
