"""Replay real saved SERP responses through the parsers — fully offline (§12.1).

These fixtures cost quota once and then serve forever. `serp_ddg_mclaren.json`
is a verbatim `ddgs` capture; `serp_serper_ferrari.json` wraps the same kind of
rows in Serper's `organic[]` envelope so both provider parsers are exercised
without a network call.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from pitwall.extract.linkedin import parse_profile
from pitwall.search.base import SearchHit
from pitwall.search.serper import SerperProvider

from .conftest import FIXTURES  # noqa: TID252

DDG = json.loads((FIXTURES / "serp_ddg_mclaren.json").read_text(encoding="utf-8"))
SERPER = json.loads((FIXTURES / "serp_serper_ferrari.json").read_text(encoding="utf-8"))


def test_the_ddg_fixture_looks_like_a_real_capture():
    assert DDG["provider"] == "ddgs"
    assert DDG["query"].startswith("site:linkedin.com/in")
    assert len(DDG["hits"]) >= 8
    assert all({"rank", "url", "title", "snippet"} <= set(h) for h in DDG["hits"])


def test_parsing_a_whole_real_result_page():
    hits = [SearchHit.from_json(h) for h in DDG["hits"]]
    parsed = [parse_profile(h.title, h.snippet, h.url) for h in hits]
    people = [p for p in parsed if p]

    assert len(people) >= 6, "most rows on a site:linkedin.com/in page are profiles"
    assert len(people) <= len(hits)
    assert all(p["linkedin_slug"] for p in people)
    assert len({p["linkedin_slug"] for p in people}) == len(people), "slugs are unique"
    named = [p for p in people if p["full_name"]]
    assert len(named) == len(people), "every person gets a name"
    with_employer = [p for p in people if p["current_company"]]
    assert len(with_employer) / len(people) >= 0.5


def test_non_profile_rows_in_a_real_page_are_dropped():
    hits = [SearchHit.from_json(h) for h in DDG["hits"]]
    for hit in hits:
        parsed = parse_profile(hit.title, hit.snippet, hit.url)
        if "/in/" not in hit.url:
            assert parsed is None, hit.url


def test_the_serper_parser_reads_its_own_envelope(settings, monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(status_code=200, json=lambda: SERPER, text="")

    monkeypatch.setattr("pitwall.search.serper.httpx.post", fake_post)
    settings.serper_api_key = "sk-test"
    provider = SerperProvider(settings)
    hits = provider.search("site:linkedin.com/in AF Corse", max_results=20, region="uk-en")

    assert len(hits) == len(SERPER["organic"])
    assert hits[0].url == SERPER["organic"][0]["link"]
    assert hits[0].title == SERPER["organic"][0]["title"]
    assert captured["json"] == {
        "q": "site:linkedin.com/in AF Corse", "num": 20, "gl": "gb", "hl": "en"
    }
    assert captured["headers"]["X-API-KEY"] == "sk-test"


def test_serper_hits_parse_into_the_same_shape(settings, monkeypatch):
    monkeypatch.setattr(
        "pitwall.search.serper.httpx.post",
        lambda url, **kw: SimpleNamespace(status_code=200, json=lambda: SERPER, text=""),
    )
    settings.serper_api_key = "sk-test"
    hits = SerperProvider(settings).search("q")
    people = [parse_profile(h.title, h.snippet, h.url) for h in hits]
    assert any(p and p["full_name"] for p in people)


@pytest.mark.parametrize("status", [401, 402, 429, 500])
def test_serper_error_codes_map_to_the_right_exceptions(settings, monkeypatch, status):
    from pitwall.search.base import ProviderError, QuotaExhausted, RateLimited

    monkeypatch.setattr(
        "pitwall.search.serper.httpx.post",
        lambda url, **kw: SimpleNamespace(status_code=status, json=dict, text="err"),
    )
    settings.serper_api_key = "sk-test"
    provider = SerperProvider(settings)
    expected = {401: ProviderError, 402: QuotaExhausted, 429: RateLimited, 500: ProviderError}
    with pytest.raises(expected[status]):
        provider.search("q")


def test_a_cached_fixture_round_trips_through_the_cache(settings):
    from pitwall.search import SerpCache

    cache = SerpCache(settings=settings)
    hits = [SearchHit.from_json(h) for h in DDG["hits"]]
    cache.put("ddgs", DDG["query"], 20, hits)
    restored = cache.get("ddgs", DDG["query"], 20)
    assert [h.url for h in restored] == [h.url for h in hits]
    assert [h.title for h in restored] == [h.title for h in hits]
