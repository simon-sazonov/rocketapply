"""Workable's public job search: every employer that hires through Workable.

Not the per-company board in `ats.py` (`apply.workable.com/{token}`), which
needs the company known in advance. This is the cross-company search behind
jobs.workable.com, so it finds employers nobody put on a list. Checked live on
28 Sep 2026:

    GET https://jobs.workable.com/api/v1/jobs?query=data%20scientist&location=Portugal

    {"totalSize": 6, "jobs": [{id, title, url, description (HTML),
     requirementsSection, employmentType, workplace (on_site | hybrid | remote),
     location {city, subregion, countryName}, created, company {title, website},
     language, department}]}

robots.txt allows `/api/`; it disallows only the HTML `/search` pages. Result
sets are small and complete (14 machine-learning jobs in Germany), so there is
no paging: one request per query and country.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from ...config import Settings, get_settings
from ...jobsearch.locations import country_name
from .base import JobPosting, PoliteClient, SourceError, iso_date, strip_html
from .search import SearchLedger

log = logging.getLogger(__name__)

ENDPOINT = "https://jobs.workable.com/api/v1/jobs"


def parse(data: Any, query: str | None = None, country: str | None = None) -> list[JobPosting]:
    out: list[JobPosting] = []
    for job in (data or {}).get("jobs", []) or []:
        title, url = job.get("title"), job.get("url")
        if not title or not url or job.get("state", "published") != "published":
            continue
        loc = job.get("location") or {}
        place = ", ".join(p for p in (loc.get("city"), loc.get("countryName")) if p)
        workplace = str(job.get("workplace") or "").lower()
        if workplace == "hybrid":
            place = f"{place} (hybrid)" if place else "hybrid"
        text = "\n\n".join(t for t in (strip_html(job.get("description")),
                                       strip_html(job.get("requirementsSection"))) if t)
        out.append(JobPosting(
            title=str(title).strip(),
            url=str(url),
            external_id=str(job.get("id") or "") or None,
            description=text or None,
            location=place or None,
            employment_type=job.get("employmentType") or None,
            posted_at=iso_date(job.get("created")),
            company_name=((job.get("company") or {}).get("title") or "").strip() or None,
            remote=True if workplace == "remote" else (False if workplace == "on_site" else None),
            country_hint=country,
            found_query=query,
            raw=job,
        ))
    return out


class WorkableSearchSource(SearchLedger):
    kind = "job_board"

    def __init__(self, *, queries: list[str], countries: list[str],
                 cursors: dict[tuple[str, str], dict[str, Any]] | None = None,
                 now: datetime | None = None, settings: Settings | None = None,
                 client: PoliteClient | None = None) -> None:
        self.settings = settings or get_settings()
        self.name = "workable-search"
        self.url = "https://jobs.workable.com"
        self._init_ledger(queries=queries, countries=[c.upper() for c in countries],
                          cursors=cursors, now=now)
        self.client = client or PoliteClient(self.settings)

    def request_url(self, query: str, country: str) -> str:
        return ENDPOINT + "?" + urlencode({"query": query, "location": country_name(country)})

    def fetch(self, *, no_cache: bool = False) -> list[JobPosting]:
        out: list[JobPosting] = []
        failures = 0
        for query, country in self.plan():
            visit = self.open_visit(query, country)
            try:
                data = self.client.get_json(self.request_url(query, country), no_cache=no_cache)
            except SourceError as exc:
                visit.requests += self.counted(self.client)
                failures += 1
                log.warning("workable-search: %s in %s — %s", query, country, exc)
                if failures >= 3 and not out:
                    raise
                continue
            visit.requests += self.counted(self.client)
            batch = parse(data, query, country)
            out.extend(batch)
            visit.found = len(batch)
            visit.total = (data or {}).get("totalSize")
            visit.complete = True
        log.info("workable-search: %d posting(s) from %d search(es)", len(out), len(self.visits))
        return out
