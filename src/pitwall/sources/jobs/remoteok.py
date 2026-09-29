"""Remote OK's public JSON feed.

Keyless. Checked live on 28 Sep 2026:

    GET https://remoteok.com/api?tag=machine-learning

    [{"legal": "API Terms of Service: Please link back … to the URL on Remote OK
      and mention Remote OK as a source…"},
     {id, position, company, location (free text), url, apply_url, date,
      salary_min, salary_max (0 = not given), tags [..], description (HTML)}, …]

The first element is the terms notice, not a job. The terms ask for a link to
the Remote OK page and credit, so the posting's link is Remote OK's `url` and
the card says "via remoteok". robots.txt asks for a 1 s Crawl-delay.

A tag returns about ten jobs, so this is a small source: a few tags per run.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

from ...config import Settings, get_settings
from .base import JobPosting, PoliteClient, SourceError, iso_date, strip_html

log = logging.getLogger(__name__)

ENDPOINT = "https://remoteok.com/api"


def parse(data: Any, tag: str | None = None) -> list[JobPosting]:
    out: list[JobPosting] = []
    for job in data or []:
        if not isinstance(job, dict) or not job.get("id"):
            continue    # the legal notice
        title, url = job.get("position"), job.get("url")
        if not title or not url:
            continue
        where = " ".join(str(job.get("location") or "").split())
        low, high = job.get("salary_min"), job.get("salary_max")
        out.append(JobPosting(
            title=strip_html(title) or str(title),
            url=str(url),
            external_id=str(job["id"]),
            description=strip_html(job.get("description")),
            location=f"Remote — {where}" if where else "Remote",
            posted_at=iso_date(job.get("date")),
            company_name=(job.get("company") or "").strip() or None,
            remote=True,
            salary_min=float(low) if isinstance(low, int | float) and low else None,
            salary_max=float(high) if isinstance(high, int | float) and high else None,
            salary_currency="USD" if low or high else None,
            found_query=tag,
            raw=job,
        ))
    return out


class RemoteOKSource:
    kind = "job_board"
    complete_listing = False

    def __init__(self, *, tags: list[str], settings: Settings | None = None,
                 client: PoliteClient | None = None) -> None:
        self.settings = settings or get_settings()
        self.name = "remoteok"
        self.url = "https://remoteok.com"
        self.tags = [t.strip().lower() for t in tags if t and t.strip()]
        self.client = client or PoliteClient(self.settings)

    def fetch(self, *, no_cache: bool = False) -> list[JobPosting]:
        out: list[JobPosting] = []
        seen: set[str] = set()
        errors: list[str] = []
        for tag in self.tags:
            try:
                data = self.client.get_json(ENDPOINT + "?" + urlencode({"tag": tag}),
                                            no_cache=no_cache)
            except SourceError as exc:
                errors.append(str(exc))
                continue
            for posting in parse(data, tag):
                if posting.external_id not in seen:
                    seen.add(posting.external_id or posting.url)
                    out.append(posting)
        if errors and not out:
            raise SourceError(errors[-1])
        log.info("remoteok: %d posting(s) from %d tag(s)", len(out), len(self.tags))
        return out
