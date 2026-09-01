"""The corroboration search — §2.5, the free verifier.

`verified` is the one status the owner is told is safe to write to, so the
tests that matter are the ones that refuse to award it: a search engine that
relaxed the quotes, and a contact-data broker republishing the same guess.
"""

from __future__ import annotations

from pitwall.enrich.base import EmailCandidate
from pitwall.enrich.corroborate import (
    CORROBORATION_QUERIES,
    CorroborateStep,
    corroborate,
    corroborate_from_corpus,
    is_independent_source,
    mentions_address,
)
from tests.conftest import StubSearcher, serp

ADDRESS = "jane.doe@alpine.com"


def _pattern_finding(email=ADDRESS, status="probable"):
    return EmailCandidate(
        email=email, status=status, source="pattern",
        evidence={"pattern": "first.last", "sample_size": 3},
        mx_valid=True,
    )


def _published(url="https://alpine.com/team"):
    return {f'"{ADDRESS}"': [serp(url, "Our team | Alpine", f"Jane Doe — {ADDRESS}")]}


# ──────────────────────── what counts as evidence ────────────────────────


def test_a_result_must_actually_contain_the_address():
    """Search engines relax a quoted phrase when a strict match finds nothing,
    and a relaxed match returns a page about the person with no address on it."""
    real = serp("https://alpine.com/team", "Team", f"Jane Doe {ADDRESS}")
    relaxed = serp("https://alpine.com/team", "Jane Doe | Alpine", "Head of Aerodynamics")
    assert mentions_address(real, ADDRESS) is True
    assert mentions_address(relaxed, ADDRESS) is False


def test_the_address_can_be_in_the_url_or_the_title():
    assert mentions_address(serp(f"https://x/contact/{ADDRESS}", "", ""), ADDRESS) is True
    assert mentions_address(serp("https://x/", f"Email {ADDRESS}", ""), ADDRESS) is True


def test_mentions_address_on_an_empty_address():
    assert mentions_address(serp("https://x/", "t", "s"), "") is False


def test_contact_data_brokers_are_not_independent_sources():
    """Finding a guess on RocketReach proves another pattern engine made the
    same guess from the same name — the same reasoning twice, not confirmation."""
    for host in ("rocketreach.co", "www.zoominfo.com", "signalhire.com",
                 "hunter.io", "apollo.io", "uk.rocketreach.co"):
        assert is_independent_source(f"https://{host}/jane-doe") is False


def test_a_breach_dump_is_not_publication():
    """An address in a leak is not an address its owner published."""
    assert is_independent_source("https://dehashed.com/x") is False
    assert is_independent_source("https://pastebin.com/x") is False


def test_a_real_page_is_an_independent_source():
    assert is_independent_source("https://alpine.com/team") is True
    assert is_independent_source("https://fia.com/programme.pdf") is True
    assert is_independent_source("") is False


# ─────────────────────── the free corpus check first ─────────────────────


def test_an_address_already_in_the_corpus_needs_no_search(enrich_ctx):
    """If §2.2 already read this address off a public page, that page IS the
    corroboration — asking a search engine again spends a query on a known
    answer."""
    searcher = StubSearcher()
    ctx = enrich_ctx(searcher=searcher)
    ctx.samples = [{"email": ADDRESS, "source_url": "https://alpine.com/team"}]

    result = corroborate(ctx, ADDRESS)
    assert result.found is True
    assert result.domain == "alpine.com"
    assert result.queries_run == 0
    assert searcher.queries == []


def test_a_corpus_entry_from_a_broker_does_not_corroborate(enrich_ctx):
    ctx = enrich_ctx(searcher=StubSearcher())
    ctx.samples = [{"email": ADDRESS, "source_url": "https://rocketreach.co/jane"}]
    assert corroborate_from_corpus(ctx, ADDRESS) is None


def test_a_corpus_entry_with_no_source_url_does_not_corroborate(enrich_ctx):
    ctx = enrich_ctx(searcher=StubSearcher())
    ctx.samples = [{"email": ADDRESS, "source_url": None}]
    assert corroborate_from_corpus(ctx, ADDRESS) is None


# ────────────────────────────── the search ───────────────────────────────


def test_a_published_address_is_found(enrich_ctx):
    ctx = enrich_ctx(searcher=StubSearcher(_published()))
    result = corroborate(ctx, ADDRESS)
    assert result.found is True
    assert result.url == "https://alpine.com/team"
    assert result.as_evidence()["corroborating_domain"] == "alpine.com"


def test_the_exact_string_is_what_is_searched_for(enrich_ctx):
    searcher = StubSearcher()
    ctx = enrich_ctx(searcher=searcher)
    corroborate(ctx, ADDRESS)
    assert searcher.queries[0] == f'"{ADDRESS}"'
    assert searcher.queries[1] == f'"Jane Doe" "{ADDRESS}"'


