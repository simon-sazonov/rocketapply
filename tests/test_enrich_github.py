"""GitHub commit emails — §2.4.

Two assertions in this file are named requirements rather than ordinary tests:

* §8: "`…@users.noreply.github.com` is filtered out — explicit test." GitHub
  issues those precisely so the address is not contactable, and storing one as
  a finding is a bug.
* §2.4: "Do not go hunting for GitHub accounts by name — the false-match rate
  is high and a wrong person's address is worse than none."

Every request goes through an injected transport. No test touches the network.
"""

from __future__ import annotations

import json

import pytest

from pitwall.enrich.cache import EnrichmentCache
from pitwall.enrich.github import (
    NOREPLY_DOMAIN,
    GitHubClient,
    GitHubStep,
    GitHubUnavailable,
    emails_from_events,
    is_noreply,
    known_logins,
    logins_in,
    lookup,
    names_match,
)
from pitwall.enrich.patterns import split_name

TOKEN = "ghp-test-token-0123456789"


class FakeResponse:
    def __init__(self, payload, status_code=200, headers=None, text=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, url, headers):
        self.requests.append((url, dict(headers)))
        if not self.responses:
            raise AssertionError(f"unexpected extra request to {url}")
        return self.responses.pop(0)


def push_event(repo, *commits):
    return {
        "type": "PushEvent",
        "repo": {"name": repo},
        "payload": {"commits": list(commits)},
    }


def commit(email, name, sha="abc123def456"):
    return {"sha": sha, "author": {"email": email, "name": name}}


def _client(settings, tmp_path, *responses, token=""):
    return GitHubClient(
        settings.with_overrides(github_token=token),
        cache=EnrichmentCache(tmp_path / "ghcache"),
        transport=FakeTransport(*responses),
    )


# ──────────────── §8: the noreply address is filtered out ────────────────


def test_the_github_noreply_address_is_never_a_finding():
    """GitHub issues these so the address is NOT contactable. §8 asks for this
    test by name."""
    assert is_noreply(f"12345+janedoe@{NOREPLY_DOMAIN}") is True
    assert is_noreply(f"janedoe@{NOREPLY_DOMAIN}") is True
    assert is_noreply("jane.doe@alpine.com") is False


@pytest.mark.parametrize(
    "email",
    [
        f"1234+jane@{NOREPLY_DOMAIN}",
        "jane@users.noreply.gitlab.com",
        "noreply@github.com",
        "no-reply@anything.com",
        "someone@example.com",
        "",
        "   ",
    ],
)
def test_every_uncontactable_shape_is_filtered(email):
    assert is_noreply(email) is True


def test_noreply_commits_are_counted_and_dropped():
    events = [push_event("alpine/sim", commit(f"1+jane@{NOREPLY_DOMAIN}", "Jane Doe"))]
    found, skipped = emails_from_events(events, split_name("Jane Doe"))
    assert found == []
    assert skipped == 1


def test_the_step_reports_noreply_as_a_normal_outcome_not_a_failure(
    enrich_ctx, settings, tmp_path
):
    """It is the normal case for anyone with the privacy setting on."""
    ctx = enrich_ctx()
    events = [push_event("alpine/sim", commit(f"1+jane@{NOREPLY_DOMAIN}", "Jane Doe"))]
    client = _client(settings, tmp_path, FakeResponse(events))
    outcome = GitHubStep(client=client, logins=["janedoe"]).run(ctx)

    assert outcome.outcome == "empty"
    assert outcome.found == []
    assert "not contactable by design" in (outcome.detail or "")


# ────────────── §2.4: identities come from evidence, never names ─────────


def test_a_login_is_read_out_of_prose():
    assert logins_in("code at https://github.com/janedoe and elsewhere") == ["janedoe"]
    assert logins_in("github.com/jane-doe/simulator") == ["jane-doe"]


def test_github_paths_that_are_not_people_are_ignored():
    for text in ("github.com/orgs/alpine", "github.com/features/copilot",
                 "github.com/marketplace", "github.com/topics/f1"):
        assert logins_in(text) == []


def test_an_identity_in_the_persons_own_headline_is_used(enrich_ctx, repos):
    ctx = enrich_ctx()
    ctx.candidate = {**ctx.candidate, "headline": "Sim engineer · github.com/janedoe"}
    assert known_logins(ctx) == ["janedoe"]


