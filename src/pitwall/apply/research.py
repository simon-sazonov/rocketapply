"""Finding out what an employer actually builds, from their own pages.

`profile/companies.yaml` is the one thing the machine cannot write — except that
it is empty, so the planner is told "(none — do not invent one)" on every run and
the closing paragraph of every letter could have been addressed to anyone. Across
five graded letters the voice scored 4.54/5 and company motivation 5.5-7/10, the
only category that failed in all five.

This module produces a grounded draft of that paragraph's raw material. It does
not replace Simon's own reading: `planner.company_notes_with_source` consults
`companies.yaml` first and only reaches the cache when nothing there matches.

WHAT THIS MODULE IS BUILT AROUND
--------------------------------
A real run on 25 Sep 2026 for a small employer returned a SERP of ResearchGate,
PaperGate, academia.edu, a "40 must-read engineering blogs" listicle and the
Geena Davis Institute. The model read those eight snippets and wrote

    "The team develops and deploys discrete diffusion models"

citing ResearchGate and PaperGate, and reported `confidence: "high"`. Two runs at
temperature 0 gave two different fabrications.

Dropping every snippet that does not name the company left three — their GitHub
org and two of their own job listings — and with only those in the prompt that
sentence is not writable. That filter is `on_topic`, it is deterministic, it runs
before the model sees anything, and it is the load-bearing part of this file. The
model calls around it are convenience; the filters are the feature.

The other half of the defence is that a blank is a correct answer. The planner
prompt already says so, `letter.py` already says so, and every gate here writes a
negative record rather than reaching for something to say.
"""

from __future__ import annotations

import logging
import pathlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from ..config import Settings, get_settings
from ..enrich.domains import domain_matches_company, is_company_domain, registrable_domain
from ..jobsearch.companies import normalize_company
from ..search.base import SearchHit
from .grounding import is_claim_bearing, ungrounded_terms
from .research_cache import CompanyResearch, ResearchCache

log = logging.getLogger(__name__)

PROMPTS = pathlib.Path(__file__).parent / "prompts"

__all__ = [
    "NOISE_HOSTS", "QUERY_TEMPLATES", "OPERATOR_TEMPLATES", "ResearchReport",
    "on_topic", "filter_hits", "dedupe", "own_domain", "gather", "rank_sources",
    "research_company", "research_companies", "prepass", "dry_run_report",
    "best_source", "ungrounded_terms", "is_claim_bearing", "flattery_hits",
    "name_the_employer",
    "plain_text",
    "RESEARCH_SCHEMA", "VERIFY_SCHEMA", "FLATTERY",
]

#: Plain keyword questions, no operators anywhere. The provider that actually
#: answers on this machine is ddgs, whose `supports_operators()` is False, and a
#: research question does not need operators the way a `site:` harvest does.
#: Each template finds a different kind of page: the first their own dated
#: technical writing, the second their own description of the work, the third a
#: concrete shipped thing.
QUERY_TEMPLATES: tuple[str, ...] = (
    "{company} engineering blog",
    "{company} engineering team what we build",
    "{company} product launch technical announcement",
)

#: Added only when the chain keeps operators (SearXNG up). A quoted exact phrase
#: separates "Meetdavis" from the Geena Davis Institute at the engine rather than
#: in our filter, so precision is much higher — but this is a bonus, never a
#: precondition, and research never early-outs without it the way `harvest` and
#: `corroborate` do.
OPERATOR_TEMPLATES: tuple[str, ...] = ('"{company}" engineering',)

#: Hosts that mention a company without being about it.
#:
#: DO NOT REPLACE THIS WITH `enrich.domains.NON_COMPANY_HOSTS`. That set contains
#: github.com, lever.co, medium.com and wikipedia.org — precisely the sources the
#: verified fix KEPT (a company's own GitHub org and their own job listings were
#: two of the three survivors), and the Wikipedia page that supplied the single
#: best fact in the good run. It answers "where does this company's mail live",
#: which is a different question with a different right answer.
#:
#: Every entry below came out of one real run and is a page that discusses
#: everybody, so naming a company on it means nothing.
NOISE_HOSTS: frozenset[str] = frozenset({
    "researchgate.net", "academia.edu", "semanticscholar.org", "core.ac.uk",
    "scribd.com", "slideshare.net", "studocu.com", "coursehero.com",
    "pinterest.com", "quora.com", "indeed.com", "glassdoor.com", "glassdoor.co.uk",
    "ziprecruiter.com", "zoominfo.com", "rocketreach.co", "apollo.io",
    "crunchbase.com", "pitchbook.com", "tracxn.com", "wellfound.com",
    "papergate.org", "paperswithcode.com", "arxiv-sanity.com",
})

#: A name shorter than this is too easy to find inside an unrelated word, so it
#: only ever matches on a word boundary.
SQUASH_FALLBACK_MIN = 7

#: Says honestly what this is, on the few fetches this module makes.
USER_AGENT = "PitWall/6.0 (cover-letter company research; +robots-compliant)"

#: How many sources the model is ever shown. A live run for a well-covered
#: employer kept twenty, which is ~12,000 characters of snippet and three times
#: the prompt the estimate was built on — for no gain, because the eleventh
#: source repeats the third. The company's own pages are ranked first, so the cap
#: bites on trade-press rewrites of the same announcement rather than on the
#: primary material.
MAX_SOURCES = 10


