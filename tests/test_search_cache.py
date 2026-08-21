"""Cache hit/miss/TTL, the rate limiter, and the provider chain (Phase 2 step 7)."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import pytest

from pitwall.ratelimit import RateLimiter, get_limiter, reset_limiters
from pitwall.search import SerpCache, build_chain, build_provider, cache_key
from pitwall.search.base import ProviderError, QuotaExhausted, SearchHit

HITS = [
    SearchHit(0, "https://www.linkedin.com/in/a", "A - Data Scientist - McLaren Racing", "x", {"raw": 1}),
    SearchHit(1, "https://www.linkedin.com/in/b", "B - Data Engineer - AF Corse", "y", {}),
]


# ─────────────────────────────── the cache ───────────────────────────────


def test_cache_key_is_content_addressed():
    assert cache_key("ddgs", "foo bar", 20) == cache_key("ddgs", "  foo   bar ", 20)
    assert cache_key("ddgs", "foo", 20) != cache_key("ddgs", "foo", 10)
    assert cache_key("ddgs", "foo", 20) != cache_key("serper", "foo", 20)


def test_miss_then_hit(settings):
    cache = SerpCache(settings=settings)
    assert cache.get("ddgs", "q", 20) is None
    assert cache.misses == 1

    cache.put("ddgs", "q", 20, HITS)
    got = cache.get("ddgs", "q", 20)
    assert got is not None
    assert cache.hits == 1
    assert [h.url for h in got] == [h.url for h in HITS]
    assert got[0].raw == {"raw": 1}


def test_files_are_sharded_two_deep(settings):
    cache = SerpCache(settings=settings)
    path = cache.put("ddgs", "q", 20, HITS)
    key = cache_key("ddgs", "q", 20)
    assert path.parent.name == key[:2]
    assert path.name == f"{key}.json"
    assert json.loads(path.read_text())["query"] == "q"


def test_expired_entries_are_a_miss_but_ignore_ttl_still_reads(settings):
    cache = SerpCache(settings=settings, ttl_days=7)
    path = cache.put("ddgs", "q", 20, HITS)
    stale = json.loads(path.read_text())
    stale["fetched_at"] = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    path.write_text(json.dumps(stale), encoding="utf-8")

    assert cache.get("ddgs", "q", 20) is None
    assert cache.get("ddgs", "q", 20, ignore_ttl=True) is not None


def test_a_zero_ttl_disables_expiry(settings):
    cache = SerpCache(settings=settings, ttl_days=0)
    path = cache.put("ddgs", "q", 20, HITS)
    stale = json.loads(path.read_text())
    stale["fetched_at"] = "2001-01-01T00:00:00+00:00"
    path.write_text(json.dumps(stale), encoding="utf-8")
    assert cache.get("ddgs", "q", 20) is not None


def test_corrupt_cache_files_are_treated_as_a_miss(settings):
    cache = SerpCache(settings=settings)
    path = cache.put("ddgs", "q", 20, HITS)
    path.write_text("{ not json", encoding="utf-8")
    assert cache.get("ddgs", "q", 20) is None


def test_size_and_clear(settings):
    cache = SerpCache(settings=settings)
    cache.put("ddgs", "one", 20, HITS)
    cache.put("ddgs", "two", 20, HITS)
    files, size = cache.size()
    assert files == 2 and size > 0
    assert cache.clear() == 2
    assert cache.size() == (0, 0)


def test_writes_are_atomic_leaving_no_tmp_files(settings):
    cache = SerpCache(settings=settings)
    cache.put("ddgs", "q", 20, HITS)
    assert list(cache.root.rglob("*.tmp")) == []


# ────────────────────────────── rate limiter ─────────────────────────────


def test_the_limiter_actually_sleeps():
    limiter = RateLimiter(per_second=20, jitter=0.0)   # 50 ms apart
    limiter.acquire()                                   # first call is free
    start = time.monotonic()
    limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.04, elapsed


def test_the_first_acquire_does_not_block():
    limiter = RateLimiter(per_second=0.25, jitter=0.0)  # one every 4 s
    start = time.monotonic()
    limiter.acquire()
    assert time.monotonic() - start < 0.5


def test_limiters_are_shared_per_provider_name():
    reset_limiters()
    a = get_limiter("ddgs", 0.25)
    b = get_limiter("ddgs", 0.25)
    assert a is b
    assert get_limiter("serper", 5.0) is not a
    reset_limiters()


def test_a_non_positive_rate_is_rejected():
    with pytest.raises(ValueError):
        RateLimiter(per_second=0)


def test_the_limiter_works_as_a_context_manager():
    with RateLimiter(per_second=100, jitter=0.0) as limiter:
        assert isinstance(limiter, RateLimiter)


# ───────────────────────────── provider chain ────────────────────────────


def test_the_free_provider_declares_degraded_operator_support(settings):
    ddgs = build_provider("ddgs", settings)
    assert ddgs.name == "ddgs"
    assert ddgs.supports_operators() is False
    assert ddgs.remaining_quota() is None


def test_paid_providers_declare_full_operator_support(settings):
    assert build_provider("serper", settings).supports_operators() is True
    assert build_provider("searxng", settings).supports_operators() is True


def test_unknown_providers_are_rejected(settings):
    with pytest.raises(ValueError, match="unknown search provider"):
        build_provider("altavista", settings)


def test_providers_without_a_key_are_dropped_from_the_chain(settings):
    settings.search_providers = ["ddgs", "serper"]
    settings.serper_api_key = ""
    assert [p.name for p in build_chain(settings)] == ["ddgs"]

    settings.serper_api_key = "sk-test"
    assert [p.name for p in build_chain(settings)] == ["ddgs", "serper"]


def test_an_empty_chain_raises_rather_than_silently_doing_nothing(settings):
    settings.serper_api_key = ""
    with pytest.raises(ProviderError, match="no usable search provider"):
        build_chain(settings, names=["serper"])


def test_serper_refuses_when_its_credits_are_gone(settings):
    settings.serper_api_key = "sk-test"
    serper = build_provider("serper", settings)
    serper.set_quota(0)
    with pytest.raises(QuotaExhausted):
        serper.search("anything")


def test_serper_refuses_without_a_key(settings):
    settings.serper_api_key = ""
    serper = build_provider("serper", settings)
    with pytest.raises(ProviderError, match="SERPER_API_KEY"):
        serper.search("anything")


def test_hits_round_trip_through_json():
    restored = SearchHit.from_json(HITS[0].to_json())
    assert restored == HITS[0]
