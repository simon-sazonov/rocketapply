"""DuckDuckGo / metasearch provider — Phase 2 of PITWALL_PLAN.md.

Free forever, keyless, and aggressively rate-limited. The exponential backoff
plus the SERP cache absorb the throttling; the polite 1-query-per-4-seconds
limiter is what keeps the free tier usable at all (§1.3 rule 6).

`supports_operators()` returns **False**: `ddgs` honours `site:` reasonably but
handles quoted multi-word phrases inconsistently, so the query builder drops
operator-dependent templates and the heuristic layer does exact-phrase matching
client-side instead (§6.2).
"""

from __future__ import annotations

import logging
from typing import Any

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..config import Settings, get_settings
from ..ratelimit import get_limiter
from .base import RateLimited, SearchHit

log = logging.getLogger(__name__)

#: DDG region codes differ from the `wt-wt` default used elsewhere.
_REGION_FALLBACK = "wt-wt"


class DDGSProvider:
    name = "ddgs"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.limiter = get_limiter("ddgs", self.settings.ddgs_queries_per_second)

    def supports_operators(self) -> bool:
        return False  # partial only

    def remaining_quota(self) -> int | None:
        return None  # unlimited, but throttled

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=8, min=8, max=120),
        retry=retry_if_exception_type(Exception),
        reraise=True,
    )
    def search(
        self, query: str, *, max_results: int = 20, region: str = _REGION_FALLBACK
    ) -> list[SearchHit]:
        from ddgs import DDGS  # imported lazily so tests need no network stack

        self.limiter.acquire()
        try:
            rows: list[dict[str, Any]] = (
                DDGS().text(query, max_results=max_results, region=region or _REGION_FALLBACK)
                or []
            )
        except Exception as exc:  # noqa: BLE001 - ddgs raises many shapes
            text = str(exc).lower()
            if any(k in text for k in ("ratelimit", "rate limit", "202", "429", "too many")):
                log.warning("ddgs rate-limited on %r", query[:80])
                raise RateLimited(str(exc)) from exc
            raise

        return [
            SearchHit(
                rank=i,
                url=r.get("href") or r.get("url") or "",
                title=r.get("title") or "",
                snippet=r.get("body") or r.get("description") or "",
                raw=r,
            )
            for i, r in enumerate(rows)
            if (r.get("href") or r.get("url"))
        ]