@dataclass
class ResearchReport:
    """What one company's research did, whether or not it found anything."""

    record: CompanyResearch = field(default_factory=CompanyResearch)
    queries_run: int = 0
    hits_seen: int = 0
    kept: int = 0
    dropped: int = 0
    pages_fetched: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    #: ok | thin | ungrounded | unverified | no-provider | model-failed | cached | skipped
    outcome: str = "ok"
    notes: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.record.usable


# ── the deterministic half ──


def _name_patterns(company: str) -> list[re.Pattern[str]]:
    """Every spelling of this employer's name that counts as a mention.

    Two directions have to work, and neither is the obvious substring test:

    * the name is spaced and the page runs it together — "Meet Davis" written as
      "MeetDavis". Allowing zero separators between the words covers it.
    * the name runs together and the page spaces it, which needs a guess at where
      the split falls, so every two-way split with both halves at least three
      characters long is tried.

    ALWAYS ANCHORED ON WORD BOUNDARIES. The first version fell back to a bare
    substring search of the de-spaced text, and "Helsing" matched "Helsingborg
    harbour" — a Swedish port read as a German defence company, which is this
    filter failing in the exact way it exists to prevent.
    """
    target = normalize_company(company)
    if not target:
        return []
    words = target.split()
    out = [re.compile(r"\b" + r"\W*".join(re.escape(w) for w in words) + r"\b", re.I)]
    if len(words) == 1 and len(target) >= SQUASH_FALLBACK_MIN:
        for cut in range(3, len(target) - 2):
            head, tail = re.escape(target[:cut]), re.escape(target[cut:])
            out.append(re.compile(r"\b" + head + r"\W*" + tail + r"\b", re.I))
    return out


def _name_pattern(company: str) -> re.Pattern[str] | None:
    """The plain spelling, for "does this sentence name the employer at all"."""
    patterns = _name_patterns(company)
    return patterns[0] if patterns else None


def host_of(hit: SearchHit) -> str | None:
    return registrable_domain(urlsplit(str(hit.url)).hostname or "")


def on_topic(hit: SearchHit, company: str) -> bool:
    """Does this result actually mention this employer?

    THIS IS THE GATE THAT PREVENTS FABRICATION, and the reasoning is in the module
    docstring: on a real run the eight results that did not name the company were
    what the model wrote its invented sentence out of, and removing them made the
    sentence unwritable.

    Four ways to pass, in order of how much they prove:

    Two ways to pass:

    1. the registrable domain is the company's own (a page title alone is then
       enough — their blog index does not always repeat their name);
    2. any spelling from `_name_patterns` appears in the title or snippet, every
       one of them anchored on word boundaries so "Helsing" does not match
       "Helsingborg";

    and one way to fail outright: a host in `NOISE_HOSTS`, which discusses
    everybody and therefore proves nothing by naming anyone.
    """
    patterns = _name_patterns(company)
    if not patterns:
        return False
    host = host_of(hit)
    if host and host in NOISE_HOSTS:
        return False
    if host and domain_matches_company(host, company) and is_company_domain(host):
        return True
    text = f"{hit.title}\n{hit.snippet}"
    return any(p.search(text) for p in patterns)


def filter_hits(
    hits: Sequence[SearchHit], company: str
) -> tuple[list[SearchHit], list[SearchHit]]:
    """`(kept, dropped)`. Order is preserved so the first result stays first."""
    kept: list[SearchHit] = []
    dropped: list[SearchHit] = []
    for hit in hits:
        (kept if on_topic(hit, company) else dropped).append(hit)
    return kept, dropped


