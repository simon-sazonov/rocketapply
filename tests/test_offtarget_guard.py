"""The guard that stopped run #7's failure mode from being silent.

Run #7 sent 200 queries to a local SearXNG whose operator-capable engines were
all CAPTCHA-suspended. It fell back to Bing, Bing dropped the `site:` filter,
and 2,170 rows about atrial fibrillation and McLaren road cars were recorded as
a clean success: 200/200 queries `done`, 0 people, ten minutes gone.
"""

from __future__ import annotations

import pytest

from pitwall.pipeline import MIN_PROFILE_SHARE, Pipeline, RunParams, off_target
from pitwall.search.base import ProviderError, SearchHit

PROFILE_QUERY = 'site:linkedin.com/in "McLaren Racing" "Performance Engineer"'


def hits(*urls: str) -> list[SearchHit]:
    return [SearchHit(rank=i, url=u, title=u) for i, u in enumerate(urls)]


JUNK = hits(
    "https://www.nhs.uk/conditions/atrial-fibrillation/",
    "https://www.mclaren.com/cars/gl_en/all-models",
    "https://en.wikipedia.org/wiki/McLaren",
)
GOOD = hits(
    "https://uk.linkedin.com/in/zak-brown-46b168104",
    "https://www.linkedin.com/in/juliagoldsmith10",
    "https://www.mclaren.com/",
)


class TestOffTarget:
    def test_no_profiles_at_all_is_off_target(self):
        assert off_target(PROFILE_QUERY, JUNK) is not None

    def test_mostly_profiles_is_fine(self):
        assert off_target(PROFILE_QUERY, GOOD) is None

    def test_an_empty_answer_is_not_off_target(self):
        # "nobody matches" is a legitimate result; only wrong rows are a fault.
        assert off_target(PROFILE_QUERY, []) is None

    def test_a_query_that_did_not_ask_for_profiles_is_never_judged(self):
        assert off_target("McLaren Racing engineers", JUNK) is None

    @pytest.mark.parametrize("prefix", ["site:", "site: ", "site:uk."])
    def test_operator_spellings_are_recognised(self, prefix):
        assert off_target(f'{prefix}linkedin.com/in "X"', JUNK) is not None

    def test_the_threshold_is_a_share_not_a_count(self):
        one_good = hits(
            "https://www.linkedin.com/in/someone",
            *[f"https://example.com/{i}" for i in range(19)],
        )
        assert MIN_PROFILE_SHARE > 1 / 20
        assert off_target(PROFILE_QUERY, one_good) is not None


class _Provider:
    """Minimal provider double; `answer` is what `search` hands back."""

    def __init__(self, name, answer):
        self.name = name
        self.answer = answer
        self.calls = 0

    def search(self, query, *, max_results=20, region="wt-wt"):
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def supports_operators(self):
        return True

    def remaining_quota(self):
        return None


class TestChainFallthrough:
    def test_an_off_target_provider_falls_through_to_the_next(self, pipeline):
        bad, good = _Provider("searxng", JUNK), _Provider("ddgs", GOOD)
        result, name, cached = pipeline._search_one(
            PROFILE_QUERY, [bad, good], 10, RunParams(no_cache=True), set()
        )
        assert name == "ddgs"
        assert result == GOOD
        assert bad.calls == 1 and good.calls == 1

    def test_off_target_results_are_never_cached(self, pipeline):
        bad, good = _Provider("searxng", JUNK), _Provider("ddgs", GOOD)
        pipeline._search_one(PROFILE_QUERY, [bad, good], 10, RunParams(no_cache=True), set())
        assert pipeline.cache.get("searxng", PROFILE_QUERY, 10) is None
        assert pipeline.cache.get("ddgs", PROFILE_QUERY, 10) == GOOD

    def test_a_provider_is_dropped_after_repeated_off_target_answers(self, pipeline):
        bad, good = _Provider("searxng", JUNK), _Provider("ddgs", GOOD)
        disabled: set[str] = set()
        offtarget: dict[str, int] = {}
        for _ in range(Pipeline.OFFTARGET_STRIKES):
            pipeline._search_one(
                PROFILE_QUERY, [bad, good], 10, RunParams(no_cache=True),
                disabled, offtarget=offtarget,
            )
        assert "searxng" in disabled

        calls_before = bad.calls
        pipeline._search_one(
            PROFILE_QUERY, [bad, good], 10, RunParams(no_cache=True),
            disabled, offtarget=offtarget,
        )
        assert bad.calls == calls_before, "a dropped provider must not be called again"

    def test_one_good_answer_clears_the_streak(self, pipeline):
        flaky = _Provider("searxng", JUNK)
        disabled: set[str] = set()
        offtarget: dict[str, int] = {}
        pipeline._search_one(
            PROFILE_QUERY, [flaky], 10, RunParams(no_cache=True), disabled,
            offtarget=offtarget,
        )
        assert offtarget["searxng"] == 1
        flaky.answer = GOOD
        pipeline._search_one(
            "site:linkedin.com/in \"Other\"", [flaky], 10,
            RunParams(no_cache=True), disabled, offtarget=offtarget,
        )
        assert "searxng" not in offtarget

    def test_every_provider_off_target_is_a_failure_not_an_empty_success(self, pipeline):
        bad = _Provider("searxng", JUNK)
        result, error, _ = pipeline._search_one(
            PROFILE_QUERY, [bad], 10, RunParams(no_cache=True), set()
        )
        # None (not []) is what makes the caller mark the query `failed`, which
        # is the whole point: run #7 marked 200 queries `done`.
        assert result is None
        assert "site:" in error


class TestCachedJunk:
    def test_junk_cached_before_the_guard_existed_is_evicted(self, pipeline):
        pipeline.cache.put("searxng", PROFILE_QUERY, 10, JUNK)
        good = _Provider("ddgs", GOOD)
        bad = _Provider("searxng", ProviderError("should not be reached live"))
        result, name, _ = pipeline._search_one(
            PROFILE_QUERY, [bad, good], 10, RunParams(), set()
        )
        assert name == "ddgs" and result == GOOD
        assert pipeline.cache.get("searxng", PROFILE_QUERY, 10) is None
