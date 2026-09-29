"""Company research — the notes that fill the "why this company" paragraph.

Two things are defended here, and the second matters more than the first.

The first is that the notes are TRUE. On a real run for a small employer the SERP
came back as ResearchGate, academia.edu and a "40 must-read engineering blogs"
listicle, and the model read them and wrote a confident sentence about a research
programme that does not exist, cited ResearchGate for it, and reported
`confidence: "high"`. The deterministic filters are what make that sentence
unwritable, and they run before the model sees anything.

The second is that the machine never outranks Simon. `profile/companies.yaml` is
hand-researched and always wins; the cache under `data/cache/research/` is
consulted only where no entry matches, and nothing in this feature may write to
`profile/`.
"""

from __future__ import annotations

import functools
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from pitwall.apply import research_cache
from pitwall.apply.loader import load_profile
from pitwall.apply.planner import company_notes_with_source, plan_letter
from pitwall.apply.research_cache import CompanyResearch, ResearchCache, cached_notes

from .conftest import ROOT  # noqa: TID252
from .test_apply_pipeline import PLAN_REPLY, StubClient, router  # noqa: TID252


@pytest.fixture(scope="module")
def profile():
    return load_profile()


@pytest.fixture
def cache(tmp_path):
    """A cache in a throwaway directory, also installed as the default one.

    The planner builds its own `ResearchCache()` with no arguments, so patching
    the class is what keeps a test off the real `data/cache/research`.
    """
    made = ResearchCache(root=tmp_path / "research", ttl_days=90)
    return made


@pytest.fixture
def installed_cache(cache, monkeypatch):
    monkeypatch.setattr(
        research_cache,
        "ResearchCache",
        functools.partial(ResearchCache, root=cache.root, ttl_days=cache.ttl_days),
    )
    return cache


def record(company="Helsing", **kw) -> CompanyResearch:
    base = {
        "company": company,
        "notes": "The team develops Twurst, a Rust implementation of the Twirp protocol.",
        "sentences": [{"text": "…", "source": "https://blog.helsing.ai/"}],
        "sources": ["https://blog.helsing.ai/"],
        "verified": True,
        "independent": True,
        "outcome": "ok",
    }
    return CompanyResearch(**{**base, **kw})


# ── the cache ──


def test_the_key_is_the_normalised_name_so_a_legal_suffix_does_not_split_the_record(cache):
    """A posting that says "Helsing GmbH" must find research done under "Helsing".

    The same reason `_company_notes` normalises before matching: an entry that
    looks correct and never matches is the worst outcome for work only a human
    can do, and it is just as bad when the machine did the work.
    """
    cache.put(record("Helsing"))
    assert cache.get("Helsing GmbH") is not None
    assert cache.key_for("Helsing GmbH") == cache.key_for("helsing")


def test_a_company_with_no_usable_name_is_not_stored_under_an_empty_key(cache):
    assert cache.key_for("   ") == ""
    assert cache.put(record(company="   ")) is None
    assert cache.get("   ") is None


def test_an_expired_record_is_a_miss_rather_than_a_stale_answer(cache):
    old = (datetime.now(UTC) - timedelta(days=120)).isoformat(timespec="seconds")
    cache.put(record(fetched_at=old))
    assert cache.get("Helsing") is None
    assert cache.get("Helsing", ignore_ttl=True) is not None


def test_a_ttl_of_zero_disables_expiry(tmp_path):
    forever = ResearchCache(root=tmp_path, ttl_days=0)
    old = (datetime.now(UTC) - timedelta(days=4000)).isoformat(timespec="seconds")
    forever.put(record(fetched_at=old))
    assert forever.get("Helsing") is not None


def test_a_torn_cache_file_is_a_miss_rather_than_a_crash(cache):
    path = cache.put(record())
    path.write_text("{ not json", encoding="utf-8")
    assert cache.get("Helsing") is None
    assert cached_notes("Helsing", cache=cache) == ""


def test_an_unknown_schema_version_is_a_miss(cache):
    path = cache.put(record())
    data = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**data, "schema_version": 99}), encoding="utf-8")
    assert cache.get("Helsing") is None


def test_the_temp_file_name_is_unique_per_write(cache, monkeypatch):
    """`SerpCache` uses a shared `.json.tmp`; this must not.

    Two writers of one record rename out from under each other and the loser
    raises FileNotFoundError — `llm.py:_store` has the same comment and the same
    uuid, after a stress test produced 213 of them.
    """
    seen: list[str] = []
    real = research_cache.os.replace

    def spy(src, dst):
        seen.append(str(src))
        return real(src, dst)

    monkeypatch.setattr(research_cache.os, "replace", spy)
    cache.put(record())
    cache.put(record())
    assert len(seen) == 2
    assert seen[0] != seen[1], "two writes shared a temp file name"
    assert not seen[0].endswith(".json.tmp")


def test_a_negative_result_is_a_real_answer_and_not_a_missing_one(cache):
    """A company where nothing citable was found is cached, so six batches over
    one shortlist cost one search round rather than six."""
    cache.put(record("Meetdavis", notes="", verified=False, outcome="thin"))
    stored = cache.get("Meetdavis")
    assert stored is not None and stored.outcome == "thin"
    assert not stored.usable
    assert cached_notes("Meetdavis", cache=cache) == ""


def test_all_lists_what_is_stored_including_what_has_gone_stale(cache):
    old = (datetime.now(UTC) - timedelta(days=400)).isoformat(timespec="seconds")
    cache.put(record("Helsing"))
    cache.put(record("Wayve", fetched_at=old))
    assert {r.company for r in cache.all()} == {"Helsing", "Wayve"}


# ── nothing unverified reaches the planner ──


