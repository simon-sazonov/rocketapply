"""Corroboration search — §2.5, and the reason no SMTP probe is needed.

*"If the address appears published anywhere public — a press release, a team
page, a conference programme, a paper — that is stronger evidence than an SMTP
probe, because it proves the address exists* and *that the person publishes
it."*

That last clause is the whole argument. An SMTP probe, on the rare domain where
it works at all, tells you a mailbox accepts mail. A published address tells you
the person *intends to be written to there*, which is the thing actually worth
knowing, and it is the only way a finding reaches `verified` without spending a
credit.

Two guards make this trustworthy rather than merely encouraging, and both exist
because a false `verified` is the worst outcome this feature has — it is the
one status the owner is told is safe to write to:

1. **The result must actually contain the address.** Search engines silently
   relax quoted phrases when a strict match returns nothing. A hit whose title
   and snippet do not contain the exact string is not evidence, however
   confidently it was returned.
2. **Contact-data brokers do not count.** RocketReach, ZoomInfo, SignalHire and
   the rest publish *inferred* addresses. Finding your guess on one proves only
   that somebody else's pattern engine made the same guess from the same name —
   which is not independent confirmation, it is the same reasoning twice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from ..search.base import SearchHit
from .base import EmailCandidate, EnrichmentContext, StepOutcome
from .checks import normalize
from .patterns import split_name

log = logging.getLogger(__name__)

#: §2.5's two queries. The first asks "is this string anywhere?"; the second
#: asks "is it anywhere *next to this person's name*?", which is what
#: distinguishes their address from a colleague's on the same page.
CORROBORATION_QUERIES: tuple[str, ...] = (
    '"{address}"',
    '"{name}" "{address}"',
)

#: Sites that publish *inferred* contact data. A hit here is not corroboration:
#: it is another pattern engine having made the same guess from the same public
#: name, so treating it as independent confirmation would let two guesses
#: promote each other to a fact.
DATA_BROKER_HOSTS: frozenset[str] = frozenset(
    {
        "rocketreach.co", "zoominfo.com", "signalhire.com", "lusha.com",
        "apollo.io", "hunter.io", "snov.io", "contactout.com", "leadiq.com",
        "anymailfinder.com", "findthatlead.com", "voilanorbert.com",
        "clearbit.com", "uplead.com", "seamless.ai", "lead411.com",
        "adapt.io", "kaspr.io", "getprospect.com", "skrapp.io", "emailsherlock.com",
        "aeroleads.com", "datanyze.com", "cufinder.io", "wiza.co",
        "salesql.com", "prospeo.io", "findymail.com", "emailhunter.co",
        "peoplefinder.com", "spokeo.com", "whitepages.com", "beenverified.com",
        "thatsthem.com", "radaris.com", "fastpeoplesearch.com",
        # Address-harvesting dumps and credential-breach mirrors: an address in
        # a leak is not an address its owner published.
        "haveibeenpwned.com", "dehashed.com", "pastebin.com", "ghostbin.com",
        "leakcheck.io", "intelx.io", "scylla.sh",
    }
)


def is_independent_source(url: str) -> bool:
    """Is this a page that *published* the address, or one that guessed it?"""
    host = (urlsplit(str(url or "")).hostname or "").lower().lstrip(".")
    if not host:
        return False
    return not any(host == h or host.endswith("." + h) for h in DATA_BROKER_HOSTS)


def mentions_address(hit: SearchHit, address: str) -> bool:
    """Does the result itself contain the exact string it was asked for?

    Search engines relax a quoted phrase when a strict match returns nothing,
    and a relaxed match for `jane.doe@alpine.com` happily returns a page about
    Jane Doe with no address on it at all. Deliberately strict: a page that
    really does contain the address but whose snippet does not show it is
    scored as "not evidence", and a missed `verified` is a far cheaper mistake
    than a wrong one.
    """
    needle = normalize(address)
    if not needle:
        return False
    haystack = f"{hit.title}\n{hit.snippet}\n{hit.url}".lower()
    return needle in haystack


@dataclass
class Corroboration:
    """Where an address was found published, if anywhere."""

    address: str
    url: str | None = None
    domain: str | None = None
    title: str | None = None
    queries_run: int = 0
    rejected: list[str] = field(default_factory=list)

    @property
    def found(self) -> bool:
        return bool(self.url)

    def as_evidence(self) -> dict[str, Any]:
        if not self.found:
            return {}
        return {
            "corroborating_url": self.url,
            "corroborating_domain": self.domain,
            "corroborating_title": (self.title or "")[:160] or None,
        }


# ─────────────────────── the free check, before searching ────────────────


def corroborate_from_corpus(
    ctx: EnrichmentContext, address: str
) -> Corroboration | None:
    """Is this address already in the harvested corpus, with a source?

    Costs nothing at all — not even a cached search. If §2.2 already read this
    exact address off a public page, that page IS the corroboration, and asking
    a search engine to tell us again would be a query spent on a known answer.
    """
    needle = normalize(address)
    for sample in ctx.samples or []:
        if normalize(str(sample.get("email") or "")) != needle:
            continue
        url = str(sample.get("source_url") or "")
        if url and is_independent_source(url):
            return Corroboration(
                address=needle, url=url,
                domain=(urlsplit(url).hostname or "").lower() or None,
                title=None, queries_run=0,
            )
    return None


# ───────────────────────────── the search ────────────────────────────────


def corroborate(
    ctx: EnrichmentContext, address: str, *, max_queries: int | None = None
) -> Corroboration:
    """Search for the exact address, and for it beside the person's name."""
    address = normalize(address)
    result = Corroboration(address=address)

    from_corpus = corroborate_from_corpus(ctx, address)
    if from_corpus is not None:
        return from_corpus

    if ctx.searcher is None or not address:
        return result
    if not ctx.searcher.supports_operators():
        # An unquoted search for an address returns pages about the domain.
        # Without operators this check cannot be made, and pretending otherwise
        # would manufacture `verified` out of noise.
        ctx.note(
            "the search provider does not support quoted phrases, so no address "
            "could be confirmed as published. Start SearXNG (`make searxng`)."
        )
        return result

    name = ctx.full_name
    limit = ctx.settings.enrich_corroborate_queries if max_queries is None else max_queries
    for template in CORROBORATION_QUERIES[:limit]:
        if "{name}" in template and not split_name(name):
            continue
        query = template.format(address=address, name=name)
        try:
            hits = ctx.searcher.search(query, max_results=8)
        except Exception as exc:  # noqa: BLE001 - a failed query is not a verdict
            log.info("corroboration query failed (%s): %s", query, exc)
            continue
        result.queries_run += 1

        for hit in hits:
            if not mentions_address(hit, address):
                continue
            if not is_independent_source(hit.url):
                host = (urlsplit(hit.url).hostname or "").lower()
                if host and host not in result.rejected:
                    result.rejected.append(host)
                continue
            result.url = hit.url
            result.domain = (urlsplit(hit.url).hostname or "").lower() or None
            result.title = hit.title
            return result
    return result


