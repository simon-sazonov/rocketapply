"""SERP row -> person fields — §8.1 and Phase 3 of PITWALL_PLAN.md.

LinkedIn profile results have a consistent SERP title shape::

    "Jane Doe - Senior Data Scientist - McLaren Racing | LinkedIn"
    "Jane Doe – Race Strategy Engineer – Scuderia Ferrari HP | LinkedIn"
    "Jane Doe - McLaren Racing | LinkedIn"
    "Jane Doe on LinkedIn: #f1 #datascience ..."      <- a POST, not a profile

The parser is deliberately regex-isolated and fixture-tested: the LLM sees the
raw title and snippet anyway, so it can recover fields the regex missed
(risk #2 in §15).
"""

from __future__ import annotations

import re
from typing import Any

from .normalize import (
    canonicalize_url,
    extract_linkedin_slug,
    extract_location,
    normalize_name,
    squash,
)

# ─────────────────────────────── patterns ────────────────────────────────

#: URL path prefixes that are *not* a person.
NON_PROFILE_PATHS = (
    "/posts/", "/pulse/", "/company/", "/jobs/", "/school/", "/showcase/",
    "/learning/", "/groups/", "/events/", "/feed/", "/newsletters/",
    "/products/", "/services/", "/today/", "/pub/dir", "/directory/",
)

#: The `| LinkedIn` / `· LinkedIn` suffix search engines append.
LINKEDIN_SUFFIX_RE = re.compile(r"\s*[|·•]\s*LinkedIn\s*$", re.I)

#: Some metasearch backends glue several results into one title string:
#: "Jorge Segers - Race Engineer at AF CORSE | LinkedInUgo Maceratini - ...".
#: The first segment is the one that belongs to this URL; the rest is another
#: person's result and would poison both the parse and the LLM prompt.
CONCATENATED_RE = re.compile(r"[|·•]\s*LinkedIn(?=\S)", re.I)

#: The other glue shape: the first result's truncation ellipsis running
#: straight into the next result's name, with no space —
#: "... @ Scuderia Ferrari ...Jorge Segers - Race Engineer at AF CORSE".
#: A genuine trailing ellipsis is followed by a space or the end of the string.
ELLIPSIS_GLUE_RE = re.compile(r"\.\.\.(?=[A-ZÀ-Ý][a-zà-ÿ])")

#: Segment separator inside a SERP title. A hyphen/en-dash needs surrounding
#: spaces, so "Data Scientist - Vehicle Dynamics" only splits where LinkedIn
#: actually puts a separator; `·` and `|` split with or without them.
SEGMENT_RE = re.compile(r"\s+[-–—/]\s+|\s*[·•]\s*|\s*\|\s*")

#: A post, not a profile: "Jane Doe on LinkedIn: ..."
POST_TITLE_RE = re.compile(r"\bon LinkedIn\s*:", re.I)

#: Employer-preposition forms: "Data Scientist at McLaren Racing".
AT_RE = re.compile(
    r"^(?P<title>.{2,90}?)\s+(?:at|@|with|for|bei|chez|presso)\s+(?P<company>.{2,80})$", re.I
)

#: Snippet forms that state the current employer.
SNIPPET_AT_RE = re.compile(
    r"(?:^|[.·•|]\s*)(?P<title>[^.·•|\n]{2,70}?)\s+(?:at|@|with|for)\s+(?P<company>[^.·•|\n]{2,60})"
)
EXPERIENCE_RE = re.compile(r"Experience\s*:\s*(?P<company>[^·•|\n.]{2,60})", re.I)

#: Trailing noise LinkedIn appends to snippets.
CONNECTION_NOISE_RE = re.compile(
    r"\b(\d[\d,\.]*\+?\s*(?:connections|followers|volgers|Kontakte|contactos|contatti|"
    r"relations|seguidores|mutual connections))\b",
    re.I,
)

#: Sentence tails that are narration about a post, not employment.
POST_TAIL_RE = re.compile(
    r"\b(posted|shared|commented|likes?|reposted|celebrating|congratulat\w*)\b.*$", re.I
)

_WS = re.compile(r"\s+")

#: Longest plausible person name in a SERP title.
MAX_NAME_LEN = 60


def clean_serp_title(title: str) -> str:
    """Cut a concatenated multi-result title down to its first result."""
    if not title:
        return ""
    for pattern in (CONCATENATED_RE, ELLIPSIS_GLUE_RE):
        m = pattern.search(title)
        if m:
            title = title[: m.start()].rstrip(" |·•-–—")
    return _WS.sub(" ", title).strip(" .·•|-–—")


