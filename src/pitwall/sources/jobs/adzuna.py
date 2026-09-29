"""Adzuna's search API — role-first discovery across countries (§7.1, §57).

    GET https://api.adzuna.com/v1/api/jobs/{country}/search/{page}
        ?app_id=…&app_key=…&what=…&title_only=…&max_days_old=…
         &results_per_page=…&sort_by=date&content-type=application/json

Free, but keyed: `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` in `.env`, from a free
account at developer.adzuna.com. Without them the source is simply not built,
and Sources & health says so rather than failing a run.

Three properties shape how the rest of the system treats it:

* **Descriptions are snippets.** Adzuna documents that it returns only part of
  the posting text, so every posting is `description_truncated=True`. "3+ years"
  can easily sit in the part that was cut, which is exactly why an unknown
  experience requirement never rejects a job.
* **`redirect_url` is Adzuna's landing page**, not the employer's. It is the
  least direct source there is, so when the same job arrives from an ATS the
  ATS link wins (dedupe's source ranking).
* **It is a search, not a listing.** A job this run's queries did not return is
  not closed; it ages out after `jobs.close_after_days` like any other partial
  source. `complete_listing = False`.

The credentials travel in the query string, so the URL is never used as a cache
key or put into an error message: both use `redacted_url`.

**robots.txt.** `api.adzuna.com/robots.txt` is `Disallow: /`. Honouring it
meant this source stored nothing, ever. The API is used under a key and
Adzuna's terms, which permit personal research, so it is `terms_govern`: the
terms are the agreement, and the robots file is addressed to crawlers. Its
published rate limit (25 a minute) is enforced as a floor on the request gap.

**Searching in a rotation.** One visit to a (query, country) search asks only
for what is newer than the previous complete visit (`max_days_old` = days since
then, plus one). A search that has never been read is read back
`backfill_days`, page by page. So when the day's budget cannot cover every
search, the ones left out lose nothing: the next visit covers their gap.
Searches never read come first, then the longest unvisited.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from ...config import Settings, get_settings
from .base import JobPosting, PoliteClient, RateLimited, SourceError, iso_date, strip_html
from .search import MIN_PAGE_PRECISION, SearchLedger, Visit, title_matches

__all__ = ["AdzunaSource", "Visit", "parse"]

log = logging.getLogger(__name__)

ENDPOINT = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"

#: 25 requests a minute is Adzuna's published limit; 2.5 s keeps under it.
MIN_GAP_SECONDS = 2.5

#: Adzuna's country codes are ISO 3166 alpha-2 in lower case, except that the
#: UK is `gb`. Countries not listed here have no Adzuna site.
SUPPORTED_COUNTRIES = frozenset({
    "at", "au", "be", "br", "ca", "ch", "de", "es", "fr", "gb", "in", "it",
    "mx", "nl", "nz", "pl", "sg", "us", "za",
})


def parse(data: Any, country: str | None = None, query: str | None = None) -> list[JobPosting]:
    """One search response, normalised. Pure — fixture-tested."""
    out: list[JobPosting] = []
    for job in (data or {}).get("results", []) or []:
        # Titles come back with the matched words wrapped in <strong>.
        title = strip_html(job.get("title"))
        url = job.get("redirect_url")
        if not title or not url:
            continue
        company = (job.get("company") or {}).get("display_name")
        location = job.get("location") or {}
        area = [a for a in location.get("area") or [] if a]
        # `area` runs country -> region -> city; the display name is usually
        # "City, Region" and loses the country, which the search already fixes.
        display = location.get("display_name") or ", ".join(reversed(area)) or None
        predicted = str(job.get("salary_is_predicted") or "0") == "1"
        contract = [job.get("contract_time"), job.get("contract_type")]
        out.append(JobPosting(
            title=title,
            url=str(url),
            external_id=str(job.get("id") or "") or None,
            description=strip_html(job.get("description")),
            location=display,
            employment_type=", ".join(str(c) for c in contract if c) or None,
            posted_at=iso_date(job.get("created")),
            company_name=company or None,
            # A predicted salary is Adzuna's estimate, not the employer's figure.
            salary_min=None if predicted else _num(job.get("salary_min")),
            salary_max=None if predicted else _num(job.get("salary_max")),
            country_hint=country.upper() if country else None,
            description_truncated=True,
            found_query=query,
            raw=job,
        ))
    return out


def _num(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def adzuna_country(iso2: str) -> str | None:
    code = (iso2 or "").strip().lower()
    code = "gb" if code == "uk" else code
    return code if code in SUPPORTED_COUNTRIES else None


class AdzunaSource(SearchLedger):
    """`what × country` searches in a rotation, within this run's allowance."""

    kind = "aggregator_api"
    complete_listing = False
    terms_govern = True
    provider = "adzuna"

    def __init__(
        self,
        app_id: str,
        app_key: str,
        *,
        queries: list[str],
        countries: list[str],
        max_requests: int | None = 40,
        results_per_page: int = 50,
        backfill_days: int = 30,
        max_pages_per_search: int = 20,
        title_only: bool = True,
        cursors: dict[tuple[str, str], dict[str, Any]] | None = None,
        now: datetime | None = None,
        settings: Settings | None = None,
        client: PoliteClient | None = None,
    ) -> None:
        if not app_id or not app_key:
            raise ValueError("Adzuna needs ADZUNA_APP_ID and ADZUNA_APP_KEY in .env")
        self.settings = settings or get_settings()
        self.name = "adzuna"
        self.url = "https://api.adzuna.com"
        self.app_id = app_id
        self.app_key = app_key
        codes = [adzuna_country(c) for c in countries]
        #: What the configured list asked for that Adzuna does not cover.
        self.unsupported = sorted({c.upper() for c in countries if not adzuna_country(c)})
        # Queries are lower-cased: Adzuna's search ignores case, and the cursor
        # table is keyed on the text, so "AI engineer" and "ai engineer" are one.
        self._init_ledger(queries=queries, countries=[c for c in codes if c], cursors=cursors,
                          max_requests=max_requests, backfill_days=backfill_days, now=now)
        self.results_per_page = max(1, min(50, int(results_per_page)))
        self.max_pages = max(1, int(max_pages_per_search))
        self.title_only = title_only
        self.client = client or PoliteClient(self.settings)
        if hasattr(self.client, "set_min_delay"):
            self.client.set_min_delay(self.url, MIN_GAP_SECONDS)

    # ── requests ──

    def request_url(self, query: str, country: str, page: int = 1,
                    days: int | None = None) -> str:
        params: dict[str, Any] = {
            "app_id": self.app_id,
            "app_key": self.app_key,
            "results_per_page": self.results_per_page,
            "max_days_old": days or self.backfill_days,
            "sort_by": "date",
            "content-type": "application/json",
        }
        params["title_only" if self.title_only else "what"] = query
        return ENDPOINT.format(country=country, page=page) + "?" + urlencode(params)

    def redacted_url(self, query: str, country: str, page: int = 1,
                     days: int | None = None) -> str:
        """The same request with the credentials removed — the cache key, and
        the only form that may appear in a log or a health row."""
        field = "title_only" if self.title_only else "what"
        return (ENDPOINT.format(country=country, page=page) + "?" + urlencode({
            field: query, "max_days_old": days or self.backfill_days,
            "results_per_page": self.results_per_page,
        }))

    def redact(self, text: str) -> str:
        return text.replace(self.app_key, "***").replace(self.app_id, "***")

    def fetch(self, *, no_cache: bool = False) -> list[JobPosting]:
        out: list[JobPosting] = []
        failures: list[str] = []
        planned = self.plan()
        stopped = ""
        for query, country in planned:
            if not self.left():
                stopped = "this run's allowance is spent"
                break
            days = self.days_for(query, country)
            visit = self.open_visit(query, country)
            page = 1
            while self.left():
                safe = self.redacted_url(query, country, page, days)
                try:
                    data = self.client.get_json(
                        self.request_url(query, country, page, days), no_cache=no_cache,
                        cache_key=safe, terms_govern=True,
                    )
                except RateLimited as exc:
                    visit.requests += self.counted(self.client)
                    stopped = self.redact(str(exc))
                    break
                except SourceError as exc:
                    visit.requests += self.counted(self.client)
                    failures.append(self.redact(str(exc)))
                    # Bad credentials fail every request identically; say so once.
                    if len(failures) >= 3 and not out:
                        raise SourceError(
                            f"adzuna: the first {len(failures)} searches all failed — "
                            f"{failures[-1]}"
                        ) from None
                    break
                visit.requests += self.counted(self.client)
                rows = parse(data, country, query)
                # A title search should return titles with every word; where the
                # site's analyser dropped one (Adzuna Italy and "ai"), keep only
                # those, and stop paging a search that has gone wrong.
                batch = [p for p in rows if not self.title_only or title_matches(p.title, query)]
                out.extend(batch)
                visit.found += len(batch)
                total = (data or {}).get("count") if isinstance(data, dict) else None
                visit.total = int(total) if isinstance(total, int | float) else visit.total
                last_page = (len(rows) < self.results_per_page
                             or (visit.total is not None
                                 and page * self.results_per_page >= visit.total))
                noisy = bool(rows) and len(batch) < MIN_PAGE_PRECISION * len(rows)
                if last_page or noisy or page >= self.max_pages:
                    visit.complete = True
                    break
                page += 1
            if stopped:
                break
        log.info("adzuna: %d posting(s) from %d search(es), %d request(s), %d failed%s",
                 len(out), len(self.visits), self.requests_made, len(failures),
                 f" — stopped: {stopped}" if stopped else "")
        return out
