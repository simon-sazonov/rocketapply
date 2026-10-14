"""A job found somewhere else — a link, or the posting's text pasted in.

The search finds most postings, but not the one a friend forwarded, the one on
a Ukrainian board no adapter reads, or the one on LinkedIn. This turns a URL
(or, where the page will not be read, its pasted text) into an ordinary row in
`jobs`, so the rest of the engine — analysis, match, truth, CV, letter — treats
it like any other posting.

Reading the page, in order of trust:

1. **Structured data.** Most boards embed a schema.org `JobPosting` as JSON-LD
   for Google Jobs: title, employer, place and the full description, exactly as
   the employer wrote them. When it is there it is used as it is.
2. **The page's text.** Otherwise the visible text is taken and one small model
   call names the title, employer and place, and points at where the posting
   starts and ends, so the navigation and footer do not become "requirements".
   The model QUOTES those boundaries rather than rewriting anything: the
   analyzer later checks every skill against the description word for word, so
   the description must be the employer's own words, in the employer's language.

A posting in Ukrainian (or German, or anything) stays in that language here.
What changes is the letter: a job added this way is marked `letter_language:
en`, and `planner.plan_letter` honours it, because Simon applies in English
even where he could write the letter in the posting's language.
"""

from __future__ import annotations

import hashlib
import json
import logging
import pathlib
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Posting", "IntakeError", "fetch_posting", "from_html", "from_text",
           "add_posting", "SOURCE_NAME"]

log = logging.getLogger(__name__)

#: The `job_sources` row every hand-added posting hangs off.
SOURCE_NAME = "added-by-hand"
#: Enough for the longest real posting; a page longer than this is mostly chrome.
PAGE_LIMIT = 24000
#: A description shorter than this is a teaser or a cookie wall, not a posting.
MIN_DESCRIPTION = 300


class IntakeError(RuntimeError):
    """The page could not be read. The message says why, in words, and the
    page offers the paste box."""


@dataclass
class Posting:
    """What was read from the page, before it is saved. Every field is
    editable on the page, so a wrong guess costs a correction, not a job."""

    title: str = ""
    company: str = ""
    location: str = ""
    description: str = ""
    url: str = ""
    language: str = ""
    #: "structured data", "page text" or "pasted text".
    method: str = ""
    posted_at: str | None = None
    employment_type: str | None = None
    #: What went less than perfectly, for the page to show.
    notes: list[str] = field(default_factory=list)
    #: The posting's own title/employer/place where they were put into English.
    original: dict[str, str] = field(default_factory=dict)

    @property
    def missing(self) -> list[str]:
        out = [name for name, value in (("title", self.title), ("employer", self.company))
               if not value.strip()]
        if len(self.description.strip()) < MIN_DESCRIPTION:
            out.append("the posting text")
        return out


# ───────────────────────────── language ─────────────────────────────

#: Letters only Ukrainian uses among the Cyrillic languages, and only Russian.
_UK_ONLY = set("іїєґІЇЄҐ")
_RU_ONLY = set("ыэъёЫЭЪЁ")
_HINTS = {
    "de": (" und ", " der ", " die ", " wir ", " sie ", " mit "),
    "fr": (" et ", " les ", " des ", " vous ", " nous ", " pour "),
    "pt": (" e ", " para ", " com ", " você ", " não ", " uma "),
    "es": (" y ", " para ", " con ", " los ", " las ", " una "),
    "it": (" e ", " per ", " con ", " della ", " sono ", " una "),
    "pl": (" i ", " oraz ", " dla ", " jest ", " się ", " w "),
    "en": (" and ", " the ", " you ", " with ", " our ", " for "),
}


def non_latin(text: str) -> bool:
    """Any letter outside the Latin script (Cyrillic, Greek, CJK…)."""
    return any(c.isalpha() and not ("a" <= c.lower() <= "z" or "\u00c0" <= c <= "\u024f")
               for c in text)


#: Ukrainian's national romanisation (2010), for a place the model did not
#: put into English. Good enough for a city name in a letter.
_UK_LATIN = dict(zip("абвгґдезиіклмнопрстуфхцчшщ",
                     ("a", "b", "v", "h", "g", "d", "e", "z", "y", "i", "k", "l", "m", "n",
                      "o", "p", "r", "s", "t", "u", "f", "kh", "ts", "ch", "sh", "shch"),
                     strict=True))
