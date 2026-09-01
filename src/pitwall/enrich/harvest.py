"""Harvesting known addresses at a domain — §2.2 of the v3 brief.

*"This is the highest-value step and the one most people miss. You do not need
the target person's address to help the target person — you need any two
addresses at their company, because two addresses reveal the format."*

That is why this step is company-scoped and its output is cached forever. One
harvest of `alpine.com` resolves the format for every Alpine candidate you will
ever look at, so the cost per person falls towards zero as the database grows.
It is also why the corpus is stored in `domain_address_samples` rather than
recomputed: a page that was indexed today may not be tomorrow.

**Where the line is** (§0.3): reading a page the search index already pointed at
is in scope; walking a domain harvesting `mailto:` links is not. So this module
issues search queries, reads what the SERP itself returned, and follows a link
only when the snippet was truncated mid-address — capped, robots-gated, and
never recursively. There is no link extraction anywhere in it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from ..search.base import SearchHit
from .base import EnrichmentContext, StepOutcome
from .checks import find_addresses, is_role_address, local_part

log = logging.getLogger(__name__)

#: §2.2's three queries, verbatim. Each finds a different kind of page:
#: the first the company's own site, the second everywhere else it is quoted
#: (press releases, conference programmes, papers), the third contact pages.
HARVEST_QUERIES: tuple[str, ...] = (
    'site:{domain} "@{domain}"',
    '"@{domain}" -site:linkedin.com',
    '"{company}" "@{domain}" contact',
)

#: Says honestly what this is, on the one fetch this module can make. Not the
#: jobs User-Agent — this is not job alerting, and a site deciding whether to
#: serve it deserves to be told what it is actually serving.
USER_AGENT = "PitWall/3.0 (personal contact research; +robots-compliant)"

#: How many truncated snippets may be confirmed by fetching the page. Small,
#: because this is the only part of the free engine that fetches anything, and
#: because it is a confirmation step rather than a discovery one.
MAX_PAGE_FETCHES = 3

#: A snippet that was cut off. Search engines all use one of these.
_TRUNCATED = re.compile(r"(…|\.\.\.)\s*$")

#: An address the snippet started and did not finish: `jane.doe@alp…`.
_PARTIAL_ADDRESS = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]*(?:…|\.\.\.)")

#: One capitalised word. Names are found by walking runs of these rather than
#: by matching a whole "Firstname Lastname" pattern, because a single regex
#: swallows the noise word in front of the name — "Contact Jane Doe" matches as
#: one span, gets rejected for containing "Contact", and `finditer` then
#: resumes past the name it was looking for.
_CAP_WORD = re.compile(r"[A-Z][\w'’-]{1,20}\Z", re.UNICODE)

#: Words that look like a name to the regex but are not one.
_NOT_A_NAME = frozenset(
    {
        "contact", "email", "press", "office", "team", "media", "about", "home",
        "careers", "jobs", "our", "the", "and", "for", "with", "meet", "staff",
        "read", "more", "get", "in", "touch", "us", "we", "you", "please",
        "click", "here", "privacy", "policy", "cookie", "terms", "all", "rights",
        "reserved", "copyright", "linkedin", "twitter", "facebook", "instagram",
        "formula", "racing", "motorsport", "engineering", "technology", "group",
        "limited", "ltd", "gmbh", "head", "chief", "senior", "lead", "principal",
        "director", "manager", "engineer", "technician", "designer", "analyst",
        # Discipline and role words. These sit immediately before a name in the
        # snippets this actually reads — "Head of Aero Marc Dubois — …" — and
        # without them the run starts one word early, producing a "first name"
        # that no format can explain and a sample that is silently wasted.
        "aero", "aerodynamics", "aerodynamicist", "vehicle", "dynamics",
        "performance", "simulation", "strategy", "race", "operations",
        "technical", "data", "software", "systems", "design", "test", "testing",
        "composite", "composites", "powertrain", "trackside", "garage", "pit",
        "wall", "deputy", "assistant", "graduate", "intern", "apprentice",
        "sponsor", "partner", "partners", "official", "website", "latest",
        "view", "profile", "tel", "fax", "address", "department", "division",
    }
)


@dataclass
class Harvested:
    """One address found on a page, with whatever the page said about it."""

    email: str
    person_name: str | None = None
    source_url: str | None = None

    def __post_init__(self) -> None:
        self.email = self.email.strip().lower()


@dataclass
class HarvestReport:
    found: list[Harvested] = field(default_factory=list)
    queries_run: int = 0
    pages_fetched: int = 0
    role_addresses_skipped: int = 0
    notes: list[str] = field(default_factory=list)


# ─────────────────────────── name attribution ────────────────────────────


def name_near(text: str, email: str, window: int = 90) -> str | None:
    """Whose address does this page say it is?

    Reads a window of prose around the address for a capitalised name. This is
    genuinely independent evidence and not circular reasoning: the *page* is
    asserting the association, and the format inference then compares that
    name to the address. Deriving the name from the address itself would be
    circular, and nothing here does that.
    """
    if not text or not email:
        return None
    haystack = str(text)
    index = haystack.lower().find(email.lower())
    if index < 0:
        return None

    before = haystack[max(0, index - window) : index]
    after = haystack[index + len(email) : index + len(email) + window]

    # A contact page lists several people in a row: "Marc Dubois
    # marc.dubois@… and Anna Klein anna.klein@…". Stop the backward window at
    # the previous address, or every address on the page is attributed to the
    # first person named on it.
    cut = max(before.rfind("@"), before.rfind("\n"))
    if cut >= 0:
        before = before[cut + 1 :]

    # Nearest name wins: the last one before the address, then the first one
    # after it. Scanning the whole window left to right picks whoever is
    # mentioned earliest on the page, which is the same bug in slower motion.
    return _last_person_name(before) or _first_person_name(after)


def _name_runs(text: str) -> list[str]:
    """Every run of two or three capitalised, non-noise words, in order.

    All-capitals tokens are rejected: `PREMA`, `TGR` and `AF` are organisations
    shouting, not people, and a wrong name is worse than no name here because
    it enters the pattern corpus as a false sample and misleads the inference
    for everyone else at that company. A team page that sets names in caps
    therefore yields nothing, which is the right way to be wrong.
    """
    out: list[str] = []
    run: list[str] = []

    def close() -> None:
        if len(run) >= 2:
            out.append(" ".join(run[:3]))

    for token in re.findall(r"\S+", text or ""):
        clean = token.strip(".,;:!?()[]{}<>\"'’«»")
        usable = (
            len(clean) >= 2
            and not clean.isupper()
            and _CAP_WORD.match(clean) is not None
            and clean.lower() not in _NOT_A_NAME
        )
        if usable:
            run.append(clean)
            continue
        close()
        run = []
    close()
    return out


def _first_person_name(text: str) -> str | None:
    runs = _name_runs(text)
    return runs[0] if runs else None


def _last_person_name(text: str) -> str | None:
    runs = _name_runs(text)
    return runs[-1] if runs else None


def name_from_title(title: str) -> str | None:
    """LinkedIn-style and team-page titles lead with the person's name."""
    head = re.split(r"[|•·–—]|\s-\s|:", str(title or ""))[0].strip()
    return _first_person_name(head) if head else None


