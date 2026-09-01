"""Contact enrichment — PITWALL_V3_EMAIL_ENRICHMENT.md.

Same shape as `pitwall.search` and `pitwall.sources.jobs`: a protocol in `base`,
one module per step, a shared cache, and a builder that turns configuration
into a live chain.

**The one rule that governs the whole package**: the free engine runs first and
in full, and Hunter is only ever reached by the owner clicking on one specific
person (§0.2). That is enforced structurally, not by convention — `free_chain()`
filters on `EnrichmentStep.spends_credits`, so a step that starts costing money
falls out of the free run whether or not anyone remembers to remove it.

Enrichment is never part of the search pipeline and never runs in bulk over the
whole table.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from ..config import Settings, get_settings
from ..db import Repos
from .base import (
    STATUS_MEANING,
    STATUSES,
    CreditsExhausted,
    EmailCandidate,
    EnrichmentContext,
    EnrichmentError,
    EnrichmentStep,
    Searcher,
    SerpSearcher,
    StepOutcome,
    redact,
    run_step,
)
from .checks import (
    AddressChecks,
    MXResult,
    check_address,
    domain_of,
    find_addresses,
    is_disposable,
    is_free_provider,
    is_role_address,
    is_valid_syntax,
    local_part,
    mx_check,
    normalize,
)
from .corroborate import (
    CorroborateStep,
    corroborate,
    is_independent_source,
    mentions_address,
)
from .domains import (
    DomainStep,
    ResolvedDomain,
    domain_from_url,
    domain_matches_company,
    is_company_domain,
    registrable_domain,
    resolve_domain,
    set_manual_domain,
)
from .harvest import HarvestStep, harvest_domain, known_people_at
from .patterns import (
    FORMATS,
    NameParts,
    PatternGuess,
    PatternStep,
    address_for,
    generic_addresses,
    infer_pattern,
    split_name,
)

log = logging.getLogger(__name__)


# ──────────────────────────────── the chain ──────────────────────────────


def free_chain(
    *,
    resolver: Any | None = None,
    fetcher: Any | None = None,
    force_harvest: bool = False,
) -> list[EnrichmentStep]:
    """The steps that cost nothing, in the order §2 puts them in.

    Domain, then corpus, then pattern, then corroboration — and the order is
    not arbitrary. Each step is the input to the next: without a domain there
    is nothing to harvest, without a corpus there is no pattern, and without a
    candidate address there is nothing to look for a publication of.
    """
    return [
        DomainStep(),
        HarvestStep(fetcher=fetcher, force=force_harvest),
        PatternStep(resolver=resolver),
        CorroborateStep(),
    ]


def is_free(steps: Sequence[EnrichmentStep]) -> bool:
    """True when nothing in this chain can cost the owner money."""
    return not any(step.spends_credits for step in steps)


# ─────────────────────────── running it for one person ───────────────────


def find_company(repos: Repos, candidate: dict[str, Any]) -> dict[str, Any] | None:
    """The company row this person's address format belongs to.

    Their own `current_company` is tried first, exactly and then normalised
    through `extract.normalize` — so "Scuderia Ferrari HP" finds the Ferrari
    row. Only if that fails does the company the *search* was aimed at stand
    in, because that says what was being looked for rather than where they
    work.
    """
    from ..extract.normalize import normalize_company

    name = str(candidate.get("current_company") or "").strip()
    if name:
        row = repos.conn.execute(
            "SELECT * FROM companies WHERE LOWER(name) = LOWER(?)", (name,)
        ).fetchone()
        if row:
            return dict(row)
        wanted = normalize_company(name)
        if wanted:
            for row in repos.conn.execute("SELECT * FROM companies"):
                if normalize_company(str(row["name"])) == wanted:
                    return dict(row)

    row = repos.conn.execute(
        """
        SELECT co.* FROM scores s
          JOIN companies co ON co.id = s.company_id
         WHERE s.candidate_id = ?
         ORDER BY COALESCE(s.final_score, s.heuristic_score, 0) DESC, s.id DESC
         LIMIT 1
        """,
        (candidate.get("id"),),
    ).fetchone()
    return dict(row) if row else None


def enrich_candidate(
    candidate_id: int,
    repos: Repos,
    settings: Settings | None = None,
    *,
    searcher: Searcher | None = None,
    steps: Sequence[EnrichmentStep] | None = None,
    run_id: int | None = None,
    persist: bool = True,
) -> EnrichmentContext:
    """Run a chain for one person and store what it found.

    Returns the context whether or not anything was found — the notes, the
    per-step outcomes and the resolved domain are what the UI shows when the
    answer is "nothing", and they are what tells the owner whether escalating
    to Hunter is worth a credit or whether the domain is simply wrong.
    """
    settings = settings or get_settings()
    row = repos.conn.execute(
        "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
    ).fetchone()
    if row is None:
        raise EnrichmentError(f"no candidate with id {candidate_id}")
    candidate = dict(row)

    # Blocklisted people are erased, not researched. Reaching this at all would
    # mean a stale selection in the UI, so it is refused rather than logged.
    if repos.candidates.is_blocked(str(candidate.get("linkedin_slug") or "")):
        raise EnrichmentError(
            f"{candidate.get('full_name') or candidate_id} is blocklisted — "
            "blocklisting is an erasure, so nothing is looked up for them"
        )

    ctx = EnrichmentContext(
        candidate=candidate,
        settings=settings,
        repos=repos,
        company=find_company(repos, candidate),
        searcher=searcher if searcher is not None else SerpSearcher(settings=settings),
        run_id=run_id,
    )

    for step in steps if steps is not None else free_chain():
        run_step(step, ctx)

    if persist:
        persist_findings(ctx)
    return ctx


def persist_findings(ctx: EnrichmentContext) -> int:
    """Write the context's findings to `contact_emails`. Never downgrades."""
    written = 0
    with ctx.repos.writing():
        for found in ctx.ranked():
            ctx.repos.contacts.upsert(
                ctx.candidate_id, found.email,
                status=found.status, source=found.source,
                evidence=found.evidence, confidence=found.confidence,
                mx_valid=found.mx_valid, is_role_address=found.is_role_address,
                is_disposable=found.is_disposable, raw=found.raw,
            )
            written += 1
    return written


