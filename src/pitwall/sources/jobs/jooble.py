"""Jooble's REST API: the widest free source for the United States.

Keyed (`JOOBLE_API_KEY_US` in `.env`). The key belongs to jooble.org, which is
the US site; country subdomains answer 403 to it, so this source covers the US
and nothing else. Checked live on 28 Sep 2026:

    POST https://jooble.org/api/{key}
    {"keywords": "machine learning engineer", "location": "United States",
     "page": "1", "ResultOnPage": "50", "datecreatedfrom": "2026-09-21"}

    {"totalCount": 4491, "jobs": [{id, title, location, snippet (HTML), salary
     ("$229.9k - $262.4k"), source, type, link (a jooble.org redirect), company,
     updated}]}

That is 4,491 US postings for one title in one week, most of them senior. So a
visit reads a few pages, newest first via `datecreatedfrom` set to the last
complete visit, and the filters judge the rest.

Jooble publishes no rate limit and sends no rate headers, so requests are
counted in the quota ledger under `jooble` against a configurable daily cap.
The key sits in the URL path: it is never a cache key and never in a message.
Descriptions are snippets and links go through Jooble, so dedupe prefers any
other source of the same job.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ...config import Settings, get_settings
from .base import JobPosting, PoliteClient, RateLimited, SourceError, iso_date, strip_html
from .search import SearchLedger

log = logging.getLogger(__name__)

ENDPOINT = "https://jooble.org/api/{key}"
#: The only country the key reaches, and how Jooble spells it.
LOCATIONS = {"US": "United States"}


def parse(data: Any, query: str | None = None, country: str | None = "US") -> list[JobPosting]:
    out: list[JobPosting] = []
    for job in (data or {}).get("jobs", []) or []:
        title, link = strip_html(job.get("title")), job.get("link")
        if not title or not link:
            continue
        snippet = strip_html(job.get("snippet")) or ""
        salary = (job.get("salary") or "").strip()
        text = "\n".join(t for t in (snippet, f"Salary: {salary}" if salary else "") if t)
        out.append(JobPosting(
            title=title,
            url=str(link),
            external_id=str(job.get("id") or "") or None,
            description=text or None,
            location=(job.get("location") or "").strip() or None,
            employment_type=(job.get("type") or "").strip() or None,
            posted_at=iso_date(job.get("updated")),
            company_name=(job.get("company") or "").strip() or None,
            country_hint=country,
            description_truncated=True,
            found_query=query,
            raw=job,
        ))
    return out


class JoobleSource(SearchLedger):
    kind = "aggregator_api"
    terms_govern = True
    provider = "jooble"

    def __init__(self, api_key: str, *, queries: list[str], countries: list[str],
                 cursors: dict[tuple[str, str], dict[str, Any]] | None = None,
                 max_requests: int | None = 60, max_pages_per_search: int = 3,
                 per_page: int = 50, backfill_days: int = 14, now: datetime | None = None,
                 settings: Settings | None = None, client: PoliteClient | None = None) -> None:
        if not api_key:
            raise ValueError("Jooble needs JOOBLE_API_KEY_US in .env")
        self.settings = settings or get_settings()
        self.name = "jooble"
        self.url = "https://jooble.org"
        self.api_key = api_key
        self._init_ledger(queries=queries,
                          countries=[c.upper() for c in countries if c.upper() in LOCATIONS],
                          cursors=cursors, max_requests=max_requests,
                          backfill_days=backfill_days, now=now)
        self.max_pages = max(1, int(max_pages_per_search))
        self.per_page = max(10, min(100, int(per_page)))
        self.client = client or PoliteClient(self.settings)

    def fetch(self, *, no_cache: bool = False) -> list[JobPosting]:
        out: list[JobPosting] = []
        failures = 0
        stopped = ""
        for query, country in self.plan():
            if not self.left():
                break
            since = self.since(query, country).strftime("%Y-%m-%d")
            visit = self.open_visit(query, country)
            for page in range(1, self.max_pages + 1):
                if not self.left():
                    break
                payload = {"keywords": query, "location": LOCATIONS[country],
                           "page": str(page), "ResultOnPage": str(self.per_page),
                           "datecreatedfrom": since}
                safe = f"jooble {query} | {country} | page {page} | since {since}"
                try:
                    data = self.client.post_json(ENDPOINT.format(key=self.api_key), payload,
                                                 no_cache=no_cache, cache_key=safe,
                                                 terms_govern=True)
                except RateLimited as exc:
                    visit.requests += self.counted(self.client)
                    stopped = str(exc)
                    break
                except SourceError as exc:
                    visit.requests += self.counted(self.client)
                    failures += 1
                    if failures >= 3 and not out:
                        raise SourceError(f"jooble: the first {failures} searches all failed — "
                                          f"{exc}") from None
                    break
                visit.requests += self.counted(self.client)
                batch = parse(data, query, country)
                out.extend(batch)
                visit.found += len(batch)
                total = (data or {}).get("totalCount")
                visit.total = int(total) if isinstance(total, int) else visit.total
                if len(batch) < self.per_page:
                    visit.complete = True
                    break
            else:
                # Read to the page cap: newest first, so what is left is older.
                visit.complete = True
            if stopped:
                break
        log.info("jooble: %d posting(s) from %d search(es), %d request(s)%s", len(out),
                 len(self.visits), self.requests_made, f" — stopped: {stopped}" if stopped else "")
        return out