def dedupe(hits: Iterable[SearchHit]) -> list[SearchHit]:
    """One hit per page, first seen wins.

    Keyed on host + path with the query string dropped, because the same post
    arrives as `?gi=…` tracking variants from different engines and three copies
    of one page look like three sources to every count downstream.
    """
    seen: set[tuple[str, str]] = set()
    out: list[SearchHit] = []
    for hit in hits:
        parts = urlsplit(str(hit.url))
        key = ((parts.hostname or "").lower(), parts.path.rstrip("/").lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(hit)
    return out


def own_domain(hits: Sequence[SearchHit], company: str) -> str | None:
    """The domain that is plausibly the company's own, or None.

    Only a domain that is both `is_company_domain` (not an ATS, not a social
    network) and `domain_matches_company` qualifies, and the most frequent one
    wins. When there is none, the caller fetches NOTHING: reading a page from a
    domain that could not be tied to the employer is the fabrication risk wearing
    a different hat.
    """
    counts: dict[str, int] = {}
    for hit in hits:
        host = host_of(hit)
        if host and is_company_domain(host) and domain_matches_company(host, company):
            counts[host] = counts.get(host, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda d: (counts[d], -list(counts).index(d)))


def rank_sources(
    hits: Sequence[SearchHit], company: str, limit: int = MAX_SOURCES
) -> list[SearchHit]:
    """The company's own pages first, then everything else in rank order.

    Stable within each group, so the engine's own ordering survives. What this
    buys is not just a shorter prompt: when the cap bites, what it removes is the
    trade-press rewrite rather than the engineering blog.
    """
    own = own_domain(hits, company)
    mine = [h for h in hits if own and host_of(h) == own]
    rest = [h for h in hits if not (own and host_of(h) == own)]
    return (mine + rest)[:limit]


def _own_hits(hits: Sequence[SearchHit], company: str) -> list[SearchHit]:
    own = own_domain(hits, company)
    return [h for h in hits if own and host_of(h) == own]


def snippet_chars(hits: Sequence[SearchHit]) -> int:
    return sum(len(h.snippet or "") for h in hits)


def deepest_source(hits: Sequence[SearchHit]) -> int:
    """Characters on the single richest source. See where it is used."""
    return max((len(h.snippet or "") for h in hits), default=0)


# ── searching ──


def _searcher(settings: Settings) -> Any:
    """The chain, with the operator filter switched off.

    `SerpSearcher` drops any provider whose `supports_operators()` is False,
    because enrichment's queries ARE operators. Research asks plain keyword
    questions, and on this machine the only reachable provider is ddgs, which
    reports False — so with the default the searcher has no providers and every
    run comes back silently empty.
    """
    from ..enrich.base import SerpSearcher

    return SerpSearcher(settings=settings, max_results=8, operators_only=False)


def gather(
    company: str,
    *,
    searcher: Any | None = None,
    settings: Settings | None = None,
    max_queries: int | None = None,
) -> tuple[list[SearchHit], list[SearchHit], list[str], list[str]]:
    """Search for this employer. Returns `(kept, dropped, queries, notes)`.

    One dead query is not a dead research run — the loop shape is `harvest`'s.
    """
    settings = settings or get_settings()
    searcher = searcher if searcher is not None else _searcher(settings)
    limit = settings.apply_research_queries if max_queries is None else max_queries

    templates = list(QUERY_TEMPLATES[:limit])
    notes: list[str] = []
    if searcher.supports_operators():
        templates += list(OPERATOR_TEMPLATES)
    else:
        notes.append(
            "the search provider does not support quoted phrases, so the queries are "
            "plain keywords and the name-mention filter is doing all the work — start "
            "SearXNG (`make searxng`) for a sharper first pass"
        )

    hits: list[SearchHit] = []
    queries: list[str] = []
    for template in templates:
        query = template.format(company=company)
        try:
            found = searcher.search(query, max_results=8)
        except Exception as exc:  # noqa: BLE001 - one dead query is not a dead run
            log.info("research query failed (%s): %s", query, exc)
            notes.append(f"a query failed: {str(exc)[:100]}")
            continue
        queries.append(query)
        hits.extend(found)

    kept, dropped = filter_hits(dedupe(hits), company)
    return kept, dropped, queries, notes


def numbered_sources(hits: Sequence[SearchHit]) -> str:
    """The corpus as the model sees it: numbered, with the URL on the line.

    Numbered rather than bulleted because the schema asks for a `source_index`
    per sentence, which is what makes a citation machine-checkable instead of
    decorative.
    """
    return "\n\n".join(
        f"[{n}] {h.url}\n{h.title}\n{clip(h.snippet or '')}"
        for n, h in enumerate(hits, start=1)
    )


#: How much of one source the model is shown.
SNIPPET_CHARS = 600


def clip(text: str, limit: int = SNIPPET_CHARS) -> str:
    """Cut to the last complete sentence inside `limit`, not to the character.

    A hard cut at 600 characters is what produced the shipped sentence "The team
    speeds up agentic workflows with WebSockets in the Responses." — the source
    said "the Responses API" and the clip landed mid-phrase, so the model
    completed a fragment and the result read as broken English rather than as a
    fact. `incomplete_sentence` catches the symptom; this removes the cause.

    Where no sentence ends inside the limit the cut is explicit, so the model can
    see that what it has is partial rather than guessing that it is whole.
    """
    body = (text or "").strip()
    if len(body) <= limit:
        return body
    window = body[:limit]
    cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
    if cut >= limit // 3:
        return window[:cut + 1]
    return window.rstrip() + " \u2026"


def corpus_text(hits: Sequence[SearchHit]) -> str:
    """Everything the model was shown, for the grounding check to search."""
    return "\n".join(f"{h.title}\n{h.snippet}" for h in hits)


def thin_record(company: str, kept: Sequence[SearchHit], queries: Sequence[str]) -> CompanyResearch:
    """A negative result, cached like any other.

    Caching a blank is what stops six batches over one shortlist costing six
    search rounds. It is distinct from a FAILURE to look — no provider, no model
    — which writes no record at all so the next run retries.
    """
    return CompanyResearch(
        company=company,
        notes="",
        sources=[str(h.url) for h in kept],
        queries=list(queries),
        verified=False,
        outcome="thin",
    )


def enough_sources(kept: Sequence[SearchHit], settings: Settings) -> bool:
    """Two conditions, and both are needed.

    The count is the verified threshold. The distinct-domain requirement stops
    three snippets scraped off one aggregator page from looking like three
    independent corroborations of each other.
    """
    if len(kept) < settings.apply_research_min_sources:
        return False
    return len({host_of(h) for h in kept if host_of(h)}) >= 2



def dry_run_report(company: str, *, console: Any = None, settings: Settings | None = None,
                   searcher: Any | None = None) -> bool:
    """Search, filter, print — and call no model. Returns whether it would proceed.

    This is the highest-value thing in the file. It is the hand experiment that
    found the fabrication, turned into a command: it shows exactly what the model
    would be shown, and what was thrown away before it. Running it costs nothing
    and answers "why did this company come back blank" without spending a token.
    """
    settings = settings or get_settings()
    kept, dropped, queries, notes = gather(company, searcher=searcher, settings=settings)
    shown = rank_sources(kept, company)
    ok = enough_sources(kept, settings)

    if console is None:
        return ok
    console.print(f"\n[bold]{company}[/]  [dim]{len(queries)} queries, "
                  f"{len(kept) + len(dropped)} hits, {len(kept)} on topic[/]")
    for note in notes:
        console.print(f"  [dim]{note}[/]")
    console.print(f"  [green]the model would see {len(shown)}[/]"
                  if ok else "  [red]nothing would be sent to the model[/]")
    for hit in shown:
        console.print(f"    + {hit.url}")
    if len(kept) > len(shown):
        console.print(f"    [dim]… and {len(kept) - len(shown)} more on topic, over the cap[/]")
    for hit in dropped:
        console.print(f"    [dim]- {hit.url}[/]")
    if not ok:
        console.print(f"  [dim]needs {settings.apply_research_min_sources} on-topic sources "
                      f"across 2 domains; the answer is an honest blank[/]")
    return ok


# ── the model calls, and the gates around them ──

RESEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sentence": {"type": "string"},
                    "source_index": {"type": "integer"},
                },
                "required": ["sentence", "source_index"],
                "additionalProperties": False,
            },
        },
        "confidence": {"type": "string", "enum": ["high", "medium", "none"]},
    },
    "required": ["notes", "confidence"],
    "additionalProperties": False,
}

