"""JobTech JobSearch — Arbetsförmedlingen's open API: every vacancy in Sweden.

Keyless and official. Checked live on 28 Sep 2026 (no robots.txt on the host):

    GET https://jobsearch.api.jobtechdev.se/search?q=data%20scientist&limit=100&offset=0
        &published-after=2026-09-21T00:00:00

    {"total": {"value": 31}, "hits": [{id, headline, webpage_url, description
     {text}, employer {name, url}, workplace_address {city, municipality,
     region}, publication_date, employment_type {label}, working_hours_type
     {label}, experience_required, application_details {url}, removed}]}

Descriptions are mostly Swedish, which the language rules read as "asks for
Swedish". The link is the Platsbanken ad; the employer's own application page
is kept in `raw.application_details.url`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from ...config import Settings, get_settings
from .base import JobPosting, PoliteClient, SourceError, iso_date
from .search import SearchLedger

log = logging.getLogger(__name__)

ENDPOINT = "https://jobsearch.api.jobtechdev.se/search"


def parse(data: Any, query: str | None = None) -> list[JobPosting]:
    out: list[JobPosting] = []
    for hit in (data or {}).get("hits", []) or []:
        title, ident = hit.get("headline"), hit.get("id")
        url = hit.get("webpage_url") or (hit.get("application_details") or {}).get("url")
        if not title or not url or hit.get("removed"):
            continue
        address = hit.get("workplace_address") or {}
        city = address.get("city") or address.get("municipality")
        kinds = [(hit.get(k) or {}).get("label") for k in ("employment_type", "working_hours_type")]
        out.append(JobPosting(
            title=str(title).strip(),
            url=str(url),
            external_id=str(ident) if ident else None,
            description=((hit.get("description") or {}).get("text") or "").strip() or None,
            location=", ".join(p for p in (city, "Sweden") if p),
            employment_type=", ".join(k for k in kinds if k) or None,
            posted_at=iso_date(hit.get("publication_date")),
            company_name=((hit.get("employer") or {}).get("name") or "").strip() or None,
            country_hint="SE",
            found_query=query,
            raw=hit,
        ))
    return out


class JobTechSource(SearchLedger):
    kind = "job_board"

    def __init__(self, *, queries: list[str],
                 cursors: dict[tuple[str, str], dict[str, Any]] | None = None,
                 per_page: int = 100, max_pages_per_search: int = 3, backfill_days: int = 30,
                 now: datetime | None = None, settings: Settings | None = None,
                 client: PoliteClient | None = None) -> None:
        self.settings = settings or get_settings()
        self.name = "jobtech"
        self.url = "https://jobsearch.api.jobtechdev.se"
        self._init_ledger(queries=queries, countries=["SE"], cursors=cursors,
                          backfill_days=backfill_days, now=now)
        self.per_page = max(10, min(100, int(per_page)))
        self.max_pages = max(1, int(max_pages_per_search))
        self.client = client or PoliteClient(self.settings)

    def fetch(self, *, no_cache: bool = False) -> list[JobPosting]:
        out: list[JobPosting] = []
        failures = 0
        for query, country in self.plan():
            after = self.since(query, country).strftime("%Y-%m-%dT00:00:00")
            visit = self.open_visit(query, country)
            for page in range(self.max_pages):
                url = ENDPOINT + "?" + urlencode({
                    "q": query, "limit": self.per_page, "offset": page * self.per_page,
                    "published-after": after,
                })
                try:
                    data = self.client.get_json(url, no_cache=no_cache)
                except SourceError as exc:
                    visit.requests += self.counted(self.client)
                    failures += 1
                    log.warning("jobtech: %s — %s", query, exc)
                    if failures >= 3 and not out:
                        raise
                    break
                visit.requests += self.counted(self.client)
                batch = parse(data, query)
                out.extend(batch)
                visit.found += len(batch)
                total = ((data or {}).get("total") or {}).get("value")
                visit.total = int(total) if isinstance(total, int) else visit.total
                if len(batch) < self.per_page:
                    visit.complete = True
                    break
            else:
                visit.complete = True
        log.info("jobtech: %d posting(s) from %d search(es)", len(out), len(self.visits))
        return out
