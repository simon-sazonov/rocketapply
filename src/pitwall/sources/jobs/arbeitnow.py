"""Arbeitnow's public job-board API — open-market discovery (§7.1 of the spec).

Keyless and free. Checked live on 22 Sep 2026:

    GET https://www.arbeitnow.com/api/job-board-api?page=N

    {"data": [ {slug, company_name, title, description (HTML), remote (bool),
                url, tags [..], job_types [..], location, created_at (epoch s)} ],
     "links": {"next": ".../job-board-api?page=2", ...},
     "meta":  {"per_page": 250, "terms": "This is a free public API for jobs,
               please do not abuse. ...", ...}}

`robots.txt` allows everything but a HubSpot tracking parameter. The terms ask
for restraint, so this reads a small number of pages (`max_pages`), newest
first, behind the same polite client as every other source — and the job
search caches for hours, so re-running on the same morning costs nothing.

There is no visa field. Descriptions are frequently German or French, which is
why the experience and visa rules carry those phrasings too.

The feed is newest-first and read to a page cap, so a posting missing from one
fetch is only older than the cap, not closed: `complete_listing = False`.
"""

from __future__ import annotations

import logging
from typing import Any

from ...config import Settings, get_settings
from .base import JobPosting, PoliteClient, iso_date, strip_html

log = logging.getLogger(__name__)

ENDPOINT = "https://www.arbeitnow.com/api/job-board-api"
#: The UK edition: same API, same shape, 100 a page (checked 28 Sep 2026).
ENDPOINT_UK = "https://www.arbeitnow.co.uk/api/job-board-api"


def parse(data: Any) -> list[JobPosting]:
    """One page of the API, normalised. Pure — this is what the fixture tests."""
    out: list[JobPosting] = []
    for job in (data or {}).get("data", []) or []:
        title = job.get("title")
        url = job.get("url")
        if not title or not url:
            continue
        job_types = [str(t) for t in job.get("job_types") or [] if t]
        remote = job.get("remote")
        out.append(JobPosting(
            title=str(title).strip(),
            url=str(url),
            external_id=str(job.get("slug") or "") or None,
            description=strip_html(job.get("description")),
            location=job.get("location") or None,
            employment_type=", ".join(job_types) or None,
            posted_at=iso_date(job.get("created_at")),
            company_name=job.get("company_name") or None,
            remote=bool(remote) if isinstance(remote, bool) else None,
            raw=job,
        ))
    return out


class ArbeitnowSource:
    """The Arbeitnow feed, newest first, to a page cap."""

    kind = "job_board"
    complete_listing = False

    def __init__(
        self,
        *,
        max_pages: int = 4,
        endpoint: str = ENDPOINT,
        name: str = "arbeitnow",
        settings: Settings | None = None,
        client: PoliteClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.name = name
        self.url = endpoint
        self.max_pages = max(1, int(max_pages))
        self.client = client or PoliteClient(self.settings)

    def fetch(self, *, no_cache: bool = False) -> list[JobPosting]:
        out: list[JobPosting] = []
        url: str | None = self.url
        pages = 0
        while url and pages < self.max_pages:
            data = self.client.get_json(url, no_cache=no_cache)
            batch = parse(data)
            out.extend(batch)
            pages += 1
            if not batch:
                break
            url = ((data or {}).get("links") or {}).get("next")
        log.info("%s: %d posting(s) from %d page(s)", self.name, len(out), pages)
        return out