#: TWO FLAT ARRAYS OF NUMBERS, and the shape is the lesson. The first version
#: asked for a list of `{index, verdict, unsupported_span}` objects, which Groq
#: produces happily and the local Qwen does not: `schema_hint` can only name
#: top-level keys, so a nested item shape reaches an unconstrained model as
#: `[object, …]` and it invents one. Every sentence then read as an unreadable
#: verdict and a whole good company came back blank.
#:
#: A sentence must appear in `supported` to survive. Silence is not a pass —
#: that is what keeps this fail-closed with a schema this simple.
VERIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "supported": {"type": "array", "items": {"type": "integer"}},
        "unsupported": {"type": "array", "items": {"type": "integer"}},
        # The label rides along on the call that is already reading every sentence
        # against every source, so it costs nothing. FLAT, like its siblings: a
        # nested `[{index, label}]` is exactly the shape that reached an
        # unconstrained local model as `[object, …]` and came back invented.
        "engineering": {"type": "array", "items": {"type": "integer"}},
        "business": {"type": "array", "items": {"type": "integer"}},
        "marketing": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["supported", "unsupported", "engineering", "business", "marketing"],
    "additionalProperties": False,
}

#: Best first. `marketing` is dropped outright — a sentence that is how a company
#: describes itself is true of its competitor with two words changed, which is the
#: exact failure the five graded letters were marked down for. `business` is kept
#: but ranked last: a funding round is true, sourced, and the wrong fact for a
#: letter about engineering.
LABEL_RANK: dict[str, int] = {"engineering": 0, "business": 1}

#: Praise, dressed as fact. Shaped like `letter.OVERCLAIM` — `(pattern, why)` —
#: and kept here rather than added to `style.yaml`, because that list is enforced
#: on the LETTER and extending it to fix a research problem would change letter
#: behaviour for every run.
FLATTERY: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:leading|pioneering|premier|world[- ]class|foremost)\b", re.I),
     "a ranking claim no snippet can support"),
    (re.compile(r"\bat\s+the\s+(?:forefront|cutting\s+edge|vanguard)\b", re.I),
     "a position claim rather than a fact about the work"),
    (re.compile(r"\b(?:mission|vision|values|culture|ethos)[- ]?(?:driven|led|focused)?\b", re.I),
     "true of every employer — say what they build instead"),
    (re.compile(r"\b(?:committed|dedicated)\s+to\b", re.I),
     "a statement of intent, not of work"),
    (re.compile(r"\brevolutioni[sz]\w+\b", re.I), "marketing copy quoted back as fact"),
    (re.compile(r"\b(?:fast|rapidly)[- ]growing\b|\bscaling\s+quickly\b", re.I),
     "a growth claim, not something about the engineering"),
    (re.compile(r"\b(?:innovative|innovation|state[- ]of[- ]the[- ]art|cutting[- ]edge)\b", re.I),
     "praise with no content"),
)

#: Typographic characters a model reaches for that the letter path then has to
#: cope with, mapped to what the profile's own facts use. U+202F in particular is
#: a narrow no-break space, and `letter.THIN_SPACE_DIGITS` treats one between
#: digits as a violation to be rewritten — so a note carrying one is a note that
#: makes the letter harder to accept for no gain. A cached note is quoted into
#: two prompts and a PDF, so it is normalised once, here, at the source.
_TYPOGRAPHIC: tuple[tuple[str, str], ...] = (
    ("\u202f", " "), ("\u00a0", " "), ("\u2007", " "), ("\u2009", " "),
    ("\u2011", "-"), ("\u2013", "-"), ("\u2014", "-"),
    ("\u2018", "'"), ("\u2019", "'"), ("\u201c", '"'), ("\u201d", '"'),
)


def plain_text(value: str) -> str:
    """The sentence with a model's typography flattened to what the profile uses."""
    out = str(value or "")
    for fancy, plain in _TYPOGRAPHIC:
        out = out.replace(fancy, plain)
    return " ".join(out.split())


#: Words a sentence must not end on. A sentence stopping here was cut off, not
#: finished, whatever punctuation follows.
_DANGLING: frozenset[str] = frozenset({
    "in", "of", "the", "a", "an", "and", "or", "with", "for", "to", "on", "at",
    "by", "from", "that", "which", "as", "into", "their", "its", "than", "is",
    "was", "are", "were", "be", "been", "has", "have", "had", "will", "would",
})

#: "They publish blog posts" is a fact about their publishing, not about what they
#: build. BOUND TO BOTH THE VERB AND THE OBJECT on purpose: a blunt rule on
#: `publish\w*` alone would also reject "publishes an open-source spec for
#: orchestration called Symphony", which is one of the best sentences this whole
#: feature has produced and which a test now protects.
META: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(
        r"\b(?:publish\w*|post\w*|shar\w*|announc\w*|present\w*|releas\w*)\b"
        r"(?:\W+\w+){0,5}?\W+"
        r"(?:blog|blogs|article|articles|news|newsletter|update|updates|"
        r"press\s+release|podcast|webinar|video|videos|case\s+stud\w+)\b", re.I),
     "that they publish things is not a fact about what they build"),
)