_UK_LATIN |= {"є": "ie", "ж": "zh", "ї": "i", "й": "i", "ю": "iu", "я": "ia", "ь": "",
              "'": "", "’": "", "ы": "y", "э": "e", "ё": "io", "ъ": ""}
_PLACES = {"київ": "Kyiv", "львів": "Lviv", "харків": "Kharkiv", "одеса": "Odesa",
           "дніпро": "Dnipro", "запоріжжя": "Zaporizhzhia", "вінниця": "Vinnytsia",
           "україна": "Ukraine", "віддалено": "Remote", "віддалена робота": "Remote",
           "удаленно": "Remote", "киев": "Kyiv", "харьков": "Kharkiv", "одесса": "Odesa",
           "днепр": "Dnipro", "львов": "Lviv", "украина": "Ukraine"}


def english_place(place: str) -> str:
    """"Дніпро, віддалено" -> "Dnipro, Remote". Latin text is returned as is."""
    if not non_latin(place):
        return place
    out = []
    for part in re.split(r"\s*[,;]\s*", place.strip()):
        key = part.lower()
        if key in _PLACES:
            out.append(_PLACES[key])
            continue
        latin = "".join(_UK_LATIN.get(c.lower(), c.lower()) for c in part)
        out.append(" ".join(w[:1].upper() + w[1:] for w in latin.split()))
    return ", ".join(dict.fromkeys(o for o in out if o))


def guess_language(text: str) -> str:
    """A two-letter guess, for the page to show before anything is generated.

    The analyzer decides the language that matters; this is only so Simon sees
    "Ukrainian posting, English letter" before he presses the button.
    """
    sample = text[:6000]
    letters = [c for c in sample if c.isalpha()]
    if not letters:
        return ""
    cyrillic = sum("Ѐ" <= c <= "ӿ" for c in letters)
    if cyrillic / len(letters) > 0.3:
        uk = sum(c in _UK_ONLY for c in sample)
        ru = sum(c in _RU_ONLY for c in sample)
        return "uk" if uk >= ru else "ru"
    lowered = f" {sample.lower()} "
    scores = {lang: sum(lowered.count(w) for w in words) for lang, words in _HINTS.items()}
    return max(scores, key=scores.get) if any(scores.values()) else ""


LANGUAGE_NAMES = {"en": "English", "uk": "Ukrainian", "ru": "Russian", "de": "German",
                  "fr": "French", "pt": "Portuguese", "es": "Spanish", "it": "Italian",
                  "pl": "Polish", "nl": "Dutch"}


# ───────────────────────────── structured data ─────────────────────────────

_LD = re.compile(r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
                 re.S | re.I)


def _walk(node: Any) -> list[dict[str, Any]]:
    """Every dict in a JSON-LD blob — `@graph`, lists and nesting included."""
    out: list[dict[str, Any]] = []
    if isinstance(node, dict):
        out.append(node)
        for value in node.values():
            out.extend(_walk(value))
    elif isinstance(node, list):
        for value in node:
            out.extend(_walk(value))
    return out


def _is_posting(node: dict[str, Any]) -> bool:
    kind = node.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return any(str(k).lower() == "jobposting" for k in kinds)


def _name(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or "").strip()
    if isinstance(value, list):
        return next((n for n in (_name(v) for v in value) if n), "")
    return str(value or "").strip()


def _place(value: Any) -> str:
    """`jobLocation` → "Kyiv, UA". It is a Place, a list of them, or text."""
    places = value if isinstance(value, list) else [value]
    out: list[str] = []
    for place in places:
        if isinstance(place, str):
            out.append(place.strip())
            continue
        if not isinstance(place, dict):
            continue
        address = place.get("address") or {}
        if isinstance(address, str):
            out.append(address.strip())
            continue
        parts = [_name(address.get(k)) for k in
                 ("addressLocality", "addressRegion", "addressCountry")]
        text = ", ".join(dict.fromkeys(p for p in parts if p))
        if text:
            out.append(text)
    return "; ".join(dict.fromkeys(p for p in out if p))


def structured_posting(page: str) -> dict[str, Any] | None:
    """The page's schema.org JobPosting, or None."""
    for block in _LD.findall(page or ""):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            # Boards sometimes leave raw newlines inside strings.
            try:
                data = json.loads(re.sub(r"[\r\n\t]+", " ", block.strip()))
            except json.JSONDecodeError:
                continue
        for node in _walk(data):
            if _is_posting(node):
                return node
    return None


