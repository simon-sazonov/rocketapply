"""MyCareersFuture — the Singapore government's national jobs portal.

Keyless and official. Checked live on 28 Sep 2026 (no robots.txt on the API
host):

    GET https://api.mycareersfuture.gov.sg/v2/jobs?search=data%20scientist&limit=100&page=0

    {"total": 135, "results": [{uuid, title, description (HTML), postedCompany
     {name}, hiringCompany {name} | null, salary {minimum, maximum, type
     {salaryType: Monthly}}, address {isOverseas, overseasCountry},
     positionLevels [{position}], minimumYearsExperience, employmentTypes
     [{employmentType}], skills [{skill}], metadata {jobDetailsUrl,
     newPostingDate}}]}

Three details matter downstream:

* **An agency posts for a client.** `postedCompany` is often Randstad or
  similar; `hiringCompany`, when present, is the employer.
* **`minimumYearsExperience` is a stated requirement.** It is written into the
  description as a sentence, so the experience rules read it the way they read
  any posting.
* **The position levels are Singaporean grades.** "Executive" and "Senior
  Executive" are early- and mid-career professional grades there, not
  C-level. They are mapped to entry / mid / senior before storing (`_levels`).
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

ENDPOINT = "https://api.mycareersfuture.gov.sg/v2/jobs"

#: MyCareersFuture position levels -> the extractor's words.
LEVELS = {
    "fresh/entry level": "entry", "non-executive": "entry", "junior executive": "entry",
    "executive": "mid", "senior executive": "mid", "professional": "mid",
    "manager": "senior", "middle management": "senior", "senior management": "senior",
}


def parse(data: Any, query: str | None = None) -> list[JobPosting]:
    out: list[JobPosting] = []
    for job in (data or {}).get("results", []) or []:
        title = job.get("title")
        meta = job.get("metadata") or {}
        url = meta.get("jobDetailsUrl")
        address = job.get("address") or {}
        if not title or not url or address.get("isOverseas"):
            continue
        employer = (job.get("hiringCompany") or {}).get("name") or \
            (job.get("postedCompany") or {}).get("name")
        levels = [LEVELS.get(str(p.get("position", "")).strip().lower())
                  for p in job.get("positionLevels") or [] if isinstance(p, dict)]
        text = strip_html(job.get("description")) or ""
        years = job.get("minimumYearsExperience")
        if isinstance(years, int | float) and years > 0:
            text += f"\n\nMinimum {years:g} years of experience required."
        skills = [s.get("skill") for s in job.get("skills") or [] if isinstance(s, dict)]
        if skills:
            text += "\n\nSkills: " + ", ".join(s for s in skills if s)
        salary = job.get("salary") or {}
        monthly = str((salary.get("type") or {}).get("salaryType") or "").lower() == "monthly"
        factor = 12 if monthly else 1
        low, high = salary.get("minimum"), salary.get("maximum")
        out.append(JobPosting(
            title=str(title).strip(),
            url=str(url),
            external_id=str(job.get("uuid") or "") or None,
            description=text.strip() or None,
            location="Singapore",
            employment_type=", ".join(e.get("employmentType", "") for e in
                                      job.get("employmentTypes") or [] if isinstance(e, dict))
            or None,
            posted_at=iso_date(meta.get("newPostingDate") or meta.get("createdAt")),
            company_name=(employer or "").strip() or None,
            salary_min=float(low) * factor if isinstance(low, int | float) and low else None,
            salary_max=float(high) * factor if isinstance(high, int | float) and high else None,
            salary_currency="SGD" if low or high else None,
            country_hint="SG",
            found_query=query,
            raw={**job, "_levels": [lv for lv in levels if lv]},
        ))
    return out


class MyCareersFutureSource(SearchLedger):
    kind = "job_board"

    def __init__(self, *, queries: list[str],
                 cursors: dict[tuple[str, str], dict[str, Any]] | None = None,
                 per_page: int = 100, max_pages_per_search: int = 2,
                 now: datetime | None = None, settings: Settings | None = None,
                 client: PoliteClient | None = None) -> None:
        self.settings = settings or get_settings()
        self.name = "mycareersfuture"
        self.url = "https://www.mycareersfuture.gov.sg"
        self._init_ledger(queries=queries, countries=["SG"], cursors=cursors, now=now)
        self.per_page = max(10, min(100, int(per_page)))
        self.max_pages = max(1, int(max_pages_per_search))
        self.client = client or PoliteClient(self.settings)

    def fetch(self, *, no_cache: bool = False) -> list[JobPosting]:
        out: list[JobPosting] = []
        failures = 0
        for query, country in self.plan():
            visit = self.open_visit(query, country)
            for page in range(self.max_pages):
                url = ENDPOINT + "?" + urlencode({"search": query, "limit": self.per_page,
                                                  "page": page})
                try:
                    data = self.client.get_json(url, no_cache=no_cache)
                except SourceError as exc:
                    visit.requests += self.counted(self.client)
                    failures += 1
                    log.warning("mycareersfuture: %s — %s", query, exc)
                    if failures >= 3 and not out:
                        raise
                    break
                visit.requests += self.counted(self.client)
                batch = parse(data, query)
                rows = len((data or {}).get("results") or [])
                out.extend(batch)
                visit.found += len(batch)
                visit.total = (data or {}).get("total")
                if rows < self.per_page:
                    visit.complete = True
                    break
            else:
                visit.complete = True
        log.info("mycareersfuture: %d posting(s) from %d search(es)", len(out), len(self.visits))
        return out
