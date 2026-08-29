"""The SearXNG adapter, against recorded responses — §0.1 acceptance criteria.

No network: every test monkeypatches `httpx.get` and replays a fixture from
`tests/fixtures/serp_searxng_*.json`. The fixtures are verbatim-shaped SearXNG
JSON envelopes with synthetic people in them.

The behaviour under test that actually matters is the third one: SearXNG answers
HTTP 200 with an empty `results` list when its upstream engines are blocked, and
explains itself in `unresponsive_engines`. Recording that as "zero results for
this query" would write a false negative into the database that looks exactly
like a company nobody works for.
"""

from __future__ import annotations

import json

import httpx
import pytest
from tenacity import wait_none

from pitwall.search.base import ProviderError, RateLimited
from pitwall.search.searxng import SearxngProvider, reset_reachability_cache

from .conftest import FIXTURES  # noqa: TID252

PREMA = json.loads((FIXTURES / "serp_searxng_prema.json").read_text(encoding="utf-8"))
BLOCKED = json.loads((FIXTURES / "serp_searxng_blocked.json").read_text(encoding="utf-8"))
PARTIAL = json.loads((FIXTURES / "serp_searxng_partial.json").read_text(encoding="utf-8"))


class FakeResponse:
    def __init__(self, payload, status_code: int = 200, text: str | None = None) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def install(monkeypatch, *responses):
    """Serve `responses` in order; record the params of every call."""
    calls: list[dict] = []
    queue = list(responses)

    def fake_get(url, **kwargs):
        calls.append({"url": url, **kwargs})
        if not queue:
            raise AssertionError(f"unexpected extra request to {url}")
        nxt = queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(httpx, "get", fake_get)
    return calls


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch, settings):
    """Keep the two real sleeps — tenacity's backoff and the 1 q/s token
    bucket — out of the test suite. `test_the_shipped_rate_is_about_one_per_second`
    is what guards the shipped value."""
    from pitwall.ratelimit import reset_limiters

    monkeypatch.setattr(SearxngProvider.search.retry, "wait", wait_none())
    settings.searxng_queries_per_second = 10_000.0
    reset_limiters()
    yield
    reset_limiters()


@pytest.fixture()
def provider(settings):
    reset_reachability_cache()
    return SearxngProvider(settings)


#: `search` retries three times before giving up, so a test that asserts on the
#: give-up path has to serve the same failure three times.
ATTEMPTS = 3


# ───────────────────────────── the fixtures ──────────────────────────────


def test_the_fixture_looks_like_a_real_searxng_envelope():
    assert set(PREMA) >= {"query", "results", "unresponsive_engines"}
    assert PREMA["query"].startswith("site:linkedin.com/in")
    row = PREMA["results"][0]
    assert {"url", "title", "content", "engines", "positions", "score"} <= set(row)


def test_an_operator_query_is_passed_through_verbatim(provider, monkeypatch):
    """The whole reason this provider exists: `ddgs` cannot do this."""
    calls = install(monkeypatch, FakeResponse(PREMA))
    hits = provider.search(PREMA["query"], max_results=20)
    assert calls[0]["params"]["q"] == 'site:linkedin.com/in "race engineer" "PREMA"'
    assert len(hits) == len(PREMA["results"])
    assert hits[0].url.startswith("https://www.linkedin.com/in/")
    assert "Race Engineer" in hits[0].title
    assert hits[0].snippet  # `content` -> snippet


def test_ranks_are_dense_and_zero_based(provider, monkeypatch):
    install(monkeypatch, FakeResponse(PREMA))
    hits = provider.search("x")
    assert [h.rank for h in hits] == list(range(len(hits)))


def test_max_results_truncates(provider, monkeypatch):
    install(monkeypatch, FakeResponse(PREMA))
    assert len(provider.search("x", max_results=3)) == 3


def test_the_raw_row_is_kept_for_the_evidence_panel(provider, monkeypatch):
    install(monkeypatch, FakeResponse(PREMA))
    hits = provider.search("x")
    assert hits[0].raw["engines"] == ["google", "bing"]


def test_rows_without_a_url_are_skipped(provider, monkeypatch):
    payload = {**PREMA, "results": [{"title": "no url", "content": ""}, *PREMA["results"]]}
    install(monkeypatch, FakeResponse(payload))
    assert len(provider.search("x")) == len(PREMA["results"])


# ────────────────── unresponsive engines are NOT zero results ──────────────


def test_a_blocked_engine_raises_instead_of_reporting_zero_results(settings, monkeypatch):
    """HTTP 200 + empty results + unresponsive_engines == captcha, not emptiness."""
    settings.searxng_engines = []          # no narrowing, so one request per attempt
    provider = SearxngProvider(settings)
    install(monkeypatch, *[FakeResponse(BLOCKED) for _ in range(ATTEMPTS)])
    with pytest.raises(RateLimited) as exc:
        provider.search("x")
    assert "google" in str(exc.value) and "bing" in str(exc.value)


def test_a_dead_engine_group_widens_before_giving_up(provider, monkeypatch):
    """A captcha'd group and a group this instance does not have look identical
    from here, and the remedy is the same: ask the full default set once."""
    calls = install(monkeypatch, FakeResponse(BLOCKED), FakeResponse(PREMA))
    hits = provider.search("x")
    assert len(calls) == 2
    assert "engines" in calls[0]["params"]
    assert "engines" not in calls[1]["params"]
    assert len(hits) == len(PREMA["results"])


def test_a_blocked_narrowed_group_still_raises_when_widening_fails(provider, monkeypatch):
    # narrow + widened, three times over
    install(monkeypatch, *[FakeResponse(BLOCKED) for _ in range(ATTEMPTS * 2)])
    with pytest.raises(RateLimited):
        provider.search("x")