def incomplete_sentence(text: str) -> str:
    """Why this is not a whole sentence, or `""` if it is.

    Deterministic and free, and it catches the one defect that is unambiguously
    not worth a model call: a sentence the snippet clipping cut in half.
    """
    body = (text or "").strip()
    if not body:
        return "it is empty"
    if body[-1] not in ".!?":
        return "it does not end in a full stop"
    words = body.rstrip(".!?").split()
    if len(words) < 5:
        return "it is too short to assert anything"
    if words[-1].lower().strip(",;:") in _DANGLING:
        return f"it stops on {words[-1]!r}, so it was cut off rather than finished"
    if body.count("(") != body.count(")") or body.count('"') % 2:
        return "it has an unclosed bracket or quotation mark"
    return ""


def truncated_against(sentence: str, corpus: str) -> str:
    """Why this sentence looks cut off WHEN COMPARED TO THE SOURCE, or `""`.

    The one truncation `incomplete_sentence` cannot see. A shipped sentence read
    "…with WebSockets in the Responses." — grammatical on its face, with a
    capitalised noun where a noun belongs. What gives it away is the source, which
    says "the Responses API": the sentence stops exactly where the source carries
    on, so the last phrase is a fragment of a longer name.

    THE CONTINUATION MUST ITSELF BE CAPITALISED, and that is the whole precision
    of this check. The first version only asked whether the source carried on, and
    "Helsing has a factory in the Netherlands." was rejected because the source
    said "in the Netherlands last year". A cut-off NAME continues with another
    capital — "Responses API", "Claude Code" — while an ordinary sentence carries
    on in lower case.
    """
    body = (sentence or "").rstrip(".!?").strip()
    words = body.split()
    if len(words) < 3 or words[-2].lower() not in {"the", "a", "an"}:
        return ""
    tail = words[-1].strip(",;:")
    if not tail[:1].isupper():
        return ""
    pattern = re.compile(
        rf"\b{re.escape(words[-2])}\s+{re.escape(tail)}\s+([A-Z][\w-]*)"
    )
    if match := pattern.search(corpus):
        return (
            f"it stops at {tail!r}, but the source says "
            f"{tail} {match.group(1)} — the name is cut in half"
        )
    return ""


def meta_hits(text: str) -> list[str]:
    """Every meta-fact pattern this text trips, with the reason."""
    return [why for pattern, why in META if pattern.search(text)]


def flattery_hits(text: str) -> list[str]:
    """Every praise pattern this text trips, with the reason."""
    return [why for pattern, why in FLATTERY if pattern.search(text)]


def best_source(
    sentence: str, hits: Sequence[SearchHit], preferred: int = 0
) -> tuple[int, list[str]]:
    """The source that grounds this sentence best, and what it still cannot.

    Returns `(1-based index, ungrounded terms)`. EVERY DISTINCTIVE TERM OF A
    SENTENCE MUST APPEAR IN ONE SOURCE. Spreading them across two is what lets a
    model say something no page says by borrowing a noun from each.

    The model's own citation is tried first and wins ties, so a correct citation
    is never silently rewritten; where the model cited the wrong page but some
    other page really does say the whole thing, the citation is corrected to the
    page that does rather than the sentence being thrown away for a bookkeeping
    error.
    """
    order = ([preferred] if 1 <= preferred <= len(hits) else []) + [
        n for n in range(1, len(hits) + 1) if n != preferred
    ]
    best, fewest = order[0] if order else 0, None
    for n in order:
        loose = ungrounded_terms(sentence, f"{hits[n - 1].title}\n{hits[n - 1].snippet}")
        if fewest is None or len(loose) < len(fewest):
            best, fewest = n, loose
        if not loose:
            break
    return best, list(fewest or [])


#: Every numeric token, for the figure check below.
_NUM = re.compile(r"\d+(?:[.,]\d+)*")


def ungrounded_figures(sentence: str, corpus: str) -> list[str]:
    """Numbers in the sentence that appear in no source. No rounding, no
    derivation — the same rule the letter's own number gate uses."""
    haystack = corpus.replace(",", "").replace(" ", "")
    return [n for n in _NUM.findall(sentence) if n.replace(",", "") not in haystack]


