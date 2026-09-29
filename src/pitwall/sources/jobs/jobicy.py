"""Jobicy: a remote-jobs feed with a published API and published manners.

Keyless. Checked live on 28 Sep 2026:

    GET https://jobicy.com/api/v2/remote-jobs?count=100&industry=data-science

    {"friendlyNotice": "…ensure Jobicy is clearly credited with a direct link to
     the source, and all application buttons redirect to the original job URL…",
     "jobs": [{id, url, jobTitle, companyName, jobIndustry [..], jobType [..],
     jobGeo ("Canada,  USA" | "EMEA" | "Anywhere"), jobLevel, pubDate,
     jobDescription (HTML), salaryMin, salaryMax, salaryCurrency}]}

Its terms ask for at least an hour between automated requests, so this is ONE
request per run, and the orchestrator's clock skips it inside the hour. The
posting keeps Jobicy's URL as its link and the source is shown as "via
Jobicy". Those are the credit and the link-back the notice asks for.

`jobGeo` becomes the location text, so "Canada, USA" becomes remote regions
US and CA, and the eligibility rules decide from there.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlencode

from ...config import Settings, get_settings
from .base import JobPosting, PoliteClient, iso_date, strip_html

log = logging.getLogger(__name__)

ENDPOINT = "https://jobicy.com/api/v2/remote-jobs"


def parse(data: Any) -> list[JobPosting]:
    out: list[JobPosting] = []
    for job in (data or {}).get("jobs", []) or []:
        title, url = job.get("jobTitle"), job.get("url")
        if not title or not url:
            continue
        geo = " ".join(str(job.get("jobGeo") or "").split())
        level = str(job.get("jobLevel") or "").strip().lower()
        low, high = job.get("salaryMin"), job.get("salaryMax")
        out.append(JobPosting(
            title=strip_html(title) or str(title),
            url=str(url),
            external_id=str(job.get("id") or "") or None,
            description=strip_html(job.get("jobDescription")) or job.get("jobExcerpt") or None,
            location=f"Remote — {geo}" if geo else "Remote",
            employment_type=", ".join(str(t) for t in job.get("jobType") or []) or None,
            posted_at=iso_date(job.get("pubDate")),
            company_name=(job.get("companyName") or "").strip() or None,
            remote=True,
            salary_min=float(low) if isinstance(low, int | float) and low else None,
            salary_max=float(high) if isinstance(high, int | float) and high else None,
            salary_currency=job.get("salaryCurrency") if low or high else None,
            raw={**job, "_levels": [level] if level and level != "any" else []},
        ))
    return out


class JobicySource:
    kind = "job_board"
    complete_listing = False
    #: Jobicy asks for an hour between automated requests.
    min_interval_hours = 1.0

    def __init__(self, *, industry: str = "data-science", count: int = 100,
                 settings: Settings | None = None, client: PoliteClient | None = None) -> None:
        self.settings = settings or get_settings()
        self.name = "jobicy"
        self.url = "https://jobicy.com"
        self.industry = industry
        self.count = max(1, min(100, int(count)))
        self.client = client or PoliteClient(self.settings)

    def fetch(self, *, no_cache: bool = False) -> list[JobPosting]:
        url = ENDPOINT + "?" + urlencode({"count": self.count, "industry": self.industry})
        out = parse(self.client.get_json(url, no_cache=no_cache))
        log.info("jobicy: %d posting(s)", len(out))
        return out