def test_partial_engine_failure_still_returns_the_rows_it_got(provider, monkeypatch):
    install(monkeypatch, FakeResponse(PARTIAL))
    hits = provider.search("x")
    assert len(hits) == 2  # google captcha'd; startpage and bing answered


def test_a_genuine_zero_falls_back_to_the_instance_default_engines(provider, monkeypatch):
    """0 results and nothing complained: the engine group may not exist here."""
    empty = {**PREMA, "results": [], "unresponsive_engines": []}
    calls = install(monkeypatch, FakeResponse(empty), FakeResponse(PREMA))
    hits = provider.search("x")
    assert len(calls) == 2
    assert "engines" in calls[0]["params"]
    assert "engines" not in calls[1]["params"], "the retry drops the engine filter"
    assert len(hits) == len(PREMA["results"])


def test_a_genuine_zero_on_the_default_engines_is_believed(settings, monkeypatch):
    settings.searxng_engines = []
    reset_reachability_cache()
    provider = SearxngProvider(settings)
    empty = {**PREMA, "results": [], "unresponsive_engines": []}
    calls = install(monkeypatch, FakeResponse(empty))
    assert provider.search("x") == []
    assert len(calls) == 1, "no engine filter to drop, so no second request"


# ──────────────────────────── engine rotation ────────────────────────────


def test_engine_groups_rotate_across_queries(settings, monkeypatch):
    settings.searxng_engines = ["google,duckduckgo", "bing,startpage", "brave"]
    provider = SearxngProvider(settings)
    calls = install(monkeypatch, *[FakeResponse(PREMA) for _ in range(4)])
    for _ in range(4):
        provider.search("x")
    used = [c["params"]["engines"] for c in calls]
    assert used == ["google,duckduckgo", "bing,startpage", "brave", "google,duckduckgo"]


def test_no_engine_parameter_when_rotation_is_disabled(settings, monkeypatch):
    settings.searxng_engines = []
    provider = SearxngProvider(settings)
    calls = install(monkeypatch, FakeResponse(PREMA))
    provider.search("x")
    assert "engines" not in calls[0]["params"]
    assert calls[0]["params"]["categories"] == "general"


def test_engines_and_categories_are_never_sent_together(settings, monkeypatch):
    """SearXNG UNIONS the two (searx/webadapter.py::parse_generic).

    Sending both runs every general engine on every query and makes the rotation
    a silent no-op — which is what it did until it was measured against a live
    container.
    """
    settings.searxng_engines = ["bing"]
    provider = SearxngProvider(settings)
    calls = install(monkeypatch, FakeResponse(PREMA))
    provider.search("x")
    assert calls[0]["params"]["engines"] == "bing"
    assert "categories" not in calls[0]["params"]


def test_engine_groups_survive_the_env_csv_form(settings):
    settings.searxng_engines = ["google,bing"]
    assert SearxngProvider(settings).next_engine_group() == "google,bing"


# ─────────────────────────── the rate limiter ────────────────────────────


def test_the_token_bucket_is_wired_up_and_shared(settings):
    """Not unbounded: the container is free, the engines it proxies are not."""
    from pitwall.ratelimit import reset_limiters

    reset_limiters()
    a = SearxngProvider(settings)
    b = SearxngProvider(settings)
    assert a.limiter is b.limiter, "one process-wide bucket, not one per worker"
    assert a.limiter.per_second == settings.searxng_queries_per_second


def test_the_shipped_rate_is_about_one_per_second():
    from pitwall.config import Settings

    assert Settings(_env_file=None).searxng_queries_per_second <= 1.0


# ─────────────────────────── HTTP failure modes ──────────────────────────


def test_403_names_the_json_format_as_the_likely_cause(provider, monkeypatch):
    install(monkeypatch, FakeResponse(None, 403, text="<html>Forbidden</html>"))
    with pytest.raises(ProviderError) as exc:
        provider.search("x")
    assert "JSON format" in str(exc.value)


def test_an_html_body_with_a_200_is_reported_as_a_format_problem(provider, monkeypatch):
    install(monkeypatch, FakeResponse(None, 200, text="<html>nope</html>"))
    with pytest.raises(ProviderError) as exc:
        provider.search("x")
    assert "search.formats" in str(exc.value)


def test_429_is_retried_then_surfaces_as_rate_limited(provider, monkeypatch):
    install(monkeypatch, *[FakeResponse(None, 429, text="") for _ in range(ATTEMPTS)])
    with pytest.raises(RateLimited):
        provider.search("x")


def test_a_transport_error_becomes_rate_limited_so_the_chain_backs_off(provider, monkeypatch):
    err = httpx.ConnectError("connection refused")
    install(monkeypatch, *[err] * ATTEMPTS)
    with pytest.raises(RateLimited):
        provider.search("x")


def test_a_retried_query_succeeds_on_the_second_attempt(provider, monkeypatch):
    install(monkeypatch, httpx.ConnectError("boom"), FakeResponse(PREMA))
    assert len(provider.search("x")) == len(PREMA["results"])


# ───────────────────────────── reachability ──────────────────────────────


def test_reachability_is_memoised_so_the_ui_does_not_pay_a_timeout(provider, monkeypatch):
    calls = install(monkeypatch, FakeResponse({}, 200), FakeResponse({}, 200))
    assert provider.is_reachable() is True
    assert provider.is_reachable() is True
    assert len(calls) == 1, "second call served from the memo"


def test_an_unreachable_instance_reports_false(provider, monkeypatch):
    install(monkeypatch, httpx.ConnectError("down"), httpx.ConnectError("down"))
    assert provider.is_reachable() is False


def test_the_provider_advertises_operator_support(provider):
    assert provider.supports_operators() is True
    assert provider.remaining_quota() is None