def screen_sentences(
    drafted: Sequence[dict[str, Any]],
    hits: Sequence[SearchHit],
    company: str,
    *,
    settings: Settings,
    banned: Sequence[str] = (),
) -> tuple[list[dict[str, Any]], list[str]]:
    """The deterministic per-sentence gates. Returns `(kept, rejected reasons)`.

    Every rejection is recorded with its reason, because a good sentence killed
    by an over-strict rule has to be visible in `--show` or the rule can never be
    tuned. A silent drop is how a filter gets left at the wrong setting forever.
    """
    from .letter import banned_phrase_hits

    corpus = corpus_text(hits)
    kept: list[dict[str, Any]] = []
    rejected: list[str] = []

    for item in drafted:
        sentence = plain_text(item.get("sentence") or "")
        if not sentence:
            continue
        index = int(item.get("source_index") or 0)

        def drop(why: str, _s: str = sentence) -> None:
            rejected.append(f"{_s} — {why}")

        if not 1 <= index <= len(hits):
            drop(f"cites source {index}, which does not exist")
            continue
        if why := incomplete_sentence(sentence):
            drop(why)
            continue
        if why := truncated_against(sentence, corpus):
            drop(why)
            continue
        # Named `reasons` rather than `hits`, which is the parameter holding the
        # sources: shadowing it here left `best_source` reading a list of strings
        # for every sentence after the first rejection.
        if reasons := meta_hits(sentence):
            drop(reasons[0])
            continue
        if reasons := flattery_hits(sentence):
            drop(reasons[0])
            continue
        if hit := banned_phrase_hits(sentence, list(banned)):
            drop(f"uses a banned phrase: {hit[0]}")
            continue
        cited, loose = best_source(sentence, hits, preferred=index)
        if len(loose) > settings.apply_research_max_ungrounded_terms:
            drop(f"asserts {', '.join(loose[:3])}, which no single source says")
            continue
        if figures := ungrounded_figures(sentence, corpus):
            drop(f"asserts the figure {figures[0]}, which no source gives")
            continue
        kept.append({"text": sentence, "source": str(hits[cited - 1].url), "index": cited})

    return kept, rejected


def name_the_employer(
    sentences: Sequence[dict[str, Any]], company: str
) -> list[dict[str, Any]]:
    """Make sure something in these sentences says who they are about.

    A note that only ever says "the team" is not attributable to anyone, and the
    planner is handed it with no other context.

    RUN THIS AFTER VERIFICATION, NOT BEFORE. The first version rewrote the
    sentence during screening, so the verifier was shown text the drafter never
    wrote and judged the prefix along with the claim — and when it dropped that
    one sentence, the survivors were back to "The team publishes…" with the
    employer named nowhere.
    """
    out = [dict(s) for s in sentences]
    pattern = _name_pattern(company)
    if not out or pattern is None or any(pattern.search(s["text"]) for s in out):
        return out
    first = out[0]["text"]
    lowered = first[:1].lower() + first[1:] if first[:1].isupper() else first
    out[0]["text"] = f"At {company}, {lowered}"
    return out


def verify_sentences(
    sentences: Sequence[dict[str, Any]],
    hits: Sequence[SearchHit],
    router: Any,
    *,
    drafted_by: str = "",
) -> tuple[list[dict[str, Any]], list[str], bool, bool]:
    """The independent audit. Returns `(kept, rejected, verified, independent)`.

    One call per company rather than one per sentence — unlike the letter, where
    thirteen sentences on a slow local model justify splitting. Here there are two
    or three and they share one corpus, so one call is cheaper and lets the
    verifier see them together.

    FAILS CLOSED, inverting `factcheck`, which passes a sentence it could not
    grade. There, a letter already exists and a human is about to read it; here,
    refusing costs exactly a blank paragraph, which is what the letter has today.
    """
    from .llm import AllProvidersFailed

    if not sentences:
        return [], [], False, False

    exclude = {drafted_by} if drafted_by else set()
    if exclude and not router.providers_for("research_verify", exclude):
        exclude = set()          # nothing but the drafter is reachable at all
    independent = bool(exclude)

    system = (PROMPTS / "08_research_verify.md").read_text(encoding="utf-8")
    user = "\n\n".join([
        "SOURCES:",
        numbered_sources(hits),
        "SENTENCES:",
        "\n".join(f"[{n}] {s['text']}" for n, s in enumerate(sentences, start=1)),
    ])
    try:
        raw = router.complete_json(
            "research_verify", system, user, schema=VERIFY_SCHEMA,
            temperature=0.0, max_tokens=400, exclude=exclude or None,
        )
    except AllProvidersFailed as exc:
        # BEING CONFIGURED IS NOT THE SAME AS BEING REACHABLE. Ollama is routed
        # first here and needs no key, so it is always "usable" and usually not
        # running. `factcheck` hit this first and does the same thing: fall back
        # to the drafter and say so, rather than refusing every company on a
        # machine where the second opinion happens to be down.
        if not exclude:
            log.info("research verification unavailable: %s", exc)
            return [], [], False, False
        log.info("no independent verifier reachable (%s); the drafter checks itself", exc)
        independent = False
        try:
            raw = router.complete_json(
                "research_verify", system, user, schema=VERIFY_SCHEMA,
                temperature=0.0, max_tokens=400,
            )
        except AllProvidersFailed as inner:
            log.info("research verification unavailable: %s", inner)
            return [], [], False, False

    def indices(key: str) -> set[int]:
        out = set()
        for value in raw.get(key) or []:
            try:
                out.add(int(value))
            except (TypeError, ValueError):
                continue
        return out

    supported, unsupported = indices("supported"), indices("unsupported")
    labels = {
        name: indices(name) for name in ("engineering", "business", "marketing")
    }
    kept, rejected = [], []
    for n, sentence in enumerate(sentences, start=1):
        # SILENCE MEANS `business` — the middle. Not best, not dropped. The
        # fail-closed decision has already been taken on the support axis above, so
        # a model that forgets to label a sentence should not thereby delete it.
        label = next((name for name in ("engineering", "marketing")
                      if n in labels[name]), "business")
        sentence = {**sentence, "label": label}
        if n in supported and n not in unsupported and label == "marketing":
            rejected.append(
                f"{sentence['text']} — this is how they describe themselves, not what "
                f"they build; it would be true of a competitor with two words changed"
            )
        elif n in supported and n not in unsupported:
            kept.append(sentence)
        elif n in unsupported:
            rejected.append(f"{sentence['text']} — the verifier could not find it in the sources")
        else:
            # Silence is not a pass. See VERIFY_SCHEMA.
            rejected.append(f"{sentence['text']} — the verifier did not vouch for it")
    return kept, rejected, True, independent


