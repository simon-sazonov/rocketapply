"""Findwork: a small remote-heavy aggregator with a keyed API.

Keyed (`FINDWORK_API_KEY`, sent as `Authorization: Token …`). Checked live on
28 Sep 2026: the rate-limit header says 10, and the search is loose —
"machine learning" returned account executives beside ML engineers — so it
runs only the primary searches, one page each, and the filters do the rest.

    GET https://findwork.dev/api/jobs/?search=data%20scientist&sort_by=date

    {"count": 84, "next": null, "results": [{id, role, company_name, location,
     remote, url, text (HTML), date_posted, keywords [..], employment_type,
     source}]}
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from ...config import Settings, get_settings
from .base import JobPosting, PoliteClient, RateLimited, SourceError, iso_date, strip_html
from .search import SearchLedger

log = logging.getLogger(__name__)

ENDPOINT = "https://findwork.dev/api/jobs/"
#: Ten requests a minute, from its rate-limit header.
MIN_GAP_SECONDS = 6.5


def parse(data: Any, query: str | None = None) -> list[JobPosting]:
    out: list[JobPosting] = []
    for job in (data or {}).get("results", []) or []:
        title, url = strip_html(job.get("role")), job.get("url")
        if not title or not url:
            continue
        remote = job.get("remote")
        where = (job.get("location") or "").strip()
        location = where or None
        if remote is True:
            location = f"Remote — {where}" if where else "Remote"
        out.append(JobPosting(
            title=title,
            url=str(url),
            external_id=str(job.get("id") or "") or None,
            description=strip_html(job.get("text")),
            location=location,
            employment_type=job.get("employment_type") or None,
            posted_at=iso_date(job.get("date_posted")),
            company_name=(job.get("company_name") or "").strip() or None,
            remote=remote if isinstance(remote, bool) else None,
            found_query=query,
            raw=job,
        ))
    return out


class FindworkSource(SearchLedger):
    kind = "aggregator_api"
    terms_govern = True
    provider = "findwork"

    def __init__(self, api_key: str, *, queries: list[str],
                 cursors: dict[tuple[str, str], dict[str, Any]] | None = None,
                 max_requests: int | None = None, now: datetime | None = None,
                 settings: Settings | None = None, client: PoliteClient | None = None) -> None:
        if not api_key:
            raise ValueError("Findwork needs FINDWORK_API_KEY in .env")
        self.settings = settings or get_settings()
        self.name = "findwork"
        self.url = "https://findwork.dev"
        self.api_key = api_key
        self._init_ledger(queries=queries, countries=["*"], cursors=cursors,
                          max_requests=max_requests, now=now)
        self.client = client or PoliteClient(self.settings)
        if hasattr(self.client, "set_min_delay"):
            self.client.set_min_delay(self.url, MIN_GAP_SECONDS)

    def fetch(self, *, no_cache: bool = False) -> list[JobPosting]:
        out: list[JobPosting] = []
        failures = 0
        for query, key in self.plan():
            if not self.left():
                break
            visit = self.open_visit(query, key)
            url = ENDPOINT + "?" + urlencode({"search": query, "sort_by": "date"})
            try:
                data = self.client.get_json(url, no_cache=no_cache, terms_govern=True,
                                            headers={"Authorization": f"Token {self.api_key}"})
            except RateLimited:
                visit.requests += self.counted(self.client)
                break
            except SourceError as exc:
                visit.requests += self.counted(self.client)
                failures += 1
                if failures >= 3 and not out:
                    raise SourceError(str(exc).replace(self.api_key, "***")) from None
                continue
            visit.requests += self.counted(self.client)
            batch = parse(data, query)
            out.extend(batch)
            visit.found = len(batch)
            visit.total = (data or {}).get("count")
            visit.complete = True
        log.info("findwork: %d posting(s) from %d search(es)", len(out), len(self.visits))
        return out
