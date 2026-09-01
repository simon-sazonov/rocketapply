"""The free engine end to end — §9's headline acceptance criterion.

    "Free engine resolves a domain, harvests >= 2 samples, infers a pattern,
     and returns a `probable` address for a real candidate **without spending a
     credit**."

That whole sentence is one test here. The rest of the file is about the two
structural promises around it: the free chain cannot spend, and enrichment
cannot be run over the whole table.
"""

from __future__ import annotations

import pytest

from pitwall import enrich as E
from pitwall.enrich.base import EnrichmentContext, StepOutcome
from tests.conftest import StubSearcher, serp

TEAM_PAGE = serp(
    "https://alpine-racing.com/team",
    "Our team | Alpine Racing",
    "Marc Dubois marc.dubois@alpine-racing.com · Anna Klein anna.klein@alpine-racing.com",
)


@pytest.fixture()
def world(repos, settings):
    """A candidate at a company whose site is findable and lists two addresses."""
    repos.conn.execute("INSERT INTO companies (name, domain) VALUES ('Alpine', 'alpine-racing.com')")
    cid, _ = repos.candidates.upsert({
        "linkedin_slug": "jane-doe",
        "profile_url": "https://uk.linkedin.com/in/jane-doe",
        "full_name": "Jane Doe",
        "current_company": "Alpine",
    })
    searcher = StubSearcher({"@alpine-racing.com": [TEAM_PAGE]})
    return cid, searcher


def _chain(mx_ok, **kwargs):
    return E.free_chain(resolver=mx_ok, **kwargs)


# ─────────────────────── §9's acceptance criterion ───────────────────────


def test_the_free_engine_resolves_harvests_infers_and_returns_probable(
    world, repos, settings, mx_ok
):
    cid, searcher = world
    ctx = E.enrich_candidate(cid, repos, settings, searcher=searcher, steps=_chain(mx_ok))

    assert ctx.domain == "alpine-racing.com"
    assert len(ctx.samples) >= 2
    assert ctx.pattern == "first.last"
    assert ctx.pattern_sample_size == 2

    best = ctx.best()
    assert best.email == "jane.doe@alpine-racing.com"
    assert best.status == "probable"
    assert ctx.credits_spent == 0.0

    stored = repos.contacts.best(cid)
    assert stored["email"] == "jane.doe@alpine-racing.com"
    assert stored["status"] == "probable"
    assert stored["evidence"]["sample_size"] == 2


def test_a_corroborated_address_reaches_verified_with_its_source(
    world, repos, settings, mx_ok
):
    """§9: "A corroborated address reaches `verified` with its source URL shown"."""
    cid, searcher = world
    searcher.pages['"jane.doe@alpine-racing.com"'] = [
        serp("https://alpine-racing.com/contact", "Contact",
             "Jane Doe, Head of Aero — jane.doe@alpine-racing.com")
    ]
    ctx = E.enrich_candidate(cid, repos, settings, searcher=searcher, steps=_chain(mx_ok))

    assert ctx.status == "verified"
    stored = repos.contacts.best(cid)
    assert stored["status"] == "verified"
    assert stored["evidence"]["corroborating_url"] == "https://alpine-racing.com/contact"
    # The pattern evidence survives the promotion.
    assert stored["evidence"]["sample_size"] == 2


def test_every_step_is_recorded_in_the_audit_trail(world, repos, settings, mx_ok):
    cid, searcher = world
    E.enrich_candidate(cid, repos, settings, searcher=searcher, steps=_chain(mx_ok))
    steps = [r["step"] for r in repos.enrichment.for_candidate(cid)]
    assert steps == ["domain", "harvest", "pattern", "corroborate"]
    assert repos.enrichment.credits_spent() == 0.0


def test_the_audit_trail_never_records_an_address(world, repos, settings, mx_ok):
    """`enrichment_log.candidate_id` is SET NULL on erasure, so a `detail` that
    carried an address would survive the erasure it is meant to allow."""
    cid, searcher = world
    E.enrich_candidate(cid, repos, settings, searcher=searcher, steps=_chain(mx_ok))
    for row in repos.enrichment.for_candidate(cid):
        assert "@" not in str(row["detail"] or "")


def test_the_second_person_at_the_same_company_is_nearly_free(
    world, repos, settings, mx_ok
):
    """The whole leverage of the design: the expensive part is learning the
    company's format, and that answer is reusable."""
    cid, searcher = world
    E.enrich_candidate(cid, repos, settings, searcher=searcher, steps=_chain(mx_ok))
    after_first = len(searcher.queries)

    other, _ = repos.candidates.upsert({
        "linkedin_slug": "marc-dubois", "profile_url": "https://x/in/marc-dubois",
        "full_name": "Marc Dubois", "current_company": "Alpine",
    })
    ctx = E.enrich_candidate(other, repos, settings, searcher=searcher, steps=_chain(mx_ok))

    # Better than probable, and still free: Marc's own address was one of the
    # two harvested from the team page, so the corpus corroborates it without a
    # single further search.
    assert ctx.best().status == "verified"
    assert ctx.best().evidence["corroborating_domain"] == "alpine-racing.com"
    assert len(searcher.queries) == after_first
    assert ctx.credits_spent == 0.0


