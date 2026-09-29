"""EURES, the European Commission's job-mobility portal (EU/EEA + CH).

Keyless, official, and the widest source for the EU countries Adzuna does not
cover (PT, IE, SE, FI, DK). Checked live on 28 Sep 2026:

    POST https://europa.eu/eures/api/jv-searchengine/public/jv-search/search
    {"keywords": [{"keyword": "data scientist", "specificSearchCode": "TITLE"}],
     "locationCodes": ["de", "pt", …], "sortSearch": "BEST_MATCH",
     "publicationPeriod": "LAST_WEEK", "resultsPerPage": 50, "page": 1, …}

    {"numberRecords": 961, "jvs": [{id, title, description (HTML), creationDate
     (epoch ms), locationMap {"DE": ["DE252"], "PT": [null]}, employer {name},
     positionScheduleCodes, positionOfferingCode, availableLanguages, …}]}

robots.txt allows the API and asks for a 10 s Crawl-delay, which the client
honours. So ONE search covers every EURES country on the profile at once
(`locationCodes` takes a list), rather than one per country.

**The search is loose.** Words are OR-ed and stemmed: "machine learning" matches
9,247 German titles this week, from "Maschinenbediener" (machine operator)
onwards. Sorted by BEST_MATCH, though, the titles containing every word come
first, and on the live data the first 22-30 of 50 did. So a visit reads pages
in relevance order, keeps only titles that contain every word of the query,
and stops at the first page that has none. The role filter still judges what
is kept. It just no longer has to wade through cooks.

`publicationPeriod` has four values (LAST_DAY, LAST_THREE_DAYS, LAST_WEEK,
LAST_MONTH). A visit picks the smallest one that covers the time since the
previous complete visit, so a skipped day loses nothing.

Job pages: https://europa.eu/eures/portal/jv-se/jv-details/{id}?lang=en
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ...config import Settings, get_settings
from ...jobsearch.locations import country_name
from .base import JobPosting, PoliteClient, SourceError, iso_date, strip_html
from .search import SearchLedger, title_matches

log = logging.getLogger(__name__)

ENDPOINT = "https://europa.eu/eures/api/jv-searchengine/public/jv-search/search"
DETAIL = "https://europa.eu/eures/portal/jv-se/jv-details/{id}?lang=en"

#: EURES members: the EU, the EEA and Switzerland. The UK left in 2021.
MEMBERS = frozenset({
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE",
    "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
    "IS", "LI", "NO", "CH",
})

#: (days covered, EURES name), smallest first.
PERIODS: tuple[tuple[int, str], ...] = (
    (1, "LAST_DAY"), (3, "LAST_THREE_DAYS"), (7, "LAST_WEEK"), (31, "LAST_MONTH"),
)

#: One search covers every country, so the cursor has one key per query.
ALL = "*"


def period_for(days: int) -> str | None:
    """The smallest EURES window covering `days`; None = no limit."""
    for limit, name in PERIODS:
        if days <= limit:
            return name
    return None


def body(query: str, countries: list[str], page: int, *, period: str | None,
         per_page: int = 50) -> dict[str, Any]:
    return {
        "resultsPerPage": per_page, "page": page, "sortSearch": "BEST_MATCH",
        "keywords": [{"keyword": query, "specificSearchCode": "TITLE"}],
        "publicationPeriod": period, "occupationUris": [], "skillUris": [],
        "requiredExperienceCodes": [], "positionScheduleCodes": [], "sectorCodes": [],
        "educationAndQualificationLevelCodes": [], "positionOfferingCodes": [],
        "locationCodes": [c.lower() for c in countries], "euresFlagCodes": [],
        "otherBenefitsCodes": [], "requiredLanguages": [], "minNumberPost": None,
        "sessionId": "pitwall",
    }


def parse(data: Any, query: str | None = None) -> list[JobPosting]:
    """One page of results, normalised. Pure — fixture-tested. Every row is
    returned; `title_matches` is the caller's decision."""
    out: list[JobPosting] = []
    for jv in (data or {}).get("jvs", []) or []:
        title, ident = jv.get("title"), jv.get("id")
        if not title or not ident:
            continue
        codes = [str(c).upper() for c in (jv.get("locationMap") or {}) if c]
        employer = jv.get("employer") or {}
        schedule = [str(s) for s in jv.get("positionScheduleCodes") or [] if s]
        offering = jv.get("positionOfferingCode")
        out.append(JobPosting(
            title=str(title).strip(),
            url=DETAIL.format(id=ident),
            external_id=str(ident),
            description=strip_html(jv.get("description")),
            # Country names, not codes: "DE" and "IN" are also US states.
            location=", ".join(country_name(c) for c in codes) or None,
            employment_type=", ".join([*schedule, *([offering] if offering else [])]) or None,
            posted_at=iso_date(jv.get("creationDate")),
            company_name=(employer.get("name") or "").strip() or None,
            country_hint=codes[0] if len(codes) == 1 else None,
            found_query=query,
            raw=jv,
        ))
    return out


class EuresSource(SearchLedger):
    """Title searches across every EURES country on the profile at once."""

    kind = "job_board"

    def __init__(self, *, queries: list[str], countries: list[str],
                 cursors: dict[tuple[str, str], dict[str, Any]] | None = None,
                 max_pages_per_search: int = 4, per_page: int = 50,
                 backfill_days: int = 31, now: datetime | None = None,
                 settings: Settings | None = None, client: PoliteClient | None = None) -> None:
        self.settings = settings or get_settings()
        self.name = "eures"
        self.url = "https://europa.eu/eures"
        self.members = [c for c in dict.fromkeys(c.upper() for c in countries) if c in MEMBERS]
        self._init_ledger(queries=queries, countries=[ALL] if self.members else [],
                          cursors=cursors, backfill_days=backfill_days, now=now)
        self.max_pages = max(1, int(max_pages_per_search))
        self.per_page = max(10, min(50, int(per_page)))
        self.client = client or PoliteClient(self.settings)

    def fetch(self, *, no_cache: bool = False) -> list[JobPosting]:
        out: list[JobPosting] = []
        failures = 0
        for query, key in self.plan():
            period = period_for(self.days_for(query, key))
            visit = self.open_visit(query, key)
            for page in range(1, self.max_pages + 1):
                try:
                    data = self.client.post_json(
                        ENDPOINT, body(query, self.members, page, period=period,
                                       per_page=self.per_page),
                        no_cache=no_cache,
                    )
                except SourceError as exc:
                    visit.requests += self.counted(self.client)
                    failures += 1
                    log.warning("eures: %s — %s", query, exc)
                    if failures >= 3 and not out:
                        raise
                    break
                visit.requests += self.counted(self.client)
                rows = parse(data, query)
                kept = [p for p in rows if title_matches(p.title, query)]
                out.extend(kept)
                visit.found += len(kept)
                total = (data or {}).get("numberRecords")
                visit.total = int(total) if isinstance(total, int) else visit.total
                # Relevance order: a page with no full match means the rest is noise.
                if not kept or len(rows) < self.per_page:
                    visit.complete = True
                    break
            else:
                visit.complete = True
        log.info("eures: %d posting(s) from %d search(es), %d request(s)",
                 len(out), len(self.visits), self.requests_made)
        return out