def test_an_identity_in_the_evidence_that_surfaced_them_is_used(enrich_ctx, repos):
    ctx = enrich_ctx()
    repos.conn.execute(
        "INSERT INTO queries (template_id, query_text, query_hash) VALUES ('t','q','h')"
    )
    repos.conn.execute(
        "INSERT INTO search_results (query_id, provider, rank, url, canonical_url,"
        " title, snippet) VALUES (1,'searxng',0,'https://x/','https://x/','Jane Doe',"
        "'portfolio at github.com/jdoe-sim')"
    )
    repos.conn.execute(
        "INSERT INTO candidate_sources (candidate_id, search_result_id) VALUES (?,1)",
        (ctx.candidate_id,),
    )
    assert "jdoe-sim" in known_logins(ctx)


def test_no_identity_means_no_guessing_from_the_name(enrich_ctx, settings, tmp_path):
    """A wrong person's address is worse than none, so nothing is attempted."""
    ctx = enrich_ctx(name="Jane Doe")
    client = _client(settings, tmp_path)          # no responses: any call fails
    outcome = GitHubStep(client=client).run(ctx)

    assert outcome.outcome == "skipped"
    assert client.transport.requests == []
    assert any("guessing one from their name is not done" in n for n in ctx.notes)


# ──────────────────── attribution inside a push event ────────────────────


def test_the_commit_author_must_be_this_person():
    """A push can carry a merge of somebody else's commits."""
    person = split_name("Jane Doe")
    assert names_match("Jane Doe", person) is True
    assert names_match("jane doe", person) is True
    assert names_match("Marc Dubois", person) is False
    assert names_match(None, person) is False
    assert names_match("dependabot[bot]", person) is False


def test_an_attributed_commit_email_is_verified(enrich_ctx, settings, tmp_path):
    """Published by its owner, in public, by design — §2.5's own standard."""
    ctx = enrich_ctx()
    events = [push_event("alpine/sim", commit("jane.doe@alpine.com", "Jane Doe"))]
    client = _client(settings, tmp_path, FakeResponse(events))
    found = GitHubStep(client=client, logins=["janedoe"]).run(ctx).found[0]

    assert found.email == "jane.doe@alpine.com"
    assert found.status == "verified"
    assert found.source == "github"
    assert found.evidence["github_repo"] == "alpine/sim"
    assert found.confidence is None


def test_a_colleagues_commit_is_only_a_guess(enrich_ctx, settings, tmp_path):
    ctx = enrich_ctx()
    events = [push_event("alpine/sim", commit("marc.dubois@alpine.com", "Marc Dubois"))]
    client = _client(settings, tmp_path, FakeResponse(events))
    outcome = GitHubStep(client=client, logins=["janedoe"]).run(ctx)

    found = outcome.found[0]
    assert found.status == "guess"
    assert found.evidence["github_unattributed"] is True
    assert any("may be a colleague" in n for n in ctx.notes)


def test_the_persons_own_commit_wins_over_a_colleagues():
    events = [
        push_event("alpine/sim",
                   commit("marc.dubois@alpine.com", "Marc Dubois"),
                   commit("jane.doe@alpine.com", "Jane Doe")),
    ]
    found, _ = emails_from_events(events, split_name("Jane Doe"))
    assert found[0].email == "jane.doe@alpine.com"
    assert found[0].attributed is True


def test_non_push_events_are_ignored():
    events = [
        {"type": "WatchEvent", "repo": {"name": "a/b"}, "payload": {}},
        {"type": "IssuesEvent", "repo": {"name": "a/b"}, "payload": {"commits": []}},
        "not a dict",
    ]
    assert emails_from_events(events, split_name("Jane Doe")) == ([], 0)


def test_a_role_address_in_a_commit_is_not_a_persons_address(
    enrich_ctx, settings, tmp_path
):
    ctx = enrich_ctx()
    events = [push_event("alpine/sim", commit("info@alpine.com", "Jane Doe"))]
    client = _client(settings, tmp_path, FakeResponse(events))
    outcome = GitHubStep(client=client, logins=["janedoe"]).run(ctx)
    assert outcome.outcome == "empty"
    assert outcome.found == []


