"""Self-hosted SearXNG provider — §0.1 of PITWALL_V2_IMPLEMENTATION.md.

The permanent escape hatch from DDG's rate limiting (risk #1 in §15 of the plan)
and, more importantly, **the provider that supports operators**. `ddgs` reports
``supports_operators() == False``, so the query builder drops 8 of the 12
templates in `config/query_templates.yaml`; SearXNG passes `site:` and quoted
phrases straight through to the upstream engines, which is what takes coverage
from 7 companies to 49.

Start it with ``make searxng`` — see `docker/searxng/`. The instance must have
the JSON format enabled, or every request answers 403 with an HTML body::

    # settings.yml
    search:
      formats: [html, json]

Three behaviours matter here, and all three exist because "unlimited" applies to
the *container*, not to the upstream engines it proxies:

1. **The token bucket stays on.** `searxng_queries_per_second` defaults to 1.0.
   Sweeping Google and Bing through a local proxy gets you captchas just as fast
   as sweeping them directly.
2. **`unresponsive_engines` is a soft failure, never "zero results".** SearXNG
   answers HTTP 200 with an empty `results` list when its engines are blocked,
   and reports why in `unresponsive_engines`. Treating that as "nobody matches
   this query" would silently poison the dataset — a captcha would look exactly
   like an empty company. It raises :class:`RateLimited` instead, so tenacity
   backs off and the chain can fall through.
3. **Engine rotation.** `SEARXNG_ENGINES` holds `;`-separated groups; each query
   uses the next group, so no single upstream engine takes the whole sweep.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time

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

#: How long a reachability probe is trusted, so the Streamlit pages that call
#: `build_chain()` on every rerun do not pay a connect timeout each time.
REACHABILITY_TTL_SECONDS = 60.0

_reach_cache: dict[str, tuple[float, bool]] = {}
_reach_lock = threading.Lock()


def _engine_names(unresponsive: list) -> str:
    """`[["google", "CAPTCHA", true], …]` -> `"bing, google"`."""
    names = {
        str(row[0]) if isinstance(row, list | tuple) and row else str(row)
        for row in unresponsive
    }
    return ", ".join(sorted(names))


def reset_reachability_cache() -> None:
    """Test helper — forget every memoised probe."""
    with _reach_lock:
        _reach_cache.clear()


class SearxngProvider:
    name = "searxng"

    def __init__(self, settings: Settings | None = None, base_url: str | None = None) -> None:
        self.settings = settings or get_settings()
        self.base_url = (base_url or self.settings.searxng_base_url).rstrip("/")
        self.limiter = get_limiter("searxng", self.settings.searxng_queries_per_second)
        self.engine_groups = list(self.settings.searxng_engines)
        # itertools.cycle is not thread-safe; the lock is what makes rotation
        # correct once Phase 1 puts N workers on this one provider instance.
        self._cycle = itertools.cycle(self.engine_groups) if self.engine_groups else None
        self._cycle_lock = threading.Lock()

    def supports_operators(self) -> bool:
        return True  # depends on the upstream engines, but site:/quotes pass through

    def remaining_quota(self) -> int | None:
        return None

    # ─────────────────────────── health ────────────────────────────

    def is_reachable(self, timeout: float = 3.0, *, use_cache: bool = True) -> bool:
        """Is the container up? Memoised for `REACHABILITY_TTL_SECONDS`."""
        now = time.monotonic()
        if use_cache:
            with _reach_lock:
                cached = _reach_cache.get(self.base_url)
            if cached and now - cached[0] < REACHABILITY_TTL_SECONDS:
                return cached[1]
        ok = self._probe(timeout)
        with _reach_lock:
            _reach_cache[self.base_url] = (now, ok)
        return ok

    def _probe(self, timeout: float) -> bool:
        for path in ("/healthz", ""):
            try:
                return httpx.get(f"{self.base_url}{path}", timeout=timeout).status_code < 500
            except httpx.HTTPError:
                continue
        return False

    # ─────────────────────────── rotation ──────────────────────────

    def next_engine_group(self) -> str | None:
        """The `engines=` value for the next query, or None to use the instance
        default set."""
        if self._cycle is None:
            return None
        with self._cycle_lock:
            return next(self._cycle)

    # ──────────────────────────── search ───────────────────────────

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception_type((RateLimited, httpx.TransportError)),
        reraise=True,
    )
    def search(
        self, query: str, *, max_results: int = 20, region: str = "uk-en"
    ) -> list[SearchHit]:
        engines = self.next_engine_group()
        hits, dead = self._request(query, max_results=max_results, region=region, engines=engines)
        if hits:
            return hits

        # Zero results on a narrowed engine set means one of two things and we
        # cannot tell which from here: the group is captcha'd, or it is not
        # enabled on this instance. Either way the answer is the same — widen to
        # whatever the instance itself considers its general engines and ask
        # once more, before drawing any conclusion about the query.
        if engines:
            log.info(
                "searxng: 0 results on engines=%s (%d unresponsive) — widening to the "
                "instance defaults", engines, len(dead),
            )
            hits, dead = self._request(query, max_results=max_results, region=region, engines=None)
            if hits:
                return hits

        # Still nothing, and the engines said why: blocked upstream, not "no
        # such people". Raising is what makes tenacity back off and lets the
        # chain fall through; returning [] would write a false negative into the
        # database that is indistinguishable from a company nobody works for.
        if dead:
            names = _engine_names(dead)
            log.warning("searxng: no results and %d unresponsive engine(s): %s", len(dead), names)
            raise RateLimited(f"searxng upstream engines unresponsive: {names}")

        return hits

    def _request(
        self, query: str, *, max_results: int, region: str, engines: str | None
    ) -> tuple[list[SearchHit], list]:
        """One HTTP call. Returns (hits, unresponsive_engines)."""
        self.limiter.acquire()
        params: dict[str, str] = {
            "q": query,
            "format": "json",
            "language": _REGION_TO_LANG.get(region, "en-GB"),
            "safesearch": "0",
        }
        # `engines` and `categories` are a UNION, not an intersection
        # (searx/webadapter.py::parse_generic). Sending both would quietly run
        # every general engine on every query and make the rotation a no-op —
        # which is exactly what it did until this was measured against a live
        # container. Send one or the other, never both.
        if engines:
            params["engines"] = engines
        else:
            params["categories"] = "general"
        try:
            resp = httpx.get(
                f"{self.base_url}/search",
                params=params,
                timeout=self.settings.searxng_timeout_seconds,
            )
        except httpx.TransportError as exc:
            raise RateLimited(f"searxng transport error: {exc}") from exc
        if resp.status_code == 429:
            raise RateLimited("searxng 429")
        if resp.status_code >= 400:
            raise ProviderError(
                f"searxng HTTP {resp.status_code} — is the JSON format enabled? "
                f"(search.formats must list `json`) {resp.text[:150]}"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderError(
                "searxng returned a non-JSON body — `json` is missing from "
                f"search.formats in settings.yml: {resp.text[:150]}"
            ) from exc

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
        return hits, list(data.get("unresponsive_engines") or [])
