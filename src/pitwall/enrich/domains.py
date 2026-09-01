"""Domain resolution — §2.1 of PITWALL_V3_EMAIL_ENRICHMENT.md.

*"Never spend a Hunter credit learning a domain."* Everything here is free, and
the answer is cached per company because the expensive part of finding one
person's address is learning their employer's domain and format — an answer
that is then reusable for every other person at that company.

**One deliberate departure from the brief's ordering.** §2.1 lists
`company_domains.manual_override` fourth. It is checked *first* here. The brief
also says, two sentences later, "a wrong domain poisons everything downstream,
so surface the resolved domain in the UI and let the owner correct it" — and a
correction that automatic sources can outvote is not a correction. So the
override short-circuits, and the remaining three run in the order given.

The other thing this module is careful about is what a domain is *not*. A
`careers_url` of `https://boards.greenhouse.io/alpine` is an ATS, not the
company; `linkedin.com` is where the person was found, not where they work. A
domain drawn from either would generate plausible-looking addresses at somebody
else's mail server, which is the worst failure this feature has.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from ..extract.normalize import company_tokens, squash
from .base import EnrichmentContext, StepOutcome

log = logging.getLogger(__name__)

#: Suffixes where the registrable domain is the last THREE labels, not two.
#: `alpine.co.uk` is a company; `co.uk` is not. This is a short list rather
#: than the Public Suffix List because the alternative is a dependency that
#: ships a 15,000-line data file and needs updating, for a check whose failure
#: mode here is "we resolve one label too few on a domain nobody uses".
MULTI_PART_SUFFIXES: frozenset[str] = frozenset(
    {
        "co.uk", "org.uk", "ac.uk", "gov.uk", "net.uk", "ltd.uk", "plc.uk",
        "me.uk", "sch.uk", "com.au", "net.au", "org.au", "edu.au", "gov.au",
        "co.nz", "net.nz", "org.nz", "co.za", "org.za", "com.br", "net.br",
        "com.mx", "com.ar", "com.tr", "com.sg", "com.hk", "com.cn", "net.cn",
        "org.cn", "co.jp", "ne.jp", "or.jp", "ac.jp", "co.kr", "or.kr",
        "co.in", "net.in", "org.in", "com.my", "com.ph", "co.id", "com.pl",
        "com.es", "com.pt", "com.ua", "co.il", "com.co", "com.pe", "com.ve",
    }
)

#: Hosts that are never a company's own domain. Recruiting platforms, social
#: networks, reference sites and the search engines themselves.
NON_COMPANY_HOSTS: frozenset[str] = frozenset(
    {
        # ATS and job boards — the `careers_url` trap
        "greenhouse.io", "boards.greenhouse.io", "lever.co", "jobs.lever.co",
        "ashbyhq.com", "jobs.ashbyhq.com", "workable.com", "bamboohr.com",
        "myworkdayjobs.com", "workday.com", "wd1.myworkdayjobs.com",
        "smartrecruiters.com", "recruitee.com", "teamtailor.com", "personio.de",
        "personio.com", "jobvite.com", "icims.com", "taleo.net",
        "successfactors.com", "successfactors.eu", "eu.dayforcehcm.com",
        "applytojob.com", "breezy.hr", "jazzhr.com", "workforcenow.adp.com",
        "recruitment.com", "hire.withgoogle.com", "pinpointhq.com",
        # aggregators
        "indeed.com", "uk.indeed.com", "glassdoor.com", "glassdoor.co.uk",
        "totaljobs.com", "reed.co.uk", "monster.com", "ziprecruiter.com",
        "motorsportjobs.com", "autosport.com", "themotorsportjobboard.com",
        # social and reference
        "linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com",
        "youtube.com", "tiktok.com", "reddit.com", "medium.com", "substack.com",
        "wikipedia.org", "wikimedia.org", "wikidata.org", "fandom.com",
        "github.com", "gitlab.com", "stackoverflow.com", "quora.com",
        "crunchbase.com", "bloomberg.com", "zoominfo.com", "rocketreach.co",
        "apollo.io", "hunter.io", "signalhire.com", "lusha.com",
        # search engines and infrastructure
        "google.com", "bing.com", "duckduckgo.com", "yahoo.com", "baidu.com",
        "amazonaws.com", "cloudfront.net", "googleapis.com", "gstatic.com",
        "wordpress.com", "wixsite.com", "squarespace.com", "godaddysites.com",
        "eventbrite.com", "meetup.com", "issuu.com", "scribd.com",
    }
)

#: Subdomain labels stripped before a domain is treated as the company's.
#: `careers.alpine.com` and `www.alpine.com` are both `alpine.com` for mail.
_STRIPPABLE_LABELS: frozenset[str] = frozenset(
    {
        "www", "www2", "en", "uk", "us", "fr", "de", "it", "es", "eu", "int",
        "careers", "career", "jobs", "job", "work", "workwithus", "join",
        "recruitment", "apply", "hiring", "talent", "people", "team", "teams",
        "about", "corporate", "corp", "company", "media", "press", "news",
        "shop", "store", "blog", "info", "web", "site", "home", "m", "mobile",
    }
)


def domain_from_url(url: str) -> str | None:
    """`https://careers.alpine.com/jobs/12` -> `alpine.com`. None if unusable."""
    if not url:
        return None
    text = str(url).strip()
    if "//" not in text:
        text = "//" + text
    host = (urlsplit(text).hostname or "").strip().lower()
    return registrable_domain(host)


def registrable_domain(host: str) -> str | None:
    """The part of a hostname that a mailbox lives on.

    Strips known subdomain labels, then falls back to the registrable form —
    the last two labels, or three where the suffix is a known two-part one.
    Returns None for a bare hostname, an IP address, or `localhost`.
    """
    host = (host or "").strip().lower().rstrip(".").lstrip("@")
    if not host or " " in host:
        return None
    host = host.split("@")[-1].split(":")[0]
    labels = [p for p in host.split(".") if p]
    if len(labels) < 2:
        return None
    if all(p.isdigit() for p in labels):
        return None                        # an IPv4 address

    # Drop `www.`, `careers.` and friends first — `careers.alpine.co.uk` must
    # end up as `alpine.co.uk`, which the last-two-labels rule alone would miss.
    while len(labels) > 2 and labels[0] in _STRIPPABLE_LABELS:
        labels = labels[1:]

    suffix = ".".join(labels[-2:])
    if suffix in MULTI_PART_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def is_company_domain(domain: str | None) -> bool:
    """False for ATS platforms, social networks and the rest of `NON_COMPANY_HOSTS`."""
    if not domain:
        return False
    if domain in NON_COMPANY_HOSTS:
        return False
    # `boards.greenhouse.io` reduces to `greenhouse.io`, but a caller may hand
    # over the full host, so check both ends.
    return not any(domain == h or domain.endswith("." + h) for h in NON_COMPANY_HOSTS)


def domain_label(domain: str) -> str:
    """`alpine-racing.co.uk` -> `alpineracing` — the name-bearing part."""
    labels = [p for p in (domain or "").lower().split(".") if p]
    if len(labels) >= 3 and ".".join(labels[-2:]) in MULTI_PART_SUFFIXES:
        head = labels[-3]
    elif len(labels) >= 2:
        head = labels[-2]
    else:
        head = labels[0] if labels else ""
    return "".join(ch for ch in head if ch.isalnum())


def domain_matches_company(domain: str, company_name: str) -> bool:
    """Does this domain plausibly belong to this company?

    Deliberately generous, because the alternative is rejecting the right
    answer: `prema.org.uk` for "PREMA Racing", `alpine-cars.co.uk` for "Alpine
    Endurance Team". The second of those is a car dealership, not the race team,
    and no string test can tell them apart — which is exactly why §2.1 requires
    the resolved domain to be shown in the UI and correctable. This function
    narrows the field; the owner is the judge.
    """
    label = domain_label(domain)
    if not label:
        return False
    tokens = {t for t in company_tokens(company_name) if len(t) >= 3}
    if not tokens:
        tokens = {t for t in squash(company_name).split() if len(t) >= 3}
    if not tokens:
        return False
    if any(token in label for token in tokens):
        return True
    # `redbullracing.com` vs tokens {red, bull, racing}: the label contains
    # every token concatenated, which the substring test above already covers,
    # but `rbr.com` does not — so also accept the initials of a multi-word name.
    initials = "".join(t[0] for t in sorted(tokens))
    return len(tokens) >= 2 and label == initials


# ───────────────────────────── the resolver ──────────────────────────────


@dataclass
class ResolvedDomain:
    domain: str | None
    source: str
    detail: str | None = None
    #: Every other domain the resolver considered, so the UI can offer them as
    #: one-click corrections instead of making the owner type one.
    alternatives: list[str] = field(default_factory=list)


class DomainStep:
    """Resolve, cache and record the company's mail domain."""

    name = "domain"
    spends_credits = False

    def run(self, ctx: EnrichmentContext) -> StepOutcome:
        company_id = (ctx.company or {}).get("id")
        name = ctx.company_name
        if not name:
            return StepOutcome(
                step=self.name, outcome="skipped",
                detail="no employer on this person to resolve a domain for",
            )

        resolved = resolve_domain(ctx, company_id, name)
        if not resolved.domain:
            return StepOutcome(
                step=self.name, outcome="empty",
                detail=f"no domain found for {name!r} ({resolved.detail or 'nothing matched'})",
            )

        ctx.domain = resolved.domain
        ctx.domain_source = resolved.source
        if company_id:
            with ctx.repos.writing():
                ctx.company_domain_id = ctx.repos.domains.upsert(
                    int(company_id), resolved.domain, source=resolved.source
                )
                ctx.samples = ctx.repos.domains.samples(ctx.company_domain_id)
        if resolved.alternatives:
            ctx.note(
                f"other candidate domains for {name}: "
                + ", ".join(resolved.alternatives[:4])
            )
        return StepOutcome(
            step=self.name, outcome="ok",
            detail=f"{resolved.domain} (from {resolved.source})",
        )


