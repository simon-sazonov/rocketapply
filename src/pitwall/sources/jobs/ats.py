"""Public ATS job-board endpoints — §2.1/§2.3.

Free, keyless, structured and entirely legitimate: these are the same endpoints
the companies' own careers pages call from the browser. Motorsport coverage is
thin — most teams run bespoke portals or Workday — so this is an opportunistic
fast path, not the main source.

Every pattern and every field name below was checked against a live board on
30 Aug 2026, not taken from documentation:

    greenhouse      boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
    lever           api.lever.co/v0/postings/{token}?mode=json
    ashby           api.ashbyhq.com/posting-api/job-board/{token}
    workable        apply.workable.com/api/v1/widget/accounts/{token}?details=true
    smartrecruiters api.smartrecruiters.com/v1/companies/{token}/postings
    recruitee       {token}.recruitee.com/api/offers/

The response shapes disagree about almost everything — Lever puts the job title
in `text` and the date in epoch milliseconds, Greenhouse nests location under
`location.name`, SmartRecruiters wraps everything in `content[]` — so each
parser is separate and each one has a fixture.
"""

from __future__ import annotations

import logging
from typing import Any

from ...config import Settings, get_settings
from .base import JobPosting, PoliteClient, SourceError, iso_date, strip_html

log = logging.getLogger(__name__)

#: `{token}` is the board slug. Order matters only for `discover_ats.py`, which
#: probes them in turn and keeps the first that answers with postings.
ENDPOINTS: dict[str, str] = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true",
    "lever": "https://api.lever.co/v0/postings/{token}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{token}",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{token}?details=true",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{token}/postings",
    "recruitee": "https://{token}.recruitee.com/api/offers/",
}

ATS_KINDS = tuple(ENDPOINTS)


def endpoint_for(kind: str, token: str) -> str:
    try:
        return ENDPOINTS[kind].format(token=token)
    except KeyError as exc:
        raise ValueError(f"unknown ATS {kind!r}; known: {sorted(ENDPOINTS)}") from exc


# ───────────────────────────── the parsers ───────────────────────────────


def _greenhouse(data: Any) -> list[JobPosting]:
    out = []
    for job in (data or {}).get("jobs", []) or []:
        url = job.get("absolute_url")
        title = job.get("title")
        if not url or not title:
            continue
        location = (job.get("location") or {}).get("name")
        out.append(JobPosting(
            title=title, url=url, external_id=str(job.get("id") or ""),
            description=strip_html(job.get("content")),
            location=location,
            posted_at=iso_date(job.get("first_published") or job.get("updated_at")),
            company_name=job.get("company_name"),
            raw=job,
        ))
    return out


def _lever(data: Any) -> list[JobPosting]:
    out = []
    for job in data or []:
        url = job.get("hostedUrl") or job.get("applyUrl")
        title = job.get("text")          # Lever's name for the job title
        if not url or not title:
            continue
        categories = job.get("categories") or {}
        out.append(JobPosting(
            title=title, url=url, external_id=str(job.get("id") or ""),
            description=job.get("descriptionPlain") or strip_html(job.get("description")),
            location=categories.get("location") or job.get("country"),
            employment_type=categories.get("commitment"),
            posted_at=iso_date(job.get("createdAt")),   # epoch milliseconds
            raw=job,
        ))
    return out


def _ashby(data: Any) -> list[JobPosting]:
    out = []
    for job in (data or {}).get("jobs", []) or []:
        url = job.get("jobUrl") or job.get("applyUrl")
        title = job.get("title")
        if not url or not title or job.get("isListed") is False:
            continue
        out.append(JobPosting(
            title=title, url=url, external_id=str(job.get("id") or ""),
            description=job.get("descriptionPlain") or strip_html(job.get("descriptionHtml")),
            location=job.get("location"),
            employment_type=job.get("employmentType"),
            posted_at=iso_date(job.get("publishedAt")),
            raw=job,
        ))
    return out


def _join_location(node: Any) -> str | None:
    """city, region, country — in that order, whatever order the source used."""
    if isinstance(node, str):
        return node or None
    if not isinstance(node, dict):
        return None
    parts = [node.get("city"), node.get("region") or node.get("state"), node.get("country")]
    return ", ".join(str(p) for p in parts if p) or None


