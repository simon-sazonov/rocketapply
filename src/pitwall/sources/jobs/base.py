"""The job-source contract — §2.3 of PITWALL_V2_IMPLEMENTATION.md.

Mirrors the shape of `pitwall.search`: a protocol here, one module per adapter,
and a shared cache. Three rules the whole package is built around:

1. **A failing adapter is non-fatal.** It records `last_status` on `job_sources`
   and the run continues. One dead career page must not cost you the postings
   every other source found.
2. **`robots.txt` is enforced in code, not documented in a README.** Every HTTP
   fetch goes through :class:`RobotsGate`, which fetches, caches and *parses*
   the file (RFC 9309, `robots.py`), and honours `Crawl-delay`. The one
   exception is an API used under a key and written terms that permit the use
   (`terms_govern=True`): those terms are the agreement, and a blanket
   `Disallow: /` on the API host is addressed to crawlers, not to key holders.
3. **Identify honestly.** The User-Agent says what this is. If a site refuses
   it, that is an answer: the adapter records the refusal and yields nothing.
   Retrying as a browser would be working around the answer, and §2.1 is
   explicit that the adapter gets dropped rather than worked around.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urljoin, urlparse

import httpx

from ...config import Settings, get_settings
from . import robots as robots_txt

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobPosting:
    """One normalised posting, provider-agnostic."""

    title: str
    url: str
    external_id: str | None = None
    description: str | None = None
    location: str | None = None
    employment_type: str | None = None
    posted_at: str | None = None
    company_name: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    # ── optional structure, filled only where a source states it ──
    #: True/False when the source says so outright; None = not stated.
    remote: bool | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    #: ISO 3166 alpha-2, when the source is country-scoped (an Adzuna search is).
    country_hint: str | None = None
    #: The source only returns a snippet (Adzuna). Downstream this lowers
    #: confidence rather than letting "not mentioned" read as "not required".
    description_truncated: bool = False
    #: The search that returned it, for a keyword source — query yield.
    found_query: str | None = None

    def __post_init__(self) -> None:
        if not self.title or not self.url:
            raise ValueError("a posting needs at least a title and a url")


class SourceError(RuntimeError):
    """This source could not be read. Recorded, never fatal."""


class RobotsDisallowed(SourceError):
    """The site's own robots.txt says no. Not something to route around."""


class RateLimited(SourceError):
    """HTTP 429. The source is fine; this client asked too often."""


class NotFound(SourceError):
    """The server answered 404: there is nothing at this address.

    Separate from the other failures because it is an answer, not an outage —
    a recorded ATS board that 404s is gone, while one that times out may not be.
    """


@runtime_checkable
class JobSource(Protocol):
    """Every adapter implements exactly this."""

    name: str
    kind: str          # ats_json | aggregator | career_page | job_board | aggregator_api

    def fetch(self, *, no_cache: bool = False) -> list[JobPosting]: ...


def returns_complete_listing(source: Any) -> bool:
    """Whether one fetch returns EVERY live posting this source has.

    Only then does "missing from this fetch" mean "closed". An ATS board lists
    everything; a keyword search, a capped sitemap or a paginated feed read to a
    page limit does not, and closing what they happened not to return would
    close half the table every run. Those sources age postings out instead
    (`JobRepo.close_stale`). Unknown adapters keep the old behaviour.
    """
    return bool(getattr(source, "complete_listing", True))


# ────────────────────────────── robots.txt ───────────────────────────────