def enrich_many(
    candidate_ids: Sequence[int],
    repos: Repos,
    settings: Settings | None = None,
    *,
    searcher: Searcher | None = None,
    steps: Sequence[EnrichmentStep] | None = None,
    run_id: int | None = None,
    on_progress: Any | None = None,
) -> list[EnrichmentContext]:
    """Enrich a hand-picked selection — never the whole table.

    §0.2: "Enrichment is never part of the search pipeline and never runs in
    bulk over the whole table. It is a manual tool on a manual selection." The
    cap is enforced here rather than in the UI, so the CLI cannot route around
    it either.
    """
    settings = settings or get_settings()
    ids = list(dict.fromkeys(int(i) for i in candidate_ids))
    if len(ids) > settings.enrich_max_selection:
        raise EnrichmentError(
            f"{len(ids)} people selected, and the limit is "
            f"{settings.enrich_max_selection}. Enrichment is a manual tool on a "
            "manual selection, not a sweep — narrow the selection."
        )

    # One searcher for the whole selection, so its cache is shared: two people
    # at the same company then cost one harvest between them, not two.
    shared = searcher if searcher is not None else SerpSearcher(settings=settings)
    out: list[EnrichmentContext] = []
    for i, candidate_id in enumerate(ids, start=1):
        if on_progress:
            on_progress(i, len(ids), candidate_id)
        try:
            out.append(
                enrich_candidate(
                    candidate_id, repos, settings,
                    searcher=shared, steps=steps, run_id=run_id,
                )
            )
        except EnrichmentError as exc:
            # One person's problem is not the selection's problem.
            log.warning("skipping candidate %s: %s", candidate_id, exc)
    return out


# ───────────────────────────── reading it back ───────────────────────────


def last_enriched_at(repos: Repos, candidate_id: int) -> str | None:
    """When this person was last looked up, found or not.

    The absence of a `contact_emails` row means "nothing found", which is not
    the same as "never looked" — and the difference decides whether the owner
    should spend a Hunter credit or just press the free button first.
    """
    row = repos.conn.execute(
        "SELECT MAX(created_at) AS at FROM enrichment_log WHERE candidate_id = ?",
        (candidate_id,),
    ).fetchone()
    return str(row["at"]) if row and row["at"] else None


def summary(ctx: EnrichmentContext) -> dict[str, Any]:
    """One person's result, in the shape the UI and the CLI both render."""
    best = ctx.best()
    return {
        "candidate_id": ctx.candidate_id,
        "name": ctx.full_name,
        "company": ctx.company_name,
        "domain": ctx.domain,
        "domain_source": ctx.domain_source,
        "status": ctx.status,
        "email": best.email if best else None,
        "evidence": best.evidence if best else {},
        "source": best.source if best else None,
        "addresses": [
            {"email": c.email, "status": c.status, "source": c.source,
             "evidence": c.evidence}
            for c in ctx.ranked()
        ],
        "samples": len(ctx.samples),
        "pattern": ctx.pattern,
        "pattern_sample_size": ctx.pattern_sample_size,
        "credits_spent": ctx.credits_spent,
        "steps": [
            {"step": s.step, "outcome": s.outcome, "detail": s.detail,
             "credits": s.credits_spent}
            for s in ctx.steps
        ],
        "notes": list(ctx.notes),
    }


__all__ = [
    "FORMATS",
    "STATUSES",
    "STATUS_MEANING",
    "AddressChecks",
    "CorroborateStep",
    "CreditsExhausted",
    "DomainStep",
    "EmailCandidate",
    "HarvestStep",
    "corroborate",
    "enrich_candidate",
    "enrich_many",
    "find_company",
    "free_chain",
    "is_free",
    "is_independent_source",
    "last_enriched_at",
    "mentions_address",
    "persist_findings",
    "summary",
    "NameParts",
    "PatternGuess",
    "PatternStep",
    "address_for",
    "generic_addresses",
    "harvest_domain",
    "infer_pattern",
    "known_people_at",
    "split_name",
    "EnrichmentContext",
    "EnrichmentError",
    "EnrichmentStep",
    "MXResult",
    "ResolvedDomain",
    "Searcher",
    "SerpSearcher",
    "StepOutcome",
    "check_address",
    "domain_from_url",
    "domain_matches_company",
    "domain_of",
    "find_addresses",
    "is_company_domain",
    "is_disposable",
    "is_free_provider",
    "is_role_address",
    "is_valid_syntax",
    "local_part",
    "mx_check",
    "normalize",
    "redact",
    "registrable_domain",
    "resolve_domain",
    "run_step",
    "set_manual_domain",
]
