"""Permanent cache for provider responses — §7.1 of the v3 brief.

*"cache.py — permanent cache — a spent credit is never spent twice."*

Same content-addressed shape as `search/cache.py` and `sources/jobs/base.py`,
with one deliberate difference: **there is no TTL.** Every other cache in this
project trades staleness against a free refetch. This one is holding something
the owner *paid for*, out of fifty a month, and a cache that quietly expired
would spend a second credit to learn the same answer. If a Hunter result is
wrong, the fix is to drop that entry on purpose — never to let a clock do it.

The other rule here is §0.4: the API key never reaches disk. It is stripped
from the cache key before hashing and from the stored body before writing, so
neither the filename nor the file can leak it.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import Settings, get_settings

log = logging.getLogger(__name__)

#: Parameter names never included in a cache key or written to disk.
SECRET_PARAMS: frozenset[str] = frozenset({"api_key", "apikey", "token", "access_token"})

#: Fields dropped from any payload before it is written — §0.3: "Phone numbers.
#: Hunter returns them. Do not request, store, or display them."
#:
#: Enforced here rather than only in the adapter because **this is the disk
#: boundary**. The adapter's allow-list governs what reaches `contact_emails`;
#: without this, the raw provider answer would still land in a cache file and
#: sit there permanently, which is storing it by any reading of the word.
FORBIDDEN_FIELDS: frozenset[str] = frozenset(
    {
        "phone_number", "phone", "phones", "telephone", "mobile", "mobile_number",
        "cell", "cell_phone", "direct_dial", "fax",
    }
)


def cache_key(provider: str, endpoint: str, params: Mapping[str, Any]) -> str:
    """`sha1(provider|endpoint|sorted params)`, secrets excluded.

    Sorted so that the same request built in a different order is the same
    entry — otherwise a refactor that reorders two keyword arguments would
    silently spend a credit on a question already answered.
    """
    clean = {
        k: str(v)
        for k, v in sorted(params.items())
        if k.lower() not in SECRET_PARAMS and v is not None
    }
    payload = "|".join([provider, endpoint, json.dumps(clean, sort_keys=True)])
    return hashlib.sha1(payload.encode()).hexdigest()


def scrub(value: Any) -> Any:
    """Recursively drop secrets and forbidden fields before storing a payload."""
    if isinstance(value, dict):
        return {
            k: scrub(v)
            for k, v in value.items()
            if str(k).lower() not in SECRET_PARAMS
            and str(k).lower() not in FORBIDDEN_FIELDS
        }
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


class EnrichmentCache:
    """Never-expiring, content-addressed store for paid provider responses."""

    def __init__(
        self, root: Path | str | None = None, settings: Settings | None = None
    ) -> None:
        settings = settings or get_settings()
        self.root = Path(root) if root else settings.cache_path / "enrich"
        self.hits = 0
        self.misses = 0
        self.writes = 0

    def path_for(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, provider: str, endpoint: str, params: Mapping[str, Any]) -> Any | None:
        path = self.path_for(cache_key(provider, endpoint, params))
        if not path.exists():
            self.misses += 1
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self.misses += 1
            return None
        self.hits += 1
        return data.get("body")

    def put(
        self, provider: str, endpoint: str, params: Mapping[str, Any], body: Any
    ) -> Path:
        key = cache_key(provider, endpoint, params)
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "provider": provider,
            "endpoint": endpoint,
            # Scrubbed on both sides: the request echo and the response body.
            "params": scrub(dict(params)),
            "body": scrub(body),
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
        tmp.replace(path)
        self.writes += 1
        return path

    def drop(self, provider: str, endpoint: str, params: Mapping[str, Any]) -> bool:
        """Evict one entry deliberately — the only way anything leaves here."""
        try:
            self.path_for(cache_key(provider, endpoint, params)).unlink()
        except OSError:
            return False
        return True

    def fetched_at(
        self, provider: str, endpoint: str, params: Mapping[str, Any]
    ) -> str | None:
        """When this answer was paid for. Shown beside a cached result, because
        a six-month-old address is still worth having but worth knowing about."""
        path = self.path_for(cache_key(provider, endpoint, params))
        if not path.exists():
            return None
        try:
            return str(json.loads(path.read_text(encoding="utf-8")).get("fetched_at"))
        except (json.JSONDecodeError, OSError):
            return None

    def size(self) -> tuple[int, int]:
        files = list(self.root.rglob("*.json")) if self.root.exists() else []
        return len(files), sum(f.stat().st_size for f in files)

    def stats(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses, "writes": self.writes}

    def clear(self) -> int:
        """Throw away paid-for answers. Never called automatically."""
        n = 0
        if self.root.exists():
            for f in self.root.rglob("*.json"):
                f.unlink()
                n += 1
        log.warning("cleared %d cached provider response(s) — those credits are gone", n)
        return n