def attribute(hit: SearchHit, email: str) -> str | None:
    """Best available name for this address on this result."""
    return (
        name_near(hit.snippet, email)
        or name_near(hit.title, email)
        or name_from_title(hit.title)
    )


# ────────────────────────────── the harvest ──────────────────────────────


def harvest_domain(
    ctx: EnrichmentContext,
    domain: str,
    company_name: str,
    *,
    max_queries: int | None = None,
    fetcher: Any | None = None,
) -> HarvestReport:
    """Run §2.2's queries and pull every address at `domain` out of the SERPs."""
    report = HarvestReport()
    if ctx.searcher is None:
        report.notes.append("no search provider available, so nothing could be harvested")
        return report
    if not ctx.searcher.supports_operators():
        # `site:` and quoted phrases are the whole technique. A provider that
        # drops them does not fail, it answers something else — the failure
        # that made v1 return thousands of rows and nobody.
        report.notes.append(
            "the available search provider does not support operators, so the "
            "harvest queries would return unrelated results. Start SearXNG "
            "(`make searxng`)."
        )
        return report

    limit = ctx.settings.enrich_harvest_queries if max_queries is None else max_queries
    seen: dict[str, Harvested] = {}
    truncated: list[SearchHit] = []

    for template in HARVEST_QUERIES[:limit]:
        query = template.format(domain=domain, company=company_name)
        try:
            hits = ctx.searcher.search(query, max_results=ctx.settings.max_results_per_query)
        except Exception as exc:  # noqa: BLE001 - one dead query is not a dead harvest
            log.info("harvest query failed (%s): %s", query, exc)
            report.notes.append(f"a harvest query failed: {str(exc)[:100]}")
            continue
        report.queries_run += 1

        for hit in hits:
            text = f"{hit.title}\n{hit.snippet}"
            for email in find_addresses(text, domain=domain):
                if is_role_address(email):
                    # Flagged, not stored: §8 requires role addresses never be
                    # returned as a person's, and one in the corpus would
                    # corrupt the format inference as well.
                    report.role_addresses_skipped += 1
                    continue
                existing = seen.get(email)
                person = attribute(hit, email)
                if existing is None:
                    seen[email] = Harvested(email, person, hit.url)
                elif existing.person_name is None and person:
                    existing.person_name = person
                    existing.source_url = hit.url
            if _looks_truncated(text, domain):
                truncated.append(hit)

    # §2.2: "Where a snippet is truncated, fetching that one indexed page to
    # confirm is in scope; crawling the domain is not." One fetch per result,
    # capped, and only while the corpus is still too thin to settle a format.
    needed = max(0, ctx.settings.enrich_min_samples_for_probable - _named_count(seen))
    if needed and truncated:
        report.pages_fetched = _confirm_truncated(
            ctx, truncated, domain, seen, report, fetcher=fetcher
        )

    report.found = list(seen.values())[: ctx.settings.enrich_max_samples]
    return report