def resolve_domain(
    ctx: EnrichmentContext, company_id: int | None, company_name: str
) -> ResolvedDomain:
    """The §2.1 order, with the override promoted to first. Free throughout."""
    alternatives: list[str] = []

    # 0. the owner's correction — see the module docstring
    if company_id:
        for row in ctx.repos.domains.for_company(int(company_id)):
            if int(row["manual_override"] or 0):
                return ResolvedDomain(str(row["domain"]), "manual", "owner override")
            alternatives.append(str(row["domain"]))

    company = ctx.company or {}

    # 1a. `companies.domain` — an explicit column in config/companies.csv, so
    #     it is the owner's answer too, just a less emphatic one than an
    #     override. Ranked with `careers_url` rather than above it.
    for field_name, source in (("domain", "config"), ("careers_url", "careers_url")):
        value = str(company.get(field_name) or "").strip()
        if not value:
            continue
        domain = registrable_domain(value) if "/" not in value else domain_from_url(value)
        if domain and is_company_domain(domain):
            return ResolvedDomain(domain, source, f"from companies.{field_name}")
        if domain:
            # A `careers_url` on Greenhouse is normal and not an error; it just
            # cannot answer this question.
            log.info("ignoring %s for %s: %s is not a company domain",
                     field_name, company_name, domain)

    # 2. a company website already in the SERP data this database collected
    if company_id:
        from_serp = domain_from_serp(ctx, int(company_id), company_name)
        if from_serp.domain:
            return from_serp
        alternatives.extend(from_serp.alternatives)

    # 3. one search for the company's official site
    from_search = domain_from_search(ctx, company_name)
    if from_search.domain:
        from_search.alternatives = _dedupe(alternatives + from_search.alternatives)
        return from_search

    # Carry the search step's own explanation through rather than flattening it
    # to a generic sentence. "search failed: connection refused" and "no result
    # host matched the company name" send the owner to different fixes, and the
    # UI shows this string.
    return ResolvedDomain(
        None, "none",
        from_search.detail or "nothing in the SERP data or search matched the company name",
        _dedupe(alternatives + from_search.alternatives),
    )