def is_profile_url(url: str) -> bool:
    """True only for `linkedin.com/in/<slug>` person pages."""
    if not url:
        return False
    low = url.lower()
    if "linkedin.com" not in low:
        return False
    if any(p in low for p in NON_PROFILE_PATHS):
        return False
    return extract_linkedin_slug(url) is not None


def clean_snippet(snippet: str) -> str:
    """Strip the connection-count boilerplate and collapse whitespace."""
    if not snippet:
        return ""
    s = CONNECTION_NOISE_RE.sub(" ", snippet)
    s = s.replace("·", " · ")
    return _WS.sub(" ", s).strip(" ·|-")


def _split_segments(rest: str) -> list[str]:
    return [p.strip(" ·|-–—") for p in SEGMENT_RE.split(rest) if p.strip(" ·|-–—")]


def parse_profile(title: str, snippet: str, url: str) -> dict[str, Any] | None:
    """Parse one SERP row into candidate fields, or None if it is not a person.

    Returns keys: ``linkedin_slug``, ``profile_url``, ``canonical_url``,
    ``full_name``, ``current_title``, ``current_company``, ``headline``,
    ``location``.
    """
    slug = extract_linkedin_slug(url or "")
    if not slug or not is_profile_url(url or ""):
        return None

    out: dict[str, Any] = {
        "linkedin_slug": slug,
        "profile_url": f"https://www.linkedin.com/in/{slug}",
        "canonical_url": canonicalize_url(url),
        "full_name": None,
        "current_title": None,
        "current_company": None,
    }

    raw_title = clean_serp_title(title or "")
    body = clean_snippet(snippet or "")

    # A profile URL whose title is a post headline is still the person's page,
    # but the title carries no employment information — use the snippet instead.
    if POST_TITLE_RE.search(raw_title):
        out["full_name"] = normalize_name(POST_TITLE_RE.split(raw_title)[0].rsplit(" on ", 1)[0])
    else:
        core = LINKEDIN_SUFFIX_RE.sub("", raw_title).strip()
        segments = [p.strip(" ·•|-–—") for p in SEGMENT_RE.split(core) if p.strip(" ·•|-–—")]
        if segments:
            name = normalize_name(segments[0])
            # Search engines occasionally concatenate several results into one
            # title string; a 60+ character "name" is that, not a person.
            out["full_name"] = name if 1 < len(name) <= MAX_NAME_LEN else None
            if out["full_name"]:
                _apply_segments(out, segments[1:])

    if not out["full_name"]:
        out["full_name"] = _name_from_slug(slug)

    # Fall back to the snippet for whatever the title did not give us.
    if not out["current_company"] or not out["current_title"]:
        _apply_snippet(out, body)

    out["headline"] = body[:400] or None
    out["location"] = extract_location(body)
    return out


def _apply_segments(out: dict[str, Any], parts: list[str]) -> None:
    """Assign title/employer from the segments following the person's name.

    The **last** segment is the employer when there are two or more; middle
    segments stay with the title unless they read like a company. A lone
    segment is a title unless it reads like a bare company name.
    """
    parts = [p for p in parts if p and squash(p) not in {"linkedin", ""}]
    if not parts:
        return

    if len(parts) == 1:
        only = parts[0]
        at = AT_RE.match(only)
        if at:
            out["current_title"] = _tidy(at.group("title"))
            out["current_company"] = _tidy_company(at.group("company"))
        elif _looks_like_role(only):
            out["current_title"] = _tidy(only)
        else:
            out["current_company"] = _tidy_company(only)
        return

    head, tail = parts[0], parts[-1]
    at = AT_RE.match(head)
    if at:
        out["current_title"] = _tidy(at.group("title"))
        out["current_company"] = _tidy_company(at.group("company"))
        return

    # "Wayne Gross - Manager / Chief Engineer" has no employer at all: the last
    # segment is another job title, not a company.
    if _looks_like_role(tail) and not _looks_like_company(tail):
        out["current_title"] = _tidy(" - ".join(parts))
        return

    title_parts = [head]
    for mid in parts[1:-1]:
        if not _looks_like_company(mid):
            title_parts.append(mid)
    out["current_title"] = _tidy(" - ".join(title_parts))
    out["current_company"] = _tidy_company(tail)


def _apply_snippet(out: dict[str, Any], body: str) -> None:
    """Recover title/employer from the snippet when the title did not have them."""
    if not body:
        return
    if not out.get("current_company"):
        m = SNIPPET_AT_RE.search(body)
        if m:
            title = _strip_leading_name(m.group("title"), out.get("full_name"))
            if not out.get("current_title") and title and _looks_like_role(title):
                out["current_title"] = _tidy(title)
            out["current_company"] = _tidy_company(m.group("company"))
        else:
            e = EXPERIENCE_RE.search(body)
            if e:
                out["current_company"] = _tidy_company(e.group("company"))


