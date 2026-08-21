"""Serper.dev provider — Phase 2 of PITWALL_PLAN.md.

POST `https://google.serper.dev/search` with `X-API-KEY`, body
`{"q": query, "num": 20, "gl": "gb", "hl": "en"}`, parse `organic[]`.
Full Google syntax, so operator templates stay enabled. Credits consumed are
tracked in the `quota` table and shown on the Setup page (§13.2).
"""

from __future__ import annotations

import logging

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..config import Settings, get_settings
from ..ratelimit import get_limiter
from .base import ProviderError, QuotaExhausted, RateLimited, SearchHit

log = logging.getLogger(__name__)

ENDPOINT = "https://google.serper.dev/search"

#: `uk-en` style regions -> Serper's (gl, hl) pair.
_REGION_TO_GL = {
    "uk-en": ("gb", "en"),
    "us-en": ("us", "en"),
    "de-de": ("de", "de"),
    "fr-fr": ("fr", "fr"),
    "it-it": ("it", "it"),
    "wt-wt": ("gb", "en"),
}


class SerperProvider:
    name = "serper"

    def __init__(self, settings: Settings | None = None, api_key: str | None = None) -> None:
        self.settings = settings or get_settings()
        self.api_key = api_key if api_key is not None else self.settings.serper_api_key
        self.limiter = get_limiter("serper", self.settings.serper_queries_per_second)
        self._credits: int | None = None

    def supports_operators(self) -> bool:
        return True

    def remaining_quota(self) -> int | None:
        return self._credits

    def set_quota(self, credits: int | None) -> None:
        self._credits = credits

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        retry=retry_if_exception_type((RateLimited, httpx.TransportError)),
        reraise=True,
    )
    def search(
        self, query: str, *, max_results: int = 20, region: str = "uk-en"
    ) -> list[SearchHit]:
        if not self.api_key:
            raise ProviderError("SERPER_API_KEY is not set")
        if self._credits is not None and self._credits <= 0:
            raise QuotaExhausted("serper credits exhausted")

        gl, hl = _REGION_TO_GL.get(region, ("gb", "en"))
        self.limiter.acquire()
        try:
            resp = httpx.post(
                ENDPOINT,
                headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
                json={"q": query, "num": min(int(max_results), 100), "gl": gl, "hl": hl},
                timeout=30.0,
            )
        except httpx.TransportError as exc:
            raise RateLimited(f"serper transport error: {exc}") from exc

        if resp.status_code == 429:
            raise RateLimited("serper 429")
        if resp.status_code in (401, 403):
            raise ProviderError(f"serper auth failed ({resp.status_code}) — check SERPER_API_KEY")
        if resp.status_code == 402:
            self._credits = 0
            raise QuotaExhausted("serper: payment required / out of credits")
        if resp.status_code >= 400:
            raise ProviderError(f"serper HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        if self._credits is not None:
            self._credits = max(0, self._credits - int(data.get("credits", 1) or 1))

        hits: list[SearchHit] = []
        for i, row in enumerate(data.get("organic", []) or []):
            url = row.get("link") or ""
            if not url:
                continue
            hits.append(
                SearchHit(
                    rank=int(row.get("position", i)),
                    url=url,
                    title=row.get("title") or "",
                    snippet=row.get("snippet") or "",
                    raw=row,
                )
            )
        return hits
