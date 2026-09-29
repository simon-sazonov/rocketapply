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

Added on 28 Sep 2026, when boards started being found from posting links:

    personio        {token}.jobs.personio.de/xml      XML, not JSON
    teamtailor      {token}.teamtailor.com/jobs.rss   RSS; robots.txt allows it

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
    "personio": "https://{token}.jobs.personio.de/xml",
    "teamtailor": "https://{token}.teamtailor.com/jobs.rss",
}

#: Boards that answer in XML rather than JSON.
TEXT_KINDS = frozenset({"personio", "teamtailor"})

#: SmartRecruiters returns 100 postings a page; Bosch has 4,776. A board is
#: read to this many pages, and only counts as complete if it ended first.
SMARTRECRUITERS_MAX_PAGES = 10

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


def _xml(text: Any) -> Any:
    import xml.etree.ElementTree as ET

    try:
        return ET.fromstring(str(text or "").strip().encode("utf-8"))
    except ET.ParseError as exc:
        raise SourceError(f"not XML ({exc})") from exc


def _personio(data: Any, token: str | None = None) -> list[JobPosting]:
    """`<workzag-jobs><position>…` — id, name, office, subcompany, seniority,
    yearsOfExperience ("2-5"), employmentType, schedule, jobDescriptions
    (name/value, HTML in CDATA), createdAt."""
    root = data if hasattr(data, "findall") else _xml(data)
    out = []
    for pos in root.findall("position"):
        ident, title = pos.findtext("id"), pos.findtext("name")
        if not ident or not title:
            continue
        parts = []
        for block in pos.findall("jobDescriptions/jobDescription"):
            head, body = block.findtext("name"), strip_html(block.findtext("value"))
            parts.append("\n".join(p for p in (head, body) if p))
        years = (pos.findtext("yearsOfExperience") or "").strip()
        if years:
            parts.append(f"Required experience: {years} years.")
        seniority = (pos.findtext("seniority") or "").strip().lower()
        kinds = [pos.findtext("employmentType"), pos.findtext("schedule")]
        company = (pos.findtext("subcompany") or "").replace("_", " ").strip() or None
        out.append(JobPosting(
            title=title.strip(),
            url=f"https://{token}.jobs.personio.de/job/{ident}" if token else str(ident),
            external_id=str(ident),
            description="\n\n".join(p for p in parts if p) or None,
            location=(pos.findtext("office") or "").strip() or None,
            employment_type=", ".join(k for k in kinds if k) or None,
            posted_at=iso_date(pos.findtext("createdAt")),
            company_name=company,
            raw={child.tag: (child.text or "").strip() for child in pos
                 if child.tag != "jobDescriptions"} | {"_levels": [seniority] if seniority else []},
        ))
    return out


_TT = "{https://teamtailor.com/locations}"


def _teamtailor(data: Any, token: str | None = None) -> list[JobPosting]:
    """RSS 2.0 with a `tt:` namespace for locations and department, and a
    plain `remoteStatus` (onsite | hybrid | remote)."""
    root = data if hasattr(data, "findall") else _xml(data)
    company = (root.findtext("channel/title") or "").strip() or None
    out = []
    for item in root.findall("channel/item"):
        title, link = item.findtext("title"), item.findtext("link")
        if not title or not link:
            continue
        places = []
        for loc in item.findall(f"{_TT}locations/{_TT}location"):
            place = ", ".join(p for p in (loc.findtext(f"{_TT}city"),
                                          loc.findtext(f"{_TT}country")) if p)
            if place and place not in places:
                places.append(place)
        status = (item.findtext("remoteStatus") or "").strip().lower()
        location = "; ".join(places)
        if status == "hybrid":
            location = f"{location} (hybrid)" if location else "hybrid"
        out.append(JobPosting(
            title=title.strip(),
            url=link.strip(),
            external_id=(item.findtext("guid") or link).strip(),
            description=strip_html(item.findtext("description")),
            location=location or None,
            posted_at=iso_date(_rfc822(item.findtext("pubDate"))),
            company_name=company,
            remote=True if status in {"remote", "fully_remote", "fully"} else
            (False if status == "onsite" else None),
            raw={"remoteStatus": status, "department": item.findtext(f"{_TT}department"),
                 "locations": places, "guid": item.findtext("guid")},
        ))
    return out


def _rfc822(value: str | None) -> str | None:
    from email.utils import parsedate_to_datetime

    if not value:
        return None
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError):
        return value


PARSERS = {
    "greenhouse": _greenhouse,
    "lever": _lever,
    "ashby": _ashby,
    "workable": _workable,
    "smartrecruiters": _smartrecruiters,
    "recruitee": _recruitee,
    "personio": _personio,
    "teamtailor": _teamtailor,
}


def parse(kind: str, data: Any, token: str | None = None) -> list[JobPosting]:
    """Normalise one ATS payload. Pure — this is what the fixtures test."""
    try:
        parser = PARSERS[kind]
    except KeyError as exc:
        raise ValueError(f"no parser for ATS {kind!r}") from exc
    if kind in TEXT_KINDS:
        return parser(data, token)  # type: ignore[call-arg]
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
        if self.ats in TEXT_KINDS:
            text = self.client.get(self.url, no_cache=no_cache,
                                   accept="application/xml, application/rss+xml, */*")
            postings = parse(self.ats, text, self.token)
        elif self.ats == "smartrecruiters":
            postings = self._smartrecruiters_pages(no_cache)
        else:
            postings = parse(self.ats, self.client.get_json(self.url, no_cache=no_cache))
        log.info("%s: %d posting(s)", self.name, len(postings))
        return postings

    def _smartrecruiters_pages(self, no_cache: bool) -> list[JobPosting]:
        """Offset paging. A board read to the page cap is not complete, so
        what it did not return is aged out, never closed on the spot."""
        out: list[JobPosting] = []
        offset = 0
        for _ in range(SMARTRECRUITERS_MAX_PAGES):
            sep = "&" if "?" in self.url else "?"
            data = self.client.get_json(f"{self.url}{sep}limit=100&offset={offset}",
                                        no_cache=no_cache)
            batch = parse(self.ats, data)
            out.extend(batch)
            total = (data or {}).get("totalFound")
            offset += 100
            if not batch or not isinstance(total, int) or offset >= total:
                return out
        self.complete_listing = False
        return out

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