class RobotsGate:
    """Fetches, caches and applies `robots.txt` per host.

    Cached in memory for the process; a run polls a handful of hosts, so this is
    one request each. A host whose robots.txt cannot be fetched is treated as
    **allowing** — that is what RFC 9309 says an unreachable file means, and
    treating it as a refusal would silently disable every source behind a flaky
    CDN.
    """

    def __init__(self, user_agent: str, timeout: float = 10.0) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self._cache: dict[str, robots_txt.Robots | None] = {}
        self._lock = threading.Lock()
        #: One lock per host, so two sources on the same host do not both
        #: fetch its robots.txt, and two hosts never wait for each other.
        self._root_locks: dict[str, threading.Lock] = {}

    def _parser(self, url: str) -> robots_txt.Robots | None:
        parts = urlparse(url)
        root = f"{parts.scheme}://{parts.netloc}"
        with self._lock:
            if root in self._cache:
                return self._cache[root]
            root_lock = self._root_locks.setdefault(root, threading.Lock())
        with root_lock:
            with self._lock:
                if root in self._cache:
                    return self._cache[root]
            parsed: robots_txt.Robots | None
            try:
                resp = httpx.get(
                    urljoin(root, "/robots.txt"),
                    timeout=self.timeout,
                    headers={"User-Agent": self.user_agent},
                    follow_redirects=True,
                )
                # Nothing published, or refused: allow (RFC 9309 §2.3.1.3).
                parsed = None if resp.status_code >= 400 else robots_txt.parse(resp.text)
            except httpx.HTTPError as exc:
                log.info("robots.txt unreachable for %s (%s) — treating as allowed", root, exc)
                parsed = None
            with self._lock:
                self._cache[root] = parsed
            return parsed

    def allows(self, url: str) -> bool:
        parsed = self._parser(url)
        return True if parsed is None else parsed.allows(self.user_agent, url)

    def crawl_delay(self, url: str, default: float = 1.0) -> float:
        parsed = self._parser(url)
        delay = parsed.crawl_delay(self.user_agent) if parsed is not None else None
        return float(delay) if delay else default

    def reset(self) -> None:
        with self._lock:
            self._cache.clear()


# ──────────────────────────────── cache ──────────────────────────────────


class JobCache:
    """A 7-day disk cache, same pattern as `search/cache.py`.

    Job boards change slowly and the same board is polled from several places
    (the Openings page, a CLI run, the ATS discovery script). Caching is what
    keeps that from being three requests.
    """

    def __init__(self, root: Path | str | None = None, ttl_days: int | None = None,
                 settings: Settings | None = None, *, ttl_hours: float | None = None) -> None:
        settings = settings or get_settings()
        self.root = Path(root) if root else settings.cache_path / "jobs"
        self.ttl_days = settings.cache_ttl_days if ttl_days is None else ttl_days
        #: Overrides `ttl_days` when set. A daily job search needs hours: with
        #: the 7-day default a board polled on Monday would show Monday's
        #: postings until the next Monday.
        self.ttl_hours = ttl_hours
        self.hits = 0
        self.misses = 0

    def _max_age(self) -> timedelta | None:
        if self.ttl_hours is not None:
            return timedelta(hours=self.ttl_hours) if self.ttl_hours > 0 else None
        return timedelta(days=self.ttl_days) if self.ttl_days > 0 else None

    def path_for(self, key: str) -> Path:
        digest = hashlib.sha1(key.encode()).hexdigest()
        return self.root / digest[:2] / f"{digest}.json"

    def get(self, key: str) -> Any | None:
        path = self.path_for(key)
        if not path.exists():
            self.misses += 1
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self.misses += 1
            return None
        fetched = data.get("fetched_at")
        max_age = self._max_age()
        if max_age is not None and fetched:
            try:
                ts = datetime.fromisoformat(fetched)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                if datetime.now(UTC) - ts > max_age:
                    self.misses += 1
                    return None
            except ValueError:
                self.misses += 1
                return None
        self.hits += 1
        return data.get("body")

    def put(self, key: str, body: Any) -> None:
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Unique per writer: sources now fetch in parallel, and two of them
        # sharing one temp name would interleave into a corrupt file.
        tmp = path.with_suffix(f".{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(
            json.dumps(
                {"fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
                 "key": key, "body": body},
                ensure_ascii=False, default=str,
            ),
            encoding="utf-8",
        )
        tmp.replace(path)

    def clear(self) -> int:
        n = 0
        if self.root.exists():
            for f in self.root.rglob("*.json"):
                f.unlink()
                n += 1
        return n


# ───────────────────────────── HTTP helper ───────────────────────────────