def test_nothing_unverified_ever_reaches_the_planner(cache):
    """Fails closed, inverting `factcheck`, which passes a sentence it cannot
    grade. Refusing here costs only a blank paragraph, which is already what
    happens today, so there is no reason to take the risk."""
    cache.put(record(verified=False, outcome="unverified"))
    assert cached_notes("Helsing", cache=cache) == ""


def test_a_record_with_no_notes_is_not_usable_however_verified_it_is(cache):
    cache.put(record(notes="   ", verified=True))
    assert cached_notes("Helsing", cache=cache) == ""


def test_cached_notes_never_raises(tmp_path):
    """It is called while a letter is being written. A missing directory must
    not be able to fail a run whose only loss is a paragraph that is blank
    today anyway."""
    gone = ResearchCache(root=tmp_path / "does" / "not" / "exist")
    assert cached_notes("Helsing", cache=gone) == ""


# ── hand-written wins ──


def with_companies(profile, entries):
    return profile.model_copy(update={"companies": entries})


def test_a_hand_written_entry_beats_the_research_cache(profile, installed_cache):
    installed_cache.put(record("Helsing", notes="MACHINE TEXT"))
    mine = with_companies(profile, [{"id": "co.helsing", "name": "Helsing",
                                     "notes": "HAND WRITTEN", "angle": "MY ANGLE"}])
    text, origin, _ = company_notes_with_source(mine, "Helsing GmbH")
    assert text == "HAND WRITTEN MY ANGLE"
    assert origin == "profile"


def test_a_hand_written_entry_with_blank_notes_still_beats_the_machine(profile, installed_cache):
    """An entry with a name and nothing else is Simon saying "I looked, there is
    nothing". Falling through to the cache there would make a deliberate blank
    impossible to record."""
    installed_cache.put(record("Helsing", notes="MACHINE TEXT"))
    mine = with_companies(profile, [{"id": "co.helsing", "name": "Helsing"}])
    text, origin, _ = company_notes_with_source(mine, "Helsing")
    assert text == ""
    assert origin == "profile"


def test_the_research_cache_is_read_only_when_no_entry_matches(profile, installed_cache):
    installed_cache.put(record("Helsing", notes="MACHINE TEXT"))
    mine = with_companies(profile, [{"id": "co.wayve", "name": "Wayve", "notes": "elsewhere"}])
    text, origin, urls = company_notes_with_source(mine, "Helsing")
    assert text == "MACHINE TEXT"
    assert origin == "research"
    assert urls == ["https://blog.helsing.ai/"]


def test_an_alias_still_matches_before_the_machine_is_consulted(profile, installed_cache):
    installed_cache.put(record("Meta Platforms", notes="MACHINE TEXT"))
    mine = with_companies(profile, [{"id": "co.meta", "name": "Meta",
                                     "aliases": ["Meta Platforms"], "notes": "HAND"}])
    text, origin, _ = company_notes_with_source(mine, "Meta Platforms")
    assert (text, origin) == ("HAND", "profile")


def test_no_company_at_all_is_an_empty_answer_not_a_lookup(profile, installed_cache):
    assert company_notes_with_source(with_companies(profile, []), "") == ("", "", [])


# ── the letter path ──


def test_an_empty_cache_leaves_the_planner_prompt_saying_do_not_invent_one(
    profile, installed_cache
):
    from pitwall.apply.analyzer import JobAnalysis
    from pitwall.apply.matcher import match
    from pitwall.apply.truth import build_truth

    analysis = JobAnalysis(role_family="QUANTITATIVE")
    client = StubClient(PLAN_REPLY)
    plan, _ = plan_letter(
        with_companies(profile, []), analysis, match(analysis, profile),
        build_truth(profile), router(groq=client),
        {"title": "Quant", "posting_company": "Helsing"},
    )
    assert "(none — do not invent one)" in client.prompts[0]
    assert plan.company_notes_source == ""
    assert plan.company_sources == []


def test_cached_notes_reach_the_planner_prompt(profile, installed_cache):
    from pitwall.apply.analyzer import JobAnalysis
    from pitwall.apply.matcher import match
    from pitwall.apply.truth import build_truth

    installed_cache.put(record("Helsing", notes="They build the Centaur agent."))
    analysis = JobAnalysis(role_family="QUANTITATIVE")
    client = StubClient(PLAN_REPLY)
    plan, _ = plan_letter(
        with_companies(profile, []), analysis, match(analysis, profile),
        build_truth(profile), router(groq=client),
        {"title": "Quant", "posting_company": "Helsing GmbH"},
    )
    assert "They build the Centaur agent." in client.prompts[0]
    assert plan.company_notes_source == "research"
    assert plan.company_sources == ["https://blog.helsing.ai/"]


def test_the_plan_records_where_the_company_notes_came_from(profile, installed_cache):
    """Provenance, not decoration: a claim about an employer is the one class of
    claim `factcheck` cannot judge, because `_FIRST_PERSON` skips every
    third-person sentence."""
    from pitwall.apply.analyzer import JobAnalysis
    from pitwall.apply.matcher import match
    from pitwall.apply.truth import build_truth

    mine = with_companies(profile, [{"name": "Helsing", "notes": "HAND",
                                     "source_urls": ["https://helsing.ai/x"]}])
    analysis = JobAnalysis(role_family="QUANTITATIVE")
    plan, _ = plan_letter(
        mine, analysis, match(analysis, profile), build_truth(profile),
        router(groq=StubClient(PLAN_REPLY)),
        {"title": "Quant", "posting_company": "Helsing"},
    )
    assert plan.company_notes_source == "profile"
    assert plan.company_sources == ["https://helsing.ai/x"]


def test_the_plan_schema_never_asks_the_model_for_a_source_url(profile):
    """`company_sources` is set from the cache AFTER the call. In the schema it
    would be a request for invented URLs."""
    from pitwall.apply.planner import PLAN_SCHEMA

    assert "company_sources" not in PLAN_SCHEMA["properties"]
    assert "company_notes_source" not in PLAN_SCHEMA["properties"]