# ─────────────────────────────── the step ────────────────────────────────


class CorroborateStep:
    """Promote a finding to `verified` if it is published somewhere public."""

    name = "corroborate"
    spends_credits = False

    def __init__(self, *, max_addresses: int = 2) -> None:
        #: How many of this person's candidate addresses to check. Two, because
        #: each one costs up to two searches and the second-ranked address is
        #: rarely the right one.
        self.max_addresses = max_addresses

    def run(self, ctx: EnrichmentContext) -> StepOutcome:
        pending = [c for c in ctx.ranked() if c.status != "verified"][: self.max_addresses]
        if not pending:
            return StepOutcome(
                step=self.name, outcome="skipped",
                detail="nothing to confirm" if not ctx.findings
                       else "already confirmed as published",
            )

        checked = 0
        rejected_hosts: list[str] = []
        for candidate in pending:
            result = corroborate(ctx, candidate.email)
            checked += 1
            for host in result.rejected:
                if host not in rejected_hosts:
                    rejected_hosts.append(host)
            if not result.found:
                continue

            # Keep everything the earlier steps proved. The pattern and its
            # sample size are still true and still worth showing next to the
            # address — §4 wants the evidence, not just the verdict.
            evidence = {**candidate.evidence, **result.as_evidence()}
            return StepOutcome(
                step=self.name, outcome="ok",
                detail=f"published on {result.domain}",
                found=[
                    EmailCandidate(
                        email=candidate.email,
                        status="verified",
                        source="corroborated",
                        evidence=evidence,
                        # Still None: this is our own finding, not a provider's
                        # score, and §4 forbids a number on either.
                        confidence=None,
                        mx_valid=candidate.mx_valid,
                        is_role_address=candidate.is_role_address,
                        is_disposable=candidate.is_disposable,
                    )
                ],
            )

        if rejected_hosts:
            ctx.note(
                "found on "
                + ", ".join(rejected_hosts[:3])
                + " — those sites publish guessed addresses, so they do not "
                "confirm anything."
            )
        return StepOutcome(
            step=self.name, outcome="empty",
            detail=f"{checked} address(es) checked, none published anywhere public",
        )