def _meta(page: str, prop: str) -> str:
    import html as htmllib

    m = re.search(rf"<meta[^>]+(?:property|name)=[\"']{re.escape(prop)}[\"'][^>]*"
                  rf"content=[\"']([^\"']*)", page, re.I) or \
        re.search(rf"<meta[^>]+content=[\"']([^\"']*)[\"'][^>]*(?:property|name)="
                  rf"[\"']{re.escape(prop)}[\"']", page, re.I)
    return htmllib.unescape(m.group(1)).strip() if m else ""


def _title_tag(page: str) -> str:
    import html as htmllib

    m = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
    return htmllib.unescape(re.sub(r"\s+", " ", m.group(1))).strip() if m else ""


# ───────────────────────────── page text ─────────────────────────────

INTAKE_PROMPT = """You read the text of a web page that contains ONE job posting, in any
language, surrounded by site navigation, cookie banners, other vacancies and footers.

Return JSON only:
- `title`: the job title exactly as the posting writes it (keep its language).
- `company`: the hiring employer's name as written. Not the job board's name.
  Empty string if the page does not say.
- `location`: city/country or "Remote", as written. Empty string if not stated.
- `title_en`, `company_en`, `location_en`: the same three in English and Latin
  script, the way an English CV would write them: "Аналітик даних" -> "Data Analyst",
  "Київ, віддалено" -> "Kyiv, Remote", an employer by the Latin name it uses itself
  ("Монобанк" -> "monobank"). Identical to the original when it is already English.
- `language`: two-letter ISO 639-1 code of the language the posting is written in.
- `starts_with`: the first 6-12 words of the posting's own text, copied EXACTLY,
  character for character: usually the first sentence about the company or the
  role. Never the browser title ("... | DOU"), breadcrumbs, a menu or a login link.
- `ends_with`: the last 6-12 words of the posting's own text, copied EXACTLY: the
  end of its last list or sentence. Never an "Apply" button, share links, or
  the other vacancies listed after it.
- `is_job_posting`: false if the page is a login wall, a search results list, an
  error page or anything that is not one job posting.

Never translate, summarise or correct anything you copy."""

INTAKE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "company": {"type": "string"},
        "location": {"type": "string"},
        "title_en": {"type": "string"},
        "company_en": {"type": "string"},
        "location_en": {"type": "string"},
        "language": {"type": "string"},
        "starts_with": {"type": "string"},
        "ends_with": {"type": "string"},
        "is_job_posting": {"type": "boolean"},
    },
    "required": ["title", "company", "location", "title_en", "company_en", "location_en",
                 "language", "starts_with", "ends_with",
                 "is_job_posting"],
    "additionalProperties": False,
}


def _pattern(needle: str, *, tail: bool = False) -> re.Pattern[str] | None:
    """`needle` matched on its WORDS alone, case ignored.

    A model copies a line across a line break as a space, and "бізнесу ." from
    the page as "бізнесу.", all the time. That is formatting, not a misquote.
    """
    words = [re.escape(w) for w in re.findall(r"\w+", needle)]
    if len(words) < 2:
        return None
    return re.compile(r"\W+".join(words) + (r"(?:[ \t]?[^\w\s]+)?" if tail else ""), re.I)


def cut(text: str, starts_with: str, ends_with: str) -> tuple[str, bool]:
    """The posting out of the page, by the model's quoted boundaries.

    Returns `(text, cut_ok)`. When a boundary cannot be found the whole page
    is kept — noise costs the analyzer some attention; a wrong cut loses the
    requirements.

    The end is its LAST sighting and the start its last sighting before that
    end: a posting that opens with its own title also has that title in the
    page's <title> and breadcrumbs, all of which come first.
    """
    ends = _pattern(ends_with, tail=True)
    starts = _pattern(starts_with)
    end_hits = list(ends.finditer(text)) if ends else []
    end = end_hits[-1].end() if end_hits else -1
    start_hits = [m.start() for m in starts.finditer(text)] if starts else []
    if end >= 0:
        start_hits = [h for h in start_hits if h < end] or []
    start = (start_hits[-1] if end >= 0 else start_hits[0]) if start_hits else -1
    if start < 0 and end < 0:
        return text, False
    lo = max(0, start)
    hi = end if end > lo else len(text)
    piece = text[lo:hi].strip()
    if len(piece) < MIN_DESCRIPTION:
        return text, False
    return piece, start >= 0 and end > lo