def test_the_planner_never_imports_the_searcher():
    """The whole reason `research_cache` is a separate module from `research`.

    Run in a subprocess, because another test in this session may legitimately
    have imported the searcher already. If reading a letter's company notes ever
    drags `build_chain` -> `ddgs` -> `httpx` into the import graph, the next
    person to add a `if stale: refresh()` line finds nothing stopping them.
    """
    code = (
        "import sys; sys.path.insert(0, 'src');\n"
        "from pitwall.apply import planner, research_cache;\n"
        "assert 'pitwall.apply.research' not in sys.modules, 'the searcher was imported';\n"
        "assert 'pitwall.search' not in sys.modules, 'the search chain was imported';\n"
        "assert 'httpx' not in sys.modules, 'httpx was imported';\n"
        "print('clean')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr
    assert "clean" in done.stdout


# ── the name-mention filter ──

MEETDAVIS_SERP = [
    ("https://engineering.ucdavis.edu/blog/meet-the-new-faculty", "Meet the new faculty", "…"),
    ("https://mae.ucdavis.edu/blog/316", "MAE blog", "UC Davis engineering"),
    ("https://www.davisconstruction.com/expertise/field-engineering", "Field engineering", "…"),
    ("https://www.researchgate.net/search/publications", "Search | ResearchGate",
     "Find the research you need for Meetdavis and everyone else"),
    ("https://papergate.org/", "PaperGate", "Search 450M+ papers on Meetdavis and more"),
    ("https://www.academia.edu/", "Academia.edu", "Share research"),
    ("https://blog.bytebytego.com/p/79-engineering-blogs", "79 engineering blogs", "…"),
    ("https://engineering.ucdavis.edu/blog", "UC Davis College of Engineering", "…"),
    ("https://github.com/meetdavis", "meetdavis", "Meetdavis on GitHub"),
    ("https://freehire.me/jobs/founding-data-engineer-meetdavis-w5dvp6hf",
     "Founding Data Engineer — Meetdavis", "Build the data pipeline at Meetdavis"),
    ("https://freehire.me/jobs/ai-engineer-intern-meetdavis-zm6udko6",
     "AI Engineer Intern — Meetdavis", "An AI-native builder at Meetdavis"),
]


def serps(rows):
    from pitwall.search.base import SearchHit

    return [SearchHit(rank=n, url=u, title=t, snippet=s)
            for n, (u, t, s) in enumerate(rows)]


def test_the_meetdavis_poisoning_never_reaches_the_model():
    """The run this whole module is built around.

    Eight of these eleven results are pages that discuss everybody. The model read
    them and wrote a confident sentence about discrete diffusion models, citing
    ResearchGate, reporting `confidence: "high"`. Every one of the eight has to be
    gone before a prompt is built.
    """
    from pitwall.apply.research import filter_hits

    kept, dropped = filter_hits(serps(MEETDAVIS_SERP), "Meetdavis")
    assert {h.url for h in kept} == {
        "https://github.com/meetdavis",
        "https://freehire.me/jobs/founding-data-engineer-meetdavis-w5dvp6hf",
        "https://freehire.me/jobs/ai-engineer-intern-meetdavis-zm6udko6",
    }
    assert len(dropped) == 8


def test_the_companys_own_github_and_job_listings_are_kept():
    """Guards against "simplifying" NOISE_HOSTS into `enrich.domains`'s
    NON_COMPANY_HOSTS, which contains github.com, lever.co and medium.com —
    precisely the three sources that survived the real run."""
    from pitwall.apply.research import NOISE_HOSTS

    for host in ("github.com", "lever.co", "medium.com", "wikipedia.org", "substack.com"):
        assert host not in NOISE_HOSTS, f"{host} carries a company's own writing"


def test_a_content_farm_is_dropped_even_when_it_names_the_company():
    from pitwall.apply.research import on_topic

    hit = serps([("https://www.researchgate.net/x", "Helsing research",
                  "Helsing Helsing Helsing")])[0]
    assert not on_topic(hit, "Helsing")


def test_a_longer_word_containing_the_name_is_not_a_mention():
    from pitwall.apply.research import on_topic

    hit = serps([("https://example.com/x", "Helsingborg harbour", "a port in Sweden")])[0]
    assert not on_topic(hit, "Helsing")


def test_a_hit_on_the_companys_own_domain_survives_a_snippet_that_omits_the_name():
    from pitwall.apply.research import on_topic

    hit = serps([("https://helsing.ai/newsroom/x", "Newsroom", "An announcement.")])[0]
    assert on_topic(hit, "Helsing")


@pytest.mark.parametrize("spelling", ["Meetdavis", "MeetDavis", "meetdavis"])
def test_a_run_together_name_matches_however_it_is_spaced(spelling):
    from pitwall.apply.research import on_topic

    hit = serps([("https://example.com/x", "Meet Davis raises a seed round", "…")])[0]
    assert on_topic(hit, spelling)


def test_the_same_page_twice_is_one_source():
    from pitwall.apply.research import dedupe

    hits = serps([
        ("https://blog.helsing.ai/posts/x", "X", "a"),
        ("https://blog.helsing.ai/posts/x?gi=abc123", "X", "a"),
        ("https://blog.helsing.ai/posts/y", "Y", "b"),
    ])
    assert len(dedupe(hits)) == 2


# ── the minimum source count ──


def a_router(**clients):
    from pitwall.apply.llm import TaskRouter

    return TaskRouter(clients=clients)


class StubSearchChain:
    """A searcher that answers every query with the same canned SERP."""

    def __init__(self, hits, *, operators=False):
        self.hits = hits
        self.operators = operators
        self.queries: list[str] = []
        self.fail_on: set[str] = set()

    def supports_operators(self) -> bool:
        return self.operators

    def search(self, query, *, max_results=10):
        self.queries.append(query)
        for needle in self.fail_on:
            if needle in query:
                raise RuntimeError(f"stub refusing {needle!r}")
        return list(self.hits)[:max_results]


# An ENGINEERING fact, not a meta-fact. The first version of this fixture said
# "Helsing publishes an engineering blog about the Twirp protocol", and once
# `meta_hits` existed it was correctly rejected — that they publish a blog is not a
# fact about what they build.
DRAFT_REPLY = {
    "notes": [{"sentence": "Helsing introduces Twurst, a Rust implementation of the "
                           "Twirp protocol.", "source_index": 1}],
    "confidence": "high",
}
VERIFY_REPLY = {"supported": [1], "unsupported": [],
                "engineering": [1], "business": [], "marketing": []}

GOOD_SERP = [
    ("https://blog.helsing.ai/posts/twurst", "Twurst | Helsing",
     "Helsing introduces Twurst, a Rust implementation of the Twirp protocol, on its "
     "engineering blog, for brownfield networks without HTTP/2 support."),
    ("https://helsing.ai/newsroom/x", "Helsing newsroom",
     "Helsing publishes an engineering blog and describes the Twirp protocol work."),
    ("https://en.wikipedia.org/wiki/Helsing_(company)", "Helsing (company)",
     "Helsing tested its Centaur agent in a trial known as Project Beyond."),
]


def research(company, serp, cache, *replies, fetcher=None, **kw):
    from pitwall.apply.research import research_company

    client = StubClient(*(replies or (DRAFT_REPLY, VERIFY_REPLY)))
    report = research_company(
        company, searcher=StubSearchChain(serps(serp)), router=a_router(groq=client),
        cache=cache, fetcher=fetcher or (lambda url: ""), **kw,
    )
    return report, client


def test_too_few_on_topic_sources_means_no_model_call_at_all(cache):
    """The assertion the whole filter buys: no prompt, no fabrication."""
    report, client = research("Meetdavis", MEETDAVIS_SERP[:8], cache)
    assert report.outcome == "thin"
    assert client.prompts == [], "the model was asked despite too few sources"
    assert not report.record.usable


def test_three_snippets_from_one_domain_are_not_three_sources(cache):
    one_site = [(f"https://helsing.ai/{n}", "Helsing", "Helsing does things") for n in range(4)]
    report, client = research("Helsing", one_site, cache)
    assert report.outcome == "thin"
    assert client.prompts == []


def test_a_company_with_nothing_findable_is_cached_as_a_negative_result(cache):
    research("Meetdavis", MEETDAVIS_SERP[:8], cache)
    stored = cache.get("Meetdavis")
    assert stored is not None and stored.outcome == "thin" and stored.notes == ""


def test_a_negative_result_is_not_searched_again_inside_the_ttl(cache):
    from pitwall.apply.research import research_company

    searcher = StubSearchChain(serps(MEETDAVIS_SERP[:8]))
    for _ in range(2):
        research_company("Meetdavis", searcher=searcher, router=a_router(groq=StubClient({})),
                         cache=cache, fetcher=lambda url: "")
    assert len(searcher.queries) == 3, "the second run searched again"


def test_no_search_provider_at_all_writes_no_record(cache):
    """A failure to LOOK is not a finding that there is nothing, so the next run
    must try again rather than being served this back."""
    from pitwall.apply.research import research_company

    searcher = StubSearchChain([])
    searcher.fail_on = {"Helsing"}
    report = research_company("Helsing", searcher=searcher, router=a_router(groq=StubClient({})),
                              cache=cache, fetcher=lambda url: "")
    assert report.outcome == "no-provider"
    assert cache.get("Helsing") is None


def test_a_failing_query_does_not_end_the_research(cache):
    from pitwall.apply.research import research_company

    searcher = StubSearchChain(serps(GOOD_SERP))
    searcher.fail_on = {"engineering blog"}
    report = research_company(
        "Helsing", searcher=searcher, router=a_router(groq=StubClient(DRAFT_REPLY, VERIFY_REPLY)),
        cache=cache, fetcher=lambda url: "",
    )
    assert report.queries_run == 2
    assert report.record.usable


def test_the_queries_carry_no_operators(cache):
    """ddgs drops operators, and ddgs is the provider that answers on this
    machine. A quoted query would be silently reinterpreted."""
    from pitwall.apply.research import research_company

    searcher = StubSearchChain(serps(GOOD_SERP), operators=False)
    research_company("Helsing", searcher=searcher, router=a_router(groq=StubClient(
        DRAFT_REPLY, VERIFY_REPLY)), cache=cache, fetcher=lambda url: "")
    assert all('"' not in q and "site:" not in q for q in searcher.queries)


def test_a_quoted_query_is_added_when_the_provider_keeps_operators(cache):
    from pitwall.apply.research import research_company

    searcher = StubSearchChain(serps(GOOD_SERP), operators=True)
    research_company("Helsing", searcher=searcher, router=a_router(groq=StubClient(
        DRAFT_REPLY, VERIFY_REPLY)), cache=cache, fetcher=lambda url: "")
    assert any('"Helsing"' in q for q in searcher.queries)


def test_a_chain_with_only_ddgs_still_answers():
    """The blocking finding: `SerpSearcher` drops every provider whose
    `supports_operators()` is False, ddgs returns False, and SearXNG is not
    running — so the default left research with no providers and every run
    silently empty."""
    from pitwall.enrich.base import SerpSearcher
    from pitwall.search.ddg import DDGSProvider

    ddgs = DDGSProvider()
    assert ddgs.supports_operators() is False
    assert SerpSearcher(providers=[ddgs], operators_only=True).providers == []
    assert SerpSearcher(providers=[ddgs], operators_only=False).providers == [ddgs]


# ── grounding ──


def test_a_sentence_whose_terms_are_in_no_snippet_is_dropped(cache):
    """The fabrication itself. No kept snippet contains "diffusion"."""
    lie = {"notes": [{"sentence": "Meetdavis develops and deploys discrete diffusion models.",
                      "source_index": 1}], "confidence": "high"}
    report, _ = research("Helsing", GOOD_SERP, cache, lie, VERIFY_REPLY)
    assert report.record.notes == ""
    assert any("diffusion" in r.lower() for r in report.record.rejected)


def test_a_confident_model_does_not_make_an_ungrounded_sentence_survive(cache):
    """`confidence` is recorded and never consulted. The real fabrication came
    back `high` and cited ResearchGate for it."""
    lie = {"notes": [{"sentence": "Helsing operates a quantum photonics fabrication line.",
                      "source_index": 1}], "confidence": "high"}
    report, _ = research("Helsing", GOOD_SERP, cache, lie, VERIFY_REPLY)
    assert not report.record.usable
    assert report.record.model_confidence == "high"


def test_a_sentence_assembled_from_two_sources_is_not_grounded(cache):
    """Every distinctive term has to be in ONE source. Borrowing a noun from each
    of two pages is how a true-sounding false claim gets made — a live run said a
    drone was launched "off the U.S. coast" when the launch was off Plymouth."""
    from pitwall.apply.research import best_source

    hits = serps([
        ("https://a.example/1", "One", "Helsing introduces Twurst for brownfield networks."),
        ("https://a.example/2", "Two", "Helsing tested Centaur in Project Beyond."),
    ])
    _, loose = best_source("Helsing tested Twurst in Project Beyond.", hits)
    assert loose, "a recombination across two sources was treated as grounded"


def test_a_citation_is_corrected_rather_than_the_sentence_thrown_away(cache):
    """A wrong index is a bookkeeping error, not a lie, when some other source
    really does say the whole thing."""
    from pitwall.apply.research import best_source

    hits = serps([
        ("https://a.example/1", "One", "nothing relevant here at all"),
        ("https://a.example/2", "Two", "Helsing introduces Twurst, a Twirp implementation."),
    ])
    cited, loose = best_source("Helsing introduces Twurst.", hits, preferred=1)
    assert (cited, loose) == (2, [])


def test_an_inflected_form_still_counts_as_grounded():
    """"partnering" against a source that says "partner" was a real rejection and
    the filter was the thing that was wrong."""
    from pitwall.apply.research import ungrounded_terms

    assert ungrounded_terms("Helsing is partnering with governments.",
                            "Helsing says: we partner with governments") == []
    assert ungrounded_terms("The drone was launched off Plymouth.",
                            "Helsing tests HX-2 drone launch off Plymouth coast") == []


def test_a_figure_in_no_snippet_is_dropped(cache):
    figure = {"notes": [{"sentence": "Helsing introduces Twurst across 47 networks.",
                         "source_index": 1}], "confidence": "high"}
    report, _ = research("Helsing", GOOD_SERP, cache, figure, VERIFY_REPLY)
    assert any("47" in r for r in report.record.rejected)


def test_a_dropped_sentence_says_why_it_was_dropped(cache):
    """A good sentence killed by an over-strict rule has to be visible, or the
    rule can never be tuned."""
    lie = {"notes": [{"sentence": "Helsing pioneered quantum photonic lithography.",
                      "source_index": 1}], "confidence": "high"}
    report, _ = research("Helsing", GOOD_SERP, cache, lie, VERIFY_REPLY)
    assert report.record.rejected and " — " in report.record.rejected[0]


@pytest.mark.parametrize("sentence", [
    "Helsing is a leading AI company.",
    "Helsing is at the forefront of defence AI.",
    "Helsing is committed to advancing European security.",
    "Helsing is a mission-driven engineering organisation.",
    "Helsing is a fast-growing defence startup.",
    "Helsing takes an innovative approach to autonomy.",
    "Helsing is revolutionising European defence.",
])
def test_flattery_is_rejected_however_it_is_phrased(sentence):
    from pitwall.apply.research import flattery_hits

    assert flattery_hits(sentence), f"praise slipped through: {sentence}"


def test_a_plain_statement_of_work_is_not_flattery():
    from pitwall.apply.research import flattery_hits

    assert flattery_hits("Helsing introduces Twurst, a Rust implementation of Twirp.") == []


def test_a_banned_phrase_from_style_yaml_is_rejected_here_too(cache, profile):
    phrase = (profile.style.banned_phrases or ["passionate about"])[0]
    reply = {"notes": [{"sentence": f"Helsing has a {phrase} engineering blog for Twirp.",
                        "source_index": 1}], "confidence": "high"}
    report, _ = research("Helsing", GOOD_SERP, cache, reply, VERIFY_REPLY, profile=profile)
    assert not report.record.usable


def test_a_sentence_that_cites_a_source_index_that_does_not_exist_is_dropped(cache):
    reply = {"notes": [{"sentence": "Helsing publishes an engineering blog about Twirp.",
                        "source_index": 99}], "confidence": "high"}
    report, _ = research("Helsing", GOOD_SERP, cache, reply, VERIFY_REPLY)
    assert any("99" in r for r in report.record.rejected)


# ── the verification call ──


def test_the_verifier_is_not_the_drafter_when_two_providers_are_reachable(cache):
    from pitwall.apply.research import research_company

    groq, ollama = StubClient(DRAFT_REPLY), StubClient(VERIFY_REPLY)
    report = research_company(
        "Helsing", searcher=StubSearchChain(serps(GOOD_SERP)),
        router=a_router(groq=groq, ollama=ollama), cache=cache, fetcher=lambda url: "",
    )
    assert report.record.independent
    assert len(ollama.prompts) == 1, "the second provider was not asked"


def test_a_self_verified_record_is_recorded_as_not_independent(cache):
    report, _ = research("Helsing", GOOD_SERP, cache)
    assert report.record.verified and not report.record.independent


def test_an_unreadable_verdict_fails_closed(cache):
    """Silence is not a pass. A sentence has to be listed as supported."""
    report, _ = research("Helsing", GOOD_SERP, cache, DRAFT_REPLY,
                         {"supported": [], "unsupported": [],
                          "engineering": [], "business": [], "marketing": []})
    assert not report.record.usable
    assert cached_notes("Helsing", cache=cache) == ""


def test_an_unsupported_verdict_drops_that_sentence_and_keeps_the_rest(cache):
    two = {"notes": [
        {"sentence": "Helsing introduces Twurst for brownfield networks.", "source_index": 1},
        {"sentence": "Helsing tested Centaur in Project Beyond.", "source_index": 3},
    ], "confidence": "high"}
    report, _ = research("Helsing", GOOD_SERP, cache, two,
                         {"supported": [2], "unsupported": [1],
                          "engineering": [2], "business": [], "marketing": []})
    assert "Centaur" in report.record.notes
    assert "Twurst" not in report.record.notes
    assert report.record.rejected


def test_a_third_person_claim_about_the_employer_is_actually_checked(cache):
    """The hole this closes: `factcheck._FIRST_PERSON` routes every sentence with
    no "I" or "my" straight to SUPPORTED without a call, and every research
    sentence is third person."""
    from pitwall.apply.factcheck import _FIRST_PERSON

    sentence = "Helsing introduces Twurst, a Rust implementation of the Twirp protocol."
    assert not _FIRST_PERSON.search(sentence), "the premise of this test changed"
    report, client = research("Helsing", GOOD_SERP, cache)
    assert any(sentence.split(",")[0] in p for p in client.prompts[1:]), \
        "the verifier was never shown the sentence"


def test_requiring_independence_refuses_a_self_verified_record(cache, monkeypatch):
    from pitwall.config import get_settings

    settings = get_settings().model_copy(update={"apply_research_require_independent": True})
    report, _ = research("Helsing", GOOD_SERP, cache, settings=settings)
    assert not report.record.usable
    assert any("only the drafting model" in r for r in report.record.rejected)


# ── page fetching ──


THIN_SERP = [
    ("https://helsing.ai/blog/twurst", "Twurst", "Helsing, short."),
    ("https://helsing.ai/newsroom/x", "Newsroom", "Helsing, also short."),
    ("https://defence-blog.com/helsing", "Helsing coverage", "Helsing does defence things."),
]


def test_a_page_is_read_only_from_a_domain_tied_to_the_company(cache):
    fetched: list[str] = []

    def fetcher(url):
        fetched.append(url)
        return "<p>Helsing introduces Twurst, a Rust implementation of Twirp.</p>"

    research("Helsing", THIN_SERP, cache, fetcher=fetcher)
    assert fetched and all("helsing.ai" in u for u in fetched)


def test_at_most_two_pages_are_read(cache):
    many = [(f"https://helsing.ai/p{n}", "Helsing", "Helsing, short.") for n in range(6)]
    many.append(("https://defence-blog.com/h", "Helsing", "Helsing coverage."))
    calls: list[str] = []
    research("Helsing", many, cache, fetcher=lambda u: calls.append(u) or "<p>x</p>")
    assert len(calls) == 2


def test_nothing_is_fetched_when_one_source_is_already_deep_enough(cache):
    def fetcher(url):
        raise AssertionError("should not have fetched anything")

    report, _ = research("Helsing", GOOD_SERP, cache, fetcher=fetcher)
    assert report.pages_fetched == 0


def test_a_robots_refusal_is_a_note_rather_than_a_crash(cache):
    def fetcher(url):
        raise RuntimeError("robots.txt disallows this")

    report, _ = research("Helsing", THIN_SERP, cache, fetcher=fetcher)
    assert any("was not read" in n for n in report.notes)


def test_a_fetched_page_becomes_a_citable_source(cache):
    body = "<p>Helsing introduces Twurst, a Rust implementation of the Twirp protocol.</p>"
    reply = {"notes": [{"sentence": "Helsing introduces Twurst, a Rust implementation of Twirp.",
                        "source_index": 1}], "confidence": "high"}
    report, _ = research("Helsing", THIN_SERP, cache, reply, VERIFY_REPLY,
                         fetcher=lambda url: body)
    assert report.pages_fetched
    assert report.record.usable


# ── the pre-pass ──


def test_each_company_is_researched_once_for_a_batch_of_many_jobs(cache, profile, monkeypatch):
    """Five jobs across two employers is two research runs, not five. What a
    company builds does not depend on which of its postings is being answered."""
    from pitwall.apply import research as research_mod

    jobs = {1: "Helsing", 2: "Helsing", 3: "Wayve", 4: "Helsing", 5: "Wayve"}
    monkeypatch.setattr(research_mod, "companies_for", lambda ids: jobs, raising=False)
    monkeypatch.setattr("pitwall.apply.jobs.companies_for", lambda ids, conn=None: jobs)

    asked: list[str] = []
    monkeypatch.setattr(
        research_mod, "research_company",
        lambda name, **kw: asked.append(name) or research_mod.ResearchReport(),
    )
    out = research_mod.prepass(list(jobs), profile=with_companies(profile, []), cache=cache)
    assert asked == ["Helsing", "Wayve"]
    assert set(out) == {"Helsing", "Wayve"}


def test_a_company_simon_has_written_about_is_never_searched(cache, profile, monkeypatch):
    from pitwall.apply import research as research_mod

    monkeypatch.setattr("pitwall.apply.jobs.companies_for",
                        lambda ids, conn=None: {1: "Helsing", 2: "Wayve"})
    asked: list[str] = []
    monkeypatch.setattr(
        research_mod, "research_company",
        lambda name, **kw: asked.append(name) or research_mod.ResearchReport(),
    )
    mine = with_companies(profile, [{"name": "Helsing", "notes": "I read their blog."}])
    research_mod.prepass([1, 2], profile=mine, cache=cache)
    assert asked == ["Wayve"], "an employer with a hand-written entry was searched anyway"


def test_a_company_with_a_fresh_record_is_not_researched_again(cache, profile, monkeypatch):
    from pitwall.apply import research as research_mod

    cache.put(record("Helsing"))
    monkeypatch.setattr("pitwall.apply.jobs.companies_for", lambda ids, conn=None: {1: "Helsing"})
    asked: list[str] = []
    monkeypatch.setattr(
        research_mod, "research_company",
        lambda name, **kw: asked.append(name) or research_mod.ResearchReport(),
    )
    research_mod.prepass([1], profile=with_companies(profile, []), cache=cache)
    assert asked == []


def test_a_research_failure_does_not_stop_the_rest(cache, profile, monkeypatch):
    from pitwall.apply import research as research_mod

    def explode(name, **kw):
        if name == "Helsing":
            raise RuntimeError("the searcher fell over")
        return research_mod.ResearchReport()

    monkeypatch.setattr(research_mod, "research_company", explode)
    reports = research_mod.research_companies(["Helsing", "Wayve"], cache=cache)
    assert [r.outcome for r in reports] == ["error", "ok"]


def test_a_batch_still_writes_letters_when_research_blows_up(monkeypatch, tmp_path):
    """Research is an improvement on the letters, not a dependency of them."""
    from pitwall.apply import batch as batch_mod

    monkeypatch.setattr("pitwall.apply.loader.load_profile", lambda *a, **k: None)
    monkeypatch.setattr(batch_mod, "LEDGER_DIR", tmp_path / "ledger")
    monkeypatch.setattr("pitwall.apply.research.prepass",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no network")))

    made = []

    def fake_generate(job_id, **kw):
        made.append(job_id)
        return SimpleNamespace(company="X", title="T", prompt_tokens=0, completion_tokens=0,
                               letter_path=None, cv_pdf=None, directory=tmp_path,
                               blocked=[], warnings=[], needs_review=False, ok=True,
                               fact=None, error="")

    batch_mod.run_batch([1, 2], name="t", out=tmp_path, cv=False, generate=fake_generate)
    assert made == [1, 2], "a research failure stopped the batch"


# ── what counts as a claim ──


@pytest.mark.parametrize("word", ["publishes", "called", "delivers", "speeds",
                                  "building", "designed", "quickly", "networks"])
def test_an_ordinary_verb_or_plural_is_not_a_claim_to_be_grounded(word):
    """The first six-company run threw away a whole employer over "publishes" and
    "called". A sentence's verbs are how it is phrased; its nouns are what it
    asserts, and only the assertions have to be in a source."""
    from pitwall.apply.research import is_claim_bearing

    assert not is_claim_bearing(word)


@pytest.mark.parametrize("word", ["diffusion", "orchestration", "Symphony",
                                  "Centaur", "Germany", "HX-2", "GPT-4"])
def test_a_proper_noun_or_technical_term_is_a_claim(word):
    from pitwall.apply.research import is_claim_bearing

    assert is_claim_bearing(word) or word[:1].isupper()


def test_an_invented_proper_noun_is_caught_even_among_true_words():
    """Capitalisation is the whole fabrication surface. Everything else in this
    sentence is in the source; the name is not."""
    from pitwall.apply.research import ungrounded_terms

    loose = ungrounded_terms(
        "The team publishes a spec for orchestration called Symphony.",
        "OpenAI published a spec for agent orchestration on its engineering blog",
    )
    assert any("Symphony" in t for t in loose)
    assert not any("publishes" in t for t in loose)


def test_the_employer_is_named_after_verification_not_before(cache):
    """The prefix must not be shown to the verifier.

    The first version rewrote the sentence during screening, so the verifier
    judged text the drafter never wrote — and when it dropped that one sentence,
    the survivors were back to "The team publishes…" with the employer named
    nowhere. A real OpenAI run shipped exactly that.
    """
    two = {"notes": [
        {"sentence": "The team tested Centaur in Project Beyond.", "source_index": 3},
        {"sentence": "The team introduces Twurst for brownfield networks.", "source_index": 1},
    ], "confidence": "high"}
    report, client = research("Helsing", GOOD_SERP, cache, two,
                              {"supported": [2], "unsupported": [1]})
    verify_prompt = client.prompts[-1]
    assert "At Helsing," not in verify_prompt, "the verifier was shown a sentence we rewrote"
    assert report.record.notes.startswith("At Helsing,")


def test_a_sentence_that_already_names_the_employer_is_left_alone(cache):
    from pitwall.apply.research import name_the_employer

    said = [{"text": "Helsing introduces Twurst.", "source": "u"}]
    assert name_the_employer(said, "Helsing") == said


def test_a_note_never_carries_typography_the_letter_path_has_to_cope_with():
    """A cached note is quoted into two prompts and a PDF.

    Groq's model reaches for U+202F and U+2011 constantly, and
    `letter.THIN_SPACE_DIGITS` treats a narrow no-break space between digits as a
    violation to rewrite — so a note carrying one makes the letter harder to
    accept for no gain at all.
    """
    from pitwall.apply.research import plain_text

    out = plain_text("Claude AI and AI‑defined systems — Anthropic’s blog")
    assert out == "Claude AI and AI-defined systems - Anthropic's blog"
    assert not any(ord(ch) > 127 for ch in out)


def test_a_drafted_sentence_is_flattened_before_it_is_cached(cache):
    fancy = {"notes": [{"sentence": "Helsing introduces Twurst for brownfield networks.",
                        "source_index": 1}], "confidence": "high"}
    report, _ = research("Helsing", GOOD_SERP, cache, fancy, VERIFY_REPLY)
    assert " " not in report.record.notes


# ── the quality gate: true is not the same as worth saying ──


def test_a_sentence_cut_off_mid_clause_is_rejected():
    """The real shipped string. The source said "the Responses API"; the snippet
    clipping landed mid-phrase and the model completed a fragment."""
    from pitwall.apply.research import truncated_against

    why = truncated_against(
        "The team speeds up agentic workflows with WebSockets in the Responses.",
        "OpenAI speeds up agentic workflows with WebSockets in the Responses API.",
    )
    assert "cut in half" in why


def test_an_ordinary_sentence_ending_in_a_place_is_not_truncated():
    """The first version asked only whether the source carried on, and rejected
    "a factory in the Netherlands" because the source said "Netherlands last year".
    A cut-off NAME continues with a capital; a sentence carries on in lower case."""
    from pitwall.apply.research import truncated_against

    assert not truncated_against(
        "Helsing has a factory in the Netherlands.",
        "Helsing opened a factory in the Netherlands last year.",
    )


@pytest.mark.parametrize(("text", "caught"), [
    ("They build systems for", True),
    ("Too short here.", True),
    ("A sentence with (an unclosed bracket.", True),
    ("Helsing introduces Twurst, a Rust implementation of the Twirp protocol.", False),
])
def test_an_incomplete_sentence_is_rejected(text, caught):
    from pitwall.apply.research import incomplete_sentence

    assert bool(incomplete_sentence(text)) is caught


def test_that_a_company_has_a_blog_is_not_a_fact_about_what_they_build():
    from pitwall.apply.research import meta_hits

    assert meta_hits("The team publishes engineering blog posts demonstrating the model.")
    assert meta_hits("Wayve announced a press release about its platform.")


def test_an_open_source_spec_is_still_kept():
    """The regression guard. A blunt rule on `publishes` would reject the best
    sentence this feature has produced."""
    from pitwall.apply.research import meta_hits

    assert meta_hits(
        "The team publishes an open-source spec for orchestration called Symphony."
    ) == []


def test_marketing_language_is_dropped_even_when_a_source_says_it(cache):
    """Helsing's own press-release phrasing. Genuinely in the source, so every
    deterministic proxy passes it — this is the one the label is for."""
    marketing = {"notes": [{
        "sentence": "Helsing introduces Twurst, a Rust implementation of the Twirp protocol.",
        "source_index": 1}], "confidence": "high"}
    report, _ = research("Helsing", GOOD_SERP, cache, marketing,
                         {"supported": [1], "unsupported": [],
                          "engineering": [], "business": [], "marketing": [1]})
    assert report.record.notes == ""
    assert any("true of a competitor" in r for r in report.record.rejected)


def test_an_unlabelled_sentence_is_treated_as_business_not_dropped():
    """Silence means the middle. The fail-closed decision is taken on the support
    axis, so a model that forgets to label must not thereby delete."""
    from pitwall.apply.research import LABEL_RANK

    assert LABEL_RANK["engineering"] < LABEL_RANK["business"]
    assert "marketing" not in LABEL_RANK, "marketing is dropped, not ranked"


def test_only_the_best_sentences_reach_the_letter(cache):
    from pitwall.config import get_settings

    # Both must be GROUNDED, or the loser is dropped by the grounding check before
    # the ranking ever sees it and the cap is never exercised.
    two = {"notes": [
        {"sentence": "Helsing tested its Centaur agent in a trial known as Project Beyond.",
         "source_index": 3},
        {"sentence": "Helsing introduces Twurst, a Rust implementation of the Twirp protocol.",
         "source_index": 1},
    ], "confidence": "high"}
    settings = get_settings().model_copy(update={"apply_research_max_notes": 1})
    report, _ = research("Helsing", GOOD_SERP, cache, two,
                         {"supported": [1, 2], "unsupported": [],
                          "engineering": [2], "business": [1], "marketing": []},
                         settings=settings)
    assert "Twurst" in report.record.notes, "the engineering fact was not ranked first"
    assert "Centaur" not in report.record.notes
    assert any("only the best" in r for r in report.record.rejected)


def test_the_verify_schema_stays_flat():
    """`schema_hint` can only name top-level keys, and a nested item shape reaching
    the local model came back invented."""
    from pitwall.apply.research import VERIFY_SCHEMA

    for name, spec in VERIFY_SCHEMA["properties"].items():
        assert spec == {"type": "array", "items": {"type": "integer"}}, name


def test_a_snippet_is_clipped_at_a_sentence_boundary():
    from pitwall.apply.research import clip

    # Cuts back to a sentence end when one is reasonably placed…
    two_sentences = "First sentence here. Second sentence here. " + "padding " * 60
    out = clip(two_sentences, 60)
    assert out == "First sentence here. Second sentence here."

    # …but never throws away most of the window to reach one. A boundary at
    # character 20 of a 200-character budget is not worth 180 characters, so the cut
    # is explicit instead and the model can see that what it has is partial.
    one_sentence = "Short. " + ("padding words " * 60) + "trailing clause."
    out = clip(one_sentence, 200)
    assert out.endswith("\u2026")
    assert len(out) <= 202


def test_a_cached_record_without_labels_is_still_usable(cache):
    """Adding the label must not invalidate every cached company."""
    cache.put(record("Helsing"))
    stored = cache.get("Helsing")
    assert stored is not None and stored.usable
    assert all("label" not in s for s in stored.sentences)
