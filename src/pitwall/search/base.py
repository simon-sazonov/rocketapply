"""The search-provider contract — Phase 2 of PITWALL_PLAN.md."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class SearchHit:
    """One SERP row, provider-agnostic."""

    rank: int
    url: str
    title: str = ""
    snippet: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "url": self.url,
            "title": self.title,
            "snippet": self.snippet,
            "raw": self.raw,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> SearchHit:
        return cls(
            rank=int(d.get("rank", 0)),
            url=str(d.get("url", "")),
            title=str(d.get("title") or ""),
            snippet=str(d.get("snippet") or ""),
            raw=d.get("raw") or {},
        )


@runtime_checkable
class SearchProvider(Protocol):
    """Every provider implements exactly this."""

    name: str

    def search(
        self, query: str, *, max_results: int = 20, region: str = "wt-wt"
    ) -> list[SearchHit]: ...

    def supports_operators(self) -> bool: ...

    def remaining_quota(self) -> int | None: ...


class ProviderError(RuntimeError):
    """A provider failed in a way the chain should fall through on."""


class QuotaExhausted(ProviderError):
    """The provider is out of credits/requests; disable it for this run."""


class RateLimited(ProviderError):
    """429 or equivalent — retry with backoff, then give up on this query."""