# ──────────────────────────── the patch method ───────────────────────────


def test_the_patch_from_header_is_the_second_method(settings, tmp_path):
    """§2.4's second confirmed method, for when an events payload has no email."""
    patch = (
        "From abc123 Mon Sep 1 12:00:00 2026\n"
        "From: Jane Doe <jane.doe@alpine.com>\n"
        "Date: Mon, 1 Sep 2026 12:00:00 +0100\n"
        "Subject: [PATCH] tyre model\n"
    )
    client = _client(settings, tmp_path, FakeResponse(None, text=patch))
    found = client.patch_email("alpine/sim", "abc123")
    assert found.email == "jane.doe@alpine.com"
    assert found.author_name == "Jane Doe"
    assert client.transport.requests[0][0] == (
        "https://github.com/alpine/sim/commit/abc123.patch"
    )


def test_a_patch_without_a_from_header_returns_nothing(settings, tmp_path):
    client = _client(settings, tmp_path, FakeResponse(None, text="diff --git a b"))
    assert client.patch_email("alpine/sim", "abc123") is None


# ──────────────────────── rate limits and failures ───────────────────────


def test_the_rate_limit_message_names_the_fix(settings, tmp_path):
    client = _client(settings, tmp_path, FakeResponse(
        {}, 403, headers={"X-RateLimit-Remaining": "0"}
    ))
    with pytest.raises(GitHubUnavailable) as exc:
        client.public_events("janedoe")
    assert "60 requests an hour" in str(exc.value)
    assert "GITHUB_TOKEN" in str(exc.value)


def test_a_token_removes_the_untokened_advice(settings, tmp_path):
    client = _client(settings, tmp_path, FakeResponse(
        {}, 429, headers={"X-RateLimit-Remaining": "0"}
    ), token=TOKEN)
    with pytest.raises(GitHubUnavailable) as exc:
        client.public_events("janedoe")
    assert "60 requests" not in str(exc.value)


def test_the_token_is_sent_as_a_bearer_header(settings, tmp_path):
    client = _client(settings, tmp_path, FakeResponse([]), token=TOKEN)
    client.public_events("janedoe")
    assert client.transport.requests[0][1]["Authorization"] == f"Bearer {TOKEN}"


def test_no_token_means_no_authorization_header(settings, tmp_path):
    client = _client(settings, tmp_path, FakeResponse([]))
    client.public_events("janedoe")
    assert "Authorization" not in client.transport.requests[0][1]


def test_an_unknown_user_is_a_state_not_a_crash(settings, tmp_path):
    client = _client(settings, tmp_path, FakeResponse({}, 404))
    with pytest.raises(GitHubUnavailable, match="404"):
        client.public_events("nobody")


def test_a_rate_limited_lookup_reports_and_stops(enrich_ctx, settings, tmp_path):
    ctx = enrich_ctx()
    client = _client(settings, tmp_path, FakeResponse(
        {}, 403, headers={"X-RateLimit-Remaining": "0"}
    ))
    report = lookup(ctx, client=client, logins=["janedoe", "another"])
    assert report.found == []
    assert any("rate limit" in n for n in report.notes)
    assert report.logins_checked == ["janedoe"]      # stopped, not hammered


def test_events_are_cached_so_a_rerun_costs_nothing(settings, tmp_path):
    cache = EnrichmentCache(tmp_path / "shared")
    events = [push_event("alpine/sim", commit("jane.doe@alpine.com", "Jane Doe"))]
    first = GitHubClient(settings, cache=cache, transport=FakeTransport(FakeResponse(events)))
    first.public_events("janedoe")

    second = GitHubClient(settings, cache=cache, transport=FakeTransport())
    assert second.public_events("janedoe") == events
    assert second.calls == 0


def test_the_step_is_free_and_last_in_the_order():
    assert GitHubStep.spends_credits is False
    assert GitHubStep.name == "github"


def test_the_step_never_appears_in_the_free_chain_by_default():
    """§10 puts it last and optional: it applies to some people and must not
    delay the steps that work for everyone."""
    from pitwall import enrich as E

    assert not any(s.name == "github" for s in E.free_chain())
    assert any(s.name == "github" for s in E.free_chain(with_github=True))