def _named_count(seen: dict[str, Harvested]) -> int:
    return sum(1 for h in seen.values() if h.person_name)


def _looks_truncated(text: str, domain: str) -> bool:
    """Did the snippet cut an address off mid-string?"""
    if _PARTIAL_ADDRESS.search(text):
        return True
    return bool(_TRUNCATED.search(text) and "@" in text and domain.split(".")[0] in text.lower())


def _confirm_truncated(
    ctx: EnrichmentContext,
    hits: list[SearchHit],
    domain: str,
    seen: dict[str, Harvested],
    report: HarvestReport,
    *,
    fetcher: Any | None = None,
) -> int:
    """Fetch at most `MAX_PAGE_FETCHES` already-indexed pages, robots-gated.

    Deliberately not a crawl: each URL came out of a SERP this step just ran,
    nothing on the fetched page is followed, and the fetch stops as soon as the
    corpus is big enough. `fetcher` is injected so the suite never makes a
    request.
    """
    get = fetcher or _polite_get(ctx)
    if get is None:
        return 0

    fetched = 0
    for hit in hits[:MAX_PAGE_FETCHES]:
        try:
            body = get(hit.url)
        except Exception as exc:  # noqa: BLE001 - robots refusal, 403, timeout
            log.info("skipping %s: %s", hit.url, exc)
            report.notes.append(f"could not read {hit.url}: {str(exc)[:80]}")
            continue
        fetched += 1
        for email in find_addresses(body, domain=domain):
            if is_role_address(email):
                report.role_addresses_skipped += 1
                continue
            person = name_near(body, email)
            existing = seen.get(email)
            if existing is None:
                seen[email] = Harvested(email, person, hit.url)
            elif existing.person_name is None and person:
                existing.person_name = person
        if _named_count(seen) >= ctx.settings.enrich_min_samples_for_probable:
            break
    return fetched


def _polite_get(ctx: EnrichmentContext):
    """A robots-gated, rate-limited, cached GET — the jobs layer's client.

    Reused rather than rewritten: `PoliteClient` already fetches and *parses*
    `robots.txt` with `urllib.robotparser`, honours `Crawl-delay`, and treats a
    403 as the site's answer rather than something to work around. Only the
    User-Agent differs, because this is not job alerting.
    """
    try:
        from ..sources.jobs.base import PoliteClient, RobotsGate
    except ImportError:  # pragma: no cover - the jobs layer is always present
        return None

    robots = RobotsGate(USER_AGENT)
    client = PoliteClient(ctx.settings, robots=robots)
    client.user_agent = USER_AGENT
    return lambda url: client.get(url, accept="text/html")


# ─────────────────────────────── the step ────────────────────────────────