# ── the pipeline ──


def research_company(
    company: str,
    *,
    router: Any | None = None,
    searcher: Any | None = None,
    fetcher: Any | None = None,
    cache: ResearchCache | None = None,
    settings: Settings | None = None,
    profile: Any | None = None,
    force: bool = False,
    max_queries: int | None = None,
) -> ResearchReport:
    """Everything known about one employer, in two or three defensible sentences.

    The order is deliberate and every step before the first model call is free:
    search, drop what does not name them, refuse if too little survives, draft,
    screen deterministically, verify independently. Anything that goes wrong at
    any point produces a blank rather than a guess.

    Every seam — `router`, `searcher`, `fetcher`, `cache` — is injectable and
    defaults to None, so the test suite exercises this function without opening a
    socket.
    """
    from .llm import AllProvidersFailed

    settings = settings or get_settings()
    cache = cache if cache is not None else ResearchCache(settings=settings)
    report = ResearchReport()

    if not normalize_company(company):
        report.outcome = "skipped"
        return report

    if not force and (stored := cache.get(company)) is not None:
        report.record, report.outcome = stored, "cached"
        return report

    kept, dropped, queries, notes = gather(
        company, searcher=searcher, settings=settings, max_queries=max_queries
    )
    report.queries_run = len(queries)
    report.hits_seen = len(kept) + len(dropped)
    report.dropped = len(dropped)
    report.notes.extend(notes)

    if not queries:
        # A failure to LOOK, not a finding that there is nothing. No record is
        # written, so the next run tries again rather than serving this back.
        report.outcome = "no-provider"
        report.notes.append("no search provider answered — nothing was cached")
        return report

    # THE DEEPEST SINGLE SOURCE, not the pile and not even the pile of their own
    # pages. Both earlier versions summed, and a well-covered employer cleared any
    # sum easily on twenty 250-character SERP fragments — so no page was ever
    # read, and every drafted sentence then died on the single-source grounding
    # check because no one fragment said a whole thing.
    #
    # This measure is the same rule the grounding check applies: a sentence has to
    # be carried by ONE source, so what has to be deep is one source.
    if deepest_source(_own_hits(kept, company)) < settings.apply_research_min_snippet_chars:
        pages, fetched = _read_own_pages(
            kept, company, fetcher=fetcher, settings=settings, notes=report.notes
        )
        kept = list(kept) + pages
        report.pages_fetched = fetched

    report.kept = len(kept)
    shown = rank_sources(kept, company)

    if not enough_sources(kept, settings):
        report.record = thin_record(company, kept, queries)
        report.outcome = "thin"
        report.notes.append(
            f"only {len(kept)} source(s) name this employer — nothing was sent to the model"
        )
        cache.put(report.record)
        return report

    router = router if router is not None else _default_router(settings)
    if profile is None:
        profile = _load_profile_quietly()
    banned = list(getattr(getattr(profile, "style", None), "banned_phrases", []) or [])

    system = (PROMPTS / "07_company_research.md").read_text(encoding="utf-8")
    user = f"COMPANY: {company}\n\nSOURCES:\n{numbered_sources(shown)}"
    before = router.tokens
    try:
        raw = router.complete_json(
            "company_research", system, user, schema=RESEARCH_SCHEMA,
            temperature=0.0, max_tokens=400,
        )
    except AllProvidersFailed as exc:
        report.outcome = "model-failed"
        report.notes.append(f"no model answered: {exc}")
        return report
    # A CACHED ANSWER STILL HAS AN AUTHOR. The first version skipped cached
    # calls here, so a re-run whose prompt was unchanged lost the drafter's name,
    # stopped excluding it, and reported a genuinely independent check as
    # self-verified. `_cached` stores the provider with the value for exactly
    # this reason.
    drafted_by = next((c.provider for c in reversed(router.calls)
                       if c.task == "company_research"), "")
    model = next((c.model for c in reversed(router.calls)
                  if c.task == "company_research"), "")

    sentences, rejected = screen_sentences(
        list(raw.get("notes") or []), shown, company, settings=settings, banned=banned
    )
    if sentences:
        sentences, verify_rejected, verified, independent = verify_sentences(
            sentences, shown, router, drafted_by=drafted_by
        )
        rejected += verify_rejected
        sentences = name_the_employer(sentences, company)
    else:
        verified, independent = False, False

    if verified and independent is False and settings.apply_research_require_independent:
        rejected.append("only the drafting model vouched for these, and that is refused here")
        sentences, verified = [], False

    after = router.tokens
    report.prompt_tokens = after[0] - before[0]
    report.completion_tokens = after[1] - before[1]

    # BEST FIRST, then capped. Joining everything is what let a funding round sit
    # beside an engineering fact in the same paragraph, and the ordering matters
    # twice over: the engineering sentence is both what `name_the_employer` prefixes
    # and what the planner latches onto when it writes `company_angle`.
    sentences = sorted(
        sentences,
        key=lambda s: (LABEL_RANK.get(str(s.get("label") or "business"), 1),
                       sentences.index(s)),
    )
    for extra in sentences[settings.apply_research_max_notes:]:
        rejected.append(
            f"{extra['text']} — true and sourced, but only the best "
            f"{settings.apply_research_max_notes} reach the letter"
        )
    sentences = sentences[:settings.apply_research_max_notes]

    report.record = CompanyResearch(
        company=company,
        notes=" ".join(s["text"] for s in sentences),
        sentences=[
            {"text": s["text"], "source": s["source"],
             **({"label": s["label"]} if s.get("label") else {})}
            for s in sentences
        ],
        sources=[str(h.url) for h in shown],
        queries=list(queries),
        model_confidence=str(raw.get("confidence") or ""),
        model=model,
        verified=verified and bool(sentences),
        independent=independent,
        rejected=rejected,
        outcome=("ok" if sentences and verified else
                 "unverified" if sentences else "ungrounded"),
    )
    report.outcome = report.record.outcome
    cache.put(report.record)
    return report