def _router() -> Any:
    from ..config import get_settings
    from .llm import TaskRouter

    settings = get_settings()
    return TaskRouter(settings=settings, cache_dir=pathlib.Path(settings.cache_dir) / "apply")


def _read_with_model(text: str, posting: Posting, router: Any) -> Posting:
    """Fill the gaps with one model call. A failure leaves the posting as it
    was, with a note — the fields are editable."""
    if router is None:
        try:
            router = _router()
        except Exception as exc:  # noqa: BLE001 - no model is a note, not a stop
            posting.notes.append(f"no model to read the page with: {exc}")
            return posting
    try:
        raw = router.complete_json("job_intake", INTAKE_PROMPT,
                                   f"PAGE TEXT:\n{text[:PAGE_LIMIT]}",
                                   schema=INTAKE_SCHEMA, temperature=0.0)
    except Exception as exc:  # noqa: BLE001 - see docstring
        posting.notes.append(f"the model could not read the page ({str(exc)[:120]}); "
                             "fill in the fields below")
        return posting
    if raw.get("is_job_posting") is False:
        posting.notes.append("this does not look like a single job posting — a login wall "
                             "or a list of jobs? Paste the posting's text instead")
    for key in ("title", "company", "location"):
        value = getattr(posting, key) or str(raw.get(key) or "").strip()
        english = str(raw.get(f"{key}_en") or "").strip()
        if value and english and english != value and non_latin(value) \
                and not non_latin(english):
            # The letter and the CV are in English: "would relocate to Дніпро"
            # reads badly, and the CV's font has no Cyrillic at all.
            posting.original[key] = value
            value = english
        setattr(posting, key, value)
    language = str(raw.get("language") or "").strip().lower()[:2]
    if language.isalpha():
        posting.language = language
    if not posting.description:
        body, ok = cut(text, str(raw.get("starts_with") or ""), str(raw.get("ends_with") or ""))
        posting.description = body
        if not ok:
            posting.notes.append("could not tell exactly where the posting starts and ends on "
                                 "the page, so the whole page text is kept")
    return posting


def from_html(page: str, url: str = "", *, router: Any = None) -> Posting:
    """Read a fetched page."""
    from ..sources.jobs.base import strip_html

    posting = Posting(url=url)
    node = structured_posting(page)
    if node is not None:
        posting.method = "structured data"
        posting.title = str(node.get("title") or node.get("name") or "").strip()
        posting.company = _name(node.get("hiringOrganization"))
        posting.location = _place(node.get("jobLocation"))
        if not posting.location and str(node.get("jobLocationType") or "").upper() == "TELECOMMUTE":
            posting.location = "Remote"
        posting.description = strip_html(node.get("description"), limit=PAGE_LIMIT) or ""
        posting.posted_at = str(node.get("datePosted") or "")[:19] or None
        kind = node.get("employmentType")
        posting.employment_type = (", ".join(map(str, kind)) if isinstance(kind, list)
                                   else str(kind or "")) or None
    text = strip_html(page, limit=PAGE_LIMIT * 2) or ""
    if posting.missing or non_latin(f"{posting.title} {posting.company} {posting.location}"):
        # No structured data, structured data with holes, or a title the
        # English letter cannot use as written: the page's text, read once.
        posting.method = posting.method or "page text"
        if not posting.title:
            posting.title = _meta(page, "og:title")
        if len(text) < MIN_DESCRIPTION and not posting.description:
            raise IntakeError("the page has almost no text — it is probably built by "
                              "JavaScript or behind a login. Paste the posting's text instead")
        if posting.description and len(posting.description) >= MIN_DESCRIPTION:
            text = posting.description
        _read_with_model(text, posting, router)
        if not posting.title:
            posting.title = _title_tag(page)
    return _finish(posting)


def _finish(posting: Posting) -> Posting:
    posting.language = posting.language or guess_language(posting.description)
    if non_latin(posting.location):
        posting.original.setdefault("location", posting.location)
        posting.location = english_place(posting.location)
    return posting


