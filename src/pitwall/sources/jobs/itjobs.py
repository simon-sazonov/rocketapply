"""ITJobs.pt, Portugal's main IT job board, through its public API.

Keyed (`ITJOBS_API_KEY` in `.env`), free: enter an email at itjobs.pt/api and
the read-only key arrives by return. Adzuna has no Portuguese site, so for
Portugal this and EURES are the market-wide sources.

Built from the documentation (itjobs.pt/api/docs, read 28 Sep 2026); without a
key the API answers only `{"error": {"message": "API key required."}}`, which
the adapter reports as a failure rather than an empty result:

    GET https://api.itjobs.pt/job/search.json?api_key=…&q=data%20scientist&limit=100&page=1

    {"total": 686, "page": 1, "limit": 100, "results": [{id, title, body (HTML),
     company {name, url}, salaryMin, salaryMax, workModel (0 on-site, 1 remote,
     2 hybrid), types [{name}], contracts [{name}], locations [{id, name}],
     publishedAt, slug}]}

Location 29 is "International": the job is not in Portugal. The public page is
https://www.itjobs.pt/oferta/{id}/{slug}. robots.txt allows the API host with a
1 s Crawl-delay; the key and its terms govern it either way.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from ...config import Settings, get_settings
from .base import JobPosting, PoliteClient, SourceError, iso_date, strip_html
from .search import SearchLedger

log = logging.getLogger(__name__)

ENDPOINT = "https://api.itjobs.pt/job/search.json"
PAGE = "https://www.itjobs.pt/oferta/{id}/{slug}"
INTERNATIONAL = "29"
WORK_MODEL = {0: "onsite", 1: "remote", 2: "hybrid"}


def parse(data: Any, query: str | None = None) -> list[JobPosting]:
    if isinstance(data, dict) and data.get("error"):
        raise SourceError(f"itjobs: {(data['error'] or {}).get('message') or 'error'}")
    out: list[JobPosting] = []
    for job in (data or {}).get("results", []) or []:
        title, ident = job.get("title"), job.get("id")
        if not title or not ident:
            continue
        places = job.get("locations") or []
        abroad = any(str(p.get("id")) == INTERNATIONAL for p in places)
        names = [str(p.get("name")) for p in places
                 if p.get("name") and str(p.get("id")) != INTERNATIONAL]
        mode = WORK_MODEL.get(int(job.get("workModel") or 0), "onsite")
        location = ", ".join(names + ([] if abroad else ["Portugal"]))
        if mode == "hybrid":
            location = f"{location} (hybrid)"
        kinds = [str(t.get("name")) for t in (job.get("types") or []) + (job.get("contracts") or [])
                 if isinstance(t, dict) and t.get("name")]
        out.append(JobPosting(
            title=str(title).strip(),
            url=PAGE.format(id=ident, slug=job.get("slug") or ""),
            external_id=str(ident),
            description=strip_html(job.get("body")),
            location=location or None,
            employment_type=", ".join(kinds) or None,
            posted_at=iso_date(job.get("publishedAt")),
            company_name=((job.get("company") or {}).get("name") or "").strip() or None,
            remote=True if mode == "remote" else (False if mode == "onsite" else None),
            salary_min=float(job["salaryMin"]) if job.get("salaryMin") else None,
            salary_max=float(job["salaryMax"]) if job.get("salaryMax") else None,
            salary_currency="EUR" if job.get("salaryMin") or job.get("salaryMax") else None,
            country_hint=None if abroad else "PT",
            found_query=query,
            raw=job,
        ))
    return out


class ITJobsSource(SearchLedger):
    kind = "job_board"
    terms_govern = True
    provider = "itjobs"

    def __init__(self, api_key: str, *, queries: list[str],
                 cursors: dict[tuple[str, str], dict[str, Any]] | None = None,
                 max_pages_per_search: int = 3, per_page: int = 100,
                 now: datetime | None = None, settings: Settings | None = None,
                 client: PoliteClient | None = None) -> None:
        if not api_key:
            raise ValueError("ITJobs.pt needs ITJOBS_API_KEY in .env (free at itjobs.pt/api)")
        self.settings = settings or get_settings()
        self.name = "itjobs"
        self.url = "https://www.itjobs.pt"
        self.api_key = api_key
        self._init_ledger(queries=queries, countries=["PT"], cursors=cursors, now=now)
        self.max_pages = max(1, int(max_pages_per_search))
        self.per_page = max(10, min(100, int(per_page)))
        self.client = client or PoliteClient(self.settings)

    def fetch(self, *, no_cache: bool = False) -> list[JobPosting]:
        out: list[JobPosting] = []
        failures = 0
        for query, country in self.plan():
            visit = self.open_visit(query, country)
            for page in range(1, self.max_pages + 1):
                params = {"q": query, "limit": self.per_page, "page": page}
                url = ENDPOINT + "?" + urlencode({"api_key": self.api_key, **params})
                safe = ENDPOINT + "?" + urlencode(params)
                try:
                    batch = parse(self.client.get_json(url, no_cache=no_cache, cache_key=safe,
                                                       terms_govern=True), query)
                except SourceError as exc:
                    visit.requests += self.counted(self.client)
                    failures += 1
                    if failures >= 2 and not out:
                        raise SourceError(str(exc).replace(self.api_key, "***")) from None
                    break
                visit.requests += self.counted(self.client)
                out.extend(batch)
                visit.found += len(batch)
                if len(batch) < self.per_page:
                    visit.complete = True
                    break
            else:
                visit.complete = True
        log.info("itjobs: %d posting(s) from %d search(es)", len(out), len(self.visits))
        return out