def research_companies(
    companies: Sequence[str],
    *,
    on_company: Any | None = None,
    **seams: Any,
) -> list[ResearchReport]:
    """One employer after another, in a plain loop.

    SERIAL BY CONSTRUCTION, and that is the point: two threads researching one
    employer would duplicate every search and race on the cache file for nothing,
    since the answer does not depend on the job being applied for.
    """
    out = []
    for name in companies:
        try:
            report = research_company(name, **seams)
        except Exception as exc:  # noqa: BLE001 - one employer is not the batch
            log.warning("research failed for %r: %s", name, exc)
            report = ResearchReport(outcome="error", notes=[str(exc)[:200]])
        out.append(report)
        if on_company is not None:
            on_company(name, report)
    return out


def prepass(
    job_ids: Sequence[int],
    *,
    profile: Any | None = None,
    on_company: Any | None = None,
    **seams: Any,
) -> dict[str, ResearchReport]:
    """Research every distinct employer in a batch, once, before the first letter.

    Skips an employer Simon has written about himself WITHOUT SEARCHING — the
    hand-written entry wins anyway, so a search for it would be pure waste — and
    skips one with a fresh record, including a fresh negative one.
    """
    from .jobs import companies_for
    from .planner import company_notes_with_source

    if profile is None:
        profile = _load_profile_quietly()
    cache = seams.get("cache") or ResearchCache(settings=seams.get("settings") or get_settings())

    wanted: list[str] = []
    for name in companies_for(job_ids).values():
        if not name or name in wanted:
            continue
        if profile is not None and company_notes_with_source(profile, name)[1] == "profile":
            continue
        if not seams.get("force") and cache.get(name) is not None:
            continue
        wanted.append(name)

    seams.setdefault("cache", cache)
    seams.setdefault("profile", profile)
    reports = research_companies(wanted, on_company=on_company, **seams)
    return dict(zip(wanted, reports, strict=True))


def _default_router(settings: Settings) -> Any:
    from .llm import TaskRouter

    return TaskRouter(settings=settings, cache_dir=pathlib.Path(settings.cache_dir) / "apply")


def _load_profile_quietly() -> Any | None:
    """The profile, for its banned phrases. A broken profile is not this
    feature's problem to report — the letter path will say so far more loudly."""
    from .loader import load_profile

    try:
        return load_profile()
    except Exception as exc:  # noqa: BLE001
        log.info("research could not load the profile: %s", exc)
        return None


def _read_own_pages(
    hits: Sequence[SearchHit],
    company: str,
    *,
    fetcher: Any | None,
    settings: Settings,
    notes: list[str],
) -> tuple[list[SearchHit], int]:
    """At most two pages, from the company's OWN domain, robots-gated.

    Reading a page the search index already pointed at is in scope; walking a
    domain is not — the same line `harvest` draws. And if no domain could be tied
    to this employer, nothing is read at all: fetching from a domain we could not
    attribute is the fabrication risk wearing a different hat.
    """
    cap = settings.apply_research_max_pages
    if cap <= 0:
        return [], 0
    domain = own_domain(hits, company)
    if not domain:
        notes.append("no domain could be tied to this employer, so no page was read")
        return [], 0

    get = fetcher if fetcher is not None else _polite_get(settings)
    out: list[SearchHit] = []
    fetched = 0
    for hit in hits:
        if fetched >= cap:
            break
        if host_of(hit) != domain:
            continue
        try:
            body = get(str(hit.url))
        except Exception as exc:  # noqa: BLE001 - a refusal is the site's answer
            notes.append(f"{hit.url} was not read: {str(exc)[:80]}")
            continue
        fetched += 1
        text = _page_extract(body, company)
        if text:
            out.append(SearchHit(rank=hit.rank, url=str(hit.url),
                                 title=str(hit.title), snippet=text))
    return out, fetched


def _page_extract(body: str, company: str) -> str:
    """The opening of the page, plus any paragraph that names the employer."""
    from ..sources.jobs.base import strip_html

    text = strip_html(body, limit=8000)
    if not text:
        return ""
    pattern = _name_pattern(company)
    head = text[:1200]
    extra = ""
    if pattern is not None:
        for para in text[1200:].split("\n"):
            if pattern.search(para):
                extra = para.strip()
                break
    return f"{head}\n{extra}".strip()[:2000]


def _polite_get(settings: Settings) -> Any:
    """A robots-gated, rate-limited, cached GET — the jobs layer's client.

    Reused rather than rewritten, exactly as `enrich.harvest` does: `PoliteClient`
    already fetches and parses `robots.txt`, honours `Crawl-delay`, and treats a
    403 as the site's answer rather than something to work around. Only the
    User-Agent differs, because this is neither job alerting nor contact research
    and a site deciding whether to serve it deserves to be told what it is.
    """
    from ..sources.jobs.base import PoliteClient, RobotsGate

    robots = RobotsGate(USER_AGENT)
    client = PoliteClient(settings, robots=robots)
    client.user_agent = USER_AGENT
    return lambda url: client.get(url, accept="text/html")