class PoliteClient:
    """robots-gated, rate-limited, cached HTTP. Every adapter goes through it.

    Shared between threads, and built for it: the job search fetches several
    sources at once. Politeness is per HOST. One request is in flight per host,
    and consecutive requests are spaced by the largest of the configured delay,
    the host's Crawl-delay and any floor an adapter sets for a published rate
    limit. Six sources on six hosts therefore run side by side, while two
    sources on one host still queue behind each other.

    The previous version slept while holding a single lock, so every source
    waited out every other source's delay and a run grew by the full time of
    each source added. Nothing here sleeps while holding a lock another host
    needs.
    """

    def __init__(self, settings: Settings | None = None, *,
                 cache: JobCache | None = None, robots: RobotsGate | None = None) -> None:
        self.settings = settings or get_settings()
        self.user_agent = self.settings.jobs_user_agent
        self.cache = cache if cache is not None else JobCache(settings=self.settings)
        self.robots = robots if robots is not None else RobotsGate(self.user_agent)
        self._last_request: dict[str, float] = {}
        self._lock = threading.Lock()
        self._host_locks: dict[str, threading.Lock] = {}
        self._min_delay: dict[str, float] = {}
        self._thread = threading.local()
        #: Requests that went to the network, per host. A cache hit costs
        #: nothing and is not counted.
        self.network_requests: dict[str, int] = {}

    # ── pacing ──

    def set_min_delay(self, host_or_url: str, seconds: float) -> None:
        """A floor on the gap between two requests to one host, for an API that
        publishes a rate limit instead of a Crawl-delay (Adzuna: 25 a minute)."""
        host = urlparse(host_or_url).netloc or host_or_url
        with self._lock:
            self._min_delay[host] = max(self._min_delay.get(host, 0.0), float(seconds))

    def last_was_network(self) -> bool:
        """Whether this thread's latest request went to the network, or tried
        to. False for a cache hit.

        Per thread on purpose: with sources running in parallel, "did the
        cache's hit counter move" can be moved by another source's thread, and
        a quota-counted source would then skip counting a request it really
        made.
        """
        return bool(getattr(self._thread, "network", False))

    def _host_lock(self, host: str) -> threading.Lock:
        with self._lock:
            return self._host_locks.setdefault(host, threading.Lock())

    def _delay(self, url: str, terms_govern: bool) -> float:
        host = urlparse(url).netloc
        base = self.settings.jobs_crawl_delay
        # Reading Crawl-delay may fetch robots.txt, so it happens before any
        # lock is taken. A terms-governed API is paced by its own floor.
        robots_delay = base if terms_govern else self.robots.crawl_delay(url, base)
        with self._lock:
            floor = self._min_delay.get(host, 0.0)
        return max(robots_delay, base, floor)

    def _fetch(self, method: str, url: str, *, key: str, accept: str, terms_govern: bool,
               redact: bool, body: Any = None, headers: dict[str, str] | None = None) -> str:
        host = urlparse(url).netloc
        delay = self._delay(url, terms_govern)
        with self._host_lock(host):
            last = self._last_request.get(host, 0.0)
            gap = delay - (time.monotonic() - last)
            if gap > 0:
                time.sleep(gap)
            self._last_request[host] = time.monotonic()
            self._thread.network = True
            with self._lock:
                self.network_requests[host] = self.network_requests.get(host, 0) + 1
            sent = {**(headers or {}), "User-Agent": self.user_agent, "Accept": accept}
            try:
                if method == "POST":
                    resp = httpx.post(url, json=body, timeout=self.settings.jobs_timeout_seconds,
                                      headers=sent, follow_redirects=True)
                else:
                    resp = httpx.get(url, timeout=self.settings.jobs_timeout_seconds,
                                     headers=sent, follow_redirects=True)
            except httpx.HTTPError as exc:
                # httpx's message repeats the URL, so it is left out whenever the
                # URL is the one thing that must not be shown.
                detail = type(exc).__name__ if redact else str(exc)
                raise SourceError(f"{key}: {detail}") from exc
        if resp.status_code == 404:
            raise NotFound(f"{key}: not found (404)")
        if resp.status_code == 403:
            raise SourceError(
                f"{key}: refused (403). The site declines this User-Agent "
                f"({self.user_agent!r}). Impersonating a browser to get past it would "
                "be working around the answer, so this source stays dark — see §2.1."
            )
        if resp.status_code == 429:
            raise RateLimited(f"{key}: rate limited (429)")
        if resp.status_code >= 400:
            raise SourceError(f"{key}: HTTP {resp.status_code}")
        self.cache.put(key, resp.text)
        return resp.text

    # ── the public calls ──

    def get(self, url: str, *, no_cache: bool = False, accept: str = "*/*",
            cache_key: str | None = None, terms_govern: bool = False,
            headers: dict[str, str] | None = None) -> str:
        """`cache_key` replaces the URL as the cache key — for URLs that carry a
        credential, which must not end up written into a cache file.

        `terms_govern=True` skips robots.txt. Only for an API used under a key
        and written terms that permit this use; see the module docstring.
        """
        key = cache_key or url
        self._thread.network = False
        if not terms_govern and not self.robots.allows(url):
            raise RobotsDisallowed(f"robots.txt disallows {key}")
        if not no_cache:
            cached = self.cache.get(key)
            if cached is not None:
                return str(cached)
        return self._fetch("GET", url, key=key, accept=accept, terms_govern=terms_govern,
                           redact=cache_key is not None, headers=headers)

    def get_json(self, url: str, *, no_cache: bool = False,
                 cache_key: str | None = None, terms_govern: bool = False,
                 headers: dict[str, str] | None = None) -> Any:
        """`headers` carry a credential some APIs want there (Findwork). The
        User-Agent is never overridable: it always says what this is."""
        extra = {"headers": headers} if headers else {}
        body = self.get(url, no_cache=no_cache, accept="application/json",
                        cache_key=cache_key, terms_govern=terms_govern, **extra)
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise SourceError(f"{cache_key or url}: response is not JSON ({exc})") from exc

    def post_json(self, url: str, body: Any, *, no_cache: bool = False,
                  cache_key: str | None = None, terms_govern: bool = False) -> Any:
        """A JSON search that is a POST (EURES, Jooble). Cached on URL + body.

        A POST here is a query, not a change: every caller is a read-only
        search endpoint that only accepts its parameters in a body.
        """
        key = cache_key or f"POST {url} {json.dumps(body, sort_keys=True, default=str)}"
        self._thread.network = False
        if not terms_govern and not self.robots.allows(url):
            raise RobotsDisallowed(f"robots.txt disallows {cache_key or url}")
        text = None if no_cache else self.cache.get(key)
        if text is None:
            text = self._fetch("POST", url, key=key, accept="application/json",
                               terms_govern=terms_govern, redact=cache_key is not None,
                               body=body)
        try:
            return json.loads(str(text))
        except json.JSONDecodeError as exc:
            raise SourceError(f"{cache_key or url}: response is not JSON ({exc})") from exc


