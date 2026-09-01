"""GitHub commit emails — §2.4 of PITWALL_V3_EMAIL_ENRICHMENT.md.

*"For software, simulation and vehicle-dynamics people this is the single best
source, because git makes the author email public by design."*

That last clause is what makes this legitimate and also what bounds it. A
commit author email is not scraped or inferred: it is a field the author chose
to publish, in a public repository, and GitHub serves it through a documented
API. By §2.5's own standard — published, therefore proof that the address
exists *and* that the person publishes it — a matched commit email is as strong
as a team page.

Two rules keep it from being worse than useless:

1. **`…@users.noreply.github.com` is filtered out.** GitHub issues those
   precisely so the address is *not* contactable. Storing one as a finding is a
   bug, and §8 requires an explicit test for it.
2. **No hunting for accounts by name.** §2.4: "Only attempt this when a GitHub
   identity is already associated with the candidate… the false-match rate is
   high and a wrong person's address is worse than none." So the identity comes
   from evidence already in the database — a `github.com/<login>` link in the
   person's own headline or in a search result that surfaced them — and never
   from guessing a username out of their name.

A push event can carry commits by other people (a merge, a co-authored branch),
so an email is only attributed when the commit's own author name matches the
person. Anything weaker is reported as a candidate, not as their address.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import Settings, get_settings
from ..ratelimit import get_limiter
from .base import EmailCandidate, EnrichmentContext, EnrichmentError, StepOutcome, redact
from .cache import EnrichmentCache
from .checks import check_address, domain_of, is_role_address, normalize
from .patterns import NameParts, split_name

log = logging.getLogger(__name__)

PROVIDER = "github"

#: The address GitHub issues so that a commit email is *not* contactable.
#: Filtering it is the single most important line in this module (§2.4, §8).
NOREPLY_DOMAIN = "users.noreply.github.com"

#: Also noreply, from other forges and from git tooling defaults.
NOREPLY_MARKERS = (
    NOREPLY_DOMAIN, "noreply.github.com", "users.noreply.gitlab.com",
    "noreply.gitlab.com", "no-reply@", "noreply@", "example.com", "localhost",
    "(none)", "invalid",
)

#: A `github.com/<login>` reference in prose. Excludes the paths that are not
#: a person: an organisation page, a repository, the docs, the marketplace.
_GITHUB_LOGIN = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))(?:/|\b)",
    re.I,
)

_NOT_A_LOGIN = frozenset(
    {
        "orgs", "organizations", "topics", "collections", "sponsors", "features",
        "marketplace", "pricing", "enterprise", "about", "explore", "trending",
        "settings", "notifications", "login", "join", "search", "apps", "site",
        "readme", "security", "customer-stories", "solutions", "resources",
        "blog", "events", "issues", "pulls", "new", "codespaces", "copilot",
    }
)

#: `From: Jane Doe <jane@example.com>` — the header a `.patch` leads with.
_PATCH_FROM = re.compile(r"^From:\s*(?P<name>[^<]*)<(?P<email>[^>]+)>", re.M)

#: Rate limit is 60/hour unauthenticated and 5,000 with a token. The limiter
#: only stops a burst; `RateLimitExhausted` is what stops the rest, because a
#: token-free hour of 60 requests cannot be paced into submission.
QUERIES_PER_SECOND = 2.0


class GitHubUnavailable(EnrichmentError):
    """Rate-limited or unreachable. A state, never a crash."""


# ────────────────────────── who is this person on GitHub ─────────────────


def logins_in(text: str) -> list[str]:
    """Every plausible `github.com/<login>` in a blob of text."""
    out: list[str] = []
    for match in _GITHUB_LOGIN.finditer(str(text or "")):
        login = match.group(1)
        if login.lower() in _NOT_A_LOGIN or login.lower() in out:
            continue
        out.append(login)
    return out


def known_logins(ctx: EnrichmentContext) -> list[str]:
    """GitHub identities already associated with this person.

    Evidence only. Their own headline, and the titles and snippets of the
    search results that surfaced them — all of which are text somebody else
    published linking this person to that account. Never a username derived
    from their name (§2.4).
    """
    sources: list[str] = [str(ctx.candidate.get("headline") or "")]
    for row in ctx.repos.candidates.evidence(ctx.candidate_id):
        sources.append(f"{row.get('title') or ''} {row.get('snippet') or ''} {row.get('url') or ''}")

    out: list[str] = []
    for text in sources:
        for login in logins_in(text):
            if login not in out:
                out.append(login)
    return out


# ─────────────────────────────── the client ──────────────────────────────


@dataclass
class CommitEmail:
    """One address found in a public commit, and where."""

    email: str
    author_name: str | None = None
    repo: str | None = None
    sha: str | None = None
    #: True when the commit's own author name matches the person we are asking
    #: about. A push event can carry somebody else's commits.
    attributed: bool = False

    def evidence(self) -> dict[str, Any]:
        return {
            "provider": "github",
            "github_repo": self.repo,
            "github_commit": (self.sha or "")[:12] or None,
            "github_author": self.author_name,
        }


@dataclass
class GitHubReport:
    found: list[CommitEmail] = field(default_factory=list)
    logins_checked: list[str] = field(default_factory=list)
    noreply_skipped: int = 0
    notes: list[str] = field(default_factory=list)
    remaining: int | None = None


class GitHubClient:
    """Public events and commit patches. Read-only, cached, token optional."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        cache: EnrichmentCache | None = None,
        transport: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.base_url = self.settings.github_base_url.rstrip("/")
        self.cache = cache if cache is not None else EnrichmentCache(settings=self.settings)
        #: Injected in every test — the suite makes no network call (§8).
        self.transport = transport
        self.limiter = get_limiter(PROVIDER, QUERIES_PER_SECOND)
        self.remaining: int | None = None
        self.calls = 0

    @property
    def token(self) -> str:
        return self.settings.github_token

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "PitWall/3.0 (personal contact research)",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _fetch(self, url: str, *, accept: str | None = None) -> Any:
        headers = self._headers()
        if accept:
            headers["Accept"] = accept
        try:
            if self.transport is not None:
                # The limiter paces *the network*, and an injected transport is
                # not the network. Acquiring it here would make the test suite
                # sleep its way through GitHub's rate limit for requests that
                # were never made.
                response = self.transport(url, headers)
            else:
                self.limiter.acquire()
                response = httpx.get(
                    url, headers=headers,
                    timeout=self.settings.github_timeout_seconds,
                    follow_redirects=True,
                )
        except httpx.HTTPError as exc:
            raise GitHubUnavailable(f"github unreachable: {self._safe(exc)}") from exc

        self.calls += 1
        left = getattr(response, "headers", {}) or {}
        if "X-RateLimit-Remaining" in left:
            try:
                self.remaining = int(left["X-RateLimit-Remaining"])
            except (TypeError, ValueError):
                self.remaining = None

        status = int(getattr(response, "status_code", 0))
        if status in (403, 429) and (self.remaining == 0 or status == 429):
            raise GitHubUnavailable(
                "github rate limit reached"
                + ("" if self.token else
                   " — it is 60 requests an hour without a token. A GITHUB_TOKEN "
                   "with no scopes at all raises that to 5,000.")
            )
        if status == 404:
            raise GitHubUnavailable("no such GitHub user or commit (404)")
        if status >= 400:
            raise GitHubUnavailable(f"github HTTP {status}")
        return response

    def _safe(self, value: Any) -> str:
        return redact(value, self.token)[:200]

    def public_events(self, login: str) -> list[dict[str, Any]]:
        """`/users/{login}/events/public` — push events carry commit authors."""
        params = {"login": login}
        cached = self.cache.get(PROVIDER, "events", params)
        if cached is not None:
            return list(cached)
        response = self._fetch(f"{self.base_url}/users/{login}/events/public")
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise GitHubUnavailable("github returned a non-JSON body") from exc
        events = payload if isinstance(payload, list) else []
        self.cache.put(PROVIDER, "events", params, events)
        return events

    def patch_email(self, repo: str, sha: str) -> CommitEmail | None:
        """`https://github.com/{owner}/{repo}/commit/{sha}.patch` — the `From:`.

        The second confirmed method in §2.4, and the one that still works when
        an events payload has no author email in it.
        """
        params = {"repo": repo, "sha": sha}
        cached = self.cache.get(PROVIDER, "patch", params)
        if cached is None:
            response = self._fetch(
                f"https://github.com/{repo}/commit/{sha}.patch", accept="text/plain"
            )
            cached = str(getattr(response, "text", "") or "")[:4000]
            self.cache.put(PROVIDER, "patch", params, cached)
        match = _PATCH_FROM.search(str(cached))
        if not match:
            return None
        return CommitEmail(
            email=normalize(match.group("email")),
            author_name=match.group("name").strip().strip('"') or None,
            repo=repo,
            sha=sha,
        )


