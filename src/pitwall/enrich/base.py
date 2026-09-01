"""The enrichment contract — §7.1 of PITWALL_V3_EMAIL_ENRICHMENT.md.

Mirrors the shape of `pitwall.search` and `pitwall.sources.jobs`: a protocol
here, one module per step, a shared cache, and a builder that turns
configuration into a live chain.

Three rules the whole package is built around, and each one is a reaction to
something specific in the brief:

1. **Every step is independently failable and non-fatal.** A step logs, records
   an `enrichment_log` line, and the chain moves on. DNS being down must not
   cost you a pattern that four harvested addresses already proved.
2. **A status is earned by evidence, never asserted.** :class:`EmailCandidate`
   cannot be constructed without an `evidence` dict, and `confidence` is
   documented as *the provider's own number only*. §4 is explicit that a bare
   percentage on a locally-derived guess is a bug, because a number invites the
   owner to trust a hypothesis.
3. **Nothing here spends money by accident.** `spends_credits` is on the
   protocol, the free chain filters on it, and the only step that sets it True
   is `hunter`. §0.2: the free logic runs first, always, and Hunter is a manual
   per-person escalation.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..config import Settings, get_settings
from ..db import STATUS_RANK, Repos
from ..search.base import SearchHit

log = logging.getLogger(__name__)

#: The four statuses of §4, best evidence first. `STATUS_RANK` in `db.py` is the
#: same ordering as numbers, and the repository enforces it on write.
STATUSES: tuple[str, ...] = ("verified", "probable", "guess", "not_found")

#: What earns each one, in the owner's words. The UI renders these verbatim, so
#: the definition and the label can never drift apart.
STATUS_MEANING: dict[str, str] = {
    "verified": "This exact address is published somewhere public, or Hunter "
                "confirmed it is deliverable. Safe to write to.",
    "probable": "Worked out from the company's address format, which two or "
                "more real addresses agree on. Nobody has confirmed this one.",
    "guess": "Worked out from a single example, or from a standard format. "
             "It is a hypothesis, not a finding.",
    "not_found": "Nothing was found. Escalating to Hunter would spend a credit.",
}


class EnrichmentError(RuntimeError):
    """A step failed. Recorded, never fatal to the chain."""


class CreditsExhausted(EnrichmentError):
    """The provider is out of credits. The UI disables the button, not crashes."""


# ──────────────────────────── secret redaction ───────────────────────────


#: §0.4 — the key must never reach a log line, an exception message, or a
#: `raw_json` blob. Matching the parameter name rather than the key's own value
#: catches it even in a URL this code did not build, such as one echoed back
#: inside a provider's error text.
_KEY_PARAM = re.compile(r"(api_key|apikey|access_token|token)=([^&\s\"']+)", re.I)


def redact(text: Any, *secrets: str | None) -> str:
    """Strip API keys from anything about to be logged, raised or stored.

    Two passes on purpose. The literal-secret pass catches the key wherever it
    appears; the query-parameter pass catches a key this process never held —
    a proxy's error page quoting the whole URL, say — which the first pass
    cannot know about.
    """
    out = str(text)
    for secret in secrets:
        if secret and len(secret) >= 8:
            out = out.replace(secret, "***")
    return _KEY_PARAM.sub(r"\1=***", out)


# ─────────────────────────────── the result ──────────────────────────────


@dataclass(frozen=True)
class EmailCandidate:
    """One address a step believes in, and exactly why.

    `evidence` is not optional and not decorative: §4 requires that every label
    carries its evidence, and this dict is what the CSV writer, the Markdown
    writer and the Streamlit chip all render from. An `EmailCandidate` with an
    empty `evidence` is a status with nothing behind it.
    """

    email: str
    status: str
    source: str
    evidence: dict[str, Any] = field(default_factory=dict)
    #: The PROVIDER'S own score, and nothing else. Stays None for everything
    #: derived locally — see §4, and the column comment in `db.py`.
    confidence: float | None = None
    mx_valid: bool | None = None
    is_role_address: bool = False
    is_disposable: bool = False
    raw: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.email or "@" not in self.email:
            raise ValueError(f"not an address: {self.email!r}")
        if self.status not in STATUSES:
            raise ValueError(f"unknown status {self.status!r}; expected one of {STATUSES}")
        object.__setattr__(self, "email", self.email.strip().lower())

    @property
    def rank(self) -> int:
        return STATUS_RANK.get(self.status, 0)

    @property
    def domain(self) -> str:
        return self.email.rsplit("@", 1)[-1]


@dataclass
class StepOutcome:
    """What one step did. Written to `enrichment_log` whether it worked or not."""

    step: str
    outcome: str                       # ok | empty | skipped | failed
    detail: str | None = None          # never an address — see the DDL
    credits_spent: float = 0.0
    found: list[EmailCandidate] = field(default_factory=list)
    samples_added: int = 0

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"


# ─────────────────────────────── the context ─────────────────────────────


@runtime_checkable
class Searcher(Protocol):
    """The slice of the search stack enrichment needs. Trivially stubbable."""

    def search(self, query: str, *, max_results: int = 10) -> list[SearchHit]: ...

    def supports_operators(self) -> bool: ...


@dataclass
class EnrichmentContext:
    """Everything one person's enrichment run accumulates as it goes.

    Steps read what earlier steps learned and add to it — the domain resolver
    fills `domain`, the harvester fills `samples`, the pattern step reads both.
    That is why this is a mutable object passed down a chain rather than a
    return value threaded through it: step 5 needs step 2's corpus.
    """

    candidate: dict[str, Any]
    settings: Settings
    repos: Repos
    company: dict[str, Any] | None = None
    searcher: Searcher | None = None
    run_id: int | None = None

    # ── filled in as the chain runs ──
    domain: str | None = None
    domain_source: str | None = None
    company_domain_id: int | None = None
    samples: list[dict[str, Any]] = field(default_factory=list)
    pattern: str | None = None
    pattern_sample_size: int = 0
    pattern_agreement: float | None = None
    findings: list[EmailCandidate] = field(default_factory=list)
    steps: list[StepOutcome] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    #: Injected so no test ever touches DNS. None = the real resolver.
    mx_lookup: Callable[[str], Any] | None = None

    @property
    def candidate_id(self) -> int:
        return int(self.candidate["id"])

    @property
    def full_name(self) -> str:
        return str(self.candidate.get("full_name") or "").strip()

    @property
    def company_name(self) -> str:
        """The employer to resolve a domain for.

        The candidate's own `current_company` wins over the company the search
        was aimed at. Those differ more often than you would think — the search
        that surfaced someone at Alpine may have been aimed at Alpine while the
        profile says "Alpine Racing UK" — and the address format follows the
        employer, not the query.
        """
        return str(
            self.candidate.get("current_company")
            or (self.company or {}).get("name")
            or ""
        ).strip()

    def add(self, candidate: EmailCandidate) -> EmailCandidate:
        """Record a finding, keeping the best-evidenced one per address."""
        for i, existing in enumerate(self.findings):
            if existing.email == candidate.email:
                if candidate.rank > existing.rank:
                    self.findings[i] = candidate
                return self.findings[i]
        self.findings.append(candidate)
        return candidate

    def note(self, text: str) -> None:
        self.notes.append(text)

    def best(self) -> EmailCandidate | None:
        if not self.findings:
            return None
        return sorted(self.findings, key=lambda c: (-c.rank, c.email))[0]

    def ranked(self) -> list[EmailCandidate]:
        return sorted(self.findings, key=lambda c: (-c.rank, c.email))

    @property
    def status(self) -> str:
        best = self.best()
        return best.status if best else "not_found"

    @property
    def credits_spent(self) -> float:
        return sum(s.credits_spent for s in self.steps)


@runtime_checkable
class EnrichmentStep(Protocol):
    """Every step implements exactly this."""

    name: str
    #: True only for steps that can cost the owner money. The free chain filters
    #: on this rather than on a hardcoded list of names, so a step that starts
    #: spending cannot quietly end up in the free run.
    spends_credits: bool

    def run(self, ctx: EnrichmentContext) -> StepOutcome: ...


def run_step(step: EnrichmentStep, ctx: EnrichmentContext) -> StepOutcome:
    """Run one step, absorb its failure, and record what happened either way.

    §7.1: "Every step is independently failable and non-fatal: log it, record
    it, continue to the next." This is the one place that promise is kept, so a
    step author cannot forget it.
    """
    try:
        outcome = step.run(ctx)
    except Exception as exc:  # noqa: BLE001 - a failed step must not end the chain
        detail = redact(exc, ctx.settings.hunter_api_key, ctx.settings.github_token)[:300]
        log.warning("enrich step %s failed: %s", step.name, detail)
        outcome = StepOutcome(step=step.name, outcome="failed", detail=detail)
        ctx.note(f"{step.name} failed: {detail}")

    ctx.steps.append(outcome)
    for found in outcome.found:
        ctx.add(found)
    with ctx.repos.writing():
        ctx.repos.enrichment.record(
            outcome.step,
            candidate_id=ctx.candidate_id,
            company_id=(ctx.company or {}).get("id"),
            run_id=ctx.run_id,
            provider="hunter" if step.spends_credits else None,
            credits_spent=outcome.credits_spent,
            outcome=outcome.outcome,
            detail=outcome.detail,
        )
    return outcome


# ─────────────────────────── the shared searcher ─────────────────────────


class SerpSearcher:
    """The search chain, cached, as enrichment wants to use it.

    Enrichment queries are `site:`/quoted-phrase queries — §2.2's harvest and
    §2.5's exact-string corroboration both depend on operators surviving to the
    upstream engine. A provider that drops them does not fail, it answers
    something else, which is the failure mode that made v1 return 2,170 rows and
    nobody. So the chain is filtered down to providers that support operators,
    and if none does, this searcher says so rather than returning noise.
    """

    def __init__(
        self,
        providers: Sequence[Any] | None = None,
        settings: Settings | None = None,
        *,
        cache: Any | None = None,
        max_results: int = 10,
    ) -> None:
        from ..search import build_chain
        from ..search.cache import SerpCache

        self.settings = settings or get_settings()
        if providers is None:
            try:
                providers = build_chain(self.settings)
            except Exception as exc:  # noqa: BLE001 - no provider is a state, not a crash
                log.warning("enrichment has no search provider: %s", exc)
                providers = []
        self.providers = [p for p in providers if p.supports_operators()]
        self.dropped = [p.name for p in providers if not p.supports_operators()]
        self.cache = cache if cache is not None else SerpCache(settings=self.settings)
        self.max_results = max_results
        self.queries_run = 0

    def supports_operators(self) -> bool:
        return bool(self.providers)

    def search(self, query: str, *, max_results: int | None = None) -> list[SearchHit]:
        """First provider that answers wins. A cached answer costs nothing."""
        n = max_results or self.max_results
        for provider in self.providers:
            cached = self.cache.get(provider.name, query, n)
            if cached is not None:
                return cached
            try:
                hits = provider.search(query, max_results=n, region=self.settings.region)
            except Exception as exc:  # noqa: BLE001 - fall through to the next provider
                log.info("enrich search via %s failed: %s", provider.name, exc)
                continue
            self.queries_run += 1
            self.cache.put(provider.name, query, n, hits)
            return hits
        return []
