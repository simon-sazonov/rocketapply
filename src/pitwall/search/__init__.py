"""Search providers and the provider chain — Phase 2 of PITWALL_PLAN.md.

The v2 chain is `searxng → ddgs → serper` (§0.2 of PITWALL_V2_IMPLEMENTATION.md),
and each step drops out for a different, specific reason:

* **searxng** is skipped when the container is not running, so a machine without
  Docker degrades to the v1 behaviour instead of failing every query. That also
  makes operator degradation automatic: `chain[0]` decides whether the query
  builder keeps the `site:`/quoted-phrase templates.
* **serper** is skipped without an API key, and skipped again when its remaining
  credits are at or below `SERPER_RESERVE_CREDITS`. Those 2,500 credits are
  one-time and never refill — the reserve is what stops a routine sweep from
  quietly spending the burst capacity you were saving.
"""

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
    "chain_report",
]

SERPER_NAMES = {"serper", "serpapi"}


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


def chain_report(
    settings: Settings | None = None,
    names: list[str] | None = None,
    *,
    credits: dict[str, int | None] | None = None,
    probe_searxng: bool = True,
) -> tuple[list[SearchProvider], list[tuple[str, str]]]:
    """Build the chain and explain every omission.

    Returns `(usable_providers, [(skipped_name, reason), …])`. The Setup page
    shows the reasons; `build_chain` throws them away.
    """
    settings = settings or get_settings()
    wanted = names if names is not None else settings.search_providers
    chain: list[SearchProvider] = []
    skipped: list[tuple[str, str]] = []

    for name in wanted:
        try:
            provider = build_provider(name, settings)
        except ValueError as exc:
            skipped.append((name, str(exc)))
            log.warning("skipping provider: %s", exc)
            continue

        if name == "searxng" and probe_searxng and not provider.is_reachable():  # type: ignore[attr-defined]
            reason = (
                f"not reachable at {settings.searxng_base_url} — start it with "
                "`make searxng` (operator templates stay disabled until you do)"
            )
            skipped.append((name, reason))
            log.warning("skipping searxng — %s", reason)
            continue

        if name in SERPER_NAMES:
            if not settings.serper_api_key:
                skipped.append((name, "no API key configured"))
                log.info("skipping %s — no API key configured", name)
                continue
            remaining = (credits or {}).get(name)
            if remaining is None:
                remaining = settings.serper_credits
            reserve = settings.serper_reserve_credits
            if remaining <= reserve:
                reason = (
                    f"{remaining} credits left, at or below the {reserve}-credit reserve. "
                    "These credits are one-time and do not refill; run with "
                    "`--use-serper` to spend into the reserve deliberately."
                )
                skipped.append((name, reason))
                log.info("skipping %s — %s", name, reason)
                continue

        chain.append(provider)

    return chain, skipped


def build_chain(
    settings: Settings | None = None,
    names: list[str] | None = None,
    *,
    credits: dict[str, int | None] | None = None,
    probe_searxng: bool = True,
) -> list[SearchProvider]:
    """Ordered fallback chain from settings, skipping unusable providers."""
    chain, skipped = chain_report(
        settings, names, credits=credits, probe_searxng=probe_searxng
    )
    if not chain:
        detail = "; ".join(f"{n}: {why}" for n, why in skipped) or "nothing configured"
        raise ProviderError(
            f"no usable search provider — set SEARCH_PROVIDER_CHAIN and any required "
            f"API key ({detail})"
        )
    return chain
