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
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from ...config import Settings, get_settings
from .base import JobPosting, PoliteClient, SourceError, iso_date, strip_html

log = logging.getLogger(__name__)

ENDPOINT = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"

#: Adzuna's country codes are ISO 3166 alpha-2 in lower case, except that the
#: UK is `gb`. Countries not listed here have no Adzuna site.
SUPPORTED_COUNTRIES = frozenset({
    "at", "au", "be", "br", "ca", "ch", "de", "es", "fr", "gb", "in", "it",
    "mx", "nl", "nz", "pl", "sg", "us", "za",
})


def parse(data: Any, country: str | None = None) -> list[JobPosting]:
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


class AdzunaSource:
    """`what × country` searches from configuration, within a request budget."""

    kind = "aggregator_api"
    complete_listing = False

    def __init__(
        self,
        app_id: str,
        app_key: str,
        *,
        queries: list[str],
        countries: list[str],
        max_requests: int = 40,
        results_per_page: int = 50,
        max_days_old: int = 14,
        title_only: bool = True,
        budget: Callable[[], bool] | None = None,
        on_request: Callable[[], None] | None = None,
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
        self.queries = [q.strip() for q in queries if q and q.strip()]
        codes = [adzuna_country(c) for c in countries]
        self.countries = list(dict.fromkeys(c for c in codes if c))
        #: What the configured list asked for that Adzuna does not cover.
        self.unsupported = sorted({c.upper() for c in countries if not adzuna_country(c)})
        self.max_requests = max(0, int(max_requests))
        self.results_per_page = max(1, min(50, int(results_per_page)))
        self.max_days_old = max(1, int(max_days_old))
        self.title_only = title_only
        #: Asked before every request; False stops the run (the daily quota).
        self.budget = budget
        #: Called after every request that went to the network.
        self.on_request = on_request
        self.client = client or PoliteClient(self.settings)
        self.requests_made = 0

    def plan(self) -> list[tuple[str, str]]:
        """Every (query, country) pair, interleaved so a capped run covers every
        country with the first queries before any country gets the last ones."""
        pairs = [(q, c) for q in self.queries for c in self.countries]
        return pairs[: self.max_requests]

    def request_url(self, query: str, country: str, page: int = 1) -> str:
        params: dict[str, Any] = {
            "app_id": self.app_id,
            "app_key": self.app_key,
            "results_per_page": self.results_per_page,
            "max_days_old": self.max_days_old,
            "sort_by": "date",
            "content-type": "application/json",
        }
        params["title_only" if self.title_only else "what"] = query
        return ENDPOINT.format(country=country, page=page) + "?" + urlencode(params)

    def redacted_url(self, query: str, country: str, page: int = 1) -> str:
        """The same request with the credentials removed — the cache key, and
        the only form that may appear in a log or a health row."""
        field = "title_only" if self.title_only else "what"
        return (ENDPOINT.format(country=country, page=page) + "?" + urlencode({
            field: query, "max_days_old": self.max_days_old,
            "results_per_page": self.results_per_page,
        }))

    def redact(self, text: str) -> str:
        return text.replace(self.app_key, "***").replace(self.app_id, "***")

    def fetch(self, *, no_cache: bool = False) -> list[JobPosting]:
        out: list[JobPosting] = []
        failures: list[str] = []
        planned = self.plan()
        for query, country in planned:
            if self.budget is not None and not self.budget():
                log.info("adzuna: daily request budget reached after %d request(s)",
                         self.requests_made)
                break
            safe = self.redacted_url(query, country)
            try:
                hits_before = self.client.cache.hits
                data = self.client.get_json(
                    self.request_url(query, country), no_cache=no_cache, cache_key=safe,
                )
                if self.client.cache.hits == hits_before:
                    self.requests_made += 1
                    if self.on_request:
                        self.on_request()
            except SourceError as exc:
                failures.append(self.redact(str(exc)))
                # Bad credentials fail every request identically; say so once.
                if len(failures) >= 3 and not out:
                    raise SourceError(
                        f"adzuna: the first {len(failures)} searches all failed — "
                        f"{failures[-1]}"
                    ) from None
                continue
            out.extend(parse(data, country))
        log.info("adzuna: %d posting(s) from %d search(es), %d failed",
                 len(out), len(planned), len(failures))
        return out