# ──────────────────────── the chain cannot spend ─────────────────────────


def test_the_free_chain_contains_nothing_that_can_spend():
    assert E.is_free(E.free_chain()) is True
    assert [s.name for s in E.free_chain()] == [
        "domain", "harvest", "pattern", "corroborate"
    ]


def test_is_free_notices_a_step_that_starts_costing_money():
    """The guard is a property on the protocol, not a remembered list of names."""

    class Expensive:
        name = "expensive"
        spends_credits = True

        def run(self, ctx):
            return StepOutcome(step=self.name, outcome="ok")

    assert E.is_free([*E.free_chain(), Expensive()]) is False


# ───────────────────── a failing step is not a failing run ───────────────


def test_a_step_that_raises_is_recorded_and_the_chain_continues(
    world, repos, settings, mx_ok
):
    class Exploding:
        name = "exploding"
        spends_credits = False

        def run(self, ctx):
            raise RuntimeError("something went wrong")

    cid, searcher = world
    steps = [*_chain(mx_ok)[:1], Exploding(), *_chain(mx_ok)[1:]]
    ctx = E.enrich_candidate(cid, repos, settings, searcher=searcher, steps=steps)

    assert ctx.best() is not None
    outcomes = {s.step: s.outcome for s in ctx.steps}
    assert outcomes["exploding"] == "failed"
    assert outcomes["pattern"] == "ok"


def test_a_failure_message_never_leaks_a_key(world, repos, settings, monkeypatch):
    class Leaky:
        name = "leaky"
        spends_credits = False

        def run(self, ctx):
            raise RuntimeError(f"GET https://api.hunter.io/v2/x?api_key={SECRET}")

    SECRET = "hunter-secret-key-abcdef123456"
    cid, searcher = world
    settings = settings.with_overrides(hunter_api_key=SECRET)
    ctx = E.enrich_candidate(cid, repos, settings, searcher=searcher, steps=[Leaky()])

    assert SECRET not in str(ctx.steps[0].detail)
    assert SECRET not in " ".join(ctx.notes)
    rows = repos.enrichment.for_candidate(cid)
    assert all(SECRET not in str(r["detail"] or "") for r in rows)


# ─────────────────────── never in bulk over the table ────────────────────


def test_a_selection_larger_than_the_cap_is_refused(repos, settings):
    """§0.2: enrichment is a manual tool on a manual selection. The cap lives
    in the library, so the CLI cannot route around it either."""
    ids = list(range(1, settings.enrich_max_selection + 2))
    with pytest.raises(E.EnrichmentError, match="manual selection"):
        E.enrich_many(ids, repos, settings, searcher=StubSearcher())


def test_a_selection_at_the_cap_is_allowed(repos, settings):
    ids = list(range(1, settings.enrich_max_selection + 1))
    assert E.enrich_many(ids, repos, settings, searcher=StubSearcher()) == []


def test_one_persons_problem_does_not_stop_the_selection(world, repos, settings, mx_ok):
    cid, searcher = world
    out = E.enrich_many(
        [cid, 999_999], repos, settings, searcher=searcher, steps=_chain(mx_ok)
    )
    assert [c.candidate_id for c in out] == [cid]


def test_the_selection_shares_one_searcher(world, repos, settings, mx_ok):
    cid, searcher = world
    other, _ = repos.candidates.upsert({
        "linkedin_slug": "marc-dubois", "profile_url": "https://x/in/marc-dubois",
        "full_name": "Marc Dubois", "current_company": "Alpine",
    })
    E.enrich_many([cid, other], repos, settings, searcher=searcher, steps=_chain(mx_ok))
    assert searcher.queries  # one shared stub saw every query


# ───────────────────────────── the blocklist ─────────────────────────────


def test_a_blocklisted_person_is_never_looked_up(world, repos, settings, mx_ok):
    """Blocklisting is an erasure. Researching an erased person would undo it."""
    cid, searcher = world
    repos.crm.block("jane-doe", "not interested")
    with pytest.raises(E.EnrichmentError, match="blocklisted"):
        E.enrich_candidate(cid, repos, settings, searcher=searcher, steps=_chain(mx_ok))


def test_an_unknown_candidate_is_an_error(repos, settings):
    with pytest.raises(E.EnrichmentError, match="no candidate"):
        E.enrich_candidate(999_999, repos, settings, searcher=StubSearcher())


# ─────────────────────────── company resolution ──────────────────────────


def test_the_company_is_matched_through_the_normaliser(repos, settings):
    """"Scuderia Ferrari HP" is the Ferrari row — the same normalisation the
    scoring layer already uses, rather than a second, different one."""
    repos.conn.execute("INSERT INTO companies (name) VALUES ('Ferrari')")
    company = E.find_company(repos, {"id": 1, "current_company": "Scuderia Ferrari HP"})
    assert company["name"] == "Ferrari"