def _workable(data: Any) -> list[JobPosting]:
    rows = (data or {}).get("jobs") or (data or {}).get("results") or []
    name = (data or {}).get("name") or (data or {}).get("description")
    out = []
    for job in rows:
        url = job.get("url") or job.get("shortlink") or job.get("application_url")
        title = job.get("title")
        if not url or not title:
            continue
        out.append(JobPosting(
            title=title, url=url, external_id=str(job.get("shortcode") or job.get("id") or ""),
            description=strip_html(job.get("description")),
            location=_join_location(job.get("location") or job),
            employment_type=job.get("employment_type"),
            posted_at=iso_date(job.get("published_on") or job.get("created_at")),
            company_name=name,
            raw=job,
        ))
    return out


def _smartrecruiters(data: Any) -> list[JobPosting]:
    out = []
    for job in (data or {}).get("content", []) or []:
        job_id = job.get("id")
        if not job_id or not job.get("name"):
            continue
        company = (job.get("company") or {}).get("identifier") or ""
        location = job.get("location") or {}
        parts = [location.get("city"), location.get("region"), location.get("country")]
        out.append(JobPosting(
            title=job["name"],
            url=f"https://jobs.smartrecruiters.com/{company}/{job_id}" if company
            else f"https://jobs.smartrecruiters.com/{job_id}",
            external_id=str(job_id),
            location=", ".join(p for p in parts if p) or None,
            employment_type=(job.get("typeOfEmployment") or {}).get("label"),
            posted_at=iso_date(job.get("releasedDate")),
            company_name=(job.get("company") or {}).get("name"),
            raw=job,
        ))
    return out


def _recruitee(data: Any) -> list[JobPosting]:
    out = []
    for job in (data or {}).get("offers", []) or []:
        url = job.get("careers_url") or job.get("careers_apply_url")
        title = job.get("title")
        if not url or not title:
            continue
        parts = [job.get("city"), job.get("state_name"), job.get("country")]
        out.append(JobPosting(
            title=title, url=url, external_id=str(job.get("id") or ""),
            description=strip_html(job.get("description")),
            location=", ".join(p for p in parts if p) or job.get("location"),
            employment_type=job.get("employment_type_code") or job.get("employment_type"),
            posted_at=iso_date(job.get("published_at") or job.get("created_at")),
            company_name=job.get("company_name"),
            raw=job,
        ))
    return out


PARSERS = {
    "greenhouse": _greenhouse,
    "lever": _lever,
    "ashby": _ashby,
    "workable": _workable,
    "smartrecruiters": _smartrecruiters,
    "recruitee": _recruitee,
}


def parse(kind: str, data: Any) -> list[JobPosting]:
    """Normalise one ATS payload. Pure — this is what the fixtures test."""
    try:
        parser = PARSERS[kind]
    except KeyError as exc:
        raise ValueError(f"no parser for ATS {kind!r}") from exc
    return parser(data)


# ───────────────────────────── the adapter ───────────────────────────────


class ATSSource:
    """One company's board on one ATS."""

    kind = "ats_json"
    #: A board lists every live posting, so absence really does mean closed.
    complete_listing = True

    def __init__(
        self,
        ats: str,
        token: str,
        *,
        company_id: int | None = None,
        company_name: str | None = None,
        settings: Settings | None = None,
        client: PoliteClient | None = None,
    ) -> None:
        if ats not in ENDPOINTS:
            raise ValueError(f"unknown ATS {ats!r}; known: {sorted(ENDPOINTS)}")
        self.settings = settings or get_settings()
        self.ats = ats
        self.token = token
        self.company_id = company_id
        self.company_name = company_name
        self.name = f"{ats}:{token}"
        self.url = endpoint_for(ats, token)
        self.client = client or PoliteClient(self.settings)

    def fetch(self, *, no_cache: bool = False) -> list[JobPosting]:
        data = self.client.get_json(self.url, no_cache=no_cache)
        postings = parse(self.ats, data)
        log.info("%s: %d posting(s)", self.name, len(postings))
        return postings

    def probe(self) -> int | None:
        """How many postings this board has, or None if it is not this company's.

        Used by `scripts/discover_ats.py`, which probes plausible tokens. A 404
        is the normal answer for a wrong guess, so it is not an error here.
        """
        try:
            return len(self.fetch())
        except SourceError:
            return None
        except ValueError:
            return None