def iso_date(value: Any) -> str | None:
    """Best-effort normalisation of the many date shapes job boards emit."""
    if value in (None, "", 0):
        return None
    if isinstance(value, int | float):
        # Lever uses epoch milliseconds; anything past 1e11 is not seconds.
        seconds = float(value) / 1000.0 if float(value) > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value).strip()
    if text.isdigit():
        return iso_date(int(text))
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(UTC)
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    return text[:19] or None


def strip_html(value: Any, limit: int = 8000) -> str | None:
    """Descriptions arrive as HTML from most boards; the LLM wants text.

    Greenhouse sends `content` entity-ENCODED — `&lt;p&gt;Join us&lt;/p&gt;` —
    so it is decoded before the tags are stripped, not after. The other order
    stripped nothing, then decoded the escaped tags into literal `<p>` text:
    every Greenhouse description in the database carried its markup as words.
    """
    import html
    import re

    if not value:
        return None
    text = str(value)
    if "&lt;" in text or "&gt;" in text:
        text = html.unescape(text)
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</li>|</div>|</h[1-6]>|</tr>|</ul>|</ol>", "\n", text,
                  flags=re.I)
    text = re.sub(r"<li[^>]*>", "\n• ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit] or None


def chunked(items: list[Any], size: int) -> Iterator[list[Any]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]