def test_the_search_target_stands_in_when_the_employer_is_unknown(repos, settings):
    repos.conn.execute("INSERT INTO companies (name) VALUES ('Alpine')")
    cid, _ = repos.candidates.upsert({
        "linkedin_slug": "x", "profile_url": "https://x/in/x", "full_name": "X Y",
    })
    repos.conn.execute(
        "INSERT INTO scores (candidate_id, company_id, final_score, prompt_version)"
        " VALUES (?, 1, 80, ?)", (cid, settings.prompt_version)
    )
    assert E.find_company(repos, {"id": cid, "current_company": None})["name"] == "Alpine"


def test_no_company_at_all_is_survivable(repos, settings):
    assert E.find_company(repos, {"id": 1, "current_company": None}) is None


# ───────────────────────── nothing found is a state ──────────────────────


def test_nothing_found_writes_no_row_but_records_the_attempt(repos, settings, mx_ok):
    """"We looked and found nothing" and "we never looked" must be tellable
    apart — otherwise the owner cannot know whether a credit is worth spending.
    The absence of a contact row means the first; the log records the second."""
    repos.conn.execute("INSERT INTO companies (name) VALUES ('Nowhere Ltd')")
    cid, _ = repos.candidates.upsert({
        "linkedin_slug": "no-one", "profile_url": "https://x/in/no-one",
        "full_name": "No One", "current_company": "Nowhere Ltd",
    })
    ctx = E.enrich_candidate(
        cid, repos, settings, searcher=StubSearcher(), steps=_chain(mx_ok)
    )
    assert ctx.status == "not_found"
    assert repos.contacts.for_candidate(cid) == []
    assert E.last_enriched_at(repos, cid) is not None


def test_last_enriched_at_is_none_before_anything_ran(repos, settings):
    cid, _ = repos.candidates.upsert({
        "linkedin_slug": "fresh", "profile_url": "https://x/in/fresh", "full_name": "F P",
    })
    assert E.last_enriched_at(repos, cid) is None


# ──────────────────────────────── summary ────────────────────────────────


def test_summary_carries_the_evidence_not_just_the_verdict(world, repos, settings, mx_ok):
    cid, searcher = world
    ctx = E.enrich_candidate(cid, repos, settings, searcher=searcher, steps=_chain(mx_ok))
    out = E.summary(ctx)
    assert out["status"] == "probable"
    assert out["evidence"]["pattern"] == "first.last"
    assert out["evidence"]["sample_size"] == 2
    assert out["domain"] == "alpine-racing.com"
    assert [s["step"] for s in out["steps"]] == [
        "domain", "harvest", "pattern", "corroborate"
    ]
    assert out["credits_spent"] == 0.0


def test_persist_is_skippable_for_a_dry_run(world, repos, settings, mx_ok):
    cid, searcher = world
    ctx = E.enrich_candidate(
        cid, repos, settings, searcher=searcher, steps=_chain(mx_ok), persist=False
    )
    assert ctx.best() is not None
    assert repos.contacts.for_candidate(cid) == []
    assert E.persist_findings(ctx) == 1
    assert repos.contacts.best(cid) is not None


def test_a_dry_run_over_a_selection_stores_nothing_either(world, repos, settings, mx_ok):
    """`enrich_many` persisted regardless of what the caller asked for, so
    `pitwall enrich --dry-run` wrote the very rows it promised not to."""
    cid, searcher = world
    out = E.enrich_many(
        [cid], repos, settings, searcher=searcher, steps=_chain(mx_ok), persist=False
    )
    assert out[0].best() is not None
    assert repos.contacts.for_candidate(cid) == []
    # The audit trail and the company's domain ARE written: they record what
    # the run did, not what it found, and suppressing them would make the log
    # lie about a run that really happened.
    assert repos.enrichment.for_candidate(cid) != []
    assert repos.domains.preferred(int(
        repos.conn.execute("SELECT id FROM companies").fetchone()["id"]
    )) is not None


def test_context_keeps_the_best_evidenced_finding_per_address(settings, repos):
    from pitwall.enrich.base import EmailCandidate

    ctx = EnrichmentContext(candidate={"id": 1}, settings=settings, repos=repos)
    ctx.add(EmailCandidate("a@x.com", "guess", "pattern", {"pattern": "first.last"}))
    ctx.add(EmailCandidate("a@x.com", "verified", "corroborated", {"corroborating_url": "u"}))
    ctx.add(EmailCandidate("a@x.com", "guess", "pattern", {"pattern": "flast"}))
    assert len(ctx.findings) == 1
    assert ctx.findings[0].status == "verified"


def test_an_email_candidate_cannot_be_built_without_a_valid_status():
    from pitwall.enrich.base import EmailCandidate

    with pytest.raises(ValueError, match="unknown status"):
        EmailCandidate("a@x.com", "definitely", "pattern", {})
    with pytest.raises(ValueError, match="not an address"):
        EmailCandidate("nonsense", "guess", "pattern", {})
