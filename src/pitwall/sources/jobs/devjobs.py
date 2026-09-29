"""The DevJobs boards: GermanTechJobs, SwissDevJobs, DevITJobs UK and NL.

One company runs all four, on one API. Keyless; checked live on 28 Sep 2026:

    GET https://{host}/api/jobsLight      the whole board, one JSON list

    [{_id, name, company, actualCity, cityCategory, workplace (office | hybrid |
      remote), expLevel (Junior | Regular | Senior | Lead), metaCategory
      (mlaidata | softwareeng | …), techCategory, technologies [..], jobType,
      hasVisaSponsorship (Yes | No), annualSalaryFrom/To, activeFrom, jobUrl,
      redirectJobUrl, language}]

robots.txt disallows `/api/` only for `Meta-ExternalAgent`. The `*` group,
which is the one this client falls under, allows it. That is easy to misread
with a grep, and this module's first draft did.

The light listing has no description, so the posting's text is built from
what it does state: its technologies, level, type and sponsorship. It is
marked as a snippet, so "not mentioned" never reads as "not required".

**Sponsorship.** `hasVisaSponsorship` is "No" on 2,512 of 2,514 UK jobs, which
is the default rather than a statement. Only "Yes" is carried over, as an
explicit offer.

Only `metaCategory == "mlaidata"` (392 of 2,514 in the UK), or a title in the
same family, is kept. The rule is the same every run, so the board still
counts as a complete listing: a kept job that disappears has closed.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ...config import Settings, get_settings
from .base import JobPosting, PoliteClient, iso_date

log = logging.getLogger(__name__)

#: ISO2 -> (host, country name, salary currency).
HOSTS: dict[str, tuple[str, str, str]] = {
    "DE": ("germantechjobs.de", "Germany", "EUR"),
    "CH": ("swissdevjobs.ch", "Switzerland", "CHF"),
    "GB": ("devitjobs.uk", "United Kingdom", "GBP"),
    "NL": ("devitjobs.nl", "Netherlands", "EUR"),
}

_FAMILY = re.compile(
    r"\b(?:data|machine learning|ml|ai|scien\w*|analyst|analytics|optimi[sz]\w*|"
    r"simulation|research|quant\w*|statistic\w*|decision)\b", re.I)


def relevant(job: dict[str, Any]) -> bool:
    return job.get("metaCategory") == "mlaidata" or bool(_FAMILY.search(job.get("name") or ""))


def parse(data: Any, country: str) -> list[JobPosting]:
    host, place, currency = HOSTS[country]
    out: list[JobPosting] = []
    for job in data or []:
        if not isinstance(job, dict) or not relevant(job) or job.get("isPaused"):
            continue
        title, slug = job.get("name"), job.get("jobUrl")
        if not title or not slug:
            continue
        workplace = str(job.get("workplace") or "").lower()
        city = job.get("actualCity") or job.get("cityCategory")
        location = ", ".join(p for p in (city, place) if p)
        if workplace == "hybrid":
            location += " (hybrid)"
        tech = [str(t) for t in job.get("technologies") or [] if t]
        level = str(job.get("expLevel") or "").strip()
        lines = [
            f"Technologies: {', '.join(tech)}." if tech else "",
            f"Level: {level}." if level else "",
            f"Job type: {job['jobType']}." if job.get("jobType") else "",
            "Visa sponsorship available." if job.get("hasVisaSponsorship") == "Yes" else "",
        ]
        out.append(JobPosting(
            title=str(title).strip(),
            url=f"https://{host}/jobs/{slug}",
            external_id=str(job.get("_id") or "") or None,
            description="\n".join(line for line in lines if line) or None,
            location=location or None,
            employment_type=job.get("jobType") or None,
            posted_at=iso_date(job.get("activeFrom")),
            company_name=(job.get("company") or "").strip() or None,
            remote=True if workplace == "remote" else (False if workplace == "office" else None),
            salary_min=float(job["annualSalaryFrom"]) if job.get("annualSalaryFrom") else None,
            salary_max=float(job["annualSalaryTo"]) if job.get("annualSalaryTo") else None,
            salary_currency=currency if job.get("annualSalaryFrom") else None,
            country_hint=country,
            description_truncated=True,
            raw={**job, "_levels": [level.lower()] if level else []},
        ))
    return out


class DevJobsSource:
    """One board, read whole, kept to the ML / AI / data family."""

    kind = "job_board"
    complete_listing = True

    def __init__(self, country: str, *, settings: Settings | None = None,
                 client: PoliteClient | None = None) -> None:
        if country not in HOSTS:
            raise ValueError(f"no DevJobs board for {country!r}; known: {sorted(HOSTS)}")
        self.settings = settings or get_settings()
        self.country = country
        self.host = HOSTS[country][0]
        self.name = f"devjobs:{self.host}"
        self.url = f"https://{self.host}"
        self.client = client or PoliteClient(self.settings)

    def fetch(self, *, no_cache: bool = False) -> list[JobPosting]:
        data = self.client.get_json(f"https://{self.host}/api/jobsLight", no_cache=no_cache)
        out = parse(data, self.country)
        log.info("%s: %d relevant of %d posting(s)", self.name, len(out),
                 len(data) if isinstance(data, list) else 0)
        return out
