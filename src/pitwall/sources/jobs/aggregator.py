"""Sitemap + schema.org `JobPosting` reader — §2.1/§2.3.

Generic on purpose. Any board that publishes a sitemap and marks its postings
up with `JobPosting` JSON-LD works here, which is most of them: it is what
Google for Jobs requires, so boards that want to be found publish it.

**The compliance path, in order:**

1. `robots.txt` decides. It is fetched, parsed with `urllib.robotparser` and
   applied to every URL — enforced in code, not documented (§2 acceptance).
2. The **sitemap** is the entry point, so discovering 2,700 postings costs one
   request instead of a crawl. `lastmod` then decides which detail pages are
   worth fetching at all, capped by `jobs.max_detail_fetches`.
3. A site that refuses this client gets recorded and left alone.

### motorsportjobs.com, checked on 30 Aug 2026

The obvious primary source for this niche, and it is configured — but
**disabled**. Its `robots.txt` is a stock Drupal one that allows `/job/` pages,
and it publishes `/sitemap.xml` with 2,736 job URLs carrying `lastmod`, and
schema.org `JobPosting` JSON-LD on each page. All good.

Its edge, however, answers **403 to every honest client** — `PitWall/2.0`,
`python-httpx/0.28`, anything identifying itself — while letting `curl/8.0` and
browsers through. Getting data out therefore means pretending to be something
this is not, and §2.1 is explicit: *drop the adapter rather than working around
it*. So the adapter is real, generic and tested, and this particular source
ships off with the reason recorded next to it in `config/job_sources.yaml`.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

from ...config import Settings, get_settings
from .base import JobPosting, PoliteClient, SourceError, iso_date, strip_html

log = logging.getLogger(__name__)

_LD_JSON_RE = re.compile(
    r"<script[^>]*type\s*=\s*[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.S | re.I,
)
_LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.S | re.I)
_URL_BLOCK_RE = re.compile(r"<url>(.*?)</url>", re.S | re.I)
_LASTMOD_RE = re.compile(r"<lastmod>\s*(.*?)\s*</lastmod>", re.S | re.I)


def parse_sitemap(xml: str) -> list[tuple[str, str | None]]:
    """`[(url, lastmod|None), …]`, from a urlset or a sitemapindex."""
    out: list[tuple[str, str | None]] = []
    blocks = _URL_BLOCK_RE.findall(xml)
    if blocks:
        for block in blocks:
            loc = _LOC_RE.search(block)
            if not loc:
                continue
            mod = _LASTMOD_RE.search(block)
            out.append((loc.group(1), mod.group(1) if mod else None))
        return out
    return [(loc, None) for loc in _LOC_RE.findall(xml)]


def _walk(node: Any) -> list[dict[str, Any]]:
    """Every JobPosting object in a JSON-LD document, however it is nested."""
    found: list[dict[str, Any]] = []
    if isinstance(node, list):
        for item in node:
            found.extend(_walk(item))
    elif isinstance(node, dict):
        types = node.get("@type")
        types = types if isinstance(types, list) else [types]
        if any(str(t).lower() == "jobposting" for t in types if t):
            found.append(node)
        for value in node.values():
            if isinstance(value, list | dict):
                found.extend(_walk(value))
    return found


def parse_job_postings(html: str, url: str) -> list[JobPosting]:
    """Pull every `JobPosting` out of a page's JSON-LD. Pure — fixture-tested."""
    out: list[JobPosting] = []
    for block in _LD_JSON_RE.findall(html):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        for node in _walk(data):
            title = node.get("title") or node.get("name")
            if not title:
                continue
            org = node.get("hiringOrganization") or {}
            company = org.get("name") if isinstance(org, dict) else str(org)
            out.append(JobPosting(
                title=str(title).strip(),
                url=str(node.get("url") or url),
                external_id=str(node.get("identifier") or "")[:120] or None,
                description=strip_html(node.get("description")),
                location=_location(node.get("jobLocation")),
                employment_type=_scalar(node.get("employmentType")),
                posted_at=iso_date(node.get("datePosted")),
                company_name=company,
                raw=node,
            ))
    return out


def _scalar(value: Any) -> str | None:
    if isinstance(value, list):
        value = value[0] if value else None
    return str(value) if value else None


def _location(node: Any) -> str | None:
    """schema.org nests this three deep and inconsistently."""
    if isinstance(node, list):
        parts = [_location(n) for n in node]
        return " / ".join(p for p in parts if p) or None
    if isinstance(node, str):
        return node or None
    if not isinstance(node, dict):
        return None
    address = node.get("address")
    if isinstance(address, str):
        return address or None
    if isinstance(address, dict):
        parts = [address.get("addressLocality"), address.get("addressRegion"),
                 address.get("addressCountry")]
        parts = [p.get("name") if isinstance(p, dict) else p for p in parts]
        return ", ".join(str(p) for p in parts if p) or None
    return node.get("name") or None


class AggregatorSource:
    """A job board read through its sitemap and its JSON-LD."""

    kind = "aggregator"
    #: Reads the newest `max_details` pages only, so a posting it did not return
    #: this time may simply be older than the cap — not gone.
    complete_listing = False

    def __init__(
        self,
        name: str,
        sitemap_url: str,
        *,
        url_contains: str = "/job",
        settings: Settings | None = None,
        client: PoliteClient | None = None,
        max_details: int | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.name = name
        self.sitemap_url = sitemap_url
        self.url_contains = url_contains
        self.client = client or PoliteClient(self.settings)
        self.max_details = (
            self.settings.jobs_max_detail_fetches if max_details is None else max_details
        )
        self.base_url = f"{urlparse(sitemap_url).scheme}://{urlparse(sitemap_url).netloc}"

    def job_urls(self, *, no_cache: bool = False) -> list[tuple[str, str | None]]:
        xml = self.client.get(self.sitemap_url, no_cache=no_cache, accept="application/xml")
        entries = parse_sitemap(xml)
        jobs = [(u, m) for u, m in entries if self.url_contains in u]
        # Newest first, so a capped run spends its budget on what changed.
        jobs.sort(key=lambda pair: pair[1] or "", reverse=True)
        log.info("%s: %d job URL(s) of %d in the sitemap", self.name, len(jobs), len(entries))
        return jobs

    def fetch(self, *, no_cache: bool = False) -> list[JobPosting]:
        urls = self.job_urls(no_cache=no_cache)[: self.max_details]
        out: list[JobPosting] = []
        failures = 0
        for url, lastmod in urls:
            try:
                html = self.client.get(url, no_cache=no_cache, accept="text/html")
            except SourceError as exc:
                failures += 1
                log.debug("%s: %s", self.name, exc)
                # A whole board behind a WAF should fail as a board, not as 40
                # separate pages, so give up once it is clearly not us.
                if failures >= 3 and not out:
                    raise SourceError(
                        f"{self.name}: the first {failures} pages all failed — {exc}"
                    ) from exc
                continue
            for posting in parse_job_postings(html, url):
                out.append(
                    posting if posting.posted_at or not lastmod
                    else JobPosting(**{**posting.__dict__, "posted_at": iso_date(lastmod)})
                )
        log.info("%s: %d posting(s) from %d page(s)", self.name, len(out), len(urls))
        return out