# ───────────────────────────── reading events ────────────────────────────


def is_noreply(email: str) -> bool:
    """§2.4: GitHub issues these precisely so the address is not contactable."""
    address = normalize(email)
    if not address:
        return True
    if domain_of(address).endswith(NOREPLY_DOMAIN):
        return True
    return any(marker in address for marker in NOREPLY_MARKERS)


def names_match(author: str | None, person: NameParts) -> bool:
    """Is this commit's author the person we are asking about?

    A push event can carry commits by other people — a merge, a co-authored
    branch, a vendored dependency bump — so an email is only that person's
    address if the commit says it is theirs.
    """
    parts = split_name(author)
    if parts is None:
        return False
    return parts.first == person.first and parts.last == person.last


def commits_in(events: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """`[(repo, commit), …]` from every PushEvent, newest first."""
    out: list[tuple[str, dict[str, Any]]] = []
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "PushEvent":
            continue
        repo = str((event.get("repo") or {}).get("name") or "")
        for commit in (event.get("payload") or {}).get("commits") or []:
            if isinstance(commit, dict):
                out.append((repo, commit))
    return out


def emails_from_events(
    events: list[dict[str, Any]], person: NameParts | None
) -> tuple[list[CommitEmail], int]:
    """Every contactable author email in these events, and how many were noreply."""
    found: dict[str, CommitEmail] = {}
    skipped = 0
    for repo, commit in commits_in(events):
        author = commit.get("author") or {}
        email = normalize(str(author.get("email") or ""))
        if not email:
            continue
        if is_noreply(email):
            skipped += 1
            continue
        name = str(author.get("name") or "").strip() or None
        attributed = bool(person and names_match(name, person))
        existing = found.get(email)
        if existing is None:
            found[email] = CommitEmail(
                email=email, author_name=name, repo=repo or None,
                sha=str(commit.get("sha") or "") or None, attributed=attributed,
            )
        elif attributed and not existing.attributed:
            found[email] = CommitEmail(
                email=email, author_name=name, repo=repo or None,
                sha=str(commit.get("sha") or "") or None, attributed=True,
            )
    # Attributed first — those are the ones that can become a finding.
    return sorted(found.values(), key=lambda c: (not c.attributed, c.email)), skipped


def lookup(
    ctx: EnrichmentContext, *, client: GitHubClient | None = None, logins: list[str] | None = None
) -> GitHubReport:
    """Commit emails for whatever GitHub identity this person already has."""
    report = GitHubReport()
    identities = logins if logins is not None else known_logins(ctx)
    if not identities:
        report.notes.append(
            "no GitHub account is linked to this person in anything already "
            "collected, and guessing one from their name is not done — the "
            "false-match rate is high and a stranger's address is worse than none"
        )
        return report

    person = split_name(ctx.full_name)
    gh = client or GitHubClient(ctx.settings)
    for login in identities[:3]:
        report.logins_checked.append(login)
        try:
            events = gh.public_events(login)
        except GitHubUnavailable as exc:
            report.notes.append(str(exc))
            break
        emails, skipped = emails_from_events(events, person)
        report.noreply_skipped += skipped
        for item in emails:
            if not any(f.email == item.email for f in report.found):
                report.found.append(item)
        if any(f.attributed for f in report.found):
            break
    report.remaining = gh.remaining
    return report


# ─────────────────────────────── the step ────────────────────────────────


class GitHubStep:
    """§2.4 — free, high precision, and last in the chain on purpose.

    Last because it only ever applies to some people. For a software or
    simulation engineer it is the best source there is; for an aerodynamicist
    with no public code it contributes nothing, and it must not delay the steps
    that work for everyone.
    """

    name = "github"
    spends_credits = False

    def __init__(self, *, client: GitHubClient | None = None, logins: list[str] | None = None) -> None:
        self.client = client
        self.logins = logins

    def run(self, ctx: EnrichmentContext) -> StepOutcome:
        report = lookup(ctx, client=self.client, logins=self.logins)
        for note in report.notes:
            ctx.note(note)

        if not report.logins_checked:
            return StepOutcome(
                step=self.name, outcome="skipped",
                detail="no GitHub identity associated with this person",
            )
        if not report.found:
            detail = f"nothing contactable in {', '.join(report.logins_checked)}'s public commits"
            if report.noreply_skipped:
                # This is the normal case for anyone with the privacy setting
                # on, and saying so stops it reading as a failure.
                detail += (
                    f" ({report.noreply_skipped} commit(s) used GitHub's noreply "
                    "address, which is not contactable by design)"
                )
            return StepOutcome(step=self.name, outcome="empty", detail=detail)

        best = report.found[0]
        checks = check_address(best.email, check_mx=False)
        if is_role_address(best.email):
            return StepOutcome(
                step=self.name, outcome="empty",
                detail="the commit author address is a shared mailbox, not a person's",
            )

        # An attributed commit email is published by its owner, in public, by
        # design — the same standard §2.5 uses for a team page. Unattributed,
        # it is somebody's address in a repository this person pushed to, which
        # is a lead rather than a finding.
        status = "verified" if best.attributed else "guess"
        evidence = {**best.evidence(), **checks.as_evidence()}
        if not best.attributed:
            evidence["github_unattributed"] = True
            ctx.note(
                f"{best.email} appears in {best.repo or 'a repository'} but the "
                "commit is not in this person's name, so it may be a colleague's."
            )

        return StepOutcome(
            step=self.name, outcome="ok",
            detail=(
                f"{'attributed' if best.attributed else 'unattributed'} commit "
                f"address in {best.repo or 'a public repository'}"
            ),
            found=[
                EmailCandidate(
                    email=best.email,
                    status=status,
                    source="github",
                    evidence=evidence,
                    confidence=None,
                    mx_valid=None,
                    is_role_address=False,
                    is_disposable=checks.is_disposable,
                )
            ],
        )