class HarvestStep:
    """Fill `domain_address_samples` for this candidate's company."""

    name = "harvest"
    spends_credits = False

    def __init__(self, *, fetcher: Any | None = None, force: bool = False) -> None:
        self.fetcher = fetcher
        #: Re-harvest even if the domain was harvested recently.
        self.force = force

    def run(self, ctx: EnrichmentContext) -> StepOutcome:
        domains = ctx.domain_list()
        if not domains:
            return StepOutcome(
                step=self.name, outcome="skipped", detail="no domain resolved yet"
            )

        parts: list[str] = []
        total_added = 0
        any_found = False
        for domain in domains:
            added, detail, found = self._harvest_one(ctx, domain)
            total_added += added
            any_found = any_found or found
            parts.append(detail)

        return StepOutcome(
            step=self.name,
            outcome="ok" if any_found else "empty",
            detail=" · ".join(parts),
            samples_added=total_added,
        )

    def _harvest_one(self, ctx: EnrichmentContext, domain: str) -> tuple[int, str, bool]:
        """One domain's corpus. Returns (new samples, a detail line, found any)."""
        domain_id = ctx.domain_id_for(domain)

        if domain_id is not None and not self.force and _harvested_recently(ctx, domain_id):
            stored = ctx.repos.domains.samples(domain_id)
            ctx.record_samples(domain, stored)
            return 0, f"{domain}: {len(stored)} stored, harvested recently", bool(stored)

        report = harvest_domain(ctx, domain, ctx.company_name, fetcher=self.fetcher)
        for note in report.notes:
            if note not in ctx.notes:      # the same "no operators" note per domain
                ctx.note(note)

        added = 0
        if domain_id is not None and report.found:
            with ctx.repos.writing():
                for item in report.found:
                    if ctx.repos.domains.add_sample(
                        domain_id, item.email,
                        person_name=item.person_name, source_url=item.source_url,
                        source="harvest",
                    ):
                        added += 1
                ctx.repos.domains.mark_harvested(domain_id)
            ctx.record_samples(domain, ctx.repos.domains.samples(domain_id))
        elif report.found:
            # No company row to hang the corpus off — still usable for this one
            # person, just not cached for the next.
            ctx.record_samples(domain, [
                {"email": h.email, "person_name": h.person_name, "source_url": h.source_url}
                for h in report.found
            ])
        else:
            ctx.record_samples(domain, ctx.samples_for(domain))

        named = sum(1 for h in report.found if h.person_name)
        detail = (
            f"{domain}: {len(report.found)} address(es) ({named} named), "
            f"{added} new, {report.queries_run} search(es)"
        )
        if report.pages_fetched:
            detail += f", {report.pages_fetched} page(s) read"
        if report.role_addresses_skipped:
            detail += f", {report.role_addresses_skipped} shared mailbox(es) skipped"
        return added, detail, bool(report.found)


def _harvested_recently(ctx: EnrichmentContext, domain_id: int) -> bool:
    """Has this domain been harvested inside the TTL, with enough to work with?

    A harvest that found nothing is retried; one that found a workable corpus
    is not, because the answer does not change often and each retry is three
    searches. `enrich_harvest_ttl_days = 0` disables the cache entirely.
    """
    ttl = ctx.settings.enrich_harvest_ttl_days
    if ttl <= 0:
        return False
    row = ctx.repos.domains.by_id(domain_id)
    if not row or not row.get("last_harvested_at"):
        return False
    fresh = ctx.repos.conn.execute(
        "SELECT ? > datetime('now', ?) AS fresh", (row["last_harvested_at"], f"-{int(ttl)} days")
    ).fetchone()
    if not fresh or not fresh["fresh"]:
        return False
    samples = ctx.repos.domains.samples(domain_id)
    named = sum(1 for s in samples if s.get("person_name"))
    return named >= ctx.settings.enrich_min_samples_for_probable


def known_people_at(ctx: EnrichmentContext, domain_id: int | None = None) -> list[dict[str, Any]]:
    """Everyone already in the database at this candidate's company.

    Free name evidence: an address harvested without a name can often be tied
    to one of these, which turns a shape-only sample into a real one. The
    candidate list was collected independently of any address, so matching
    against it is not circular.
    """
    company = ctx.company_name
    if not company:
        return []
    rows = ctx.repos.conn.execute(
        "SELECT id, full_name FROM candidates"
        " WHERE full_name IS NOT NULL AND full_name != ''"
        "   AND LOWER(COALESCE(current_company, '')) = LOWER(?)"
        " LIMIT 500",
        (company,),
    ).fetchall()
    return [dict(r) for r in rows]


def unattributed(samples: list[dict[str, Any]]) -> list[str]:
    """Local parts of samples nobody could be attached to — for the UI."""
    return [local_part(str(s.get("email") or "")) for s in samples if not s.get("person_name")]
