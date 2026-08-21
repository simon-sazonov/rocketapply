"""Self-hosted SearXNG provider — Phase 10 of PITWALL_PLAN.md.

The permanent escape hatch from DDG's rate limiting (risk #1 in §15): free,
unlimited, and aggregating Google/Bing/Brave through your own instance. Point
`SEARXNG_BASE_URL` at it and put `searxng` first in `SEARCH_PROVIDERS`.

The instance must have the JSON format enabled::

    # settings.yml
    search:
      formats: [html, json]
"""

from __future__ import annotations

import logging

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..config import Settings, get_settings
from ..ratelimit import get_limiter
from .base import ProviderError, RateLimited, SearchHit

log = logging.getLogger(__name__)

_REGION_TO_LANG = {
    "uk-en": "en-GB",
    "us-en": "en-US",
    "de-de": "de-DE",
    "fr-fr": "fr-FR",
    "it-it": "it-IT",
    "wt-wt": "all",
}


class SearxngProvider:
    name = "searxng"

    def __init__(self, settings: Settings | None = None, base_url: str | None = None) -> None:
        self.settings = settings or get_settings()
        self.base_url = (base_url or self.settings.searxng_base_url).rstrip("/")
        self.limiter = get_limiter("searxng", self.settings.searxng_queries_per_second)

    def supports_operators(self) -> bool:
        return True  # depends on the upstream engines, but site:/quotes pass through

    def remaining_quota(self) -> int | None:
        return None

    def is_reachable(self, timeout: float = 3.0) -> bool:
        try:
            return httpx.get(f"{self.base_url}/healthz", timeout=timeout).status_code < 500
        except httpx.HTTPError:
            try:
                return httpx.get(self.base_url, timeout=timeout).status_code < 500
            except httpx.HTTPError:
                return False

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception_type((RateLimited, httpx.TransportError)),
        reraise=True,
    )
    def search(
        self, query: str, *, max_results: int = 20, region: str = "uk-en"
    ) -> list[SearchHit]:
        self.limiter.acquire()
        params: dict[str, str] = {
            "q": query,
            "format": "json",
            "language": _REGION_TO_LANG.get(region, "en-GB"),
            "safesearch": "0",
            "categories": "general",
        }
        try:
            resp = httpx.get(f"{self.base_url}/search", params=params, timeout=30.0)
        except httpx.TransportError as exc:
            raise RateLimited(f"searxng transport error: {exc}") from exc
        if resp.status_code == 429:
            raise RateLimited("searxng 429")
        if resp.status_code >= 400:
            raise ProviderError(
                f"searxng HTTP {resp.status_code} — is the JSON format enabled? {resp.text[:150]}"
            )
        data = resp.json()
        hits: list[SearchHit] = []
        for i, row in enumerate(data.get("results", []) or []):
            url = row.get("url") or ""
            if not url:
                continue
            hits.append(
                SearchHit(
                    rank=i,
                    url=url,
                    title=row.get("title") or "",
                    snippet=row.get("content") or "",
                    raw=row,
                )
            )
            if len(hits) >= max_results:
                break
        return hits
