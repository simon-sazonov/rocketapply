"""Content-addressed SERP cache — §7.2 of PITWALL_PLAN.md.

Key: ``sha1(provider|query_text|max_results)``.
Path: ``data/cache/<sha1[:2]>/<sha1>.json``.
Body: ``{"fetched_at": iso, "provider":…, "query":…, "hits": [...]}``.

Built *before* the providers on purpose: it is what lets you iterate on the
parser a hundred times after burning quota once. `--no-cache` forces a refetch;
`--cache-only` runs entirely offline against whatever is already stored.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..config import Settings, get_settings
from .base import SearchHit


def cache_key(provider: str, query: str, max_results: int) -> str:
    normalized = " ".join(query.split())
    return hashlib.sha1(f"{provider}|{normalized}|{max_results}".encode()).hexdigest()


class SerpCache:
    def __init__(self, root: Path | str | None = None, ttl_days: int | None = None,
                 settings: Settings | None = None) -> None:
        settings = settings or get_settings()
        self.root = Path(root) if root else settings.cache_path
        self.ttl_days = settings.cache_ttl_days if ttl_days is None else ttl_days
        self.hits = 0
        self.misses = 0
        self.writes = 0

    def path_for(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(
        self, provider: str, query: str, max_results: int, *, ignore_ttl: bool = False
    ) -> list[SearchHit] | None:
        """Return cached hits, or None on a miss/expiry."""
        path = self.path_for(cache_key(provider, query, max_results))
        if not path.exists():
            self.misses += 1
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self.misses += 1
            return None
        if not ignore_ttl and self.ttl_days > 0 and self.is_expired(data.get("fetched_at")):
            self.misses += 1
            return None
        self.hits += 1
        return [SearchHit.from_json(h) for h in data.get("hits", [])]

    def is_expired(self, fetched_at: str | None) -> bool:
        if not fetched_at:
            return True
        try:
            ts = datetime.fromisoformat(fetched_at)
        except ValueError:
            return True
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return datetime.now(UTC) - ts > timedelta(days=self.ttl_days)

    def put(self, provider: str, query: str, max_results: int, hits: list[SearchHit]) -> Path:
        path = self.path_for(cache_key(provider, query, max_results))
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "provider": provider,
            "query": query,
            "max_results": max_results,
            "hits": [h.to_json() for h in hits],
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
        self.writes += 1
        return path

    def stats(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses, "writes": self.writes}

    def size(self) -> tuple[int, int]:
        """(file count, total bytes) — shown on the Setup page."""
        files = list(self.root.rglob("*.json")) if self.root.exists() else []
        return len(files), sum(f.stat().st_size for f in files)

    def clear(self) -> int:
        n = 0
        if self.root.exists():
            for f in self.root.rglob("*.json"):
                f.unlink()
                n += 1
        return n
