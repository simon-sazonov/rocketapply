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

log = logging.getLogger(__name__)

__all__ = [
    "STATUSES",
    "STATUS_MEANING",
    "AddressChecks",
    "CreditsExhausted",
    "DomainStep",
    "EmailCandidate",
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
