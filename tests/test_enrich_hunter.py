"""The Hunter adapter — §5, and the credit arithmetic §5.2 asks to be verified.

Every request in this file goes through an injected transport. Nothing here
touches the network, and no test needs a key.

The credit costs asserted here were read off help.hunter.io on 1 September 2026
and are recorded in `enrich/hunter.py`. §5.2 required them to be verified
rather than guessed, because the recommendation threshold depends on them —
and one of them changed the implementation: Domain Search is billed per ten
results, so `limit` is the price, and the adapter pins it.
"""

from __future__ import annotations

import json

import pytest

from pitwall.enrich.base import CreditsExhausted
from pitwall.enrich.cache import EnrichmentCache, cache_key, scrub
from pitwall.enrich.hunter import (
    CREDIT_COST,
    DOMAIN_SEARCH_LIMIT,
    HunterAuthError,
    HunterClient,
    HunterDomainStep,
    HunterError,
    HunterFinderStep,
    advise,
    hunter_available,
    hunter_pattern_to_local,
    parse_account,
    unresolved_at_company,
)

KEY = "hunter-test-key-0123456789abcdef"


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeTransport:
    """Records every request and answers from a scripted queue."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, url, params):
        self.requests.append((url, dict(params)))
        if not self.responses:
            raise AssertionError(f"unexpected extra request to {url}")
        return self.responses.pop(0)

    @property
    def last_params(self):
        return self.requests[-1][1]


def _client(settings, tmp_path, *responses, key=KEY):
    return HunterClient(
        settings.with_overrides(hunter_api_key=key),
        cache=EnrichmentCache(tmp_path / "hcache"),
        transport=FakeTransport(*responses),
    )


ACCOUNT_NEW = {
    "data": {
        "plan_name": "Free",
        "reset_date": "2026-10-01",
        "requests": {
            "searches": {"used": 16, "available": 50},
            "verifications": {"used": 0, "available": 100},
        },
    }
}

ACCOUNT_OLD = {
    "data": {"plan_name": "Free", "reset_date": "2026-10-01",
             "calls": {"used": 16, "available": 50}}
}

FINDER_HIT = {
    "data": {
        "email": "Jane.Doe@alpine.com",
        "score": 97,
        "first_name": "Jane",
        "last_name": "Doe",
        "position": "Head of Aerodynamics",
        "verification": {"status": "valid", "date": "2026-08-30"},
        "sources": [{"uri": "https://alpine.com/team", "domain": "alpine.com"}],
        # Hunter really does return this. §0.3 forbids storing it.
        "phone_number": "+33 1 23 45 67 89",
    }
}

FINDER_MISS = {"data": {"email": None, "score": None}}

DOMAIN_HIT = {
    "data": {
        "domain": "alpine.com",
        "pattern": "{first}.{last}",
        "emails": [
            {"value": "marc.dubois@alpine.com", "first_name": "Marc",
             "last_name": "Dubois", "type": "personal",
             "sources": [{"uri": "https://alpine.com/team"}]},
            {"value": "anna.klein@alpine.com", "first_name": "Anna",
             "last_name": "Klein", "type": "personal"},
            {"value": "info@alpine.com", "type": "generic"},
        ],
    }
}


# ─────────────────────── the account endpoint (free) ─────────────────────


def test_the_new_account_shape_is_read():
    account = parse_account(ACCOUNT_NEW)
    assert (account.credits_used, account.credits_available) == (16, 50)
    assert account.remaining == 34
    assert account.reset_date == "2026-10-01"


def test_the_older_account_shape_is_read_too():
    """Hunter has shipped both; an adapter that knows only one breaks silently
    on an account that returns the other."""
    account = parse_account(ACCOUNT_OLD)
    assert account.remaining == 34


def test_an_unrecognisable_account_payload_does_not_crash():
    account = parse_account({"data": {}})
    assert account.remaining == 0
    assert account.exhausted is True


def test_the_sidebar_sentence_is_the_one_the_brief_asks_for():
    """§5.1: `Hunter · 34 of 50 credits left · resets 1 Oct`."""
    assert parse_account(ACCOUNT_NEW).describe() == (
        "Hunter · 34 of 50 credits left · resets 1 Oct"
    )


def test_the_account_call_is_free_and_never_cached(settings, tmp_path):
    client = _client(settings, tmp_path, FakeResponse(ACCOUNT_NEW), FakeResponse(ACCOUNT_NEW))
    assert client.account().remaining == 34
    assert client.account().remaining == 34
    assert len(client.transport.requests) == 2   # live every time, by design
    assert CREDIT_COST["account"] == 0.0


# ────────────────────────── errors and exhaustion ────────────────────────


def test_no_key_is_a_clear_message_not_a_crash(settings, tmp_path):
    client = _client(settings, tmp_path, key="")
    with pytest.raises(HunterAuthError, match="HUNTER_API_KEY"):
        client.account()


def test_a_rejected_key_is_not_retryable(settings, tmp_path):
    client = _client(settings, tmp_path, FakeResponse(
        {"errors": [{"id": "unauthorized", "code": 401, "details": "no valid API key"}]}, 401
    ))
    with pytest.raises(HunterAuthError, match="401"):
        client.account()


@pytest.mark.parametrize("status", [402, 403, 429])
def test_out_of_credits_raises_a_disable_not_a_crash(settings, tmp_path, status):
    """§8: "Hunter out-of-credits produces a clean disable, not a crash. This
    WILL happen — it is 50 credits a month — so it needs a real test."

    Hunter's own docs give 429 for "you have reached your usage limit", not the
    402 the brief expected; all three are treated as "not now".
    """
    client = _client(settings, tmp_path, FakeResponse(
        {"errors": [{"id": "usage", "code": status, "details": "usage limit"}]}, status
    ))
    with pytest.raises(CreditsExhausted):
        client.email_finder("alpine.com", "Jane", "Doe")


def test_a_server_error_is_an_ordinary_failure(settings, tmp_path):
    client = _client(settings, tmp_path, FakeResponse({}, 503))
    with pytest.raises(HunterError, match="503"):
        client.email_finder("alpine.com", "Jane", "Doe")


def test_an_html_error_page_does_not_break_the_parser(settings, tmp_path):
    class NotJson:
        status_code = 500
        text = "<html>gateway timeout</html>"

        def json(self):
            raise ValueError("not json")

    client = _client(settings, tmp_path, NotJson())
    with pytest.raises(HunterError):
        client.account()


# ───────────────────────── §0.4: the key never leaks ─────────────────────


def test_an_error_message_never_contains_the_key(settings, tmp_path):
    client = _client(settings, tmp_path, FakeResponse(
        {"errors": [{"details": f"bad request to ?api_key={KEY}"}]}, 400
    ))
    with pytest.raises(HunterError) as exc:
        client.account()
    assert KEY not in str(exc.value)
    assert "api_key=***" in str(exc.value)


def test_the_key_is_not_in_the_cache_key_or_the_cached_file(settings, tmp_path):
    client = _client(settings, tmp_path, FakeResponse(FINDER_HIT))
    client.email_finder("alpine.com", "Jane", "Doe")
    files = list((tmp_path / "hcache").rglob("*.json"))
    assert files
    body = files[0].read_text(encoding="utf-8")
    assert KEY not in body
    assert "api_key" not in body


def test_cache_keys_ignore_the_secret_and_the_argument_order():
    a = cache_key("hunter", "email-finder", {"domain": "x.com", "first_name": "J", "api_key": KEY})
    b = cache_key("hunter", "email-finder", {"first_name": "J", "domain": "x.com"})
    assert a == b


def test_scrub_removes_secrets_at_every_depth():
    out = scrub({"a": {"api_key": "x", "b": [{"token": "y", "c": 1}]}})
    assert out == {"a": {"b": [{"c": 1}]}}


# ──────────────── §0.3: no phone number is ever stored ───────────────────


def test_the_phone_number_hunter_returns_is_dropped(enrich_ctx, repos, settings, tmp_path):
    """Hunter returns it for free. Storing it would still be a breach."""
    ctx = enrich_ctx()
    ctx.domain = "alpine.com"
    ctx.company_domain_id = repos.domains.upsert(int(ctx.company["id"]), "alpine.com")
    ctx.settings = settings.with_overrides(hunter_api_key=KEY)
    client = _client(settings, tmp_path, FakeResponse(FINDER_HIT))

    outcome = HunterFinderStep(client=client).run(ctx)
    stored = json.dumps(outcome.found[0].raw)
    assert "+33" not in stored
    assert "phone" not in stored.lower()


def test_the_cached_payload_holds_no_phone_number(settings, tmp_path):
    """The cache is the disk boundary, and a permanent one — an entry written
    there has no TTL. The adapter's allow-list governs what reaches
    `contact_emails`; without a scrub here the raw answer would still sit in a
    cache file forever, which is storing it by any reading of the word."""
    client = _client(settings, tmp_path, FakeResponse(FINDER_HIT))
    client.email_finder("alpine.com", "Jane", "Doe")
    text = " ".join(f.read_text() for f in (tmp_path / "hcache").rglob("*.json"))
    assert "jane.doe@alpine.com" in text.lower()   # the answer IS kept
    assert "+33" not in text                        # the phone number is not
    assert "phone" not in text.lower()


def test_a_cached_answer_still_reads_back_after_the_scrub(settings, tmp_path):
    """Scrubbing must not damage the thing the credit was spent on."""
    cache = EnrichmentCache(tmp_path / "hcache")
    first = HunterClient(
        settings.with_overrides(hunter_api_key=KEY), cache=cache,
        transport=FakeTransport(FakeResponse(FINDER_HIT)),
    )
    first.email_finder("alpine.com", "Jane", "Doe")

    second = HunterClient(
        settings.with_overrides(hunter_api_key=KEY), cache=cache,
        transport=FakeTransport(),
    )
    again = second.email_finder("alpine.com", "Jane", "Doe")
    assert again["data"]["email"] == "Jane.Doe@alpine.com"
    assert second.cache_hits == 1


# ─────────────────────────── Email Finder (1 credit) ─────────────────────


@pytest.fixture()
def ready(enrich_ctx, repos, settings):
    """A context with a resolved domain and a Hunter key."""
    ctx = enrich_ctx()
    ctx.domain = "alpine.com"
    ctx.domain_source = "config"
    ctx.company_domain_id = repos.domains.upsert(int(ctx.company["id"]), "alpine.com")
    ctx.settings = settings.with_overrides(hunter_api_key=KEY)
    repos.quota.set_credits("hunter", 50, reset_at="2026-10-01")
    return ctx


def test_a_found_address_costs_one_credit_and_is_recorded(ready, repos, settings, tmp_path):
    client = _client(settings, tmp_path, FakeResponse(FINDER_HIT))
    outcome = HunterFinderStep(client=client).run(ready)

    assert outcome.ok
    assert outcome.credits_spent == 1.0
    found = outcome.found[0]
    assert found.email == "jane.doe@alpine.com"
    assert found.source == "hunter_finder"
    assert repos.quota.credits("hunter") == 49


def test_a_deliverable_address_earns_verified(ready, settings, tmp_path):
    """§4: "or Hunter returned it as deliverable"."""
    client = _client(settings, tmp_path, FakeResponse(FINDER_HIT))
    assert HunterFinderStep(client=client).run(ready).found[0].status == "verified"


def test_an_unverified_hunter_hit_is_only_probable(ready, settings, tmp_path):
    payload = {"data": {**FINDER_HIT["data"], "verification": {"status": "unknown"}}}
    client = _client(settings, tmp_path, FakeResponse(payload))
    assert HunterFinderStep(client=client).run(ready).found[0].status == "probable"


def test_the_confidence_number_is_hunters_own_and_labelled_as_such(
    ready, settings, tmp_path
):
    """The one place a number is legitimate — §6: "provider's own score ONLY"."""
    client = _client(settings, tmp_path, FakeResponse(FINDER_HIT))
    found = HunterFinderStep(client=client).run(ready).found[0]
    assert found.confidence == 97.0
    assert found.evidence["provider"] == "hunter"


def test_nothing_found_costs_nothing(ready, repos, settings, tmp_path):
    """Hunter's docs: "If no email can be found, no credit is charged." Charging
    for it locally would make the ledger lie."""
    client = _client(settings, tmp_path, FakeResponse(FINDER_MISS))
    outcome = HunterFinderStep(client=client).run(ready)

    assert outcome.outcome == "empty"
    assert outcome.credits_spent == 0.0
    assert repos.quota.credits("hunter") == 50


def test_a_role_address_from_hunter_is_not_this_persons_address(
    ready, settings, tmp_path
):
    payload = {"data": {"email": "info@alpine.com", "score": 90,
                        "verification": {"status": "valid"}}}
    client = _client(settings, tmp_path, FakeResponse(payload))
    found = HunterFinderStep(client=client).run(ready).found[0]
    assert found.status == "guess"
    assert found.is_role_address is True
    assert any("shared mailbox" in n for n in ready.notes)


def test_a_second_lookup_of_the_same_person_is_free(ready, repos, settings, tmp_path):
    """§7.1: "a spent credit is never spent twice"."""
    cache = EnrichmentCache(tmp_path / "shared")
    first = HunterClient(ready.settings, cache=cache, transport=FakeTransport(FakeResponse(FINDER_HIT)))
    HunterFinderStep(client=first).run(ready)
    assert repos.quota.credits("hunter") == 49

    second = HunterClient(ready.settings, cache=cache, transport=FakeTransport())
    outcome = HunterFinderStep(client=second).run(ready)
    assert outcome.credits_spent == 0.0
    assert "no credit spent" in (outcome.detail or "")
    assert repos.quota.credits("hunter") == 49


def test_no_credit_is_spent_without_a_domain(ready, repos, settings, tmp_path):
    """A credit spent on the wrong domain is a credit gone."""
    ready.domain = None
    client = _client(settings, tmp_path)
    outcome = HunterFinderStep(client=client).run(ready)
    assert outcome.outcome == "skipped"
    assert repos.quota.credits("hunter") == 50


def test_the_step_skips_cleanly_when_credits_are_gone(ready, repos, settings, tmp_path):
    repos.quota.set_credits("hunter", 0, reset_at="2026-10-01")
    outcome = HunterFinderStep(client=_client(settings, tmp_path)).run(ready)
    assert outcome.outcome == "skipped"
    assert "reset" in (outcome.detail or "")


def test_the_step_declares_that_it_spends():
    assert HunterFinderStep.spends_credits is True
    assert HunterDomainStep.spends_credits is True


# ────────────────────────── Domain Search (1 credit) ─────────────────────


def test_the_limit_is_pinned_so_the_call_costs_exactly_one_credit(
    ready, settings, tmp_path
):
    """§5.2 asked for the real cost, and the real cost is 1 credit per 1-10
    addresses returned — so `limit` IS the price. Unpinned, a company with 40
    published addresses would quietly cost four credits against a button that
    promised one."""
    client = _client(settings, tmp_path, FakeResponse(DOMAIN_HIT))
    HunterDomainStep(client=client).run(ready)
    assert int(client.transport.last_params["limit"]) == DOMAIN_SEARCH_LIMIT == 10


def test_a_caller_cannot_raise_the_limit_past_the_price(settings, tmp_path):
    client = _client(settings, tmp_path, FakeResponse(DOMAIN_HIT))
    client.domain_search("alpine.com", limit=500)
    assert int(client.transport.last_params["limit"]) == 10


def test_a_paid_domain_call_feeds_the_free_engine(ready, repos, settings, tmp_path):
    """§5.2: "Feed any addresses Hunter returns back into
    domain_address_samples, so a paid call permanently improves the free
    engine.""" ""
    client = _client(settings, tmp_path, FakeResponse(DOMAIN_HIT))
    outcome = HunterDomainStep(client=client).run(ready)

    assert outcome.credits_spent == 1.0
    stored = {s["email"]: s["person_name"] for s in repos.domains.samples(ready.company_domain_id)}
    assert stored["marc.dubois@alpine.com"] == "Marc Dubois"
    assert stored["anna.klein@alpine.com"] == "Anna Klein"
    # A role address in the corpus would corrupt the format inference.
    assert "info@alpine.com" not in stored


def test_hunters_pattern_is_translated_into_the_local_vocabulary(
    ready, repos, settings, tmp_path
):
    """One engine reads the corpus, not two."""
    client = _client(settings, tmp_path, FakeResponse(DOMAIN_HIT))
    HunterDomainStep(client=client).run(ready)
    assert repos.domains.by_id(ready.company_domain_id)["pattern"] == "first.last"


@pytest.mark.parametrize(
    ("hunter", "local"),
    [("{first}.{last}", "first.last"), ("{f}{last}", "flast"),
     ("{first}{last}", "firstlast"), ("{last}.{first}", "last.first"),
     ("{something_new}", None), ("", None)],
)
def test_the_pattern_translation_table(hunter, local):
    assert hunter_pattern_to_local(hunter) == local


def test_the_next_person_at_that_company_is_then_free(ready, repos, settings, tmp_path):
    """The whole argument for spending on a domain rather than a person."""
    from pitwall.enrich.patterns import PatternStep

    client = _client(settings, tmp_path, FakeResponse(DOMAIN_HIT))
    HunterDomainStep(client=client).run(ready)

    other = ready
    other.findings.clear()
    other.samples = repos.domains.samples(ready.company_domain_id)
    outcome = PatternStep(resolver=lambda d: ["aspmx.l.google.com"]).run(other)
    assert outcome.found[0].status == "probable"
    assert outcome.found[0].evidence["sample_size"] == 2


# ──────────────────────── availability and advice ────────────────────────


def test_no_key_means_unavailable_with_a_useful_reason(settings, repos):
    ok, why = hunter_available(settings.with_overrides(hunter_api_key=""), repos)
    assert ok is False
    assert "works without one" in why


def test_zero_credits_means_unavailable_with_the_reset_date(settings, repos):
    repos.quota.set_credits("hunter", 0, reset_at="2026-10-01")
    ok, why = hunter_available(settings.with_overrides(hunter_api_key=KEY), repos)
    assert ok is False
    assert "1 Oct" in why


def test_a_key_and_credits_means_available(settings, repos):
    repos.quota.set_credits("hunter", 12)
    ok, _ = hunter_available(settings.with_overrides(hunter_api_key=KEY), repos)
    assert ok is True


def test_the_domain_call_is_recommended_once_several_people_are_unresolved(
    repos, settings
):
    repos.conn.execute("INSERT INTO companies (name) VALUES ('Alpine')")
    for i in range(4):
        repos.candidates.upsert({
            "linkedin_slug": f"p{i}", "profile_url": f"https://x/in/p{i}",
            "full_name": f"P{i} Q", "current_company": "Alpine",
        })
    advice = advise(repos, 1, "alpine.com", settings)
    assert advice.recommend == "domain"
    assert advice.unresolved == 4
    assert "instead of 4 calls" in advice.reason


def test_one_unresolved_person_is_cheaper_to_look_up_directly(repos, settings):
    repos.conn.execute("INSERT INTO companies (name) VALUES ('Alpine')")
    repos.candidates.upsert({
        "linkedin_slug": "p", "profile_url": "https://x/in/p",
        "full_name": "P Q", "current_company": "Alpine",
    })
    assert advise(repos, 1, "alpine.com", settings).recommend == "finder"


def test_no_domain_means_no_spend_is_advised(repos, settings):
    advice = advise(repos, None, None, settings)
    assert advice.recommend == "none"
    assert "guessing" in advice.reason


def test_a_confirmed_address_stops_counting_as_unresolved(repos, settings):
    repos.conn.execute("INSERT INTO companies (name) VALUES ('Alpine')")
    ids = []
    for i in range(3):
        cid, _ = repos.candidates.upsert({
            "linkedin_slug": f"p{i}", "profile_url": f"https://x/in/p{i}",
            "full_name": f"P{i} Q", "current_company": "Alpine",
        })
        ids.append(cid)
    assert unresolved_at_company(repos, 1) == 3
    repos.contacts.upsert(ids[0], "a@alpine.com", status="verified", source="corroborated")
    assert unresolved_at_company(repos, 1) == 2
    # A `probable` is still a hypothesis, and the domain call is what settles it.
    repos.contacts.upsert(ids[1], "b@alpine.com", status="probable", source="pattern")
    assert unresolved_at_company(repos, 1) == 2


def test_discarded_people_do_not_inflate_the_recommendation(repos, settings):
    from pitwall.crm import CRM

    repos.conn.execute("INSERT INTO companies (name) VALUES ('Alpine')")
    cid, _ = repos.candidates.upsert({
        "linkedin_slug": "p", "profile_url": "https://x/in/p",
        "full_name": "P Q", "current_company": "Alpine",
    })
    CRM(repos.conn).ignore(cid)
    assert unresolved_at_company(repos, 1) == 0


# ──────────────────────────── the credit ledger ──────────────────────────


def test_credits_are_accounted_on_the_existing_quota_table(ready, repos, settings, tmp_path):
    """§6: "Credit accounting reuses the existing `quota` table … Do not build
    a parallel counter."""
    client = _client(settings, tmp_path, FakeResponse(FINDER_HIT))
    HunterFinderStep(client=client).run(ready)
    assert repos.quota.credits("hunter") == 49
    assert repos.quota.used_today("hunter:calls") == 1


def test_the_log_records_what_each_call_cost(ready, repos, settings, tmp_path):
    from pitwall.enrich.base import run_step

    client = _client(settings, tmp_path, FakeResponse(FINDER_HIT))
    run_step(HunterFinderStep(client=client), ready)
    rows = repos.enrichment.for_candidate(ready.candidate_id)
    assert rows[-1]["provider"] == "hunter"
    assert rows[-1]["credits_spent"] == 1.0
    assert repos.enrichment.credits_spent("hunter") == 1.0


def test_the_free_chain_still_contains_no_hunter_step():
    from pitwall import enrich as E

    assert E.is_free(E.free_chain()) is True
    assert not any("hunter" in s.name for s in E.free_chain())


def test_the_escalation_chain_resolves_the_domain_before_spending():
    from pitwall import enrich as E

    steps = E.escalation_chain("finder")
    assert [s.name for s in steps] == ["domain", "hunter.finder"]
    assert E.is_free(steps) is False


def test_the_escalation_chain_can_target_a_company():
    from pitwall import enrich as E

    assert [s.name for s in E.escalation_chain("domain")] == ["domain", "hunter.domain"]
