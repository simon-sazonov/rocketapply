"""Search providers and the provider chain — Phase 2 of PITWALL_PLAN.md."""

from __future__ import annotations

import logging

from ..config import Settings, get_settings
from .base import ProviderError, QuotaExhausted, RateLimited, SearchHit, SearchProvider
from .cache import SerpCache, cache_key
from .ddg import DDGSProvider
from .searxng import SearxngProvider
from .serper import SerperProvider

log = logging.getLogger(__name__)

__all__ = [
    "DDGSProvider",
    "ProviderError",
    "QuotaExhausted",
    "RateLimited",
    "SearchHit",
    "SearchProvider",
    "SearxngProvider",
    "SerpCache",
    "SerperProvider",
    "cache_key",
    "build_provider",
    "build_chain",
]


def build_provider(name: str, settings: Settings | None = None) -> SearchProvider:
    settings = settings or get_settings()
    match name:
        case "ddgs":
            return DDGSProvider(settings)
        case "serper" | "serpapi":
            return SerperProvider(settings)
        case "searxng":
            return SearxngProvider(settings)
        case _:
            raise ValueError(f"unknown search provider {name!r}")


def build_chain(settings: Settings | None = None, names: list[str] | None = None) -> list[SearchProvider]:
    """Ordered fallback chain from settings, skipping unusable providers."""
    settings = settings or get_settings()
    wanted = names or settings.search_providers
    chain: list[SearchProvider] = []
    for name in wanted:
        try:
            provider = build_provider(name, settings)
        except ValueError as exc:
            log.warning("skipping provider: %s", exc)
            continue
        if name in {"serper", "serpapi"} and not settings.serper_api_key:
            log.info("skipping %s — no API key configured", name)
            continue
        chain.append(provider)
    if not chain:
        raise ProviderError(
            "no usable search provider — set SEARCH_PROVIDERS and any required API key"
        )
    return chain