def from_text(text: str, url: str = "", *, router: Any = None) -> Posting:
    """Read pasted text. Pasted HTML is fine too."""
    from ..sources.jobs.base import strip_html

    raw = text or ""
    if "<" in raw and ">" in raw and structured_posting(raw):
        posting = from_html(raw, url, router=router)
        posting.method = "pasted text"
        return posting
    clean = (strip_html(raw, limit=PAGE_LIMIT * 2) if re.search(r"<[a-z/][^>]*>", raw, re.I)
             else raw).strip()
    if len(clean) < MIN_DESCRIPTION:
        raise IntakeError("that is too short to be a job posting — paste the whole posting, "
                          "requirements included")
    posting = Posting(url=url, method="pasted text")
    _read_with_model(clean, posting, router)
    return _finish(posting)


def fetch_posting(url: str, *, router: Any = None, fetcher: Any = None) -> Posting:
    """Fetch the page and read it. Raises `IntakeError` with a reason a person
    can act on (almost always: paste the text instead)."""
    from ..sources.jobs.base import NotFound, RateLimited, RobotsDisallowed, SourceError

    url = url.strip()
    if not re.match(r"https?://[^\s/]+\.[^\s/]+", url):
        raise IntakeError("that does not look like a web address (it should start with https://)")
    host = re.sub(r"^https?://(www\.)?", "", url).split("/")[0].lower()
    if host.endswith("linkedin.com"):
        raise IntakeError("LinkedIn does not let anything but a signed-in browser read its "
                          "postings. Open the posting, copy its text and paste it below")
    get = fetcher if fetcher is not None else _get()
    try:
        page = get(url)
    except RobotsDisallowed as exc:
        raise IntakeError(f"{host} asks robots not to read this page, so it was not fetched. "
                          "Paste the posting's text instead") from exc
    except NotFound as exc:
        raise IntakeError("the page was not found — the posting may have closed") from exc
    except RateLimited as exc:
        raise IntakeError(f"{host} refused for now (rate limit). Try again later or "
                          "paste the text") from exc
    except SourceError as exc:
        code = re.search(r"\b([45]\d\d)\b", str(exc))
        why = f"it answered {code.group(1)}" if code else str(exc)[:80]
        raise IntakeError(f"{host} does not serve its pages to an automated reader ({why}). "
                          "Paste the posting's text instead") from exc
    except Exception as exc:  # noqa: BLE001 - network trouble, in words
        raise IntakeError(f"could not reach {host} ({type(exc).__name__}: {str(exc)[:100]}). "
                          "Paste the posting's text instead") from exc
    return from_html(page, url, router=router)


def _get() -> Any:
    """The jobs layer's robots-gated, rate-limited, cached client."""
    from ..sources.jobs.base import PoliteClient

    client = PoliteClient()
    return lambda url: client.get(url, accept="text/html,application/xhtml+xml")


# ───────────────────────────── saving ─────────────────────────────


def add_posting(conn: sqlite3.Connection, posting: Posting, *,
                letter_language: str = "en") -> int:
    """Save it as a job and put it on the selection. Returns the job id.

    The same URL twice is the same job (refreshed, not duplicated). Pasted text
    with no URL is keyed on its own text, for the same reason.
    """
    from ..db import JobRepo, JobSourceRepo
    from ..jobsearch import tracker
    from ..sources.jobs.base import JobPosting

    if posting.missing:
        raise IntakeError("still missing: " + ", ".join(posting.missing))
    url = posting.url.strip() or (
        "pasted:" + hashlib.sha1(posting.description.encode("utf-8")).hexdigest()[:16])
    source_id = JobSourceRepo(conn).upsert(SOURCE_NAME, "manual")
    job = JobPosting(
        title=posting.title.strip(), url=url, description=posting.description.strip(),
        location=posting.location.strip() or None, company_name=posting.company.strip(),
        posted_at=posting.posted_at, employment_type=posting.employment_type,
        raw={"added_by": posting.method or "hand", "letter_language": letter_language,
             "posting_language": posting.language, "notes": posting.notes,
             "original": posting.original},
    )
    job_id = JobRepo(conn).upsert(source_id, job).job_id
    tracker.ensure_for_job(conn, job_id, "SHORTLISTED",
                           note=f"added from {posting.method or 'a link'}")
    return job_id