def test_a_broker_hit_is_rejected_and_reported(enrich_ctx):
    ctx = enrich_ctx(searcher=StubSearcher({
        f'"{ADDRESS}"': [serp("https://rocketreach.co/jane-doe", "Jane Doe", ADDRESS)]
    }))
    result = corroborate(ctx, ADDRESS)
    assert result.found is False
    assert "rocketreach.co" in result.rejected


def test_a_relaxed_search_result_does_not_corroborate(enrich_ctx):
    ctx = enrich_ctx(searcher=StubSearcher({
        f'"{ADDRESS}"': [serp("https://alpine.com/people", "Jane Doe | Alpine", "Head of Aero")]
    }))
    assert corroborate(ctx, ADDRESS).found is False


def test_no_searcher_returns_no_corroboration(enrich_ctx):
    ctx = enrich_ctx(searcher=None)
    assert corroborate(ctx, ADDRESS).found is False


def test_a_provider_without_operators_refuses_to_guess(enrich_ctx):
    class NoOperators(StubSearcher):
        def supports_operators(self):
            return False

    ctx = enrich_ctx(searcher=NoOperators(_published()))
    assert corroborate(ctx, ADDRESS).found is False
    assert any("quoted phrases" in n for n in ctx.notes)


def test_a_failing_query_is_not_a_verdict(enrich_ctx):
    searcher = StubSearcher(_published())
    searcher.fail_on = {f'"{ADDRESS}"'}
    ctx = enrich_ctx(searcher=searcher)
    assert corroborate(ctx, ADDRESS).found is False


def test_the_name_query_is_skipped_for_an_unusable_name(enrich_ctx):
    searcher = StubSearcher()
    ctx = enrich_ctx(name="Madonna", searcher=searcher)
    corroborate(ctx, ADDRESS)
    assert searcher.queries == [f'"{ADDRESS}"']


# ─────────────────────────────── the step ────────────────────────────────


def test_the_step_promotes_a_probable_address_to_verified(enrich_ctx):
    ctx = enrich_ctx(searcher=StubSearcher(_published()))
    ctx.add(_pattern_finding())

    outcome = CorroborateStep().run(ctx)
    assert outcome.ok
    found = outcome.found[0]
    assert found.status == "verified"
    assert found.source == "corroborated"
    assert found.evidence["corroborating_domain"] == "alpine.com"


def test_promotion_keeps_the_evidence_the_earlier_steps_earned(enrich_ctx):
    """§4 wants the evidence, not just the verdict: the pattern and its sample
    size are still true and still worth showing next to the address."""
    ctx = enrich_ctx(searcher=StubSearcher(_published()))
    ctx.add(_pattern_finding())
    found = CorroborateStep().run(ctx).found[0]
    assert found.evidence["pattern"] == "first.last"
    assert found.evidence["sample_size"] == 3
    assert found.mx_valid is True


def test_verified_still_carries_no_confidence_number(enrich_ctx):
    ctx = enrich_ctx(searcher=StubSearcher(_published()))
    ctx.add(_pattern_finding())
    assert CorroborateStep().run(ctx).found[0].confidence is None


def test_nothing_published_leaves_the_finding_alone(enrich_ctx):
    ctx = enrich_ctx(searcher=StubSearcher())
    ctx.add(_pattern_finding())
    outcome = CorroborateStep().run(ctx)
    assert outcome.outcome == "empty"
    assert ctx.best().status == "probable"


def test_the_step_notes_when_only_brokers_had_it(enrich_ctx):
    ctx = enrich_ctx(searcher=StubSearcher({
        f'"{ADDRESS}"': [serp("https://rocketreach.co/jane", "Jane Doe", ADDRESS)]
    }))
    ctx.add(_pattern_finding())
    CorroborateStep().run(ctx)
    assert any("guessed addresses" in n for n in ctx.notes)


def test_the_step_skips_with_nothing_to_check(enrich_ctx):
    ctx = enrich_ctx(searcher=StubSearcher())
    assert CorroborateStep().run(ctx).outcome == "skipped"


def test_an_already_verified_finding_is_not_re_searched(enrich_ctx):
    searcher = StubSearcher(_published())
    ctx = enrich_ctx(searcher=searcher)
    ctx.add(_pattern_finding(status="verified"))
    assert CorroborateStep().run(ctx).outcome == "skipped"
    assert searcher.queries == []


def test_only_the_top_addresses_are_checked(enrich_ctx):
    """Each address costs up to two searches, and the fifth-ranked guess is
    never the right one."""
    searcher = StubSearcher()
    ctx = enrich_ctx(searcher=searcher)
    for i in range(5):
        ctx.add(_pattern_finding(email=f"guess{i}@alpine.com", status="guess"))
    CorroborateStep(max_addresses=2).run(ctx)
    assert len({q for q in searcher.queries if "guess" in q}) <= 4


def test_the_step_is_free():
    assert CorroborateStep.spends_credits is False


def test_both_brief_queries_use_a_quoted_phrase():
    for template in CORROBORATION_QUERIES:
        assert template.count('"') >= 2
