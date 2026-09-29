"""Himalayas: remote jobs, filtered by where the candidate can work from.

Keyless. Checked live on 28 Sep 2026:

    GET https://himalayas.app/jobs/api/search?q=machine%20learning&country=Portugal

    {"totalCount": 181, "nextCursor": "…", "jobs": [{title, companyName,
     applicationLink, guid, description (HTML), excerpt, pubDate (epoch s),
     locationRestrictions ["Portugal", …] ([] = anywhere), seniority
     ["Senior"], employmentType, minSalary, maxSalary, currency, categories}]}

**Search from where you can work, not where the job is.** A remote job is open
to someone only if they can work from where they are. So the `country` filter
is the candidate's home (or the countries they are authorised in), not the
fourteen countries they would move to. The API applies that filter itself.

**robots.txt** disallows `/jobs*&page=` and `/jobs/*?page=`: offset paging is
refused, and this adapter never sends `page`. The API's own notice (21 Aug
2026) asks clients to page with `cursor=` instead, which robots.txt does not
mention. A few cursor pages per search are read, no more.
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

ENDPOINT = "https://himalayas.app/jobs/api/search"


def parse(data: Any, query: str | None = None) -> list[JobPosting]:
    out: list[JobPosting] = []
    for job in (data or {}).get("jobs", []) or []:
        title = job.get("title")
        url = job.get("applicationLink") or job.get("guid")
        if not title or not url:
            continue
        where = [str(c) for c in job.get("locationRestrictions") or [] if c]
        low, high = job.get("minSalary"), job.get("maxSalary")
        out.append(JobPosting(
            title=str(title).strip(),
            url=str(url),
            external_id=str(job.get("guid") or url),
            description=strip_html(job.get("description")) or job.get("excerpt") or None,
            location="Remote — " + (", ".join(where) if where else "Worldwide"),
            employment_type=job.get("employmentType") or None,
            posted_at=iso_date(job.get("pubDate")),
            company_name=(job.get("companyName") or "").strip() or None,
            remote=True,
            salary_min=float(low) if isinstance(low, int | float) and low else None,
            salary_max=float(high) if isinstance(high, int | float) and high else None,
            salary_currency=job.get("currency") if low or high else None,
            found_query=query,
            raw={**job, "_levels": [str(s).lower() for s in job.get("seniority") or []]},
        ))
    return out


class HimalayasSource(SearchLedger):
    kind = "job_board"

    def __init__(self, *, queries: list[str], work_from: list[str],
                 cursors: dict[tuple[str, str], dict[str, Any]] | None = None,
                 max_pages_per_search: int = 2, now: datetime | None = None,
                 settings: Settings | None = None, client: PoliteClient | None = None) -> None:
        self.settings = settings or get_settings()
        self.name = "himalayas"
        self.url = "https://himalayas.app"
        self._init_ledger(queries=queries, countries=[c.upper() for c in work_from],
                          cursors=cursors, now=now)
        self.max_pages = max(1, int(max_pages_per_search))
        self.client = client or PoliteClient(self.settings)

    def fetch(self, *, no_cache: bool = False) -> list[JobPosting]:
        out: list[JobPosting] = []
        failures = 0
        for query, country in self.plan():
            visit = self.open_visit(query, country)
            cursor: str | None = None
            for _ in range(self.max_pages):
                params = {"q": query, "country": country_name(country)}
                if cursor:
                    params["cursor"] = cursor
                try:
                    data = self.client.get_json(ENDPOINT + "?" + urlencode(params),
                                                no_cache=no_cache)
                except SourceError as exc:
                    visit.requests += self.counted(self.client)
                    failures += 1
                    log.warning("himalayas: %s — %s", query, exc)
                    if failures >= 3 and not out:
                        raise
                    break
                visit.requests += self.counted(self.client)
                batch = parse(data, query)
                out.extend(batch)
                visit.found += len(batch)
                visit.total = (data or {}).get("totalCount")
                cursor = (data or {}).get("nextCursor")
                if not cursor or not batch:
                    visit.complete = True
                    break
            else:
                visit.complete = True
        log.info("himalayas: %d posting(s) from %d search(es)", len(out), len(self.visits))
        return out