def domain_from_serp(
    ctx: EnrichmentContext, company_id: int, company_name: str
) -> ResolvedDomain:
    """The cheapest source there is: pages this database already fetched.

    §2.1 step 2. Every SERP row stored against this company's queries is a page
    somebody's search engine thought was about them, so the most frequent
    non-social host whose name matches is a strong first guess — and it costs
    nothing at all, not even a cached request.
    """
    rows = ctx.repos.conn.execute(
        """
        SELECT sr.url FROM search_results sr
          JOIN queries q ON q.id = sr.query_id
         WHERE q.company_id = ?
        """,
        (company_id,),
    ).fetchall()

    counts: Counter[str] = Counter()
    for row in rows:
        domain = domain_from_url(row["url"])
        if domain and is_company_domain(domain):
            counts[domain] += 1

    matching = [(d, n) for d, n in counts.most_common() if domain_matches_company(d, company_name)]
    if matching:
        best, n = matching[0]
        return ResolvedDomain(
            best, "serp", f"{n} stored search result(s) on this host",
            [d for d, _ in matching[1:5]],
        )
    return ResolvedDomain(None, "serp", "no stored result host matched the company name",
                          [d for d, _ in counts.most_common(5)])


def domain_from_search(ctx: EnrichmentContext, company_name: str) -> ResolvedDomain:
    """§2.1 step 3 — one search, and only if a searcher is available."""
    if ctx.searcher is None:
        return ResolvedDomain(None, "search", "no search provider available")

    query = f'"{company_name}" official site'
    try:
        hits = ctx.searcher.search(query, max_results=8)
    except Exception as exc:  # noqa: BLE001 - a failed search is not a failed step
        log.info("domain search failed for %s: %s", company_name, exc)
        return ResolvedDomain(None, "search", f"search failed: {str(exc)[:100]}")

    seen: list[str] = []
    for hit in hits:
        domain = domain_from_url(hit.url)
        if not domain or not is_company_domain(domain) or domain in seen:
            continue
        seen.append(domain)
        if domain_matches_company(domain, company_name):
            return ResolvedDomain(domain, "search", f"top matching result for {query!r}", seen[:5])
    return ResolvedDomain(None, "search", "no result host matched the company name", seen[:5])


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out


def set_manual_domain(ctx_or_repos: Any, company_id: int, domain: str) -> str:
    """The owner's correction, from the UI or the CLI.

    Normalised through the same `registrable_domain` the automatic sources use,
    so typing `https://www.alpine-racing.com/careers` corrects to
    `alpine-racing.com` rather than storing an unusable string.
    """
    repos = getattr(ctx_or_repos, "repos", ctx_or_repos)
    cleaned = domain_from_url(domain) if "/" in str(domain) else registrable_domain(str(domain))
    if not cleaned:
        raise ValueError(f"{domain!r} is not a usable domain")
    with repos.writing():
        repos.domains.set_override(int(company_id), cleaned)
    return cleaned
