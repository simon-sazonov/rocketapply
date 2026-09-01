"""The permanent provider cache — §7.1's "a spent credit is never spent twice".

The property that distinguishes this cache from the other two in the project is
that it has **no TTL**. Every other cache trades staleness against a free
refetch; this one is holding something bought out of fifty a month.
"""

from __future__ import annotations

import json

from pitwall.enrich.cache import EnrichmentCache, cache_key, scrub


def test_a_stored_answer_comes_back(tmp_path):
    cache = EnrichmentCache(tmp_path)
    cache.put("hunter", "email-finder", {"domain": "x.com"}, {"data": {"email": "a@x.com"}})
    assert cache.get("hunter", "email-finder", {"domain": "x.com"})["data"]["email"] == "a@x.com"


def test_a_miss_is_none_not_an_error(tmp_path):
    assert EnrichmentCache(tmp_path).get("hunter", "email-finder", {"domain": "x"}) is None


def test_there_is_no_expiry(tmp_path):
    """A cache that quietly expired would spend a second credit to learn the
    same answer. Nothing here reads a clock — asserted by ageing the file."""
    cache = EnrichmentCache(tmp_path)
    params = {"domain": "x.com"}
    cache.put("hunter", "domain-search", params, {"data": {"pattern": "{first}.{last}"}})
    path = cache.path_for(cache_key("hunter", "domain-search", params))

    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["fetched_at"] = "2019-01-01T00:00:00+00:00"
    path.write_text(json.dumps(stored), encoding="utf-8")

    assert cache.get("hunter", "domain-search", params) is not None


def test_the_age_is_reported_so_a_stale_answer_can_be_shown_as_one(tmp_path):
    cache = EnrichmentCache(tmp_path)
    cache.put("hunter", "email-finder", {"domain": "x.com"}, {"data": {}})
    assert cache.fetched_at("hunter", "email-finder", {"domain": "x.com"})
    assert cache.fetched_at("hunter", "email-finder", {"domain": "other"}) is None


def test_an_entry_leaves_only_when_dropped_on_purpose(tmp_path):
    cache = EnrichmentCache(tmp_path)
    params = {"domain": "x.com"}
    cache.put("hunter", "email-finder", params, {"data": {}})
    assert cache.drop("hunter", "email-finder", params) is True
    assert cache.get("hunter", "email-finder", params) is None
    assert cache.drop("hunter", "email-finder", params) is False


def test_a_corrupt_file_is_a_miss_not_a_crash(tmp_path):
    cache = EnrichmentCache(tmp_path)
    params = {"domain": "x.com"}
    cache.put("hunter", "email-finder", params, {"data": {}})
    cache.path_for(cache_key("hunter", "email-finder", params)).write_text("{ not json")
    assert cache.get("hunter", "email-finder", params) is None


def test_the_key_is_order_independent_and_secret_free():
    assert cache_key("h", "e", {"a": 1, "b": 2}) == cache_key("h", "e", {"b": 2, "a": 1})
    assert cache_key("h", "e", {"a": 1}) == cache_key("h", "e", {"a": 1, "api_key": "s"})


def test_a_none_parameter_does_not_change_the_key():
    assert cache_key("h", "e", {"a": 1}) == cache_key("h", "e", {"a": 1, "b": None})


def test_different_questions_are_different_entries():
    assert cache_key("h", "e", {"domain": "a.com"}) != cache_key("h", "e", {"domain": "b.com"})
    assert cache_key("h", "email-finder", {}) != cache_key("h", "domain-search", {})


def test_secrets_never_reach_disk(tmp_path):
    cache = EnrichmentCache(tmp_path)
    cache.put(
        "hunter", "e", {"domain": "x.com", "api_key": "supersecret"},
        {"data": {"access_token": "supersecret", "email": "a@x.com"}},
    )
    text = " ".join(f.read_text() for f in tmp_path.rglob("*.json"))
    assert "supersecret" not in text
    assert "a@x.com" in text


def test_phone_numbers_never_reach_disk(tmp_path):
    """§0.3, enforced at the disk boundary — see `FORBIDDEN_FIELDS`."""
    cache = EnrichmentCache(tmp_path)
    cache.put(
        "hunter", "e", {"domain": "x.com"},
        {"data": {"email": "a@x.com", "phone_number": "+33 1 23 45 67 89",
                  "people": [{"mobile": "07000 000000", "name": "A"}]}},
    )
    text = " ".join(f.read_text() for f in tmp_path.rglob("*.json"))
    assert "+33" not in text
    assert "07000" not in text
    assert "a@x.com" in text


def test_scrub_leaves_ordinary_data_alone():
    payload = {"email": "a@x.com", "sources": [{"uri": "https://x/y"}], "score": 97}
    assert scrub(payload) == payload


def test_size_and_stats_report_something_useful(tmp_path):
    cache = EnrichmentCache(tmp_path)
    assert cache.size() == (0, 0)
    cache.put("hunter", "e", {"d": 1}, {"x": 1})
    cache.get("hunter", "e", {"d": 1})
    cache.get("hunter", "e", {"d": 2})
    count, size = cache.size()
    assert count == 1 and size > 0
    assert cache.stats() == {"hits": 1, "misses": 1, "writes": 1}


def test_clear_is_possible_but_loud(tmp_path, caplog):
    cache = EnrichmentCache(tmp_path)
    cache.put("hunter", "e", {"d": 1}, {"x": 1})
    with caplog.at_level("WARNING"):
        assert cache.clear() == 1
    assert any("credits are gone" in r.message for r in caplog.records)