def _strip_leading_name(text: str, name: str | None) -> str:
    """`"Ana Silva, Performance Engineer"` -> `"Performance Engineer"`."""
    t = (text or "").strip()
    if name and squash(t).startswith(squash(name)):
        t = t[len(name):].lstrip(" ,-–—·")
    if "," in t:
        head, _, tail = t.rpartition(",")
        if head and tail.strip():
            t = tail.strip()
    return t


ROLE_WORDS = frozenset(
    {
        "engineer", "engineering", "scientist", "science", "analyst", "analytics",
        "developer", "manager", "lead", "head", "director", "specialist",
        "consultant", "researcher", "research", "student", "intern", "graduate",
        "strategist", "strategy", "architect", "designer", "technician",
        "programmer", "officer", "coordinator", "data", "software", "simulation",
        "performance", "aerodynamicist", "mechanic", "apprentice", "principal",
        "chief", "senior", "junior", "trainee", "associate", "assistant", "phd",
        "candidate", "professor", "founder", "owner", "president", "vp", "cto",
    }
)

COMPANY_WORDS = frozenset(
    {
        "racing", "motorsport", "motorsports", "team", "gmbh", "ltd", "llc",
        "inc", "ag", "spa", "srl", "bv", "sas", "university", "college",
        "school", "group", "technologies", "technology", "sport", "sports",
        "f1", "gp", "corse", "scuderia", "automotive", "engineering",
    }
)


def _looks_like_role(text: str) -> bool:
    toks = set(squash(text).split())
    if toks & ROLE_WORDS:
        # "Racing Bulls" contains no role words; "Data Engineering" does.
        return not (toks & (COMPANY_WORDS - {"engineering"}))
    return False


def _looks_like_company(text: str) -> bool:
    toks = set(squash(text).split())
    return bool(toks & COMPANY_WORDS) and not (toks & (ROLE_WORDS - {"engineering"}))


#: Connectors that start a subordinate clause after the employer name —
#: "BMW M Motorsport for the LMDh Hypercar programme".
CLAUSE_RE = re.compile(r"\s+(?:for|with|in|on|since|during|as|to|from|where|who|which)\s+", re.I)

#: Words that mean the captured string is prose, not an organisation.
PROSE_WORDS = frozenset(
    {"years", "experience", "university", "least", "including", "such", "role",
     "candidate", "responsible", "requirements", "must", "will", "should"}
)

#: Longest plausible employer name, in words.
MAX_COMPANY_WORDS = 7


def _tidy_company(text: str | None) -> str | None:
    """Trim narration and clauses off an employer string, or reject it as prose.

    Snippet-derived employers are the risky ones: a sentence like "…requires at
    least 2 years of motorsport engineering experience" will otherwise be
    stored as somebody's current company.
    """
    if not text:
        return None
    t = POST_TAIL_RE.sub("", str(text)).strip()
    t = CLAUSE_RE.split(t)[0]
    if "," in t:
        t = re.split(r"\s+(?:and|&)\s+|,\s*", t)[0]
    tidied = _tidy(t)
    if not tidied:
        return None
    words = tidied.split()
    if len(words) > MAX_COMPANY_WORDS:
        return None
    if {w.lower().strip(".,") for w in words} & PROSE_WORDS:
        return None
    if words[0][:1].islower() and words[0].lower() not in {"e", "af", "de", "van", "von"}:
        return None
    return tidied


def _tidy(text: str | None) -> str | None:
    if not text:
        return None
    t = _WS.sub(" ", str(text)).strip(" .,·|-–—")
    t = re.sub(r"\s*[|·]\s*LinkedIn\s*$", "", t, flags=re.I).strip()
    return t or None


def _name_from_slug(slug: str) -> str:
    """`jane-doe-1a2b3c` -> `Jane Doe`. Last-resort name recovery."""
    parts = [p for p in re.split(r"[-_]", slug) if p and not re.fullmatch(r"[0-9a-f]{4,}", p)]
    parts = [p for p in parts if not p.isdigit()]
    return " ".join(p.capitalize() for p in parts[:4]) if parts else slug


def parse_many(rows: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Parse a list of `search_results` rows. Returns (row, fields) for hits."""
    out: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for row in rows:
        fields = parse_profile(row.get("title", ""), row.get("snippet", ""), row.get("url", ""))
        if fields:
            out.append((row, fields))
    return out
